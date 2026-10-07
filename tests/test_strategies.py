"""Offline tests of the pure strategy functions: pair maker (B) and Kalshi market making (C).
Run: python tests/test_strategies.py"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
config.C_CUT_ONLY = False  # two-sided mode is tested here; cut-only in test_execute_dry
import pair_maker  # noqa: E402
import stat_model  # noqa: E402
import strategy_c  # noqa: E402
import strategy_d  # noqa: E402

# Delaware-like at 06:55 UTC 2026-10-04: SUSQ NO_D 0.12/0.125, NO_R 0.84/0.845; Kalshi p_D 0.986
BOOKS = {"D": {"bids": [(0.12, 3000)], "asks": [(0.125, 2000)]},
         "R": {"bids": [(0.84, 4000)], "asks": [(0.845, 6000)]}}
P = {"D": 0.986, "R": 0.014}


def pin():
    config.TICK, config.MIN_EDGE, config.ROTATE_MIN_GAIN, config.MAKER_CLIP = 0.005, 0.005, 0.001, 500
    config.C_LIMIT, config.C_SKEW, config.C_SKEW_MAX, config.C_QUOTE_EDGE, config.C_CLIP = 2_000, 0.10, 0.25, 0.02, 500


def sides(res):
    return {(o["leg"], o["side"]) for o in res["orders"]}


# ---- B: pair maker ----------------------------------------------------------
def test_maker_asks_both_legs_at_best_ask_when_a_swap_can_redeploy():
    pin()
    o = pair_maker.pair_quotes(BOOKS, {"D": 9_000, "R": 9_000}, 0.965, cash=0)
    assert sorted((x["leg"], x["side"], x["price"], x["qty"]) for x in o) == [("D", "sell", 0.125, 500), ("R", "sell", 0.845, 500)]


def test_maker_does_not_sell_when_no_swap_could_use_the_cash():
    pin()
    assert not [x for x in pair_maker.pair_quotes(BOOKS, {"D": 9_000, "R": 9_000}, 0.975, cash=0) if x["side"] == "sell"]


def test_maker_bids_only_below_one_and_within_cash():
    pin()
    o = [x for x in pair_maker.pair_quotes(BOOKS, {"D": 0, "R": 0}, 0.965, cash=96) if x["side"] == "buy"]
    assert sorted((x["leg"], x["price"], x["qty"]) for x in o) == [("D", 0.12, 100), ("R", 0.84, 100)]


def test_maker_size_limited_by_room():
    pin()
    o = pair_maker.pair_quotes(BOOKS, {"D": 9_000, "R": 9_000}, 0.965, cash=0, fav="D", room=120)
    assert {x["qty"] for x in o if x["side"] == "sell"} == {120}


# ---- C: Kalshi market making ------------------------------------------------
def test_c_flat_race_quotes_to_add_toward_kalshi():
    pin()
    res = strategy_c.quotes(BOOKS, P, {"D": 5_000, "R": 5_000}, cash=10_000)
    assert ("D", "sell") in sides(res) and ("R", "buy") in sides(res)                  # sell rich leg, buy cheap leg
    sell = [o for o in res["orders"] if o["side"] == "sell"][0]
    assert sell["price"] == 0.125                                                       # at the best ask, never below


def test_c_adding_quotes_share_the_room():
    pin()
    res = strategy_c.quotes(BOOKS, P, {"D": 5_000, "R": 6_700}, cash=10_000)          # exposure 1,700: room 300
    assert sum(o["qty"] for o in res["orders"] if o["adds"]) <= 300


def test_c_above_limit_only_cuts_and_joins_the_touch():
    pin()
    res = strategy_c.quotes(BOOKS, P, {"D": 10_000, "R": 22_000}, cash=10_000)        # exposure 12,000
    assert all(not o["adds"] for o in res["orders"]) and res["orders"]
    for o in res["orders"]:
        assert o["price"] == (0.12 if o["side"] == "buy" else 0.845)                   # best bid / best ask


def test_c_cutting_never_goes_past_zero_exposure():
    pin()
    config.C_SKEW_MAX = 0.9                                                             # make both cut quotes active
    res = strategy_c.quotes(BOOKS, P, {"D": 10_000, "R": 10_300}, cash=10_000)        # exposure 300
    assert sum(o["qty"] for o in res["orders"] if not o["adds"]) <= 300


def test_c_bid_never_lets_anyone_sell_us_a_pair_at_or_above_one():
    pin()
    res = strategy_c.quotes(BOOKS, P, {"D": 5_000, "R": 5_000}, cash=10_000)
    for o in res["orders"]:
        if o["side"] == "buy":
            other = "R" if o["leg"] == "D" else "D"
            assert o["price"] + BOOKS[other]["bids"][0][0] < 1 - 1e-9
            assert o["price"] < BOOKS[o["leg"]]["asks"][0][0] - 1e-9


def test_c_no_sells_without_holdings():
    pin()
    res = strategy_c.quotes(BOOKS, P, {"D": 0, "R": 0}, cash=10_000)
    assert all(o["side"] == "buy" for o in res["orders"])


def test_c_inventory_settles_where_skew_offsets_the_gap():
    # rich leg 0.111 above fair: risk-adding sells stop roughly at exposure L x (0.111 - 0.02) / 0.10
    pin()
    adds_at = lambda e: any(o["adds"] and o["side"] == "sell" for o in
                            strategy_c.quotes(BOOKS, P, {"D": 10_000, "R": 10_000 + e}, cash=0)["orders"])
    assert adds_at(1_000) and not adds_at(1_900)


def test_c_null_case_market_at_fair_no_risk_adding_quotes():
    pin()
    fair_books = {"D": {"bids": [(0.01, 9000)], "asks": [(0.02, 9000)]}, "R": {"bids": [(0.98, 9000)], "asks": [(0.99, 9000)]}}
    res = strategy_c.quotes(fair_books, P, {"D": 5_000, "R": 5_000}, cash=10_000)
    assert not [o for o in res["orders"] if o["adds"]]


# ---- D: Senate-control stat arb ---------------------------------------------
def pin_d():
    config.D_ENTRY_GAP, config.D_EXIT_GAP, config.D_BAND_FRAC, config.D_MIN_TRADE = 0.03, 0.01, 0.10, 25
    config.D_MAX_ORDERS, config.D_CLIP = 6, 1_000


def test_model_reproduces_the_and_example():
    stat_model.R_HOLDOVER, stat_model.R_CONTROL = 48, 50            # need both of 2 races: X = A and B
    try:
        assert abs(stat_model.p_control([0.6, 0.7], 0.0) - 0.42) < 1e-9
        d = stat_model.deltas([0.6, 0.7], 0.0)
        assert abs(d[0] - 0.7) < 1e-6 and abs(d[1] - 0.6) < 1e-6   # dP(X)/dP(A) = P(B)
    finally:
        stat_model.R_HOLDOVER, stat_model.R_CONTROL = 31, 50


def test_model_calibration_recovers_a_known_correlation():
    import random
    random.seed(7)
    p = [random.uniform(0.05, 0.95) for _ in range(35)]
    rho = stat_model.calibrate(p, stat_model.p_control(p, 0.35))
    assert abs(rho - 0.35) < 1e-3


CTRL = {"D": {"bid": 0.315, "ask": 0.325}, "R": {"bid": 0.67, "ask": 0.68}}     # SUSQ P(R) ~ 0.32
HB = {"Texas Senate": {"D": {"bid": 0.35, "ask": 0.36}, "R": {"bid": 0.62, "ask": 0.63}},
      "Maine Senate": {"D": {"bid": 0.40, "ask": 0.41}, "R": {"bid": 0.57, "ask": 0.58}}}
DEL = {"Texas Senate": 0.15, "Maine Senate": 0.147}


def test_d_enters_long_r_when_susq_is_below_kalshi_and_hedges_next_round():
    pin_d()
    res = strategy_d.plan(0.375, CTRL, DEL, HB, {}, cash=10_000)
    assert res["direction"] == 1
    assert [(o["race"], o["leg"], o["side"]) for o in res["orders"]] == [("ctrl", "D", "buy")]   # no hedge yet
    res = strategy_d.plan(0.375, CTRL, DEL, HB, {("ctrl", "D"): 1_000}, cash=10_000)
    hedges = {(o["race"], o["leg"]): o["qty"] for o in res["orders"] if o["kind"] == "hedge"}
    assert hedges == {("Texas Senate", "R"): 150, ("Maine Senate", "R"): 147}           # N x delta, NO on the Republican


def test_d_no_entry_below_the_entry_gap():
    pin_d()
    assert strategy_d.plan(0.34, CTRL, DEL, HB, {}, cash=10_000)["orders"] == []


def test_d_hedge_inside_the_band_is_left_alone():
    pin_d()
    led = {("ctrl", "D"): 1_000, ("Texas Senate", "R"): 140, ("Maine Senate", "R"): 150}  # off by 10 and 3 (band 100)
    res = strategy_d.plan(0.375, CTRL, DEL, HB, led, cash=0)
    assert not [o for o in res["orders"] if o["kind"] == "hedge"]


def test_d_exits_everything_when_the_gap_closes():
    pin_d()
    led = {("ctrl", "D"): 1_000, ("Texas Senate", "R"): 150, ("Maine Senate", "R"): 147}
    res = strategy_d.plan(0.325, CTRL, DEL, HB, led, cash=10_000)                     # gap 0.005 <= 0.01
    assert res["direction"] == 0
    assert {(o["race"], o["leg"], o["side"], o["qty"]) for o in res["orders"]} == {
        ("ctrl", "D", "sell", 1_000), ("Texas Senate", "R", "sell", 150), ("Maine Senate", "R", "sell", 147)}


def test_d_exits_when_the_gap_changes_sign():
    pin_d()
    res = strategy_d.plan(0.28, CTRL, DEL, HB, {("ctrl", "D"): 1_000}, cash=10_000)
    assert res["direction"] == 0 and ("ctrl", "D", "sell") in {(o["race"], o["leg"], o["side"]) for o in res["orders"]}


def test_d_short_direction_uses_the_other_legs():
    pin_d()
    res = strategy_d.plan(0.27, CTRL, DEL, HB, {}, cash=10_000)                        # SUSQ R too high
    assert res["direction"] == -1 and res["orders"][0]["leg"] == "R"
    res = strategy_d.plan(0.27, CTRL, DEL, HB, {("ctrl", "R"): 1_000}, cash=10_000)
    assert {o["leg"] for o in res["orders"] if o["kind"] == "hedge"} == {"D"}


def test_d_pays_missing_hedges_before_adding_control():
    # 1,000 control held, no hedges yet: they need 150 x 0.63 + 147 x 0.58 = 179.8. With 180 cash nothing is
    # left for more control, and the hedges are placed in full (before the fix the add took the cash first)
    pin_d()
    res = strategy_d.plan(0.375, CTRL, DEL, HB, {("ctrl", "D"): 1_000}, cash=180)
    assert not [o for o in res["orders"] if o["kind"] == "ctrl" and o["side"] == "buy"]
    assert {(o["race"], o["qty"]) for o in res["orders"] if o["kind"] == "hedge"} == {("Texas Senate", 150), ("Maine Senate", 147)}


def test_d_rebalances_a_state_far_off_its_own_target():
    # live 2026-10-04: control 3,589, North Carolina target 325 held 0: the old band (10% of 3,589 = 359)
    # skipped it; the band is per state now (10% of 325 = 33)
    pin_d()
    deltas = {"North Carolina Senate": 0.0906, "Texas Senate": 0.149}
    hb = {"North Carolina Senate": {"D": {"bid": 0.145, "ask": 0.15}, "R": {"bid": 0.845, "ask": 0.85}}, **HB}
    led = {("ctrl", "D"): 3_589, ("Texas Senate", "R"): 484}
    res = strategy_d.plan(0.375, CTRL, deltas, hb, led, cash=3_000)
    nc = [o for o in res["orders"] if o["race"] == "North Carolina Senate"]
    assert nc and nc[0]["side"] == "buy" and nc[0]["qty"] == 325                        # 3,589 x 0.0906
    assert not [o for o in res["orders"] if o["race"] == "Texas Senate"]                # 534 vs 484: inside 10%


def test_d_spends_no_more_than_its_cash():
    pin_d()
    res = strategy_d.plan(0.375, CTRL, DEL, HB, {}, cash=300)
    assert sum(o["qty"] * o["price"] for o in res["orders"] if o["side"] == "buy") <= 300 + 1e-9


# ---- C fast unwind (user, 2026-10-07): sell the excess leg into bids within C_DUMP_GAP of Kalshi fair ----
def test_c_dump_sells_excess_leg_down_to_fair_minus_gap():
    pin(); config.C_DUMP_GAP = 0.03
    # Illinois-like: p_D 0.968, we hold 2,855 NO_R (fair 0.968); bids 0.945 x1,000, 0.94 x1,000, 0.935 x5,000
    books = {"D": {"bids": [(0.03, 9000)], "asks": [(0.04, 9000)]},
             "R": {"bids": [(0.945, 1000), (0.94, 1000), (0.935, 5000)], "asks": [(0.955, 9000)]}}
    o = strategy_c.dump(books, {"D": 0.968, "R": 0.032}, {"D": 0, "R": 2_855})
    assert o["leg"] == "R" and o["side"] == "sell" and o["kind"] == "take"
    assert o["price"] == 0.94 and o["qty"] == 2_000              # 0.935 < 0.968 - 0.03 = 0.938 -> not sold


def test_c_dump_never_sells_more_than_the_excess():
    pin(); config.C_DUMP_GAP = 0.03
    books = {"D": {"bids": [(0.5, 99_000)], "asks": [(0.51, 9000)]}, "R": {"bids": [(0.48, 9000)], "asks": [(0.49, 9000)]}}
    o = strategy_c.dump(books, {"D": 0.5, "R": 0.5}, {"D": 700, "R": 200})   # p tie -> fav D, excess is NO_D 500
    assert o["leg"] == "D" and o["qty"] == 500


def test_c_dump_nothing_when_bids_too_far_below_fair_or_off():
    pin(); config.C_DUMP_GAP = 0.03
    books = {"D": {"bids": [(0.1, 9000)], "asks": [(0.105, 9000)]}, "R": {"bids": [(0.855, 9000)], "asks": [(0.86, 9000)]}}
    assert strategy_c.dump(books, {"D": 0.986, "R": 0.014}, {"D": 0, "R": 2_497}) is None   # Delaware: 0.855 < 0.956
    assert strategy_c.dump(books, {"D": 0.5, "R": 0.5}, {"D": 0, "R": 0}) is None          # nothing to sell
    config.C_DUMP_GAP = None
    assert strategy_c.dump(books, {"D": 0.15, "R": 0.85}, {"D": 0, "R": 500}) is None


def test_c_dump_sells_when_bid_above_fair():
    pin(); config.C_DUMP_GAP = 0.03
    # CO-08-like: hold 500 NO_D, fair 0.146, best bid 0.21 (above fair: selling gains vs Kalshi)
    books = {"D": {"bids": [(0.21, 300), (0.2, 900)], "asks": [(0.215, 9000)]}, "R": {"bids": [(0.765, 9000)], "asks": [(0.77, 9000)]}}
    o = strategy_c.dump(books, {"D": 0.854, "R": 0.146}, {"D": 500, "R": 0})
    assert o["leg"] == "D" and o["qty"] == 500 and o["price"] == 0.2 and o["edge_vs_fair"] > 0


# ---- E: Kalshi-jump breakout (user, 2026-10-07) ------------------------------------------------------------
import strategy_e  # noqa: E402


def pin_e():
    config.TICK = 0.005
    config.E_JUMP, config.E_JUMP_WINDOW_S, config.E_BASELINE_S, config.E_MIN_HISTORY_S = 0.03, 60, 7_200, 1_800
    config.E_MARGIN, config.E_EXIT_SLACK = 0.01, 0.005


def flat_hist(now, fair=0.60, mid=0.58, minutes=60):
    """One sample every 10 s for `minutes`, Kalshi fair and SUSQ mid constant (usual gap 0.02)."""
    return [(now - s, fair, mid) for s in range(minutes * 60, 0, -10)]


def test_e_signal_on_kalshi_jump_when_susq_has_not_followed():
    pin_e(); now = 100_000.0
    sig = strategy_e.signal(flat_hist(now), now, fair=0.65, ask=0.59)       # Kalshi +0.05, SUSQ ask still 0.59
    assert sig and abs(sig["jump"] - 0.05) < 1e-9 and abs(sig["baseline"] - 0.02) < 1e-9
    assert abs(sig["target"] - 0.63) < 1e-9 and sig["limit"] == 0.62          # 0.65 - 0.02 - 0.01


def test_e_no_signal_without_jump_or_when_susq_followed():
    pin_e(); now = 100_000.0
    assert strategy_e.signal(flat_hist(now), now, fair=0.62, ask=0.59) is None     # +0.02 < 0.03
    assert strategy_e.signal(flat_hist(now), now, fair=0.65, ask=0.63) is None     # SUSQ already at 0.63 > 0.62


def test_e_no_signal_without_enough_history():
    pin_e(); now = 100_000.0
    assert strategy_e.signal(flat_hist(now, minutes=20), now, fair=0.65, ask=0.59) is None   # < 30 min


def test_e_jump_must_be_recent():
    pin_e(); now = 100_000.0
    h = flat_hist(now)[:-30] + [(now - s, 0.65, 0.58) for s in range(300, 0, -10)]   # Kalshi up 5 min ago
    assert strategy_e.signal(h, now, fair=0.65, ask=0.59) is None


def test_e_buy_size_walks_to_the_limit_within_cash():
    asks = [(0.59, 100), (0.60, 300), (0.62, 1000), (0.63, 9000)]
    assert strategy_e.size_buy(asks, 0.62, 10_000) == (1400, 0.62)
    qty, worst = strategy_e.size_buy(asks, 0.62, 200)                        # cash-limited, at the limit price
    assert qty == math.floor(200 / 0.62) and qty * 0.62 <= 200 + 1e-9
    assert strategy_e.size_buy([(0.63, 500)], 0.62, 1_000) == (0, None)         # nothing at or below the limit


def test_e_exit_caught_up_reversal_or_hold():
    pin_e()
    pos = {"entry_fair": 0.65, "baseline": 0.02}
    assert strategy_e.exit_rule(pos, fair=0.65, bid=0.625)["why"] == "caught up"     # 0.625 >= 0.63 - 0.005
    assert strategy_e.exit_rule(pos, fair=0.65, bid=0.60) is None                     # not yet: hold
    r = strategy_e.exit_rule(pos, fair=0.63, bid=0.58)                               # Kalshi gave back 0.02 >= 0.015
    assert r["why"] == "reversal" and r["floor"] == 0.58
    assert strategy_e.exit_rule({"entry_fair": None, "baseline": None}, 0.65, 0.7) is None   # no usual gap yet
    assert strategy_e.size_sell([(0.63, 200), (0.625, 500), (0.62, 9000)], 0.625, 600) == (600, 0.625)


if __name__ == "__main__":
    names = [n for n in dir() if n.startswith("test_")]
    bad = 0
    for n in names:
        try:
            globals()[n]()
            print("PASS", n)
        except Exception as e:
            bad += 1
            print("FAIL", n, repr(e))
    print(f"{len(names) - bad}/{len(names)} passed")
    sys.exit(1 if bad else 0)
