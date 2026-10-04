"""Offline tests of the pure strategy functions: pair maker (B) and Kalshi market making (C).
Run: python tests/test_strategies.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
import pair_maker  # noqa: E402
import strategy_c  # noqa: E402

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
