"""One-off correction of history.json for points recorded while strategy D's shares were left out of
"value at settlement" (2026-10-04, from D_LIVE_SINCE until the fixed snapshot): adds D's holdings at
cost at each point's time back to "settle" and "fair". Idempotent: corrected points get "d_fixed".
    python tools/fix_history_d.py order_tags.json history.json
Reads our fills (API) and D's order tags; writes history.json in place.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # project root
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import snapshot  # noqa: E402
from susq_client import SusqClient  # noqa: E402

FIXED_FROM = "2100-01-01T00:00:00+00:00"   # points from fixed snapshots carry d_fixed, so no time cut-off is needed

tags_path, hist_path = sys.argv[1], sys.argv[2]
hist = json.load(open(hist_path, encoding="utf-8"))
todo = [p for p in hist if config.D_LIVE_SINCE <= p["t"] < FIXED_FROM and not p.get("d_fixed")]
if todo:
    c = SusqClient()
    tid = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}")["id"]
    fills = snapshot.tagged_fills(c, tid, snapshot.load_tags(tags_path))["D"]
    for p in todo:
        add = sum(snapshot.d_cost_basis(fills, until=p["t"]).values())
        p["settle"] = round(p["settle"] + add, 2)
        if p.get("fair") is not None:
            p["fair"] = round(p["fair"] + add, 2)
        p["d_fixed"] = True
    json.dump(hist, open(hist_path, "w", encoding="utf-8"), separators=(",", ":"))
print(f"history: {len(todo)} points corrected for D", file=sys.stderr)
