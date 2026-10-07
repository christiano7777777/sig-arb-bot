"""Strategy E: Kalshi-jump breakout (user, 2026-10-07). Pure functions, no API.

Per leg x of a race (NO prices): fair_x = 1 - p_x from Kalshi (mid, overround removed), SUSQ NO bid/ask/mid.
SUSQ usually sits a race-specific distance from Kalshi (the "usual gap", median of fair_x - mid_x over the
last E_BASELINE_S), so E trades a CHANGE in that gap, not the gap itself:
  entry   Kalshi fair_x rose >= E_JUMP within E_JUMP_WINDOW_S, and SUSQ has not followed: buy NO_x at asks
          <= target - E_MARGIN, target = fair_x - usual gap (where SUSQ's mid goes if it follows as usual)
  exit    caught up: the best bid >= target(now) - E_EXIT_SLACK  -> sell into bids down to that price
          reversal:  fair_x <= entry fair - E_JUMP / 2               -> sell at the best bid
          otherwise: hold (an unexited position is a hold to settlement)
"""
import math
import statistics

import config


def down(x):
    return round(math.floor(round(x / config.TICK, 6)) * config.TICK, 6)


def baseline(hist, now):
    """Usual gap fair - mid over the last E_BASELINE_S, before the jump window. hist: [(t, fair, mid)]."""
    pts = [f - m for t, f, m in hist
           if now - config.E_BASELINE_S <= t <= now - config.E_JUMP_WINDOW_S and m is not None]
    return statistics.median(pts) if len(pts) >= 10 else None


def signal(hist, now, fair, ask):
    """Entry for one leg, or None. hist: [(t, fair, mid)] oldest first (before now); ask: best NO ask."""
    if not hist or hist[0][0] > now - config.E_MIN_HISTORY_S or ask is None:
        return None
    base = baseline(hist, now)
    if base is None:
        return None
    ref = [f for t, f, m in hist if t <= now - config.E_JUMP_WINDOW_S]
    if not ref:
        return None
    jump = fair - ref[-1]                              # Kalshi move since the start of the window
    if jump < config.E_JUMP - 1e-9:
        return None
    target = fair - base
    limit = down(target - config.E_MARGIN)
    if limit < ask - 1e-9 or limit >= 1:               # SUSQ already followed (or no room left)
        return None
    return {"jump": round(jump, 4), "baseline": round(base, 4), "target": round(target, 4), "limit": limit}


def size_buy(asks, limit, cash):
    """Walk NO asks [(px, qty)] best first up to limit. One order goes out at the limit, so the size is also
    capped at cash / limit (every share filling at the limit stays within cash). Returns (qty, worst price)."""
    qty, worst = 0, None
    for px, q in asks:
        if px > limit + 1e-9:
            break
        qty, worst = qty + q, px
    qty = math.floor(min(qty, cash / limit) + 1e-9) if limit > 0 else 0
    return (qty, worst) if qty >= 1 else (0, None)


def exit_rule(pos, fair, bid):
    """Exit for a held leg, or None. pos: {"entry_fair", "baseline"} (None after a restart: no reversal rule;
    the usual gap is then filled in from fresh history). Returns {"why", "floor"} (lowest price to sell at)."""
    if bid is None or pos.get("baseline") is None:
        return None
    floor_px = down(fair - pos["baseline"] - config.E_EXIT_SLACK)
    if bid >= floor_px - 1e-9:
        return {"why": "caught up", "floor": floor_px}
    if pos.get("entry_fair") is not None and fair <= pos["entry_fair"] - config.E_JUMP / 2 + 1e-9:
        return {"why": "reversal", "floor": bid}
    return None


def size_sell(bids, floor_px, held):
    """Walk NO bids [(px, qty)] best first down to floor_px. Returns (qty, lowest price) or (0, None)."""
    qty, worst = 0, None
    for px, q in bids:
        if px < floor_px - 1e-9 or qty >= held:
            break
        n = math.floor(min(q, held - qty) + 1e-9)
        if n >= 1:
            qty, worst = qty + n, px
    return qty, worst
