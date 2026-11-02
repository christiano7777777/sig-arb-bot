#!/usr/bin/env bash
# Restarts the arb-bot run chain when it breaks. Runs inside watchdog.yml, checks every 2 minutes.
#   chain down (no run in progress, last poll >= 5 min old)  -> start arb-bot.yml (at most once per 10 min)
#   workflow disabled (a halt, or stopped by hand)           -> alert, never restart
#   last finished run lasted < 5 min (bad key, STOP file)    -> alert, never restart (same rule as the bot's chain)
#   run in progress but last poll >= 20 min old              -> alert only: could be a failed snapshot push, not a hang
#   GitHub read failed                                       -> skip this check: unknown is not "down"
# Two bots cannot run at once: arb-bot.yml's concurrency group makes an extra run wait.
# Alerts are GitHub issues (one open issue per kind), so they reach the repo owner by email.
# Needs GH_TOKEN, GITHUB_REPOSITORY, END_AT (ISO time; the watchdog disables itself after it).
set -u
BOT=arb-bot.yml
DOWN_S=${DOWN_S:-300}
HUNG_S=${HUNG_S:-1200}
RESTART_GAP_S=${RESTART_GAP_S:-600}
CRASH_S=${CRASH_S:-300}
CHECK_S=${CHECK_S:-120}
JOB_S=${JOB_S:-20700}          # then hand over to a new watchdog run (jobs are killed at 6 h)
last_restart=0

log() { echo "$(date -u +%H:%M:%S)  $*"; }

alert() {   # alert <kind> <text>
    local title="Bot watchdog: $1"
    log "ALERT $1: $2"
    [ -n "$(gh issue list -R "$GITHUB_REPOSITORY" --state open --search "\"$title\" in:title" --json number --jq '.[].number')" ] && return
    gh issue create -R "$GITHUB_REPOSITORY" --title "$title" --body "$2 (run time $(date -u +%FT%TZ))" >/dev/null \
        || log "could not open the alert issue"
}

check() {
    local state runs health now in_progress poll age lasted
    now=$(date +%s)
    state=$(gh api "repos/$GITHUB_REPOSITORY/actions/workflows/$BOT" --jq .state) || { log "read failed (state), skipped"; return; }
    if [ "$state" != "active" ]; then
        alert "bot workflow disabled" "arb-bot.yml is '$state' (a halt, or stopped by hand). Not restarting; see the last run's bot.log."
        return
    fi
    runs=$(gh run list -R "$GITHUB_REPOSITORY" --workflow "$BOT" --limit 20 --json status,conclusion,startedAt,updatedAt) \
        || { log "read failed (runs), skipped"; return; }
    health=$(gh api "repos/$GITHUB_REPOSITORY/contents/health.json?ref=dashboard-data" -H "Accept: application/vnd.github.raw") \
        || { log "read failed (health), skipped"; return; }
    poll=$(echo "$health" | jq -r '.last_poll // empty')
    [ -n "$poll" ] || { log "no last_poll in health.json, skipped"; return; }
    age=$(( now - $(date -d "$poll" +%s) ))
    in_progress=$(echo "$runs" | jq '[.[] | select(.status == "in_progress")] | length')

    if [ "$in_progress" -gt 0 ]; then
        if [ "$age" -ge "$HUNG_S" ]; then
            alert "bot not reporting" "A run is in progress but the last poll is ${age}s old: the bot may be hung, or its snapshot push failing. Not restarted."
        else
            log "ok: run in progress, last poll ${age}s ago"
        fi
        return
    fi
    if [ "$age" -lt "$DOWN_S" ]; then
        log "no run in progress, last poll ${age}s ago: waiting"
        return
    fi
    lasted=$(echo "$runs" | jq -r '[.[] | select(.conclusion == "success" or .conclusion == "failure")][0]
        | if . == null then "" else ((.updatedAt | fromdate) - (.startedAt | fromdate)) end')
    if [ -n "$lasted" ] && [ "$lasted" -lt "$CRASH_S" ]; then
        alert "bot crashes at start-up" "The last finished run lasted only ${lasted}s (bad key, STOP file ...). Not restarting."
        return
    fi
    if [ $(( now - last_restart )) -lt "$RESTART_GAP_S" ]; then
        log "down, last poll ${age}s ago: restarted recently, waiting"
        return
    fi
    log "DOWN: no run in progress, last poll ${age}s ago: starting $BOT"
    if gh workflow run "$BOT" -R "$GITHUB_REPOSITORY" --ref main; then
        last_restart=$now
        alert "bot restarted" "The chain was down (last poll ${age}s old) and the watchdog started arb-bot.yml. Close this issue to get told about the next restart."
    else
        log "restart call failed, will try again at the next check"
    fi
}

finished() {
    [ "$(date +%s)" -ge "$(date -d "$END_AT" +%s)" ]
}

if [ "${ALERT_TEST:-}" = "true" ]; then
    alert "alert test" "Test of the watchdog's alert channel; if this reached you, alerts work. Safe to close."
fi

T0=$(date +%s)
while true; do
    if finished; then
        log "past END_AT $END_AT: disabling the watchdog"
        gh workflow disable watchdog.yml -R "$GITHUB_REPOSITORY"
        exit 0
    fi
    check
    [ "${WATCHDOG_ONCE:-}" = "1" ] && exit 0
    [ $(( $(date +%s) - T0 )) -ge "$JOB_S" ] && break
    sleep "$CHECK_S"
done

for wait in 15 30 60 120 0; do
    gh workflow run watchdog.yml -R "$GITHUB_REPOSITORY" --ref main && exit 0
    log "hand-over failed; next try in ${wait}s"
    sleep "$wait"
done
exit 1
