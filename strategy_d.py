"""Strategy D: Senate-control stat arb, delta-hedged with the state races (user, 2026-10-04). Pure, no API.

Trade SUSQ's "U.S. Senate" market toward Kalshi's control price. Prices are NO prices.
  gap = Kalshi P(R control) - SUSQ P(R control), SUSQ P(R control) = mid of the NO on "Democrats win"
  long R control  (gap > 0): hold NO on Dem control;  hedge: NO on the Republican in each state
  short R control (gap < 0): hold NO on Rep control;  hedge: NO on the Democrat in each state
Hedge size per state = N x delta_i, delta_i = dP(R control)/dp_i (stat_model, national swing calibrated
to Kalshi): a unit of control gains delta_i per unit rise in p_i; a NO-on-Republican share loses 1.
Rules (user): enter at |gap| >= D_ENTRY_GAP; hold (and grow while the gap is still >= entry); exit
everything once |gap| <= D_EXIT_GAP or the gap changes sign; never converging = hold to settlement.
Rebalance a hedge only when it is off target by more than the band (gamma: deltas drift as prices move).
"""
import math

import config


def susq_p_r(ctrl_book):
    """SUSQ-implied P(R control) = mid of the NO on 'Democrats win the Senate', or None."""
    b, a = ctrl_book["D"]["bid"], ctrl_book["D"]["ask"]
    return None if b is None or a is None else (b + a) / 2


def plan(k_r, ctrl_book, deltas, hedge_books, ledger, cash):
    """k_r: Kalshi P(R control). ctrl_book / hedge_books[race]: {"D"/"R": {"bid", "ask"}} NO prices on SUSQ.
    deltas: {race: delta} for the races SUSQ lists (the hedgeable ones). ledger: D's own NO shares,
    {("ctrl", "D"|"R"): n, (race, "D"|"R"): n}. cash: SUSQies D may spend.
    Returns {"gap", "direction", "target_n", "hedge_targets", "orders": [...]}; each order is
    {"race", "leg", "side", "qty", "price", "kind": "ctrl"|"hedge"} (race "ctrl" = the control market)."""
    p_s = susq_p_r(ctrl_book)
    out = {"gap": None, "direction": 0, "target_n": 0, "hedge_targets": {}, "orders": []}
    if p_s is None:
        return out
    gap = k_r - p_s
    out["gap"] = round(gap, 4)
    held_d, held_r = ledger.get(("ctrl", "D"), 0), ledger.get(("ctrl", "R"), 0)
    cur = 1 if held_d > 0 else -1 if held_r > 0 else 0          # +1 long R control, -1 short
    want = cur
    if cur != 0 and (abs(gap) <= config.D_EXIT_GAP or gap * cur < 0):
        want = 0                                                 # converged (or flipped): exit everything
    elif cur == 0 and abs(gap) >= config.D_ENTRY_GAP:
        want = 1 if gap > 0 else -1
    out["direction"] = want
    ctrl_leg = "D" if want >= 0 else "R"                         # long R = NO on Dem control
    hedge_leg = "R" if want >= 0 else "D"                        # long R hedge = NO on the state Republican
    orders, spend = [], cash

    # control leg: exit, hold, or add (adding only while the gap is still >= entry, after paying the ask)
    held_ctrl = ledger.get(("ctrl", ctrl_leg), 0)
    n = held_ctrl
    for leg in "DR":                                             # wrong-side or exit: sell what D holds
        h = ledger.get(("ctrl", leg), 0)
        if h >= 1 and (want == 0 or leg != ctrl_leg) and ctrl_book[leg]["bid"] is not None:
            q = min(h, config.D_CLIP)
            orders.append({"race": "ctrl", "leg": leg, "side": "sell", "qty": math.floor(q), "price": ctrl_book[leg]["bid"], "kind": "ctrl"})
            if leg == ctrl_leg:
                n -= q
    if want != 0:
        # hedges first: cash the current position still needs for its hedges is reserved before any add
        # (live 2026-10-04: control grew to 3,589 while cash for its hedges ran out -> under-hedged)
        deficit = sum(max(0.0, held_ctrl * d - ledger.get((r, hedge_leg), 0)) * (hedge_books[r][hedge_leg]["ask"] or 1.0)
                      for r, d in deltas.items() if r in hedge_books)
        spend -= deficit
        ask = ctrl_book[ctrl_leg]["ask"]
        fair = k_r if ctrl_leg == "D" else 1 - k_r               # NO on Dem control pays if Republicans control
        if ask is not None and fair - ask >= config.D_ENTRY_GAP - 1e-9:
            unit = ask + sum(d * (hedge_books[r][hedge_leg]["ask"] or 1.0) for r, d in deltas.items()
                             if r in hedge_books)                # one unit of control plus its hedges
            add = min(config.D_CLIP, math.floor(max(spend, 0) / unit)) if unit > 0 else 0
            if add >= 1:
                orders.append({"race": "ctrl", "leg": ctrl_leg, "side": "buy", "qty": add, "price": ask, "kind": "ctrl"})
                spend -= add * ask
                n += add
        spend += deficit                                         # the reserved cash now pays the hedges
    out["target_n"] = max(n, 0)
    # hedges follow the control shares D actually holds (after this round's sales, before its buy fills):
    # a partly filled buy is hedged next round instead of over-hedged now
    n_h = max(held_ctrl - sum(o["qty"] for o in orders if o["kind"] == "ctrl" and o["side"] == "sell"
                              and o["leg"] == ctrl_leg), 0) if want != 0 else 0

    # hedges: N x delta on the hedge side; everything on the other side (or after an exit) goes to 0.
    # Band per state, relative to THAT state's target (live bug 2026-10-04: a band of 10% of the whole
    # control position, 359 shares, was larger than every state's shortfall, so D never rebalanced)
    moves = []
    for race, d in deltas.items():
        if race not in hedge_books:
            continue
        tgt = n_h * d if want != 0 else 0.0
        out["hedge_targets"][race] = round(tgt)
        for leg in "DR":
            held = ledger.get((race, leg), 0)
            goal = tgt if (leg == hedge_leg and want != 0) else 0.0
            diff = goal - held
            band = max(config.D_MIN_TRADE, config.D_BAND_FRAC * goal)
            if (goal == 0 and held >= 1) or abs(diff) > band:
                moves.append((abs(diff), race, leg, diff))
    for _, race, leg, diff in sorted(moves, key=lambda t: -t[0]):
        book = hedge_books[race][leg]
        if diff > 0 and book["ask"] is not None:
            q = min(math.floor(diff), math.floor(max(spend, 0) / book["ask"]))
            if q >= 1:
                orders.append({"race": race, "leg": leg, "side": "buy", "qty": q, "price": book["ask"], "kind": "hedge"})
                spend -= q * book["ask"]
        elif diff < 0 and book["bid"] is not None:
            q = min(math.floor(-diff), math.floor(ledger.get((race, leg), 0)))
            if q >= 1:
                orders.append({"race": race, "leg": leg, "side": "sell", "qty": q, "price": book["bid"], "kind": "hedge"})
    out["orders"] = orders[:config.D_MAX_ORDERS] if len(orders) > config.D_MAX_ORDERS else orders
    return out
