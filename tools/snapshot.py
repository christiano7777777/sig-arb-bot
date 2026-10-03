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
            # shares above the pair count (legs briefly unequal), valued at their own average cost
            "unpaired_value": round(sum((max(-p["quantity"], 0) - pairs) * p["costBasis"] / max(-p["quantity"], 1)
                                        for p in legs.values() if -p["quantity"] > pairs), 2),
        })
    rows.sort(key=lambda r: (r["current_edge"] is None, r["current_edge"] if r["current_edge"] is not None else 9))

    total_pairs = sum(r["pairs"] for r in rows)
    activity, recent = trade_activity(c)
    return {
        "activity": activity,
        "recent": recent,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cash": round(cash, 2),
        "reserve": config.RESERVE,
        "hard_reserve": getattr(config, "HARD_RESERVE", config.RESERVE),
        "extra_enabled": getattr(config, "EXTRA_CAPITAL_ENABLED", False),
        "extra_min_edge": getattr(config, "EXTRA_MIN_EDGE", None),
        "initial": t["initialBalance"],
        "pairs": total_pairs,
        "races": len(rows),
        "cost_basis": round(sum(p["costBasis"] for legs in races.values() for p in legs.values()), 2),
        # every NO+NO pair pays 1; unpaired shares (legs briefly unequal) at their cost
        "value_at_settlement": round(cash + total_pairs + sum(r["unpaired_value"] for r in rows), 2),
        "mark_to_market": round(cash + pos["summary"]["totalMarketValue"], 2),
        "warnings": warnings,
        "rows": rows,
    }


CACHE = Path(__file__).resolve().parents[1] / "state" / "trades_cache.json"
WINDOWS_H = (1, 6, 24)


def fetch_trades(c, max_pages=15):
    """Trade legs from the last 24 h. Trades already seen are cached in state/, so after the first
    call only the newest page is read (one API read)."""
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    since = datetime.now(timezone.utc).timestamp() - 24 * 3600
    cursor = None
    for _ in range(max_pages):
        r = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}/portfolio/transactions", limit=200, cursor=cursor)
        page = [t for t in r.get("data", []) if t.get("event_type") == "trade"]
        new = [t for t in page if t["event_id"] not in cache]
        for t in new:
            cache[t["event_id"]] = {k: t[k] for k in ("createdAt", "orderType", "quantity", "price", "marketTitle")}
        oldest = min((datetime.fromisoformat(t["createdAt"].replace("Z", "+00:00")).timestamp() for t in page),
                     default=0)
        pg = r.get("pagination", {})
        if len(new) < len(page) or oldest < since or not pg.get("hasMore"):
            break                      # reached trades we already have, or older than 24 h
        cursor = pg["nextCursor"]
    cache = {k: v for k, v in cache.items()
             if datetime.fromisoformat(v["createdAt"].replace("Z", "+00:00")).timestamp() >= since}
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache), encoding="utf-8")
    return list(cache.values())


def trade_activity(c):
    """Pair the legs (same race, side of trade and size, within 5 s), then count buys, exits
    (sold at a NO-bid sum >= 1) and swap sales (sold below 1 to fund a bigger edge)."""
    legs = []
    for t in fetch_trades(c):
        g = TITLE.match(t["marketTitle"].strip())
        legs.append({"ts": datetime.fromisoformat(t["createdAt"].replace("Z", "+00:00")),
                     "act": t["orderType"], "race": g.group(2) if g else t["marketTitle"],
                     "party": g.group(1) if g else "", "qty": abs(t["quantity"]), "px": t["price"]})
    legs.sort(key=lambda x: x["ts"])
    pairs, used = [], set()
    for i, a in enumerate(legs):
        if i in used:
            continue
        for j in range(i + 1, min(i + 8, len(legs))):
            b = legs[j]
            if (j not in used and b["race"] == a["race"] and b["act"] == a["act"] and b["qty"] == a["qty"]
                    and b["party"] != a["party"] and (b["ts"] - a["ts"]).total_seconds() <= 5):
                s = a["px"] + b["px"]
                kind = "buy" if a["act"] == "BUY" else ("exit" if s >= 1 - 1e-9 else "swap")
                pairs.append({"ts": a["ts"], "kind": kind, "race": a["race"], "qty": a["qty"], "sum": s})
                used |= {i, j}
                break
    now = datetime.now(timezone.utc)
    history_h = (now - legs[0]["ts"]).total_seconds() / 3600 if legs else 0
    activity = {"history_hours": round(history_h, 2)}
    for h in WINDOWS_H:
        span = max(min(h, history_h), 1 / 60)          # divide by the time actually covered
        recent_pairs = [p for p in pairs if (now - p["ts"]).total_seconds() <= h * 3600]
        activity[f"{h}h"] = {k: {"orders": sum(p["kind"] == k for p in recent_pairs),
                                 "pairs": sum(p["qty"] for p in recent_pairs if p["kind"] == k),
                                 "per_hour": round(sum(p["kind"] == k for p in recent_pairs) / span, 2)}
                             for k in ("exit", "swap", "buy")}
        activity[f"{h}h"]["covered_hours"] = round(span, 2)
        b = [p for p in recent_pairs if p["kind"] == "buy"]
        bq = sum(p["qty"] for p in b)
        activity[f"{h}h"]["buy_edge"] = round(sum(p["qty"] * (1 - p["sum"]) for p in b) / bq, 4) if bq else None
    activity["one_legged_legs_24h"] = len(legs) - 2 * len(pairs)
    recent = [{"ts": p["ts"].isoformat(timespec="seconds"), "kind": p["kind"], "race": p["race"],
               "pairs": p["qty"], "price": round(p["sum"], 4)} for p in reversed(pairs[-25:])]
    return activity, recent


if __name__ == "__main__":
    print(json.dumps(build(SusqClient()), indent=1))
