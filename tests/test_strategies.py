"""Offline tests of the pure strategy functions: pair maker (B) and Kalshi market making (C).
Run: python tests/test_strategies.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
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
