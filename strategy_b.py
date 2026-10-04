"""Strategy B decisions for one race: pure function, no API calls (used by the shadow tool now,
by the live executor later).

Legs are "D" and "R"; prices are NO prices on SUSQ. Kalshi gives the fair win probability p of
each party, so the fair NO price of party x is 1 - p[x].
  favourite f: party with p >= B_MIN_FAVOURITE (else the race is not traded)
  underdog  u: the other party
  exposure = NO_u - NO_f = shares that pay 0 if the underdog wins (matched pairs always pay 1)
Selling the rich leg (usually NO_f) or buying the cheap leg (usually NO_u) raises exposure by 1 per
share, so both draw on the same caps; trades that lower exposure are never capped.

Rules agreed with the user (2026-10-04):
  - "don't make it worse": our NO ask >= the leg's current best NO ask (never undercut);
    our NO bid + the other leg's best NO bid < 1 (nobody can sell us a pair at >= 1)
  - a race is active while either leg has shares; left when both legs are 0
  - closing (user, 2026-10-04): once the race holds no arb pairs (swapped out or exited), B unwinds
    the leftover leg SLOWLY with skewed two-sided quotes that still earn the spread:
      ask: at the best ask (never below it), B_CLOSE_CLIP shares per round
      bid: at the best bid, size B_CLOSE_BID_RATIO * clip * (1 - leftover / B_RACE_CAP), i.e. none
           while the leftover is large, more as it shrinks (size skew: we sell more than we buy back)
    until both legs are 0
"""
import math

import config

TICK = config.TICK
INF = float("inf")


def up(x):
    return round(math.ceil(round(x / TICK, 6)) * TICK, 6)


def down(x):
    return round(math.floor(round(x / TICK, 6)) * TICK, 6)


def decide(books, p, held, room_total, kalshi_ok=True, kalshi_jump=False, cash=INF):
    """books: {"D"/"R": {"bids": [(px, qty)...] best first, "asks": [...]}} in NO prices.
    p: Kalshi probabilities {"D": .., "R": ..}. held: NO shares {"D": .., "R": ..}.
    room_total: shares of exposure still allowed across all races (from B_TOTAL_CAP_FRAC).
    cash: SUSQies available for buys (a buy the cash cannot pay for must not take up cap room).
    Returns {"active", "why", "exposure", "orders": [...]}; each order is
    {"leg", "side": "sell"/"buy", "kind": "take"/"quote", "price", "qty", "edge_vs_fair"}."""
    out = {"active": held["D"] > 0 or held["R"] > 0, "orders": [], "why": ""}
    if not out["active"]:
        out["why"] = "no shares on either leg: race left"
        return out
    pairs = min(held["D"], held["R"])
    if pairs < 1:
        # closing mode (no Kalshi needed): slow, skewed two-sided quotes on the leftover leg
        out["mode"] = "closing"
        leg = "D" if held["D"] >= 1 else "R"
        other = "R" if leg == "D" else "D"
        left = held[leg]
        if left < 1:
            return out
        bids, asks = books[leg]["bids"], books[leg]["asks"]
        ref = lambda px: round(px - (1.0 - p[leg]), 4) if kalshi_ok else 0.0
        if asks:                                   # sell at the best ask: never undercut anyone
            out["orders"].append({"leg": leg, "side": "sell", "kind": "quote", "price": asks[0][0],
                                  "qty": math.floor(min(left, config.B_CLOSE_CLIP) + 1e-9), "edge_vs_fair": ref(asks[0][0])})
        shrink = max(0.0, 1.0 - left / config.B_RACE_CAP)
        qb = math.floor(config.B_CLOSE_BID_RATIO * config.B_CLOSE_CLIP * shrink + 1e-9)
        qb = min(qb, math.floor(config.B_RACE_CAP - left + 1e-9))      # never above the race cap
        if bids and qb >= 1:
            b = bids[0][0]                         # buy back at the best bid (earns the spread)
            if asks:
                b = min(b, round(asks[0][0] - TICK, 6))
            if books[other]["bids"]:
                b = min(b, round(1.0 - books[other]["bids"][0][0] - TICK, 6))   # no pair sold to us at >= 1
            if b > 0:
                out["orders"].append({"leg": leg, "side": "buy", "kind": "quote", "price": b, "qty": qb,
                                      "edge_vs_fair": -ref(b) if kalshi_ok else 0.0})
        out["why"] = "" if out["orders"] else "closing: nothing to quote"
        return out
    out["mode"] = "holding"
    if not kalshi_ok:
        out["why"] = "Kalshi fair value not trusted"
        return out
    fav = max(p, key=p.get)
    und = "R" if fav == "D" else "D"
    exposure = held[und] - held[fav]
    out["exposure"] = exposure
    if p[fav] < config.B_MIN_FAVOURITE - 1e-9:
        out["why"] = f"favourite only {p[fav]:.3f} on Kalshi"
        return out
    room = max(0.0, min(config.B_RACE_CAP - max(exposure, 0.0), room_total))
    fair = {x: 1.0 - p[x] for x in "DR"}

    def raises(leg, side):
        # selling NO_f or buying NO_u raises exposure; the opposite trades lower it
        return (leg == fav and side == "sell") or (leg == und and side == "buy")

    # 1) every candidate order at full size; prio = edge vs fair at the best level we would trade
    cands = []
    for leg in "DR":
        other = "R" if leg == "D" else "D"
        bids, asks = books[leg]["bids"], books[leg]["asks"]
        # take: sell into bids >= fair + TAKE_EDGE (only shares we hold)
        lv = [(px, qty) for px, qty in bids if px >= fair[leg] + config.B_TAKE_EDGE - 1e-9]
        if lv and held[leg] >= 1:
            cands.append({"leg": leg, "side": "sell", "kind": "take", "price": min(px for px, _ in lv),
                          "qty": min(sum(q for _, q in lv), held[leg]), "prio": lv[0][0] - fair[leg]})
        # take: buy asks <= fair - TAKE_EDGE
        lv = [(px, qty) for px, qty in asks if px <= fair[leg] - config.B_TAKE_EDGE + 1e-9]
        if lv:
            qty = sum(q for _, q in lv) if raises(leg, "buy") else min(sum(q for _, q in lv), max(held[other] - held[leg], 0))
            cands.append({"leg": leg, "side": "buy", "kind": "take", "price": max(px for px, _ in lv),
                          "qty": qty, "prio": fair[leg] - lv[0][0]})
        if kalshi_jump:
            continue                                   # fair value just moved: no resting quotes
        # quote: ask at/above the best ask (never undercut), and >= fair + QUOTE_EDGE
        if held[leg] >= 1 and asks:
            a = max(asks[0][0], up(fair[leg] + config.B_QUOTE_EDGE))
            if a < 1:
                cands.append({"leg": leg, "side": "sell", "kind": "quote", "price": a, "qty": held[leg],
                              "prio": a - fair[leg]})
        # quote: bid <= fair - QUOTE_EDGE, below the best ask, and our bid + other leg's best bid < 1
        if bids or asks:
            b = down(fair[leg] - config.B_QUOTE_EDGE)
            if asks:
                b = min(b, round(asks[0][0] - TICK, 6))
            if books[other]["bids"]:
                b = min(b, round(1.0 - books[other]["bids"][0][0] - TICK, 6))
            qty = INF if raises(leg, "buy") else max(held[other] - held[leg], 0)
            if b > 0 and qty >= 1:
                cands.append({"leg": leg, "side": "buy", "kind": "quote", "price": b, "qty": qty,
                              "prio": fair[leg] - b})
    # 2) race cap (and room_total): takes first (a sure fill beats a quote that may never fill),
    #    each group by largest edge;
    #    a leg is never sold beyond what is held (take + quote together)
    left, sold, cash_left = room, {"D": 0.0, "R": 0.0}, cash
    for o in sorted(cands, key=lambda o: (o["kind"] != "take", -o["prio"])):
        qty = o["qty"]
        if o["side"] == "sell":
            qty = min(qty, held[o["leg"]] - sold[o["leg"]])
        if o["side"] == "buy":
            qty = min(qty, cash_left / o["price"])
        if raises(o["leg"], o["side"]):
            qty = min(qty, left)
        qty = math.floor(qty + 1e-9)
        if qty < 1:
            continue
        if raises(o["leg"], o["side"]):
            left -= qty
        if o["side"] == "sell":
            sold[o["leg"]] += qty
        else:
            cash_left -= qty * o["price"]
        out["orders"].append({"leg": o["leg"], "side": o["side"], "kind": o["kind"], "price": o["price"],
                              "qty": qty, "edge_vs_fair": round(o["prio"] if o["kind"] == "quote" else
                                                                (o["price"] - fair[o["leg"]] if o["side"] == "sell"
                                                                 else fair[o["leg"]] - o["price"]), 4)})
    return out
