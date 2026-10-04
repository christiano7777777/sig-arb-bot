"""Keep the complete fill log: add every trade/deposit not yet in fills.json (read-only API use).
    python tools/append_fills.py fills.json
Reads the transaction list newest first and stops at the first page that holds a transaction we
already have, so after the first call this is one API read. With no fills.json yet, the whole
history is read once (one page every 4 s, so the live bot keeps its read budget).
fills.json: list of transactions, oldest first, keyed by event_id (no duplicates).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # project root
import config  # noqa: E402
from susq_client import SusqClient  # noqa: E402

KEEP = ("event_id", "event_type", "createdAt", "orderType", "quantity", "price",
        "marketTitle", "marketId", "exchangeId", "amount", "transactionType", "reason")

path = Path(sys.argv[1])
try:
    fills = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    fills = []
known = {f["event_id"] for f in fills}

c = SusqClient()
new, cursor = [], None
while True:
    r = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}/portfolio/transactions", limit=200, cursor=cursor)
    page = r.get("data", [])
    fresh = [{k: t.get(k) for k in KEEP} for t in page if t["event_id"] not in known]
    new += fresh
    pg = r.get("pagination", {})
    if len(fresh) < len(page) or not pg.get("hasMore"):
        break                      # reached transactions we already have, or the start of the history
    cursor = pg["nextCursor"]
    time.sleep(4)

if new:
    seen = set(known)
    for t in new:                  # a transaction can appear on two pages if new ones arrive mid-read
        if t["event_id"] not in seen:
            fills.append(t)
            seen.add(t["event_id"])
    fills.sort(key=lambda t: (t["createdAt"], t["event_id"]))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(fills, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
print(f"fills: {len(fills)} (+{len(new)})", file=sys.stderr)
