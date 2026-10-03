"""Rebuild the equity curve since the start of the Cup, from the trade history (read-only).

value at settlement(t) = cash(t) + pairs held(t) + unpaired NO shares at their average cost
(every NO+NO pair pays 1; a leg waiting for its other half is counted at what it cost, not at 0)
cash is replayed from the initial balance using every trade's quantity x price; the result is
verified against the dashboard's recorded values (matched to 0.01 at 7 points on 2026-10-04).
The trade list can lag live trading by minutes, so only history older than the live record is used.
    python tools/backfill_history.py > backfill.json
"""
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # project root
import config  # noqa: E402
from susq_client import SusqClient  # noqa: E402

TITLE = re.compile(r"^Will the (\w+) Party win the (.+?)\??$")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def all_transactions(c):
    rows, cursor = [], None
    while True:
        r = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}/portfolio/transactions", limit=200, cursor=cursor)
        rows += r.get("data", [])
        pg = r.get("pagination", {})
        if not pg.get("hasMore"):
            return rows
        cursor = pg["nextCursor"]


def build(c):
    t = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}")
    rows = all_transactions(c)
    types = sorted({r.get("event_type") for r in rows})
    trades = sorted((r for r in rows if r.get("event_type") == "trade"), key=lambda r: r["createdAt"])

    cash = t["initialBalance"]
    no = defaultdict(float)                     # (race, party) -> NO shares
    cost = defaultdict(float)                   # (race, party) -> cost of those shares
    races = set()
    points, last_emit = [], None
    for r in trades:
        g = TITLE.match(r["marketTitle"].strip())
        race, party = (g.group(2), g.group(1)) if g else (r["marketTitle"], "")
        races.add(race)
        q = abs(r["quantity"])
        # quantity is negative for NO; price is the NO price for NO trades
        k = (race, party)
        if r["orderType"] == "BUY":
            cash -= q * r["price"]
            no[k] += q
            cost[k] += q * r["price"]
        else:
            cash += q * r["price"]
            if no[k] > 0:
                cost[k] -= cost[k] * min(q, no[k]) / no[k]      # average-cost removal
            no[k] -= q
        now = ts(r["createdAt"])
        value = cash
        for rc in races:
            d, rp = no[(rc, "Democratic")], no[(rc, "Republican")]
            pairs = max(0.0, min(d, rp))
            value += pairs
            for leg, n in ((rc, "Democratic"), (rc, "Republican")):
                extra = no[(leg, n)] - pairs
                if extra > 1e-9 and no[(leg, n)] > 0:
                    value += extra * cost[(leg, n)] / no[(leg, n)]
        point = {"t": now.isoformat(timespec="seconds"), "settle": round(value, 2)}
        # one point per second; a later leg in the same second replaces the earlier one
        if last_emit == point["t"]:
            points[-1] = point
        else:
            points.append(point)
        last_emit = point["t"]

    # the platform's hourly portfolio history is interpolated between snapshots, so the backfilled
    # part of the curve has no mark-to-market (null); live snapshots record it from then on
    for p in points:
        p["mtm"] = None
    opening = [r for r in rows if r.get("event_type") == "deposit"]
    if opening:                                  # the curve starts at the opening balance
        start = ts(opening[0]["createdAt"]).isoformat(timespec="seconds")
        if not points or start < points[0]["t"]:
            points.insert(0, {"t": start, "settle": t["initialBalance"], "mtm": t["initialBalance"]})

    check = {"event_types": types, "trades": len(trades), "replayed_cash": round(cash, 2),
             "live_cash": round(t["myBalance"], 2), "cash_diff": round(t["myBalance"] - cash, 2)}
    return {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "check": check, "points": points}


if __name__ == "__main__":
    out = build(SusqClient())
    print(json.dumps(out["check"]), file=sys.stderr)
    print(json.dumps(out["points"], separators=(",", ":")))
