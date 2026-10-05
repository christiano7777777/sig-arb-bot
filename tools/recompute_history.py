"""Recompute 'settle' (value at settlement) of recorded history points from the trade replay.
    python tools/recompute_history.py backfill.json history.json
The replay (tools/backfill_history.py) rebuilds cash and positions from every trade, counting a pair
only when both legs are held and single legs at their average cost: the snapshot's own definition,
applied consistently. Recorded points carried bookkeeping errors on 2026-10-04 (one-leg races counted as
pairs ~12:30-15:42, D's holdings missing after restarts, D left out before 13:55); replacing their
'settle' with the replay removes those jumps. 'fair' moves by the same amount (it was off by the same
error); mark-to-market is the platform's own number and is left alone. Points newer than 10 minutes are
skipped (the trade list can lag). Deterministic, so running it again changes nothing.
"""
import bisect
import json
import sys
from datetime import datetime, timedelta, timezone

back_path, hist_path = sys.argv[1], sys.argv[2]
back = [p for p in json.load(open(back_path, encoding="utf-8")) if p.get("settle") is not None]
hist = json.load(open(hist_path, encoding="utf-8"))
times = [p["t"] for p in back]
cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
changed = 0
for p in hist:
    if p["t"] >= cutoff or not times or p["t"] < times[0]:
        continue
    i = bisect.bisect_right(times, p["t"]) - 1                 # last replayed trade at or before the point
    new = back[i]["settle"]
    diff = round(new - p["settle"], 2)
    if abs(diff) > 0.5:
        p["settle"] = new
        if p.get("fair") is not None:
            p["fair"] = round(p["fair"] + diff, 2)
        changed += 1
# Kalshi-fair dips (2026-10-05): a snapshot whose Kalshi reads partly failed recorded 'fair' with some C/D
# positions missing. Where fair - settle sits > 300 below its rolling median (+-6 points), use the median.
gaps = [(p["fair"] - p["settle"]) if p.get("fair") is not None else None for p in hist]
dips = 0
for i, g in enumerate(gaps):
    if g is None or hist[i]["t"] >= cutoff:
        continue
    near = sorted(x for x in gaps[max(0, i - 6):i + 7] if x is not None)
    med = near[len(near) // 2]
    if g < med - 300:
        hist[i]["fair"] = round(hist[i]["settle"] + med, 2)
        dips += 1
print(f"history: {dips} Kalshi-fair dips smoothed", file=sys.stderr)
json.dump(hist, open(hist_path, "w", encoding="utf-8"), separators=(",", ":"))
print(f"history: {changed} points recomputed from the trade replay", file=sys.stderr)
