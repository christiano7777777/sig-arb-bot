"""Offline tests of strategy_b.decide (pure function, no network).
Run: python tests/test_strategy_b.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
import strategy_b  # noqa: E402

# Delaware-like race at 06:55 UTC 2026-10-04: SUSQ NO_D 0.12/0.125, NO_R 0.84/0.845; Kalshi p_D 0.986
BOOKS = {"D": {"bids": [(0.12, 3000), (0.115, 5000)], "asks": [(0.125, 2000)]},
         "R": {"bids": [(0.84, 4000)], "asks": [(0.845, 6000), (0.85, 9000)]}}
P = {"D": 0.986, "R": 0.014}


def pin():
    config.B_MIN_FAVOURITE, config.B_TAKE_EDGE, config.B_QUOTE_EDGE = 0.95, 0.05, 0.02
    config.B_RACE_CAP, config.TICK = 5_000, 0.005


def orders(res, **match):
    return [o for o in res["orders"] if all(o[k] == v for k, v in match.items())]


def test_race_left_when_both_legs_zero():
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 0, "R": 0}, room_total=1e9)
    assert not r["active"] and r["orders"] == []


def test_race_stays_active_with_one_leg_left():
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 0, "R": 300}, room_total=1e9)
    assert r["active"]


def test_no_trading_when_kalshi_not_trusted():
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 10_000, "R": 10_000}, room_total=1e9, kalshi_ok=False)
    assert r["orders"] == []


def test_no_trading_below_favourite_threshold():
    pin()
    r = strategy_b.decide(BOOKS, {"D": 0.9, "R": 0.1}, {"D": 10_000, "R": 10_000}, room_total=1e9)
    assert r["orders"] == []


def test_sells_rich_leg_and_respects_race_cap():
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 37_588, "R": 37_588}, room_total=1e9, cash=0)
    sells = orders(r, leg="D", side="sell", kind="take")
    assert sells and sells[0]["qty"] == 5_000                       # 8,000 bid >= 0.064, capped at 5k
    raising = sum(o["qty"] for o in r["orders"] if (o["leg"], o["side"]) in (("D", "sell"), ("R", "buy")))
    assert raising <= 5_000                                         # all exposure-raising orders share the cap


def test_total_cap_binds_before_race_cap():
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 37_588, "R": 37_588}, room_total=1_200)
    raising = sum(o["qty"] for o in r["orders"] if (o["leg"], o["side"]) in (("D", "sell"), ("R", "buy")))
    assert raising <= 1_200


def test_existing_exposure_uses_up_room():
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 0, "R": 5_000}, room_total=1e9)   # already 5k at risk
    assert not [o for o in r["orders"] if (o["leg"], o["side"]) in (("D", "sell"), ("R", "buy"))]


def test_ask_never_undercuts_best_ask():
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 1_000, "R": 1_000}, room_total=1e9)
    for o in orders(r, side="sell", kind="quote"):
        assert o["price"] >= BOOKS[o["leg"]]["asks"][0][0] - 1e-9


def test_bid_never_lets_anyone_sell_us_a_pair_at_or_above_one():
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 1_000, "R": 1_000}, room_total=1e9)
    for o in orders(r, side="buy", kind="quote"):
        other = "R" if o["leg"] == "D" else "D"
        assert o["price"] + BOOKS[other]["bids"][0][0] < 1 - 1e-9
        assert o["price"] < BOOKS[o["leg"]]["asks"][0][0] - 1e-9            # never crosses the ask


def test_no_quotes_after_kalshi_jump():
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 1_000, "R": 1_000}, room_total=1e9, kalshi_jump=True)
    assert not orders(r, kind="quote")


def test_cap_goes_to_the_bigger_edge_first_within_a_race():
    # buying NO_R (0.845 vs fair 0.986, edge 0.141) beats selling NO_D (0.12 vs 0.014, edge 0.106)
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 37_588, "R": 37_588}, room_total=1e9, cash=10_000)
    first_raising = [o for o in r["orders"] if (o["leg"], o["side"]) in (("D", "sell"), ("R", "buy"))][0]
    assert (first_raising["leg"], first_raising["side"]) == ("R", "buy")


def test_buys_limited_by_cash():
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 37_588, "R": 37_588}, room_total=1e9, cash=500)
    assert sum(o["qty"] * o["price"] for o in r["orders"] if o["side"] == "buy") <= 500 + 1e-9


def test_closing_sells_a_clip_at_best_ask_and_no_buyback_at_full_size():
    pin(); config.B_CLOSE_CLIP, config.B_CLOSE_BID_RATIO = 500, 0.5
    r = strategy_b.decide(BOOKS, P, {"D": 0, "R": 5_000}, room_total=1e9, cash=10_000)
    assert r["mode"] == "closing"
    assert [(o["leg"], o["side"], o["price"], o["qty"]) for o in r["orders"]] == [("R", "sell", 0.845, 500)]


def test_closing_buys_back_more_as_leftover_shrinks_and_earns_spread():
    pin(); config.B_CLOSE_CLIP, config.B_CLOSE_BID_RATIO = 500, 0.5
    r = strategy_b.decide(BOOKS, P, {"D": 0, "R": 1_000}, room_total=1e9, cash=10_000)
    sell = orders(r, side="sell")[0]; buy = orders(r, side="buy")[0]
    assert sell["price"] == 0.845 and buy["price"] == 0.84          # ask at best ask, bid at best bid
    assert buy["qty"] == 200 and sell["qty"] == 500                 # 0.5 * 500 * (1 - 1000/5000); net selling


def test_closing_works_without_kalshi():
    pin()
    config.B_CLOSE_CLIP = 500
    r = strategy_b.decide(BOOKS, P, {"D": 0, "R": 300}, room_total=1e9, kalshi_ok=False)
    assert orders(r, side="sell")[0]["qty"] == 300


def test_holding_mode_while_pairs_remain():
    pin()
    r = strategy_b.decide(BOOKS, P, {"D": 10, "R": 5_010}, room_total=1e9, cash=0)
    assert r["mode"] == "holding"


# books where NO_D's bid sits 0.061 above fair (0.014): a take with no exposure, none once skewed
NEAR = {"D": {"bids": [(0.075, 3000)], "asks": [(0.08, 2000)]},
        "R": {"bids": [(0.91, 4000)], "asks": [(0.915, 6000)]}}


def test_skew_needs_more_edge_to_add_risk_as_exposure_grows():
    pin(); config.B_SKEW = 0.05
    flat = strategy_b.decide(NEAR, P, {"D": 10_000, "R": 10_000}, room_total=1e9, cash=0, race_cap=5_000)
    assert orders(flat, leg="D", side="sell", kind="take")                      # 0.061 >= 0.05: take
    loaded = strategy_b.decide(NEAR, P, {"D": 6_000, "R": 10_000}, room_total=1e9, cash=0, race_cap=5_000)
    assert not orders(loaded, leg="D", side="sell", kind="take")                # needs 0.05 + 0.04 now


CONVERGED = {"D": {"bids": [(0.06, 3000)], "asks": [(0.065, 2000)]},       # SUSQ moved toward Kalshi
             "R": {"bids": [(0.92, 4000)], "asks": [(0.925, 6000)]}}


def test_moderate_skew_brings_a_buyback_quote_to_the_touch_when_exposed():
    pin(); config.B_SKEW = 0.07                                                 # r_f = 0.014 + 0.07
    r = strategy_b.decide(CONVERGED, P, {"D": 5_000, "R": 10_000}, room_total=1e9, cash=10_000, race_cap=5_000)
    buy = orders(r, leg="D", side="buy", kind="quote")
    assert buy and buy[0]["price"] == 0.06 and buy[0]["qty"] <= 5_000           # joins the best bid
    assert not orders(r, side="buy", kind="take") and not orders(r, leg="R", side="sell", kind="take")


def test_strong_skew_takes_to_cut_risk_but_not_past_zero():
    pin(); config.B_SKEW = 0.12                                                 # r_f = 0.134, r_u = 0.866
    r = strategy_b.decide(CONVERGED, P, {"D": 5_000, "R": 10_000}, room_total=1e9, cash=10_000, race_cap=5_000)
    takes = [o for o in r["orders"] if o["kind"] == "take"]
    assert {(o["leg"], o["side"]) for o in takes} == {("D", "buy"), ("R", "sell")}
    assert sum(o["qty"] for o in r["orders"] if (o["leg"], o["side"]) in (("D", "buy"), ("R", "sell"))) <= 5_000


def test_risk_reducing_orders_stop_at_zero_exposure():
    pin(); config.B_SKEW = 0.30
    r = strategy_b.decide(BOOKS, P, {"D": 8_000, "R": 10_000}, room_total=1e9, cash=1e6, race_cap=5_000)
    lowering = sum(o["qty"] for o in r["orders"] if (o["leg"], o["side"]) in (("D", "buy"), ("R", "sell")))
    assert lowering <= 2_000


def test_no_skew_without_exposure():
    pin(); config.B_SKEW = 0.05
    r = strategy_b.decide(BOOKS, P, {"D": 1_000, "R": 1_000}, room_total=1e9, race_cap=5_000)
    assert r["reservation"] == {"D": round(1 - 0.986, 4), "R": 0.986}


def test_quote_behind_the_touch_does_not_starve_a_quote_at_the_touch():
    # Minnesota Governor, live 2026-10-04 08:2x: the NO_R ask (0.965, behind the 0.88 best ask) used up
    # the risk-reduction room, so a buy-back bid that could rest at the touch was dropped
    pin(); config.B_SKEW = 0.02
    books = {"D": {"bids": [(0.035, 500)], "asks": [(0.09, 2008)]},
             "R": {"bids": [(0.875, 2241)], "asks": [(0.88, 4124)]}}
    r = strategy_b.decide(books, {"D": 0.9515, "R": 0.0485}, {"D": 8_423, "R": 9_832}, room_total=1e9,
                          cash=1_000, race_cap=2_916)
    assert not orders(r, leg="R", side="sell", kind="quote")                  # behind the touch: not produced
    assert orders(r, leg="D", side="buy", kind="quote")                       # the buy-back at the best bid survives


def test_null_case_fair_prices_no_takes():
    # SUSQ priced at Kalshi fair: nothing is far enough from fair to take
    pin()
    books = {"D": {"bids": [(0.010, 5000)], "asks": [(0.020, 5000)]},
             "R": {"bids": [(0.980, 5000)], "asks": [(0.990, 5000)]}}
    r = strategy_b.decide(books, P, {"D": 1_000, "R": 1_000}, room_total=1e9)
    assert not orders(r, kind="take")


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
