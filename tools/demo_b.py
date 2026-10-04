"""READ-ONLY walk-through of strategy B's decision for one or more races, on live data. Sends no
orders. Prints every step: Kalshi prices -> fair value -> position and exposure -> caps -> skew ->
reservation prices -> candidate orders -> what the executor would actually send (touch filter,
caps, cash), using the same code as the live bot (strategy_b.decide).
    python tools/demo_b.py "Minnesota Governor" ["Delaware Senate" ...]
SUSQ reads: market list (2-3) + positions + balance + bulk prices + 2 books per race.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # project root
import config  # noqa: E402
import kalshi  # noqa: E402
import strategy_b  # noqa: E402
from arb_math import no_asks_from_yes_bids, no_bids_from_yes_asks  # noqa: E402
from baskets import list_markets, two_party_baskets  # noqa: E402
from susq_client import SusqClient  # noqa: E402

names = sys.argv[1:] or ["Minnesota Governor"]
c = SusqClient()
t = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}")
baskets = {b["name"]: b for b in two_party_baskets(list_markets(c, config.TOURNAMENT_SLUG))[0]}
pos = {p["exchangeId"]: p for p in c.get(f"/tournaments/{config.TOURNAMENT_SLUG}/portfolio/positions")["positions"]
       if not p["settled"]}
cash = t["myBalance"]
cost = sum(p.get("costBasis") or 0.0 for p in pos.values())
cap_total = config.B_TOTAL_CAP_FRAC * (cash + cost)
no = lambda ex: max(-pos.get(ex, {}).get("quantity", 0), 0)


def legs_of(name):
    return {l["party"]: l["exchange_id"] for l in baskets[name]["legs"]}


# pairs in every held B race (the race cap is this race's share of them)
held_b = {n: {x: no(e) for x, e in legs_of(n).items()} for n in config.B_RACES if n in baskets}
held_b = {n: h for n, h in held_b.items() if h["D"] >= 1 or h["R"] >= 1}
pairs_total = sum(min(h.values()) for h in held_b.values()) or 1.0
spend = max(0.0, cash - config.HARD_RESERVE)

print(f"Portfolio: cash {cash:,.2f} + cost basis {cost:,.2f}  ->  total cap {config.B_TOTAL_CAP_FRAC:.0%} = {cap_total:,.0f} shares at risk")
print(f"Pairs held in B races: {pairs_total:,.0f}.  Cash B may spend on buys: {spend:,.2f} (above the {config.HARD_RESERVE:,} hard reserve)")
print(f"Settings: take >= {config.B_TAKE_EDGE} from r, quote >= {config.B_QUOTE_EDGE} from r, skew {config.B_SKEW}, "
      f"favourite >= {config.B_MIN_FAVOURITE}, Kalshi spread <= {config.B_MAX_KALSHI_SPREAD}")

for name in names:
    print("\n" + "=" * 100 + f"\n{name}\n" + "=" * 100)
    ex = legs_of(name)
    h = {x: no(e) for x, e in ex.items()}
    k = kalshi.fair(config.B_RACES[name], config.B_MAX_KALSHI_SPREAD)
    print("STEP 1  Kalshi (public prices)")
    if not k.get("mid"):
        print(f"   not usable: {k['why']}  -> B does not trade this race"); continue
    for x in "DR":
        print(f"   {x}: YES mid {k['mid'][x]:.4f} (spread {k['spread'][x]:.3f})")
    tot = k["mid"]["D"] + k["mid"]["R"]
    print(f"   overround: mids sum to {tot:.4f}; divide by it -> p_D = {k['p']['D']:.4f}, p_R = {k['p']['R']:.4f}")
    fav = max(k["p"], key=k["p"].get); und = "R" if fav == "D" else "D"
    fair = {x: 1 - k["p"][x] for x in "DR"}
    print(f"STEP 2  Fair NO prices: NO_D = 1 - p_D = {fair['D']:.4f}, NO_R = 1 - p_R = {fair['R']:.4f}   "
          f"(favourite {fav} at {k['p'][fav]:.1%}: {'eligible' if k['p'][fav] >= config.B_MIN_FAVOURITE else 'BELOW threshold, no trading'})")
    books = {}
    for x in "DR":
        ob = c.get(f"/exchanges/{ex[x]}/orderbook", tournamentId=t["id"], depth=50)
        books[x] = {"asks": no_asks_from_yes_bids(ob["bids"]), "bids": no_bids_from_yes_asks(ob["asks"])}
    print("STEP 3  SUSQ order book in NO prices (from the YES book: NO ask = 1 - YES bid, NO bid = 1 - YES ask)")
    for x in "DR":
        bb, ba = books[x]["bids"][:1], books[x]["asks"][:1]
        print(f"   NO_{x}: best bid {bb[0][0] if bb else '-'} ({bb[0][1] if bb else 0:,.0f}) | best ask {ba[0][0] if ba else '-'} "
              f"({ba[0][1] if ba else 0:,.0f})   vs fair {fair[x]:.4f}:  bid - fair = {bb[0][0] - fair[x] if bb else 0:+.4f}, "
              f"fair - ask = {fair[x] - ba[0][0] if ba else 0:+.4f}")
    pairs = min(h.values()); exposure = h[und] - h[fav]
    race_cap = cap_total * pairs / pairs_total
    print(f"STEP 4  Position: NO_D {h['D']:,.0f}, NO_R {h['R']:,.0f} -> pairs {pairs:,.0f}; "
          f"exposure = NO_{und} - NO_{fav} = {exposure:,.0f} shares (pay 0 if {und} wins)")
    print(f"        race cap = total cap x pairs share = {cap_total:,.0f} x {pairs:,.0f}/{pairs_total:,.0f} = {race_cap:,.0f}; "
          f"room to add risk = {max(race_cap - max(exposure, 0), 0):,.0f}")
    mode = "closing" if pairs < 1 else "holding"
    print(f"        mode: {mode}" + ("  (no pairs left: slow two-sided unwind at the touch)" if mode == "closing" else ""))
    if mode == "holding":
        u = 0 if race_cap <= 0 else max(-1.5, min(1.5, exposure / race_cap))
        shift = config.B_SKEW * u
        print(f"STEP 5  Skew: exposure / race cap = {u:.3f} -> shift = {config.B_SKEW} x {u:.3f} = {shift:.4f}")
        print(f"        reservation r_{fav} = fair + shift = {fair[fav] + shift:.4f}  (keener to buy back NO_{fav})")
        print(f"        reservation r_{und} = fair - shift = {fair[und] - shift:.4f}  (keener to sell NO_{und})")
        r = {fav: fair[fav] + shift, und: fair[und] - shift}
        print("STEP 6  Rules against r (sell if price >= r + edge, buy if price <= r - edge):")
        for x in "DR":
            bb = books[x]["bids"][0][0] if books[x]["bids"] else None
            ba = books[x]["asks"][0][0] if books[x]["asks"] else None
            add = "adds risk" if x == fav else "cuts risk"
            print(f"   NO_{x}: take-sell needs bid >= {r[x] + config.B_TAKE_EDGE:.4f} (bid {bb}) -> "
                  f"{'YES' if bb is not None and bb >= r[x] + config.B_TAKE_EDGE - 1e-9 else 'no'}  [{add if x == fav else 'cuts risk'}]")
            print(f"          take-buy  needs ask <= {r[x] - config.B_TAKE_EDGE:.4f} (ask {ba}) -> "
                  f"{'YES' if ba is not None and ba <= r[x] - config.B_TAKE_EDGE + 1e-9 else 'no'}  [{'cuts risk' if x == fav else 'adds risk'}]")
            print(f"          quote ask = max(best ask {ba}, r + {config.B_QUOTE_EDGE} = {r[x] + config.B_QUOTE_EDGE:.4f}); "
                  f"quote bid <= r - {config.B_QUOTE_EDGE} = {r[x] - config.B_QUOTE_EDGE:.4f}, below the ask, and + other leg's bid < 1")
    res = strategy_b.decide(books, k["p"], h, 1e18, kalshi_ok=k["ok"], cash=spend, race_cap=race_cap)
    print("STEP 7  strategy_b.decide (the live code) returns:")
    for o in res["orders"] or [{"none": res["why"]}]:
        print(f"   {o}")
    print("STEP 8  What the executor sends (quotes only at the best price; takes first; caps; cash):")
    sent = 0
    for o in res["orders"]:
        top = books[o["leg"]]["asks" if o["side"] == "sell" else "bids"]
        at_touch = top and (abs(o["price"] - top[0][0]) < 1e-9 or (o["side"] == "buy" and o["price"] > top[0][0]))
        if o["kind"] == "quote" and not at_touch:
            print(f"   skip  {o['kind']} {o['side']} {o['qty']:,} NO_{o['leg']} @ {o['price']}: behind the best "
                  f"{'ask' if o['side'] == 'sell' else 'bid'} {top[0][0] if top else '-'}, would not fill")
            continue
        sent += 1
        print(f"   SEND  {o['kind']} {o['side']} {o['qty']:,} NO_{o['leg']} @ {o['price']}  "
              f"({o['edge_vs_fair']:+.4f} vs Kalshi fair)")
    if not sent:
        print("   nothing this round")
print("\n(read-only: no orders were sent)")
