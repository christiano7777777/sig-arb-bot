"""Put the replayed curve in front of the live equity history.
    python tools/merge_history.py backfill.json history.json
Live points are the recorded snapshots (they carry mark-to-market). Everything before the first live
point is replaced by the replay, so a corrected replay also replaces older replayed points.
With no live history yet, replayed points older than 10 minutes are used (the trade list can lag).
"""
import json
import sys
from datetime import datetime, timedelta, timezone

back_path, hist_path = sys.argv[1], sys.argv[2]
back = json.load(open(back_path, encoding="utf-8"))
try:
    hist = json.load(open(hist_path, encoding="utf-8"))
except (OSError, ValueError):
    hist = []
opening_t = back[0]["t"] if back else None
live = [p for p in hist if p.get("mtm") is not None and p["t"] != opening_t]
if live:
    cutoff = live[0]["t"]
    live = [p for p in hist if p["t"] >= cutoff]
else:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
merged = [p for p in back if p["t"] < cutoff] + live
json.dump(merged, open(hist_path, "w", encoding="utf-8"), separators=(",", ":"))
