#!/usr/bin/env bash
# Every 2 minutes: build the portfolio snapshot and force-push it as the only commit on the
# `dashboard-data` branch (no history build-up). Runs in the background inside the bot job.
# Needs GH_TOKEN (the job's GITHUB_TOKEN, contents: write) and GITHUB_REPOSITORY.
set -u
cd "$(dirname "$0")/.."
work=$(mktemp -d)
while true; do
    if python tools/snapshot.py > "$work/snapshot.json.tmp" 2>> snapshot.err; then
        mv "$work/snapshot.json.tmp" "$work/snapshot.json"
        (
            cd "$work" || exit 0
            rm -rf .git
            git init -q -b dashboard-data
            git add snapshot.json
            git -c user.name="github-actions[bot]" \
                -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
                commit -q -m "portfolio snapshot $(date -u +%FT%TZ)"
            git push -q -f "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" \
                dashboard-data 2>> "$OLDPWD/snapshot.err" || true
        )
    fi
    sleep 120
done
