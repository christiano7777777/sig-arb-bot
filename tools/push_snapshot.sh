#!/usr/bin/env bash
# Every 2 minutes: build the portfolio snapshot, append it to the equity-curve history, and
# force-push both files as the only commit on the `dashboard-data` branch (no history build-up
# in git; the curve lives in history.json). Runs in the background inside the bot job.
# Needs GH_TOKEN (the job's GITHUB_TOKEN, contents: write) and GITHUB_REPOSITORY.
set -u
cd "$(dirname "$0")/.."
repo_dir=$(pwd)
remote="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
work=$(mktemp -d)

# start from the equity curve saved by earlier runs (if any)
if git clone -q --depth 1 -b dashboard-data "$remote" "$work/prev" 2>/dev/null; then
    cp "$work/prev/history.json" "$work/history.json" 2>/dev/null || true
fi
# fill in the curve before the first live point by replaying the trade history since the Cup began
if python tools/backfill_history.py > "$work/backfill.json" 2>> snapshot.err; then
    python tools/merge_history.py "$work/backfill.json" "$work/history.json" 2>> snapshot.err
fi

while true; do
    if python tools/snapshot.py > "$work/snapshot.json.tmp" 2>> snapshot.err; then
        mv "$work/snapshot.json.tmp" "$work/snapshot.json"
        python tools/append_history.py "$work/snapshot.json" "$work/history.json" 2>> snapshot.err
        (
            cd "$work" || exit 0
            rm -rf .git
            git init -q -b dashboard-data
            git add snapshot.json history.json
            git -c user.name="github-actions[bot]" \
                -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
                commit -q -m "portfolio snapshot $(date -u +%FT%TZ)"
            git push -q -f "$remote" dashboard-data 2>> "$repo_dir/snapshot.err" || true
        )
    fi
    sleep 120
done
