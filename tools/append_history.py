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
# one-snapshot glitch guard (2026-10-04 16:44: settle -9.8k for one snapshot while mark-to-market did not
# move): a settle jump > JUMP with mtm almost unchanged is held back until the next snapshot confirms it
JUMP, pend_path = 3000, hist_path + ".pending"
last = hist[-1] if hist else None
if last and abs(point["settle"] - last["settle"]) > JUMP and abs(point["mtm"] - last["mtm"]) < JUMP / 3:
    try:
        pend = json.load(open(pend_path, encoding="utf-8"))
    except (OSError, ValueError):
        pend = None
    if not pend or abs(pend["settle"] - point["settle"]) > JUMP / 3:   # not confirmed yet: hold it back
        json.dump(point, open(pend_path, "w", encoding="utf-8"))
        print(f"history: settle jump {point['settle'] - last['settle']:+,.0f} held back", file=sys.stderr)
        sys.exit(0)
    hist.append(pend)                              # confirmed by this snapshot: keep both
try:
    import os
    os.remove(pend_path)
except OSError:
    pass
hist.append(point)
json.dump(hist[-MAX_POINTS:], open(hist_path, "w", encoding="utf-8"), separators=(",", ":"))
