"""Tests for arb_math with hand-computed answers. Run: python -m pytest -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arb_math import ceil_to_tick, no_asks_from_yes_bids, walk_baskets  # noqa: E402


def test_no_asks_are_complement_of_yes_bids():
    bids = [{"price": 0.895, "quantity": 300}, {"price": 0.89, "quantity": 500}]
    assert no_asks_from_yes_bids(bids) == [(0.105, 300), (0.11, 500)]


def test_planted_edge_walks_two_levels():
    # leg A: 100 @ 0.885, 200 @ 0.89 ; leg B: 150 @ 0.105, 1000 @ 0.11
    a = [(0.885, 100), (0.89, 200)]
    b = [(0.105, 150), (0.11, 1000)]
    r = walk_baskets([a, b], min_payout=1.0, min_edge=0.005)
    # step1 100 @ 0.990 ; step2 50 @ 0.995 (0.89+0.105) ; next 0.89+0.11 = 1.000 > 0.995 -> stop
    assert r["quantity"] == 150
    assert abs(r["total_cost"] - (100 * 0.990 + 50 * 0.995)) < 1e-9
    assert abs(r["locked_profit_min"] - (150 - 148.75)) < 1e-9
    assert r["worst_prices"] == [0.89, 0.105]


def test_null_case_no_edge_gives_zero():
    r = walk_baskets([[(0.89, 1000)], [(0.11, 1000)]], 1.0, 0.005)
    assert r["quantity"] == 0 and r["total_cost"] == 0


def test_zero_edge_allowed_when_min_edge_zero():
    r = walk_baskets([[(0.89, 1000)], [(0.11, 400)]], 1.0, 0.0)
    assert r["quantity"] == 400 and r["locked_profit_min"] == 0


def test_caps():
    legs = [[(0.4, 1000)], [(0.5, 1000)]]
    assert walk_baskets(legs, 1.0, 0.005, max_baskets=10)["quantity"] == 10
    assert walk_baskets(legs, 1.0, 0.005, max_cost=90.5)["quantity"] == 100  # floor(90.5/0.9)


def test_fractional_book_quantity_floors():
    r = walk_baskets([[(0.4, 10.7)], [(0.5, 20)]], 1.0, 0.005)
    assert r["quantity"] == 10


def test_three_leg_exhaustive_set():
    # buy YES on 3 mutually exclusive, exhaustive outcomes: payout exactly 1
    r = walk_baskets([[(0.3, 50)], [(0.3, 50)], [(0.35, 20)]], 1.0, 0.005)
    assert r["quantity"] == 20


def test_ceil_to_tick():
    assert ceil_to_tick(0.889, 0.005) == 0.89
    assert ceil_to_tick(0.105, 0.005) == 0.105
    assert ceil_to_tick(0.1051, 0.005) == 0.11


from arb_math import walk_exit  # noqa: E402


def test_exit_walk_planted():
    # NO bids D: 0.13 x 100, 0.125 x 500 ; R: 0.875 x 300, 0.87 x 1000
    d = [(0.13, 100), (0.125, 500)]
    r = [(0.875, 300), (0.87, 1000)]
    out = walk_exit([d, r], min_sum=1.0)
    # 100 @ 1.005 ; 200 @ 0.125+0.875 = 1.000 ; next 0.125+0.87 = 0.995 < 1 stop
    assert out["quantity"] == 300
    assert abs(out["proceeds"] - (100 * 1.005 + 200 * 1.0)) < 1e-9
    assert out["worst_prices"] == [0.125, 0.875]


def test_exit_walk_null_and_cap():
    assert walk_exit([[(0.11, 1000)], [(0.87, 1000)]], 1.0)["quantity"] == 0      # 0.98 < 1
    assert walk_exit([[(0.13, 1000)], [(0.87, 1000)]], 1.0, max_baskets=7)["quantity"] == 7


from arb_math import fill_price, widen_limits  # noqa: E402


def test_widen_limits_buy_and_sell():
    assert widen_limits([0.105, 0.885], 0.995, 0.005, up=True) == [0.11, 0.885]
    assert widen_limits([0.105, 0.89], 0.995, 0.005, up=True) == [0.105, 0.89]       # no slack left
    assert widen_limits([0.5, 0.48], 0.995, 0.005, up=True) == [0.51, 0.485]
    assert widen_limits([0.13, 0.875], 1.0, 0.005, up=False) == [0.125, 0.875]
    assert widen_limits([0.99, 0.0], 1.5, 0.005, up=True)[0] == 0.995                # leg capped at 0.995


def test_fill_price():
    lad = [(0.1, 5), (0.11, 5), (0.12, 100)]
    assert fill_price(lad, 5) == 0.1 and fill_price(lad, 6) == 0.11 and fill_price(lad, 200) is None
