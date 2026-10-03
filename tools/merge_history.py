"""Put backfilled points (older than the first live point) in front of the live equity history.
    python tools/merge_history.py backfill.json history.json
With no live history yet, backfilled points older than 10 minutes are used (the trade list can lag).
"""
import json
import sys
from datetime import datetime, timedelta, timezone

back_path, hist_path = sys.argv[1], sys.argv[2]
back = json.load(open(back_path, encoding="utf-8"))
try:
    live = json.load(open(hist_path, encoding="utf-8"))
except (OSError, ValueError):
    live = []
if live:
    cutoff = live[0]["t"]
    live_start = [p for p in live if p["t"] >= cutoff]
else:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    live_start = []
merged = [p for p in back if p["t"] < cutoff] + live_start
json.dump(merged, open(hist_path, "w", encoding="utf-8"), separators=(",", ":"))
