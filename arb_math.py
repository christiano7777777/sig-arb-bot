"""Pure functions for the basket arbitrage. No network calls, so they can be unit-tested.

Price conventions (from the API docs):
  * Order books are YES-normalised: bids/asks are YES prices in [0, 1].
  * Buying NO at p is the same trade as selling YES at 1 - p, so it matches YES *bids*.
    => NO ask ladder = [(1 - yes_bid_price, yes_bid_quantity) for each YES bid level]
"""
import math

EPS = 1e-9


def no_asks_from_yes_bids(yes_bids):
    """YES bids (best first, i.e. price descending) -> NO asks (best first, price ascending)."""
    asks = [(round(1.0 - lvl["price"], 6), lvl["quantity"]) for lvl in yes_bids]
    return sorted(asks, key=lambda a: a[0])


def no_bids_from_yes_asks(yes_asks):
    """YES asks -> NO bids (best first, price descending). Selling NO at p = buying YES at 1 - p."""
    bids = [(round(1.0 - lvl["price"], 6), lvl["quantity"]) for lvl in yes_asks]
    return sorted(bids, key=lambda b: -b[0])


def ceil_to_tick(price, tick):
    """Smallest on-tick price >= price (a buy limit must not be below the level we want to take)."""
    return round(math.ceil(price / tick - EPS) * tick, 6)


def floor_to_tick(price, tick):
    """Largest on-tick price <= price (a sell limit must not be above the bid we want to hit)."""
    return round(math.floor(price / tick + EPS) * tick, 6)


def walk_baskets(ladders, min_payout, min_edge, max_baskets=None, max_cost=None):
    """Walk all legs' ask ladders together, buying the same quantity on every leg.

    ladders     : one list per leg of (price, quantity), best price first.
    min_payout  : guaranteed payout per basket.
    min_edge    : only take a step if its marginal basket cost <= min_payout - min_edge.
    max_baskets : optional cap on total quantity.
    max_cost    : optional cap on total spend.

    Returns dict with quantity, total cost, average cost per basket, the worst
    price reached on each leg, and the list of steps (for printing).
    """
    idx = [0] * len(ladders)                       # current level on each leg
    left = [lad[0][1] if lad else 0 for lad in ladders]  # quantity left at that level
    qty, cost, steps = 0, 0.0, []
    worst = [None] * len(ladders)

    while all(i < len(lad) for i, lad in zip(idx, ladders)):
        prices = [lad[i][0] for i, lad in zip(idx, ladders)]
        marginal = sum(prices)
        if marginal > min_payout - min_edge + EPS:
            break

        step = math.floor(min(left) + EPS)         # whole shares only
        if max_baskets is not None:
            step = min(step, max_baskets - qty)
        if max_cost is not None:
            step = min(step, math.floor((max_cost - cost) / marginal + EPS))
        if step <= 0:
            break

        qty += step
        cost += step * marginal
        worst = prices
        steps.append({"prices": prices, "quantity": step, "marginal_cost": round(marginal, 6)})

        # consume `step` from every leg, advance legs whose level is used up
        for k in range(len(ladders)):
            left[k] -= step
            if left[k] <= EPS:
                idx[k] += 1
                if idx[k] < len(ladders[k]):
                    left[k] = ladders[k][idx[k]][1]
        if max_baskets is not None and qty >= max_baskets:
            break

    return {
        "quantity": qty,
        "total_cost": round(cost, 6),
        "avg_cost": round(cost / qty, 6) if qty else None,
        "locked_profit_min": round(qty * min_payout - cost, 6),
        "worst_prices": worst,
        "steps": steps,
    }


def walk_exit(bid_ladders, min_sum, max_baskets=None):
    """Walk the NO *bid* ladders of all legs, selling the same quantity on every leg,
    while the marginal basket proceeds (sum of bids) stay >= min_sum.

    Reuses walk_baskets: selling at bid b is scored as a "cost" of 1 - b, so
    proceeds >= min_sum  <=>  sum(1 - b) <= n_legs - min_sum.
    """
    n = len(bid_ladders)
    cost_ladders = [[(round(1.0 - b, 6), q) for b, q in lad] for lad in bid_ladders]
    r = walk_baskets(cost_ladders, min_payout=n - min_sum, min_edge=0.0, max_baskets=max_baskets)
    q = r["quantity"]
    return {
        "quantity": q,
        "proceeds": round(q * n - r["total_cost"], 6),
        "avg_proceeds": round(n - r["avg_cost"], 6) if q else None,
        "worst_prices": [round(1.0 - c, 6) for c in r["worst_prices"]] if q else None,
    }


def widen_limits(limits, bound, tick, up=True):
    """Give the legs' limit prices the slack left before `bound`, one tick at a time, round-robin.

    Buys (up=True): raise limits while sum(limits) + tick <= bound (each leg <= 1 - tick).
    Sells (up=False): lower limits while sum(limits) - tick >= bound (each leg >= tick).
    Orders still fill at the book price; the slack only matters if the book moves by a tick,
    and then it keeps both legs filling instead of one.
    """
    out = list(limits)
    step = tick if up else -tick
    k = 0
    stalled = 0
    while stalled < len(out):
        nxt = round(out[k] + step, 6)
        new_sum = sum(out) + step
        ok_sum = new_sum <= bound + EPS if up else new_sum >= bound - EPS
        ok_leg = tick - EPS <= nxt <= 1 - tick + EPS
        if ok_sum and ok_leg:
            out[k] = nxt
            stalled = 0
        else:
            stalled += 1
        k = (k + 1) % len(out)
    return out


def fill_price(ladder, qty):
    """Worst price level needed to fill `qty` from a ladder (best first), or None if too thin."""
    left = qty
    for price, q in ladder:
        left -= q
        if left <= EPS:
            return price
    return None
