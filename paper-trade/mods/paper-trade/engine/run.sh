#!/usr/bin/env bash
# Paper Trade AI runner. The panel's buttons call it; you can also run it yourself.
#   run.sh onboard   interview about how you invest (interactive claude)
#   run.sh plan      build a short- and medium-term plan from your profile
#   run.sh decide    one market-hours decision (does nothing outside the regular session)
#   run.sh auto      automatic decision: decide only if the last automatic one was 25+ minutes ago
#   run.sh start plan|decide|auto   start one of the above in the background (the panel's exec has a 30 s limit)
# Environment: PAPER_HOME (data, default ~/.paper-trade), PAPER_MODEL (claude model, default: your claude setting)
set -u
ENGINE="$(cd "$(dirname "$0")" && pwd)"
SELF="$ENGINE/run.sh"
# Print usage before touching the data folder, so a bare or mistyped command creates nothing.
case "${1:-} ${2:-}" in "start plan"|"start decide"|"start auto"|onboard\ *|plan\ *|decide\ *|auto\ *) ;; *) sed -n '2,8p' "$SELF"; exit 64 ;; esac
H="${PAPER_HOME:-$HOME/.paper-trade}"
case "$H" in /*) ;; *) H="$PWD/$H" ;; esac  # make it absolute, since we cd below
export PAPER_HOME="$H"
export TZ=Asia/Seoul
PAPER=(python3 -I "$ENGINE/paper.py")
CMD="python3 -I $ENGINE/paper.py"
mkdir -p "$H/logs"
# Claude Code reads files in its working folder without a rule, so run from the data folder to keep reads there.
cd "$H" || exit 1

prompt() { sed -e "s#{{ENGINE}}#$ENGINE#g" -e "s#{{HOME}}#$H#g" "$ENGINE/prompts/$1.md"; }
model() { [ -n "${PAPER_MODEL:-}" ] && printf -- '--model\n%s\n' "$PAPER_MODEL"; }
fail() { "${PAPER[@]}" journal failed "$1" >/dev/null; echo "$1" >&2; exit 1; }
need_claude() { command -v claude >/dev/null || { echo "claude CLI not found: https://claude.com/claude-code" >&2; exit 127; }; }
need_profile() { [ -f "$H/investor_profile.md" ] || { echo "Set up your profile first." >&2; exit 2; }; }
need_plan() { [ -f "$H/portfolio.json" ] || { echo "Make a plan first." >&2; exit 2; }; }
recent_auto() { [ -n "$(find "$H/last-auto" -mmin -25 2>/dev/null)" ]; }
running() { [ -f "$H/ai-running" ] && kill -0 "$(cut -d' ' -f1 "$H/ai-running")" 2>/dev/null; }
# One plan or decision at a time. The panel reads this file to show the running job.
busy() {
  if running; then echo "Already running: $(cut -d' ' -f2 "$H/ai-running")" >&2; exit 75; fi
  echo "$$ $1 $(date +%s)" > "$H/ai-running"
  trap 'rm -f "$H/ai-running"' EXIT
}
# Engine commands the AI may run: lookups in every stage, orders only while deciding.
LOOKUP=("Bash($CMD quote *)" "Bash($CMD fin *)" "Bash($CMD news *)" "Bash($CMD theme *)" "Bash($CMD search *)" "Bash($CMD chart *)" "Bash($CMD status)")
# Orders inside AI runs count as AI orders even without --source, so the engine's buy limits apply.
export PAPER_SOURCE=ai

decide() {
  need_claude; need_plan
  busy "$1"
  exec >> "$H/logs/decide-$(date +%F).log" 2>&1
  echo "== $(date) $1"
  "${PAPER[@]}" pulse > "$H/pulse.tmp" || fail "Could not fetch market data; skipping this round."
  mv "$H/pulse.tmp" "$H/pulse.json"
  python3 -I -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["market_open"] else 3)' "$H/pulse.json"
  [ $? -eq 3 ] && { echo "market closed"; exit 0; }
  claude -p "$(prompt decide)" $(model) --no-session-persistence --disable-slash-commands --strict-mcp-config \
    --tools Read Bash WebSearch \
    --allowedTools "Read(/$H/**)" "${LOOKUP[@]}" "Bash($CMD buy *)" "Bash($CMD sell *)" "Bash($CMD journal *)" "WebSearch" < /dev/null \
    || fail "Decision run failed (exit $?)."
}

case "${1:-}" in
  start)
    # The panel's exec has a 30 s limit, so long jobs are detached. Report why a job cannot start right here.
    case "${2:-}" in
      plan) need_claude; need_profile ;;
      decide|auto) need_claude; need_plan ;;
      *) echo "start plan|decide|auto" >&2; exit 64 ;;
    esac
    if running; then echo "Already running: $(cut -d' ' -f2 "$H/ai-running")" >&2; exit 75; fi
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
      || fail "Planning run failed (exit $?)."
    "${PAPER[@]}" plan --check --draft || fail "The plan draft did not pass the checks."
    out=$("${PAPER[@]}" review final 2>&1) || fail "Could not finalize the plan: $out"
    "${PAPER[@]}" journal "plan ready" "$out" >/dev/null
    echo "done $out"
    ;;
  decide)
    decide decide
    ;;
  auto)
    # However many panels are open or reloaded, automatic decisions run at most every 25 minutes.
    if recent_auto; then exit 0; fi
    touch "$H/last-auto"
    decide auto
    ;;
  *)
    sed -n '2,8p' "$SELF"; exit 64 ;;
esac
