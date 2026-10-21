#!/usr/bin/env bash
# autoclipper autopilot supervisor (systemd unit: autoclipper-autopilot).
# Runs fresh headless Claude Code sessions on SESSION_PROMPT.md, on Jay's Claude subscription,
# until a session writes done, blocked or stop to .autopilot/STATUS. Before each session a
# near-free Haiku probe reads the subscription's limits; when the 5-hour window is used up it
# sleeps until the reset. It stops at WEEKLY_MAX weekly usage, after STALL_MAX sessions in a row
# that record no progress, or at the deadline, and posts why to the test Discord webhook.
# Keeps Windows awake while it runs.
#   autopilot.sh           run (normally via systemd)
#   autopilot.sh --probe   print the subscription's current usage and exit
set -uo pipefail
export PATH="/home/jay/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
# Subscription only: an API key in the environment would move Claude Code onto API billing.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDECODE

ROOT=/home/jay/projects/autoclipper
AP="$ROOT/.autopilot"
MODEL="${AUTOPILOT_MODEL:-opus}"
EFFORT="${AUTOPILOT_EFFORT:-high}"
HOURS="${AUTOPILOT_HOURS:-24}"
WEEKLY_MAX="${AUTOPILOT_WEEKLY_MAX:-0.70}"
STALL_MAX="${AUTOPILOT_STALL_MAX:-3}"
RETRY_SEC="${AUTOPILOT_RETRY_SEC:-900}"
DEADLINE=$(( $(date +%s) + HOURS * 3600 ))
WINTMP=/mnt/c/Users/jayvi/AppData/Local/Temp
FLAG="$WINTMP/autoclipper-awake.flag"
reason="stopped by signal"

mkdir -p "$AP/logs"
log() { echo "$(date '+%F %T') $*" | tee -a "$AP/supervisor.log"; }
pct() { awk -v x="$1" 'BEGIN { printf "%d%%", x * 100 }'; }

# The last rate-limit report in a stream-json log: "<status> <resetsAt> <5-hour used> <week used>".
limits() {
  jq -r 'select(.type=="rate_limit_event") | .rate_limit_info |
    "\(.status) \(.resetsAt // 0) \(.unifiedWindows.five_hour.utilization // 0) \(.unifiedWindows.seven_day.utilization // 0)"' \
    "$1" 2>/dev/null | tail -1
}

# A near-free Haiku call, made only for the rate-limit report it returns.
probe() {
  (cd "$AP/logs" && timeout 120 claude -p "Reply with: ok" --model claude-haiku-5-5 --safe-mode --tools "" \
    --strict-mcp-config --no-session-persistence --system-prompt "Reply with: ok" \
    --output-format stream-json --verbose) </dev/null >"$AP/logs/probe.jsonl" 2>&1
  limits "$AP/logs/probe.jsonl"
}

# Changes whenever a session records progress.
progress_mark() { stat -c %Y "$AP/STATE.md" "$AP/HEARTBEAT" 2>/dev/null | tr '\n' ' '; }

# Posts to the private test webhook when .env has one. Never prints the URL.
notify() {
  local url
  url=$(grep -oP '^DISCORD_TEST_WEBHOOK_URL=\K\S+' "$ROOT/.env" 2>/dev/null) || return 0
  curl -fsS -m 20 -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg c "autoclipper autopilot: $1" '{content: $c}')" "$url" >/dev/null 2>&1 || true
}

if [ "${1:-}" = --probe ]; then
  read -r rl_status rl_reset five week <<<"$(probe)"
  [ -n "${rl_status:-}" ] || { echo "no rate-limit report; see $AP/logs/probe.jsonl"; exit 1; }
  echo "$rl_status: 5-hour window $(pct "$five") used, week $(pct "$week") used, next reset $(date -d "@$rl_reset" '+%a %H:%M')"
  exit 0
fi

exec 9>"$AP/.lock"
flock -n 9 || { log "another supervisor holds $AP/.lock, exiting"; exit 1; }

cleanup() { rm -f "$FLAG"; log "supervisor stopped: $reason"; notify "stopped: $reason"; }
trap cleanup EXIT
trap 'exit 0' TERM INT

# Windows sleeps after 30 min idle on AC; hold it awake until the flag file goes away.
cp "$AP/keep-awake.ps1" "$WINTMP/autoclipper-keep-awake.ps1"
touch "$FLAG"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\Users\jayvi\AppData\Local\Temp\autoclipper-keep-awake.ps1' \
  -Hours "$HOURS" </dev/null >/dev/null 2>&1 &

log "supervisor start: $MODEL/$EFFORT, weekly cap $(pct "$WEEKLY_MAX"), deadline $(date -d "@$DEADLINE" '+%F %T')"
run=0
stall=0
while :; do
  status=$(tr -d '[:space:]' < "$AP/STATUS" 2>/dev/null)
  case "$status" in done|blocked|stop) reason="STATUS=$status"; break ;; esac
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then reason="deadline reached"; break; fi

  read -r rl_status rl_reset five week <<<"$(probe)"
  if [ -z "${rl_status:-}" ]; then
    log "probe returned no rate-limit report (logs/probe.jsonl); retrying in ${RETRY_SEC}s"
    sleep "$RETRY_SEC"; continue
  fi
  [[ "$rl_reset" =~ ^[0-9]+$ ]] || rl_reset=0
  if awk -v w="$week" -v m="$WEEKLY_MAX" 'BEGIN { exit !(w >= m) }'; then
    reason="weekly usage at $(pct "$week"), cap $(pct "$WEEKLY_MAX")"; break
  fi
  if [ "$rl_status" = rejected ]; then
    if [ "$rl_reset" -ge "$DEADLINE" ]; then reason="usage limit resets after the deadline"; break; fi
    nap=$(( rl_reset - $(date +%s) + 60 )); [ "$nap" -lt 60 ] && nap=$RETRY_SEC
    log "usage limit (5-hour $(pct "$five"), week $(pct "$week")); sleeping until $(date -d "+$nap seconds" '+%F %T')"
    sleep "$nap"; continue
  fi

  run=$((run + 1))
  out="$AP/logs/run-$(printf '%03d' "$run")-$(date +%m%d-%H%M).jsonl"
  before=$(progress_mark)
  start=$(date +%s)
  log "run $run start (5-hour $(pct "$five"), week $(pct "$week")) -> logs/$(basename "$out")"
  timeout 6h claude -p "$(cat "$AP/SESSION_PROMPT.md")" \
    --model "$MODEL" --effort "$EFFORT" --fallback-model sonnet \
    --permission-mode auto --permission-prompts none \
    --session-id "$(cat /proc/sys/kernel/random/uuid)" --name "autoclipper-autopilot-$run" \
    --add-dir /mnt/c/Users/jayvi/Videos /mnt/c/Users/jayvi/AppData/Roaming/obs-studio \
      "/mnt/c/Battlestate Games/Escape from Tarkov/Logs" /mnt/c/Users/jayvi/AppData/Local/Wardogs/Saved \
    --output-format stream-json --verbose \
    > "$out" 2>&1 < /dev/null
  rc=$?
  result=$(jq -rc 'select(.type=="result") | "\(.subtype) is_error=\(.is_error): \(.result // "" | tostring | gsub("\\s+"; " ") | .[0:200])"' "$out" 2>/dev/null | tail -1)
  log "run $run end: rc=$rc after $(( $(date +%s) - start ))s; ${result:-no result line}"

  # A session cut off by the usage limit isn't stuck; the next probe waits for the reset.
  read -r rl_status _ <<<"$(limits "$out")"
  [ "${rl_status:-}" = rejected ] && continue
  if [ "$(progress_mark)" = "$before" ]; then
    stall=$((stall + 1))
    if [ "$stall" -ge "$STALL_MAX" ]; then reason="$stall sessions in a row recorded no progress"; break; fi
    sleep $(( stall * 120 ))
  else
    stall=0
    sleep 30
  fi
done
