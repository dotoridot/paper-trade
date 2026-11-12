#!/usr/bin/env bash
# 모의투자 AI 실행기. 모의투자 탭의 버튼이 부르고, 터미널에서 직접 불러도 된다.
#   run.sh onboard   투자 성향 대화 (대화형 claude)
#   run.sh plan      성향대로 단기·중기 계획 작성
#   run.sh decide    장중 판단 한 번 (장이 닫혀 있으면 아무것도 하지 않음)
# 환경 변수: PAPER_HOME(데이터, 기본 ~/.paper-trade), PAPER_MODEL(claude 모델, 기본은 claude 설정)
set -u
ENGINE="$(cd "$(dirname "$0")" && pwd)"
export PAPER_HOME="${PAPER_HOME:-$HOME/.paper-trade}"
export TZ=Asia/Seoul
H="$PAPER_HOME"
PAPER=(python3 -I "$ENGINE/paper.py")
mkdir -p "$H/logs"

prompt() { sed -e "s#{{ENGINE}}#$ENGINE#g" -e "s#{{HOME}}#$H#g" "$ENGINE/prompts/$1.md"; }
model() { [ -n "${PAPER_MODEL:-}" ] && printf -- '--model\n%s\n' "$PAPER_MODEL"; }
fail() { "${PAPER[@]}" journal 실패 "$1" >/dev/null; echo "$1" >&2; exit 1; }
need_claude() { command -v claude >/dev/null || { echo "claude CLI가 없습니다: https://claude.com/claude-code" >&2; exit 127; }; }
# 계획과 판단은 한 번에 하나만 돈다. 모의투자 탭은 이 파일로 진행 중인 작업을 보여 준다.
busy() {
  local f="$H/ai-running"
  if [ -f "$f" ] && kill -0 "$(cut -d' ' -f1 "$f")" 2>/dev/null; then echo "이미 실행 중: $(cut -d' ' -f2 "$f")" >&2; exit 75; fi
  echo "$$ $1 $(date +%s)" > "$f"
  trap 'rm -f "$f"' EXIT
}

case "${1:-}" in
  start)
    # 탭의 exec는 30초 제한이 있어 오래 걸리는 작업을 떼어 내 돌린다.
    case "${2:-}" in plan|decide) ;; *) echo "start plan|decide" >&2; exit 64 ;; esac
    nohup "$0" "$2" >/dev/null 2>&1 &
    echo "{\"started\": \"$2\"}"
    ;;
  onboard)
    need_claude
    exec claude "$(prompt onboard)" $(model) \
      --allowedTools "Read" "Edit(/$H/investor_profile.md)" "Edit(/$H/strategy.json)" "Edit(/$H/rules.json)" \
      "Bash(python3 -I $ENGINE/paper.py *)"
    ;;
  plan)
    need_claude
    [ -f "$H/investor_profile.md" ] || { echo "먼저 투자 성향을 정하세요: run.sh onboard" >&2; exit 2; }
    busy plan
    exec >> "$H/logs/plan-$(date +%F).log" 2>&1
    echo "== $(date)"
    rm -f "$H/portfolio.draft.json"
    claude -p "$(prompt plan)" $(model) --no-session-persistence --disable-slash-commands --strict-mcp-config \
      --tools Read Edit Write Bash WebSearch \
      --allowedTools "Read" "Edit(/$H/portfolio.draft.json)" "Bash(python3 -I $ENGINE/paper.py *)" "WebSearch" < /dev/null \
      || fail "계획 작성 실패(종료 코드 $?)."
    "${PAPER[@]}" plan --check --draft || fail "계획 초안이 검사를 통과하지 못했습니다."
    out=$("${PAPER[@]}" review final --unreviewed 2>&1) || fail "계획 확정 실패: $out"
    "${PAPER[@]}" journal "계획 확정" "$out" >/dev/null
    echo "done $out"
    ;;
  decide)
    need_claude
    busy decide
    exec >> "$H/logs/decide-$(date +%F).log" 2>&1
    echo "== $(date)"
    "${PAPER[@]}" pulse > "$H/pulse.tmp" || fail "시장 자료를 받지 못해 이번 회차를 건너뜁니다."
    mv "$H/pulse.tmp" "$H/pulse.json"
    python3 -I -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["market_open"] else 3)' "$H/pulse.json"
    [ $? -eq 3 ] && { echo "market closed"; exit 0; }
    [ -f "$H/portfolio.json" ] || fail "계획이 없어 이번 회차를 건너뜁니다. 먼저 계획을 세우세요."
    claude -p "$(prompt decide)" $(model) --no-session-persistence --disable-slash-commands --strict-mcp-config \
      --tools Read Bash WebSearch \
      --allowedTools "Read" "Bash(python3 -I $ENGINE/paper.py *)" "WebSearch" < /dev/null \
      || fail "판단 실행 실패(종료 코드 $?)."
    ;;
  *)
    sed -n '2,6p' "$0"; exit 64 ;;
esac
