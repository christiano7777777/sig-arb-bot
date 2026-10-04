"""Append the latest snapshot to the equity-curve history.
    python tools/append_history.py snapshot.json history.json
history.json: [{"t": iso time, "settle": value if every pair pays 1 (strategy-B leftover legs at cost),
                "mtm": mark-to-market, "fair": settle with B's leftover legs at Kalshi fair (None before B)}, ...]
Keeps at most MAX_POINTS (one point per 2 min -> ~35 days).
"""
import json
import sys

MAX_POINTS = 25_000

snap_path, hist_path = sys.argv[1], sys.argv[2]
snap = json.load(open(snap_path, encoding="utf-8"))
try:
    hist = json.load(open(hist_path, encoding="utf-8"))
except (OSError, ValueError):
    hist = []
b = snap.get("b") or {}
fair = None if b.get("leftover_fair_minus_cost") is None else round(snap["value_at_settlement"] + b["leftover_fair_minus_cost"], 2)
hist.append({"t": snap["updated"], "settle": snap["value_at_settlement"], "mtm": snap["mark_to_market"], "fair": fair})
json.dump(hist[-MAX_POINTS:], open(hist_path, "w", encoding="utf-8"), separators=(",", ":"))
