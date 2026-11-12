#!/usr/bin/env bash
# 모의투자 AI 실행기. 모의투자 탭의 버튼이 부르고, 터미널에서 직접 불러도 된다.
#   run.sh onboard   투자 성향 대화 (대화형 claude)
#   run.sh plan      성향대로 단기·중기 계획 작성
#   run.sh decide    장중 판단 한 번 (정규장이 아니면 아무것도 하지 않음)
#   run.sh auto      자동 판단: 마지막 자동 판단 뒤 25분이 지났을 때만 decide
#   run.sh start plan|decide|auto   위 작업을 백그라운드로 시작 (탭의 exec는 30초 제한)
# 환경 변수: PAPER_HOME(데이터, 기본 ~/.paper-trade), PAPER_MODEL(claude 모델, 기본은 claude 설정)
set -u
ENGINE="$(cd "$(dirname "$0")" && pwd)"
SELF="$ENGINE/run.sh"
export PAPER_HOME="${PAPER_HOME:-$HOME/.paper-trade}"
export TZ=Asia/Seoul
H="$PAPER_HOME"
PAPER=(python3 -I "$ENGINE/paper.py")
CMD="python3 -I $ENGINE/paper.py"
mkdir -p "$H/logs"
# Claude Code는 작업 폴더 안의 파일을 규칙 없이 읽으므로, 읽기 범위가 데이터 폴더로 한정되게 여기서 실행한다.
cd "$H" || exit 1

prompt() { sed -e "s#{{ENGINE}}#$ENGINE#g" -e "s#{{HOME}}#$H#g" "$ENGINE/prompts/$1.md"; }
model() { [ -n "${PAPER_MODEL:-}" ] && printf -- '--model\n%s\n' "$PAPER_MODEL"; }
fail() { "${PAPER[@]}" journal 실패 "$1" >/dev/null; echo "$1" >&2; exit 1; }
need_claude() { command -v claude >/dev/null || { echo "claude CLI가 없습니다: https://claude.com/claude-code" >&2; exit 127; }; }
need_profile() { [ -f "$H/investor_profile.md" ] || { echo "먼저 투자 성향을 정하세요." >&2; exit 2; }; }
need_plan() { [ -f "$H/portfolio.json" ] || { echo "먼저 계획을 세우세요." >&2; exit 2; }; }
recent_auto() { [ -n "$(find "$H/last-auto" -mmin -25 2>/dev/null)" ]; }
running() { [ -f "$H/ai-running" ] && kill -0 "$(cut -d' ' -f1 "$H/ai-running")" 2>/dev/null; }
# 계획과 판단은 한 번에 하나만 돈다. 모의투자 탭은 이 파일로 진행 중인 작업을 보여 준다.
busy() {
  if running; then echo "이미 실행 중: $(cut -d' ' -f2 "$H/ai-running")" >&2; exit 75; fi
  echo "$$ $1 $(date +%s)" > "$H/ai-running"
  trap 'rm -f "$H/ai-running"' EXIT
}
# AI가 부르는 엔진 명령. 조회 명령은 모든 단계에, 주문은 판단 단계에만 연다.
LOOKUP=("Bash($CMD quote *)" "Bash($CMD fin *)" "Bash($CMD news *)" "Bash($CMD theme *)" "Bash($CMD search *)" "Bash($CMD chart *)" "Bash($CMD status)")
# AI 실행 중의 주문은 --source를 빠뜨려도 AI 주문으로 다뤄 엔진의 매수 제한이 걸린다.
export PAPER_SOURCE=ai

decide() {
  need_claude; need_plan
  busy "$1"
  exec >> "$H/logs/decide-$(date +%F).log" 2>&1
  echo "== $(date) $1"
  "${PAPER[@]}" pulse > "$H/pulse.tmp" || fail "시장 자료를 받지 못해 이번 회차를 건너뜁니다."
  mv "$H/pulse.tmp" "$H/pulse.json"
  python3 -I -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["market_open"] else 3)' "$H/pulse.json"
  [ $? -eq 3 ] && { echo "market closed"; exit 0; }
  claude -p "$(prompt decide)" $(model) --no-session-persistence --disable-slash-commands --strict-mcp-config \
    --tools Read Bash WebSearch \
    --allowedTools "Read(/$H/**)" "${LOOKUP[@]}" "Bash($CMD buy *)" "Bash($CMD sell *)" "Bash($CMD journal *)" "WebSearch" < /dev/null \
    || fail "판단 실행 실패(종료 코드 $?)."
}

case "${1:-}" in
  start)
    # 탭의 exec는 30초 제한이 있어 오래 걸리는 작업을 떼어 내 돌린다. 시작할 수 없는 이유는 여기서 바로 알린다.
    case "${2:-}" in
      plan) need_claude; need_profile ;;
      decide|auto) need_claude; need_plan ;;
      *) echo "start plan|decide|auto" >&2; exit 64 ;;
    esac
    if running; then echo "이미 실행 중: $(cut -d' ' -f2 "$H/ai-running")" >&2; exit 75; fi
    if [ "$2" = auto ] && recent_auto; then echo '{"skipped": "auto"}'; exit 0; fi
    nohup bash "$SELF" "$2" >> "$H/logs/start.log" 2>&1 &
    echo "{\"started\": \"$2\"}"
    ;;
  onboard)
    need_claude
    exec claude "$(prompt onboard)" $(model) \
      --allowedTools "Read(/$H/**)" "Edit(/$H/investor_profile.md)" "Edit(/$H/strategy.json)" "Edit(/$H/rules.json)" \
      "Bash($CMD search *)" "Bash($CMD status)"
    ;;
  plan)
    need_claude; need_profile
    busy plan
    exec >> "$H/logs/plan-$(date +%F).log" 2>&1
    echo "== $(date)"
    rm -f "$H/portfolio.draft.json"
    claude -p "$(prompt plan)" $(model) --no-session-persistence --disable-slash-commands --strict-mcp-config \
      --tools Read Edit Write Bash WebSearch \
      --allowedTools "Read(/$H/**)" "Edit(/$H/portfolio.draft.json)" "${LOOKUP[@]}" "Bash($CMD pulse)" "Bash($CMD macro)" \
      "Bash($CMD plan *)" "WebSearch" < /dev/null \
      || fail "계획 작성 실패(종료 코드 $?)."
    "${PAPER[@]}" plan --check --draft || fail "계획 초안이 검사를 통과하지 못했습니다."
    out=$("${PAPER[@]}" review final 2>&1) || fail "계획 확정 실패: $out"
    "${PAPER[@]}" journal "계획 확정" "$out" >/dev/null
    echo "done $out"
    ;;
  decide)
    decide decide
    ;;
  auto)
    # 탭을 여러 개 열거나 다시 불러와도 자동 판단은 25분에 한 번만 돈다.
    if recent_auto; then exit 0; fi
    touch "$H/last-auto"
    decide auto
    ;;
  *)
    sed -n '2,8p' "$0"; exit 64 ;;
esac
