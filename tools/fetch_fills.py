"""Read-only: download the full transaction history to JSON, throttled to ~15 reads/min
so the live bot (same account, 100 reads/min) is not starved."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # project root
import config
from susq_client import SusqClient
c = SusqClient()
rows, cursor, n = [], None, 0
while True:
    r = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}/portfolio/transactions", limit=200, cursor=cursor)
    rows += r.get("data", []); n += 1
    pg = r.get("pagination", {})
    if not pg.get("hasMore"):
        break
    cursor = pg["nextCursor"]; time.sleep(4)
out = Path(sys.argv[1]); out.write_text(json.dumps(rows))
print(n, "pages,", len(rows), "rows; event types:", sorted({x.get("event_type") for x in rows}))
