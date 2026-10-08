"""Strategy C: Kalshi-anchored two-sided market making (user, 2026-10-04). Pure function, no API.

Per race, around a reservation price that leans against our directional inventory:
    fair_x   = 1 - p_x                       (Kalshi mid, overround removed; NO price of party x)
    exposure = NO_und - NO_fav               (shares that pay 0 if the underdog wins)
    shift    = C_SKEW * exposure / C_LIMIT, capped at +-C_SKEW_MAX
    r_fav    = fair_fav + shift,  r_und = fair_und - shift
Quotes (no takes; never more than held is offered for sale):
    adds risk     sell NO_fav  at the best NO ask (never below it)      if best ask >= r_fav + C_QUOTE_EDGE
                  buy  NO_und  at min(r_und - edge, ask - tick, 1 - other leg's best bid - tick); this may
                               RAISE the best bid: the only push toward Kalshi the quote rule allows
    cuts risk     buy  NO_fav  AT the best bid (no improving: that would push away from Kalshi)
                  sell NO_und  AT the best ask
                  each only if the price is on the right side of r +- edge, and never past zero exposure
Cut-only mode (C_CUT_ONLY, user 2026-10-05): no new inventory anywhere; every quote moves the exposure
toward zero and never past it (no bids in races we do not hold).
Inventory: risk-adding quotes only while exposure < C_LIMIT (size <= the room); above it only risk-cutting
quotes, so today's large positions unwind as those fill. Inventory settles roughly where the skew
offsets the mispricing: bigger SUSQ-Kalshi gaps hold more, small gaps hold almost nothing.
"Don't make it worse": no ask below the best ask; no bid that lets anyone sell us a pair at >= 1.
"""
import math

import config

INF = float("inf")


def up(x):
    return round(math.ceil(round(x / config.TICK, 6)) * config.TICK, 6)


def down(x):
    return round(math.floor(round(x / config.TICK, 6)) * config.TICK, 6)


def quotes(books, p, held, cash):
    """books: {"D"/"R": {"bids": [(px, qty)..], "asks": [..]}} NO prices without our own orders, best first.
    p: Kalshi probabilities {"D", "R"}. held: NO shares per leg. cash: SUSQies available for buys.
    Returns {"exposure", "reservation", "orders": [{"leg", "side", "price", "qty", "adds", "edge_vs_fair"}]}."""
    fav = max(p, key=p.get)
    und = "R" if fav == "D" else "D"
    fair = {x: 1.0 - p[x] for x in "DR"}
    exposure = held[und] - held[fav]
    shift = max(-config.C_SKEW_MAX, min(config.C_SKEW_MAX, config.C_SKEW * exposure / config.C_LIMIT))
    r = {fav: fair[fav] + shift, und: fair[und] - shift}
    room = max(0.0, config.C_LIMIT - exposure)          # risk the adding quotes may still take on
    if getattr(config, "C_CUT_ONLY", False):            # user, 2026-10-05: only reduce what C holds; the
        room = max(0.0, -exposure)                       # 'adding' quotes may only bring a negative exposure to 0
    cut = max(0.0, exposure)                             # risk the cutting quotes may remove
    q, e, tick = config.C_CLIP, config.C_QUOTE_EDGE, config.TICK
    bb = {x: books[x]["bids"][0][0] if books[x]["bids"] else None for x in "DR"}
    ba = {x: books[x]["asks"][0][0] if books[x]["asks"] else None for x in "DR"}
    out = []

    def add(leg, side, price, qty, adds):
        qty = math.floor(qty + 1e-9)
        if qty >= 1 and 0 < price < 1:
            out.append({"leg": leg, "side": side, "price": round(price, 6), "qty": qty, "adds": adds,
                        "edge_vs_fair": round(price - fair[leg] if side == "sell" else fair[leg] - price, 4)})

    # adds: sell the favourite's NO at the best ask
    if ba[fav] is not None and ba[fav] >= r[fav] + e - 1e-9 and room >= 1:
        add(fav, "sell", ba[fav], min(q, room, held[fav]), True)
    # adds: buy the underdog's NO, possibly raising the best bid toward fair value
    if room >= 1:
        b = down(r[und] - e)
        if ba[und] is not None:
            b = min(b, round(ba[und] - tick, 6))
        if bb[fav] is not None:
            b = min(b, round(1.0 - bb[fav] - tick, 6))
        if bb[und] is None or b >= bb[und] - 1e-9:     # at or above the best bid (a lower one cannot fill)
            add(und, "buy", b, min(q, room, cash / b if b > 0 else 0), True)
    # cuts: buy the favourite's NO back at the best bid
    if cut >= 1 and bb[fav] is not None and bb[fav] <= r[fav] - e + 1e-9:
        add(fav, "buy", bb[fav], min(q, cut, cash / bb[fav] if bb[fav] > 0 else 0), False)
    # cuts: sell the underdog's NO at the best ask
    if cut >= 1 and ba[und] is not None and ba[und] >= r[und] + e - 1e-9:
        add(und, "sell", ba[und], min(q, cut, held[und]), False)
    # the two adding quotes together stay within the room; the two cutting ones never go past zero exposure
    left = {True: room, False: cut}
    for o in out:
        o["qty"] = math.floor(min(o["qty"], left[o["adds"]]) + 1e-9)
        left[o["adds"]] -= o["qty"]
    out = [o for o in out if o["qty"] >= 1]
    return {"exposure": exposure, "reservation": {x: round(r[x], 4) for x in "DR"}, "orders": out}


def dump_top(books, held):
    """No usable Kalshi price (user, 2026-10-08: flat anyway): sell the larger leg's excess at the best bid only, at
    most the size there (no walking down an unknown book). Returns a take order or None."""
    leg = "D" if held["D"] > held["R"] else "R"
    excess = abs(held["D"] - held["R"])
    if excess < 1 or not books[leg]["bids"]:
        return None
    px, size = books[leg]["bids"][0]
    qty = math.floor(min(excess, size, held[leg]) + 1e-9)
    if qty < 1:
        return None
    return {"leg": leg, "side": "sell", "price": round(px, 6), "qty": qty, "adds": False, "kind": "take",
            "edge_vs_fair": 0.0}


def dump(books, p, held, gap=None):
    """Fast unwind (user, 2026-10-07): sell C's excess leg into the bids at prices >= Kalshi fair - C_DUMP_GAP.
    books[x]["bids"]: full NO bid ladder of leg x, best first. The excess leg is the one the cutting quotes sell:
    the underdog's NO when exposure > 0, the favourite's NO when exposure < 0. Returns one take order
    (limit = the lowest acceptable level reached, size = what those levels hold, at most the excess) or None."""
    gap = getattr(config, "C_DUMP_GAP", None) if gap is None else gap
    if gap is None:
        return None
    fav = max(p, key=p.get)
    und = "R" if fav == "D" else "D"
    fair = {x: 1.0 - p[x] for x in "DR"}
    exposure = held[und] - held[fav]
    leg, excess = (und, exposure) if exposure > 0 else (fav, -exposure)
    floor_px = up(fair[leg] - gap)                       # on the tick grid, never below fair - gap
    qty, px = 0.0, None
    for price, size in books[leg]["bids"]:
        if price < floor_px - 1e-9:
            break
        qty, px = qty + size, price
    qty = math.floor(min(qty, excess, held[leg]) + 1e-9)
    if px is None or qty < 1:
        return None
    return {"leg": leg, "side": "sell", "price": round(px, 6), "qty": qty, "adds": False, "kind": "take",
            "edge_vs_fair": round(px - fair[leg], 4)}
