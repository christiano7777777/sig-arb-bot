#!/usr/bin/env bash
# Last step of every bot run (also when cancelled): merge this run's order tags into order_tags.json on
# the dashboard-data branch, so tags of orders placed after the last 2-minute snapshot are not lost
# (2026-10-04: a D fill at a restart had no tag, so D's ledger and the legs check were 2 shares off).
set -u
cd "$(dirname "$0")/.."
remote="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
work=$(mktemp -d)
git clone -q --depth 1 -b dashboard-data "$remote" "$work" || exit 0
python tools/merge_tags.py state/order_tags.jsonl "$work/order_tags.json" || exit 0
cd "$work" && git add order_tags.json && git -c user.name="github-actions[bot]" \
    -c user.email="41898282+github-actions[bot]@users.noreply.github.com" commit -q -m "order tags at end of run" \
    && git push -q "$remote" dashboard-data || true
