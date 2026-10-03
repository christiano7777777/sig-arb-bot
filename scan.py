"""READ-ONLY scanner: fetches the books for each basket and prints the edge and executable size.

Sends GET requests only. It cannot place orders.

    python scan.py                       # one scan
    python scan.py --tournament <slug>   # if the markets sit in more than one tournament
    python scan.py --watch 10            # rescan every 10 s (min 10 s: one scan = 6 reads, budget is 100 reads/min)
"""
import argparse
import re
import time

import config
from arb_math import ceil_to_tick, no_asks_from_yes_bids, walk_baskets
from susq_client import SusqClient


def party_of(title):
    """Label a market from its title. Returns 'R', 'D' or '?' (never guessed)."""
    rep = bool(re.search(r"republican|\bGOP\b", title, re.I))
    dem = bool(re.search(r"democrat", title, re.I))
    return "R" if rep and not dem else "D" if dem and not rep else "?"


def pick_context(market, slug):
    tours = [c["tournament"] for c in market["contexts"] if c["type"] == "tournament" and c["tournament"]]
    if slug:
        match = [t for t in tours if t["slug"] == slug]
        if not match:
            raise SystemExit(f"market {market['id']} is not in tournament '{slug}'")
        return match[0]
    if len(tours) == 1:
        return tours[0]
    names = ", ".join(f"{t['slug']} ({t['name']})" for t in tours) or "none (public context only)"
    raise SystemExit(f"market {market['id']}: choose a tournament with --tournament. Available: {names}")


def book_for(client, market_id, tournament_id):
    ob = client.get(f"/markets/{market_id}/orderbook", tournamentId=tournament_id, depth=200)
    for ctx in ob.get("contexts", []):
        if ctx.get("tournament") and ctx["tournament"]["id"] == tournament_id:
            return ctx["orderbook"]
    return ob  # top-level fields when only one context is returned


def scan_basket(client, basket, slug):
    print(f"\n=== {basket['name']} ===")
    legs, tour = [], None
    for mid in basket["market_ids"]:
        m = client.get(f"/markets/{mid}")
        t = pick_context(m, slug)
        if tour and t["id"] != tour["id"]:
            raise SystemExit("legs resolve to different tournaments; pass --tournament")
        tour = t
        if len(m["exchanges"]) != 1:
            raise SystemExit(f"market {mid} has {len(m['exchanges'])} exchanges; expected a binary market")
        legs.append({"market_id": mid, "title": m["title"], "status": m["status"],
                     "party": party_of(m["title"]), "exchange_id": m["exchanges"][0]["id"],
                     "settlementDate": m["settlementDate"]})

    tinfo = client.get(f"/tournaments/{tour['slug']}")
    print(f"tournament: {tinfo['name']} [{tinfo['slug']}] status={tinfo['status']} "
          f"end={tinfo['endDate']} balance={tinfo['myBalance']} {tinfo['currencyName']}")
    for leg in legs:
        print(f"  market {leg['market_id']}  party={leg['party']}  exchange={leg['exchange_id']}  "
              f"status={leg['status']}  settles={leg['settlementDate']}\n     \"{leg['title']}\"")
    if sorted(leg["party"] for leg in legs) != ["D", "R"]:
        print("  WARNING: could not identify exactly one R and one D market from the titles. Check by hand.")

    rels = client.get("/relationships", marketId=basket["market_ids"][0], tournamentId=tour["id"])
    print(f"  engine relationships touching market {basket['market_ids'][0]}: {len(rels['data'])}")
    for r in rels["data"]:
        print(f"     {r['type']} exhaustive={r['isExhaustive']} status={r['status']} label={r['label']!r}")

    ladders = []
    for leg in legs:
        book = book_for(client, leg["market_id"], tour["id"])
        ex = next(e for e in book["exchanges"] if e["exchangeId"] == leg["exchange_id"])
        ladder = no_asks_from_yes_bids(ex["bids"])
        ladders.append(ladder)
        top = ", ".join(f"{p:.3f}x{q:g}" for p, q in ladder[:5]) or "(empty)"
        print(f"  NO asks {leg['party']} (best 5): {top}   book asOf={ex['asOf']}")

    if not all(ladders):
        print("  one leg has no NO liquidity -> nothing to do")
        return
    best_sum = sum(lad[0][0] for lad in ladders)
    edge = basket["min_payout"] - best_sum
    print(f"  best NO ask sum = {best_sum:.4f}   edge at top of book = {edge:+.4f}")

    min_edge = 0.0 if config.TRADE_AT_ZERO_EDGE else config.MIN_EDGE
    res = walk_baskets(ladders, basket["min_payout"], min_edge)
    if res["quantity"] == 0:
        print(f"  executable size at edge >= {min_edge}: 0")
        return
    print(f"  executable size at edge >= {min_edge}: {res['quantity']} baskets, "
          f"cost {res['total_cost']:.3f}, avg {res['avg_cost']:.4f}, "
          f"locked profit >= {res['locked_profit_min']:.3f}")
    for s in res["steps"]:
        print(f"     {s['quantity']:>8} @ {' + '.join(f'{p:.3f}' for p in s['prices'])} = {s['marginal_cost']:.3f}")
    limits = [ceil_to_tick(p, config.TICK) for p in res["worst_prices"]]
    worst_case = sum(limits)
    print(f"  limit prices (on {config.TICK} tick): {limits}  -> worst-case basket cost {worst_case:.3f}"
          + ("" if worst_case <= basket["min_payout"] else "  WARNING: above payout after tick rounding"))
    print("  (no capital cap or reserve applied yet: not configured)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tournament", help="tournament slug")
    ap.add_argument("--watch", type=float, help="rescan every N seconds (>= 10)")
    args = ap.parse_args()
    client = SusqClient()
    while True:
        print(time.strftime("%Y-%m-%d %H:%M:%S"))
        for basket in config.BASKETS:
            scan_basket(client, basket, args.tournament)
        if not args.watch:
            break
        time.sleep(max(args.watch, 10))


if __name__ == "__main__":
    main()
