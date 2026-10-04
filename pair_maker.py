"""Pair maker: resting two-sided liquidity on held races (user, 2026-10-04). Pure function, no API.

On a race we hold, quote BOTH legs at the touch, as a pair:
  asks: each leg at its best NO ask (never below it: "don't make it worse"), so held pairs sell at
        the ASK sum as maker instead of being hit into the bid sum by a swap/exit. Offered only when
        the pair's ask sum >= the cheapest new pair anywhere + ROTATE_MIN_GAIN, so a swap can always
        redeploy the proceeds at a gain.
  bids: each leg at its best NO bid, only while the bid sum <= 1 - MIN_EDGE (nobody can sell us a pair
        at >= 1; every filled pair is an entry at an edge), sized to the cash available.
One-leg fills leave unequal legs, which strategy B treats as exposure (caps and skew manage it).
"""
import math

import config


def pair_quotes(books, held, cheapest_new_pair, cash, fav=None, room=math.inf):
    """books: {"D"/"R": {"bids": [(px, qty)..], "asks": [..]}} NO prices, best first (top of book is enough).
    held: NO shares per leg. fav: Kalshi favourite or None. room: B exposure room (selling NO_fav adds
    exposure if only that leg fills). Returns a list of orders {"leg", "side", "price", "qty"}."""
    out = []
    pairs = math.floor(min(held["D"], held["R"]) + 1e-9)
    asks = {x: books[x]["asks"][0][0] for x in "DR" if books[x]["asks"]}
    bids = {x: books[x]["bids"][0][0] for x in "DR" if books[x]["bids"]}
    if len(asks) == 2 and pairs >= 1 and cheapest_new_pair is not None:
        s_ask = asks["D"] + asks["R"]
        if s_ask >= cheapest_new_pair + config.ROTATE_MIN_GAIN - 1e-9:
            q = min(config.MAKER_CLIP, pairs)
            if fav is not None:
                q = min(q, math.floor(room + 1e-9))   # a fill on NO_fav alone must fit B's risk room
            if q >= 1:
                out += [{"leg": x, "side": "sell", "price": asks[x], "qty": q} for x in "DR"]
    if len(bids) == 2:
        s_bid = bids["D"] + bids["R"]
        if s_bid <= 1.0 - config.MIN_EDGE + 1e-9:
            q = min(config.MAKER_CLIP, math.floor(cash / s_bid + 1e-9))
            if q >= 1:
                out += [{"leg": x, "side": "buy", "price": bids[x], "qty": q} for x in "DR"]
    return out
