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
fair = snap.get("value_fair")                     # C and D positions at Kalshi fair (snapshot computes it)
if fair is None and b.get("leftover_fair_minus_cost") is not None:
    fair = round(snap["value_at_settlement"] + b["leftover_fair_minus_cost"], 2)
point = {"t": snap["updated"], "settle": snap["value_at_settlement"], "mtm": snap["mark_to_market"], "fair": fair}
if snap.get("strategy_now"):                      # value per strategy now: A settlement, B/C/D market value
    point["s2"] = snap["strategy_now"]
if "value_fair" in snap:
    point["d_fixed"] = True                       # D already counted by the snapshot (tools/fix_history_d.py skips it)
hist.append(point)
json.dump(hist[-MAX_POINTS:], open(hist_path, "w", encoding="utf-8"), separators=(",", ":"))
