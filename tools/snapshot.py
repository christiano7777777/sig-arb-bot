"""Read-only portfolio snapshot for the dashboard (docs/index.html).

Prints JSON: cash, every held NO+NO pair with its current edge, and the portfolio value assuming
every pair pays 1. Three API reads (tournament, positions, bulk prices).
    python tools/snapshot.py > snapshot.json
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


def build(c):
    t = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}")
    cash = t["myBalance"]
    pos = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}/portfolio/positions")
    races = defaultdict(dict)
    for p in pos["positions"]:
        if p["quantity"] and not p["settled"]:
            g = TITLE.match(p["marketTitle"].strip())
            name, party = (g.group(2), g.group(1)) if g else (p["marketTitle"], p["exchangeId"])
            races[name][party] = p

    ids = [p["exchangeId"] for legs in races.values() for p in legs.values()]
    quotes = {}
    for i in range(0, len(ids), 100):
        r = c.get("/exchanges/prices", ids=",".join(ids[i:i + 100]), tournamentId=t["id"])
        quotes.update({q["exchangeId"]: q for q in r["data"]})

    rows, warnings = [], []
    for race, legs in races.items():
        q = {party: -p["quantity"] for party, p in legs.items()}        # NO shares (positive)
        if len(legs) != 2 or len(set(q.values())) != 1 or min(q.values()) < 0:
            warnings.append(f"{race}: legs {q}")
        pairs = min(max(v, 0) for v in q.values())
        cost = sum(p["costBasis"] for p in legs.values())
        yes_asks = [quotes.get(p["exchangeId"], {}).get("bestAsk") for p in legs.values()]
        yes_bids = [quotes.get(p["exchangeId"], {}).get("bestBid") for p in legs.values()]
        sell = None if None in yes_asks else round(sum(1 - a for a in yes_asks), 4)  # NO bids sum
        buy = None if None in yes_bids else round(sum(1 - b for b in yes_bids), 4)   # NO asks sum
        rows.append({
            "race": race,
            "pairs": pairs,
            "avg_cost": round(cost / pairs, 4) if pairs else None,
            "sell_sum": sell,                                        # what selling a pair pays now
            "current_edge": None if sell is None else round(1 - sell, 4),  # given up by selling now
            "buy_sum": buy,
            "locked": round(pairs - cost, 2),
        })
    rows.sort(key=lambda r: (r["current_edge"] is None, r["current_edge"] if r["current_edge"] is not None else 9))

    total_pairs = sum(r["pairs"] for r in rows)
    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cash": round(cash, 2),
        "reserve": config.RESERVE,
        "initial": t["initialBalance"],
        "pairs": total_pairs,
        "races": len(rows),
        "cost_basis": round(sum(p["costBasis"] for legs in races.values() for p in legs.values()), 2),
        "value_at_settlement": round(cash + total_pairs, 2),          # every NO+NO pair pays 1
        "mark_to_market": round(cash + pos["summary"]["totalMarketValue"], 2),
        "warnings": warnings,
        "rows": rows,
    }


if __name__ == "__main__":
    print(json.dumps(build(SusqClient()), indent=1))
