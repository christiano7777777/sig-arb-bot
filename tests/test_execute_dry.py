"""Offline tests of the multi-race executor against a fake API (no network).
Run: python tests/test_execute_dry.py"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("SUSQ_API_KEY", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
import execute  # noqa: E402

TID = "t-1"
SLUG = "midterm-elections"
NO_EDGE = {"D": ([(0.875, 100000)], [(0.99, 5)]), "R": ([(0.12, 100000)], [(0.99, 5)])}  # NO asks 1.005


class FakeClient:
    """Serves canned markets / books / positions. Any POST is a test failure (dry run)."""

    def __init__(self, races, held=None, balance=98_996.55):
        # races: name -> {party letter: (yes_bids, yes_asks)} ; held: exchangeId -> (signed qty, cost)
        self.markets, self.books, self.held, self.balance = [], {}, held or {}, balance
        self.gets, self.posts = [], []
        mid = 100
        party = {"D": "Democratic", "R": "Republican", "I": "Independent"}
        for race, legs in races.items():
            for p, (bids, asks) in legs.items():
                mid += 1
                ex = str(mid + 1000)
                self.markets.append({"id": str(mid), "title": f"Will the {party[p]} Party win the {race}?",
                                     "status": "open", "exchanges": [{"id": ex}]})
                self.books[ex] = (bids, asks)

    def ex_of(self, race, party_letter):
        party = {"D": "Democratic", "R": "Republican"}[party_letter]
        title = f"Will the {party} Party win the {race}?"
        return next(m["exchanges"][0]["id"] for m in self.markets if m["title"] == title)

    def get(self, path, **params):
        self.gets.append(path)
        if path == f"/tournaments/{SLUG}":
            return {"id": TID, "slug": SLUG, "myBalance": self.balance}
        if path == f"/tournaments/{SLUG}/markets":
            return {"data": self.markets, "pagination": {"hasMore": False, "nextCursor": None}}
        if path == "/exchanges/prices":
            data = []
            for ex in params["ids"].split(","):
                bids, asks = self.books[ex]
                data.append({"exchangeId": ex, "bestBid": max(p for p, _ in bids) if bids else None,
                             "bestAsk": min(p for p, _ in asks) if asks else None})
            return {"data": data, "missingIds": []}
        if path.startswith("/exchanges/") and path.endswith("/orderbook"):
            bids, asks = self.books[path.split("/")[2]]
            return {"bids": [{"price": p, "quantity": q} for p, q in sorted(bids, reverse=True)],
                    "asks": [{"price": p, "quantity": q} for p, q in sorted(asks)]}
        if path == f"/tournaments/{SLUG}/portfolio/positions":
            return {"positions": [{"exchangeId": ex, "quantity": q, "costBasis": c, "settled": False}
                                  for ex, (q, c) in self.held.items()]}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, body):
        self.posts.append((path, body))
        raise AssertionError("dry run must never POST")


def make_runner(fake, min_edge=0.005, exposure=None, capital=50_000, reserve=50_000, race_cap=5_000,
                denylist=(), extra=False, b_enabled=False, maker=False, d_enabled=False, a_cap=None):
    # pin every setting the tests rely on, so editing config.py cannot silently change a test
    config.MIN_EDGE, config.MAX_UNHEDGED_EXPOSURE = min_edge, exposure
    config.MAX_CAPITAL_PER_RUN, config.RESERVE, config.PER_RACE_CAP = capital, reserve, race_cap
    config.EXIT_ENABLED, config.EXIT_MIN_SUM, config.TOURNAMENT_SLUG = True, 1.0, SLUG
    config.RACE_DENYLIST = set(denylist)
    config.EXTRA_CAPITAL_ENABLED, config.EXTRA_MIN_EDGE, config.HARD_RESERVE = extra, 0.015, 1_000
    config.ROTATE_MAX_SPEND = 2_000
    config.B_ENABLED = b_enabled
    config.REALTIME_ENABLED = False                 # feed tests attach a fake feed explicitly
    config.D_ENABLED = d_enabled
    config.A_CAPITAL_CAP = a_cap
    config.C_LIMIT, config.C_SKEW, config.C_SKEW_MAX, config.C_QUOTE_EDGE, config.C_CLIP = 2_000, 0.10, 0.25, 0.02, 500
    config.C_EXTRA_RACES = 5
    config.POSITIONS_REFRESH_S, config.BULK_REFRESH_S = 30, 30
    config.MAKER_ENABLED = maker
    config.MAKER_RACES, config.MAKER_MIN_PAIRS, config.MAKER_CLIP = 6, 500, 500
    config.MAKER_LIFE_S, config.MAKER_MAX_ORDERS, config.ROTATE_MIN_GAIN = 300, 8, 0.001
    execute.STATE_DIR = Path(__file__).parent / "_state_test"
    execute.STOP_FILE = execute.STATE_DIR / "STOP"
    execute.STOP_FILE.unlink(missing_ok=True)          # a halt in an earlier test must not leak
    r = execute.Runner(fake, live=False, max_baskets=None)
    r.sent = []

    def capture(path, body, label):
        # record the order and move the fake cash as if it filled at the limit prices
        r.sent.append((path, body))
        legs = body.get("legs", [body])
        amount = sum(l["quantity"] * l["price"] for l in legs)
        fake.balance += amount if legs[0]["action"] == "sell" else -amount
    r.order = capture
    return r


def basket(r, name):
    return next(b for b in r.baskets if b.name == name)


# ---- discovery --------------------------------------------------------------
def test_discovery_two_party_only_and_denylist():
    races = {"Kansas Senate": NO_EDGE, "Iowa Senate": NO_EDGE,
             "Montana Senate": {**NO_EDGE, "I": ([(0.02, 10)], [(0.03, 10)])}}
    r = make_runner(FakeClient(races), denylist={"Iowa Senate"})
    assert [b.name for b in r.baskets] == ["Kansas Senate"]
    assert [l["party"] for l in r.baskets[0].legs] == ["D", "R"]


# ---- entries ----------------------------------------------------------------
def test_planted_entry_builds_correct_multileg():
    # NO_D asks 0.105 x 500 ; NO_R asks 0.885 x 300, 0.89 x 1000
    races = {"Kansas Senate": {"D": ([(0.895, 500)], [(0.99, 5)]), "R": ([(0.115, 300), (0.11, 1000)], [(0.99, 5)])}}
    fake = FakeClient(races)
    r = make_runner(fake)
    r.poll()
    path, body = r.sent[0]
    assert path == "/orders/multi-leg" and fake.posts == []
    # 300 @ 0.990, then 200 @ 0.105+0.890 = 0.995 (edge 0.005 ok), then D level used up
    assert [l["quantity"] for l in body["legs"]] == [500, 500]
    assert [l["price"] for l in body["legs"]] == [0.105, 0.89]
    assert [l["exchangeId"] for l in body["legs"]] == [fake.ex_of("Kansas Senate", "D"), fake.ex_of("Kansas Senate", "R")]
    assert all(l["side"] == "no" and l["action"] == "buy" and l["tournamentId"] == TID for l in body["legs"])


def test_no_edge_sends_nothing_and_reads_no_books():
    fake = FakeClient({"Kansas Senate": NO_EDGE, "Iowa Senate": NO_EDGE})
    r = make_runner(fake)
    r.poll()
    assert r.sent == [] and not any(g.endswith("/orderbook") for g in fake.gets)


def test_only_signalled_race_gets_books_read():
    races = {"Kansas Senate": {"D": ([(0.9, 100)], [(0.99, 5)]), "R": ([(0.12, 100)], [(0.99, 5)])},
             "Iowa Senate": NO_EDGE}
    fake = FakeClient(races)
    r = make_runner(fake)
    r.poll()
    read = {g.split("/")[2] for g in fake.gets if g.endswith("/orderbook")}
    assert read == {fake.ex_of("Kansas Senate", "D"), fake.ex_of("Kansas Senate", "R")}
    assert len(r.sent) == 1


def test_exposure_cap_limits_size():
    races = {"Kansas Senate": {"D": ([(0.895, 100000)], [(0.99, 5)]), "R": ([(0.115, 100000)], [(0.99, 5)])}}
    r = make_runner(FakeClient(races), exposure=500)
    r.poll()
    assert [l["quantity"] for l in r.sent[0][1]["legs"]] == [564, 564]     # floor(500 / 0.885)


def test_reserve_limits_spend():
    races = {"Kansas Senate": {"D": ([(0.895, 100000)], [(0.99, 5)]), "R": ([(0.115, 100000)], [(0.99, 5)])}}
    r = make_runner(FakeClient(races), reserve=98_900)
    r.poll()
    assert [l["quantity"] for l in r.sent[0][1]["legs"]] == [97, 97]       # 96.55 / 0.99


def test_per_race_cap():
    races = {"Kansas Senate": {"D": ([(0.895, 100000)], [(0.99, 5)]), "R": ([(0.115, 100000)], [(0.99, 5)])}}
    fake = FakeClient(races)
    fake.held = {fake.ex_of("Kansas Senate", "D"): (-4000, 440.0), fake.ex_of("Kansas Senate", "R"): (-4000, 4460.0)}
    r = make_runner(fake)
    r.poll()
    assert [l["quantity"] for l in r.sent[0][1]["legs"]] == [100, 100]     # (5000 - 4900) / 0.995 worst-case limits


def test_skip_when_holding_yes():
    races = {"Kansas Senate": {"D": ([(0.895, 1000)], [(0.99, 5)]), "R": ([(0.115, 1000)], [(0.99, 5)])}}
    fake = FakeClient(races)
    fake.held = {fake.ex_of("Kansas Senate", "D"): (50, 5.0)}
    r = make_runner(fake)
    r.poll()
    assert r.sent == []


def test_idempotency_keys_unique():
    races = {"Kansas Senate": {"D": ([(0.9, 100)], [(0.99, 5)]), "R": ([(0.12, 100)], [(0.99, 5)])},
             "Iowa Senate": {"D": ([(0.9, 100)], [(0.99, 5)]), "R": ([(0.12, 100)], [(0.99, 5)])}}
    r = make_runner(FakeClient(races))
    r.poll(); r.poll()
    keys = [b["idempotencyKey"] for _, b in r.sent]
    assert len(keys) == 4 and len(set(keys)) == 4


# ---- exits ------------------------------------------------------------------
def held_pairs(fake, race, n):
    return {fake.ex_of(race, "D"): (-n, 0.11 * n), fake.ex_of(race, "R"): (-n, 0.88 * n)}


def test_exit_planted_sells_pairs():
    # YES asks D 0.87, R 0.125 -> NO bids 0.13 + 0.875 = 1.005
    races = {"Kansas Senate": {"D": ([(0.875, 10)], [(0.87, 300)]), "R": ([(0.12, 10)], [(0.125, 1000)])}}
    fake = FakeClient(races)
    fake.held = held_pairs(fake, "Kansas Senate", 1437)
    r = make_runner(fake, exposure=500)
    r.poll()
    path, body = r.sent[0]
    assert all(l["action"] == "sell" and l["side"] == "no" for l in body["legs"])
    assert [l["price"] for l in body["legs"]] == [0.125, 0.875]     # 1.005 widened down to the 1.000 floor
    assert [l["quantity"] for l in body["legs"]] == [300, 300]


def test_exit_at_exactly_one():
    races = {"Kansas Senate": {"D": ([(0.875, 10)], [(0.875, 50)]), "R": ([(0.12, 10)], [(0.125, 50)])}}
    fake = FakeClient(races)
    fake.held = held_pairs(fake, "Kansas Senate", 1437)
    r = make_runner(fake)
    r.poll()
    assert [l["quantity"] for l in r.sent[0][1]["legs"]] == [50, 50]


def test_exit_never_sells_more_than_held():
    races = {"Kansas Senate": {"D": ([(0.875, 10)], [(0.87, 5000)]), "R": ([(0.12, 10)], [(0.125, 5000)])}}
    fake = FakeClient(races)
    fake.held = {fake.ex_of("Kansas Senate", "D"): (-40, 4.4), fake.ex_of("Kansas Senate", "R"): (-40, 35.2)}
    r = make_runner(fake)
    r.poll()
    assert [l["quantity"] for l in r.sent[0][1]["legs"]] == [40, 40]


def test_hold_when_exit_below_one():
    races = {"Kansas Senate": {"D": ([(0.875, 10)], [(0.89, 5000)]), "R": ([(0.12, 10)], [(0.13, 5000)])}}
    fake = FakeClient(races)
    fake.held = held_pairs(fake, "Kansas Senate", 1437)
    r = make_runner(fake)
    r.poll()
    assert r.sent == []


def test_no_exit_without_holdings():
    races = {"Kansas Senate": {"D": ([(0.875, 10)], [(0.87, 5000)]), "R": ([(0.12, 10)], [(0.125, 5000)])}}
    r = make_runner(FakeClient(races))
    r.poll()
    assert r.sent == []


# ---- unequal legs ---------------------------------------------------------------
def _fix_first_order(races, held, action, limits, target):
    fake = FakeClient(races)
    fake.held = {fake.ex_of("Kansas Senate", "D"): (-held[0], 0), fake.ex_of("Kansas Senate", "R"): (-held[1], 0)}
    r = make_runner(fake)
    basket(r, "Kansas Senate").fix_imbalance(list(held), action, limits, target)
    return fake, r.sent


def test_fix_after_buy_picks_cheaper_reform():
    # 10 extra NO-D bought at 0.12. Buy NO-R at 0.885 -> pair 1.005 (-0.005/sh); sell NO-D at 0.10 (-0.02/sh)
    races = {"Kansas Senate": {"D": ([(0.85, 50)], [(0.9, 50)]), "R": ([(0.115, 50)], [(0.99, 5)])}}
    fake, sent = _fix_first_order(races, (110, 100), "buy", [0.12, 0.88], 1.0)
    o = sent[0][1]
    assert (o["exchangeId"], o["action"], o["quantity"], o["price"]) == (fake.ex_of("Kansas Senate", "R"), "buy", 10, 0.885)


def test_fix_after_buy_picks_cheaper_unwind():
    # 10 extra NO-D bought at 0.12. Buy NO-R at 0.92 (-0.04/sh); sell NO-D at 0.115 (-0.005/sh)
    races = {"Kansas Senate": {"D": ([(0.85, 50)], [(0.885, 50)]), "R": ([(0.08, 50)], [(0.99, 5)])}}
    fake, sent = _fix_first_order(races, (110, 100), "buy", [0.12, 0.88], 1.0)
    o = sent[0][1]
    assert (o["exchangeId"], o["action"], o["quantity"], o["price"]) == (fake.ex_of("Kansas Senate", "D"), "sell", 10, 0.115)


def test_fix_after_sell_picks_cheaper():
    # sold NO-R at 0.875 but NO-D (extra, 10) did not sell; target 1.0.
    # buy back NO-R at 0.88 (-0.005/sh) vs sell NO-D at 0.11 -> 0.985 (-0.015/sh)
    races = {"Kansas Senate": {"D": ([(0.85, 50)], [(0.89, 50)]), "R": ([(0.12, 50)], [(0.99, 5)])}}
    fake, sent = _fix_first_order(races, (110, 100), "sell", [0.125, 0.875], 1.0)
    o = sent[0][1]
    assert (o["exchangeId"], o["action"], o["quantity"], o["price"]) == (fake.ex_of("Kansas Senate", "R"), "buy", 10, 0.88)


def test_fix_halts_when_book_cannot_absorb():
    races = {"Kansas Senate": {"D": ([(0.85, 50)], [(0.9, 3)]), "R": ([(0.115, 3)], [(0.99, 5)])}}
    fake = FakeClient(races)
    fake.held = {fake.ex_of("Kansas Senate", "D"): (-110, 0), fake.ex_of("Kansas Senate", "R"): (-100, 0)}
    r = make_runner(fake)
    try:
        basket(r, "Kansas Senate").fix_imbalance([110, 100], "buy", [0.12, 0.88], 1.0)
        assert False, "should halt"
    except execute.Halt:
        pass
    assert r.sent == []


# ---- priority and rotation ---------------------------------------------------
DEEP = 100000
EDGE_02 = {"D": ([(0.5, DEEP)], [(0.99, 5)]), "R": ([(0.52, DEEP)], [(0.99, 5)])}    # NO asks 0.5+0.48 = 0.98
EDGE_01 = {"D": ([(0.5, DEEP)], [(0.99, 5)]), "R": ([(0.51, DEEP)], [(0.99, 5)])}    # 0.5+0.49 = 0.99
EDGE_005 = {"D": ([(0.5, DEEP)], [(0.99, 5)]), "R": ([(0.505, DEEP)], [(0.99, 5)])}  # 0.5+0.495 = 0.995
# held race whose NO bids sum to 0.995: YES asks 0.5 and 0.505 -> NO bids 0.5 + 0.495
SELLER_0995 = {"D": ([(0.4, 5)], [(0.5, DEEP)]), "R": ([(0.4, 5)], [(0.505, DEEP)])}
SELLER_0990 = {"D": ([(0.4, 5)], [(0.5, DEEP)]), "R": ([(0.4, 5)], [(0.51, DEEP)])}


def legs_of(body):
    return [(l["exchangeId"], l["action"], l["quantity"], l["price"]) for l in body["legs"]]


def test_higher_edge_gets_capital_first():
    fake = FakeClient({"Aaa race": EDGE_01, "Zzz race": EDGE_02}, balance=50_000 + 600)
    r = make_runner(fake)
    r.poll()
    buys = [b for _, b in r.sent if b["legs"][0]["action"] == "buy"]
    assert buys[0]["legs"][0]["exchangeId"] == fake.ex_of("Zzz race", "D")      # edge 0.02 first
    assert len(buys) == 1                                                       # 0.01 race: out of budget


def test_rotation_sells_0995_to_fund_edge_01():
    fake = FakeClient({"Held race": SELLER_0995, "New race": EDGE_01}, balance=50_000.5)
    fake.held = {fake.ex_of("Held race", "D"): (-5000, 2500.0), fake.ex_of("Held race", "R"): (-5000, 2450.0)}
    r = make_runner(fake, exposure=500)                                         # avg cost 0.99 / pair
    r.poll()
    assert len(r.sent) == 2
    (p1, sell), (p2, buy) = r.sent                                              # sell first, then buy
    assert [x[1] for x in legs_of(sell)] == ["sell", "sell"]
    assert [x[0] for x in legs_of(sell)] == [fake.ex_of("Held race", "D"), fake.ex_of("Held race", "R")]
    assert [x[3] for x in legs_of(sell)] == [0.5, 0.495]
    assert [x[1] for x in legs_of(buy)] == ["buy", "buy"]
    assert [x[0] for x in legs_of(buy)] == [fake.ex_of("New race", "D"), fake.ex_of("New race", "R")]
    assert sum(x[3] for x in legs_of(buy)) <= 0.99 + 1e-9                       # bought at edge >= 0.01


def test_no_rotation_for_edge_below_001():
    fake = FakeClient({"Held race": SELLER_0995, "New race": EDGE_005}, balance=50_000.5)
    fake.held = {fake.ex_of("Held race", "D"): (-5000, 2500.0), fake.ex_of("Held race", "R"): (-5000, 2450.0)}
    r = make_runner(fake)
    r.poll()
    assert r.sent == []


def test_no_rotation_when_seller_below_0995():
    fake = FakeClient({"Held race": SELLER_0990, "New race": EDGE_01}, balance=50_000.5)
    fake.held = {fake.ex_of("Held race", "D"): (-5000, 2500.0), fake.ex_of("Held race", "R"): (-5000, 2450.0)}
    r = make_runner(fake)
    r.poll()
    assert r.sent == []


def test_rotation_big_edge_justifies_selling_below_0995():
    # new pair 0.98 (edge 0.02); held pair bids 0.985 -> gain 0.005 -> swap
    seller = {"D": ([(0.4, 5)], [(0.5, DEEP)]), "R": ([(0.4, 5)], [(0.515, DEEP)])}    # NO bids 0.5 + 0.485
    fake = FakeClient({"Held race": seller, "New race": EDGE_02}, balance=50_000.5)
    fake.held = {fake.ex_of("Held race", "D"): (-5000, 2450.0), fake.ex_of("Held race", "R"): (-5000, 2450.0)}
    r = make_runner(fake, exposure=500)                                         # avg cost 0.98
    r.poll()
    actions = [b["legs"][0]["action"] for _, b in r.sent]
    assert actions == ["sell", "buy"]
    assert sum(x[3] for x in legs_of(r.sent[1][1])) <= 0.985 - 0.005 + 1e-9


def test_swap_buys_only_what_the_sellers_can_absorb():
    # seller only has 20 pairs bid at 0.995 -> buy 20 new pairs first, then sell those 20
    seller = {"D": ([(0.4, 5)], [(0.5, 20)]), "R": ([(0.4, 5)], [(0.505, 20)])}
    fake = FakeClient({"Held race": seller, "New race": EDGE_01}, balance=50_000.5)
    fake.held = {fake.ex_of("Held race", "D"): (-5000, 2500.0), fake.ex_of("Held race", "R"): (-5000, 2450.0)}
    r = make_runner(fake, exposure=500)
    r.poll()
    assert [b["legs"][0]["action"] for _, b in r.sent] == ["sell", "buy"]
    assert r.sent[0][1]["legs"][0]["quantity"] == 20
    assert r.sent[1][1]["legs"][0]["quantity"] == 20


def test_rotation_skips_when_gain_too_small():
    # new pair 0.99 (edge 0.01); held pair bids 0.99 -> gain 0 -> no swap
    fake = FakeClient({"Held race": SELLER_0990, "New race": EDGE_01}, balance=50_000.5)
    fake.held = {fake.ex_of("Held race", "D"): (-5000, 2400.0), fake.ex_of("Held race", "R"): (-5000, 2400.0)}
    r = make_runner(fake)
    r.poll()
    assert r.sent == []


def test_rotation_may_sell_below_cost():
    # held pair cost 0.998 > its 0.995 bid, but swapping into a 0.98 pair still nets >= 0.005
    fake = FakeClient({"Held race": SELLER_0995, "New race": EDGE_02}, balance=50_000.5)
    fake.held = {fake.ex_of("Held race", "D"): (-5000, 2500.0), fake.ex_of("Held race", "R"): (-5000, 2490.0)}
    r = make_runner(fake, exposure=500)
    r.poll()
    assert [b["legs"][0]["action"] for _, b in r.sent] == ["sell", "buy"]


def test_plain_exit_never_below_cost():
    # NO bids sum 1.000 but the pairs cost 1.002 each -> hold
    races = {"Kansas Senate": {"D": ([(0.875, 10)], [(0.875, 50)]), "R": ([(0.12, 10)], [(0.125, 50)])}}
    fake = FakeClient(races)
    fake.held = {fake.ex_of("Kansas Senate", "D"): (-100, 12.2), fake.ex_of("Kansas Senate", "R"): (-100, 88.0)}
    r = make_runner(fake)
    r.poll()
    assert r.sent == []


def test_exit_first_and_entries_capped_per_poll():
    # 8 races with edges plus 1 held race at S = 1.000: the exit goes first, then only 4 entries
    races = {f"Race {i}": {"D": ([(0.9, 100)], [(0.99, 5)]), "R": ([(0.12, 100)], [(0.99, 5)])} for i in range(8)}
    races["Held race"] = {"D": ([(0.875, 10)], [(0.875, 50)]), "R": ([(0.12, 10)], [(0.125, 50)])}
    fake = FakeClient(races)
    fake.held = held_pairs(fake, "Held race", 100)
    r = make_runner(fake)
    config.MAX_ENTRIES_PER_POLL = 4
    r.poll()
    actions = [b["legs"][0]["action"] for _, b in r.sent]
    assert actions == ["sell"] + ["buy"] * 4


def test_swap_sizes_new_pair_to_what_the_seller_can_fund():
    # new race: 100 pairs at 0.97, then deep liquidity at 0.995. Held race bids 0.990.
    # The swap must buy only the 0.97 level (<= 0.990 - 0.001), not walk to 0.995 and find no seller.
    new = {"D": ([(0.5, 100), (0.475, DEEP)], [(0.99, 5)]), "R": ([(0.53, DEEP)], [(0.99, 5)])}
    fake = FakeClient({"Held race": SELLER_0990, "New race": new}, balance=50_000.5)
    fake.held = {fake.ex_of("Held race", "D"): (-5000, 2450.0), fake.ex_of("Held race", "R"): (-5000, 2450.0)}
    r = make_runner(fake, exposure=500)
    r.poll()
    assert [b["legs"][0]["action"] for _, b in r.sent] == ["sell", "buy"]
    buy = r.sent[1][1]
    assert buy["legs"][0]["quantity"] == 100                      # only the 0.97 level
    assert sum(l["price"] for l in buy["legs"]) <= 0.990 - 0.001 + 1e-9


def _swap_setup(balance=50_000.5):
    fake = FakeClient({"Held race": SELLER_0995, "New race": EDGE_01}, balance=balance)
    fake.held = {fake.ex_of("Held race", "D"): (-5000, 2500.0), fake.ex_of("Held race", "R"): (-5000, 2450.0)}
    return fake, make_runner(fake, exposure=500)


def _fill_buys_with(r, filled):
    """Pretend every pair buy fills only `filled` pairs (sales fill in full)."""
    for b in r.baskets:
        real = b.send_pair
        def fake_send(action, q, limits, before, exit_target=None, real=real):
            got = real(action, q, limits, before, exit_target)
            return filled if action == "buy" else got
        b.send_pair = fake_send


def test_swap_sells_first_even_with_plenty_of_cash():
    fake, r = _swap_setup(balance=1_000 + 40_000)     # below the 50k reserve (a swap), lots of cash (old: buy first)
    r.poll()
    assert [b["legs"][0]["action"] for _, b in r.sent] == ["sell", "buy"]


def test_swap_never_buys_more_than_it_sold():
    fake, r = _swap_setup()
    for b in r.baskets:                                       # the sale fills only 7 pairs
        real = b.send_pair
        b.send_pair = lambda action, q, limits, before, exit_target=None, real=real:             (real(action, q, limits, before, exit_target) and 7) if action == "sell" else real(action, q, limits, before, exit_target)
    r.poll()
    assert [b["legs"][0]["action"] for _, b in r.sent] == ["sell", "buy"]
    assert r.sent[1][1]["legs"][0]["quantity"] <= 7


def test_every_swap_releases_cash():
    fake, r = _swap_setup(balance=1_000 + 300)
    before = fake.balance
    r.poll()
    assert [b["legs"][0]["action"] for _, b in r.sent] == ["sell", "buy"]
    assert fake.balance > before                              # proceeds > cost (fills at the limits)


def test_swap_sells_first_when_cash_cannot_cover_it():
    # 300 cash above the hard reserve, swap worth ~990 -> sell first, then buy with the proceeds
    fake, r = _swap_setup(balance=1_000 + 300)
    r.poll()
    assert [b["legs"][0]["action"] for _, b in r.sent] == ["sell", "buy"]
    sell, buy = r.sent[0][1], r.sent[1][1]
    assert buy["legs"][0]["quantity"] <= sell["legs"][0]["quantity"]               # never buys more than sold
    assert sum(l["price"] for l in buy["legs"]) <= sum(l["price"] for l in sell["legs"]) - 0.001 + 1e-9


def test_swap_sells_first_with_no_cash_at_all():
    fake, r = _swap_setup(balance=1_000)
    r.poll()
    assert [b["legs"][0]["action"] for _, b in r.sent] == ["sell", "buy"]


def test_sell_first_swap_buys_even_when_the_sale_frees_under_50():
    # live bug 2026-10-04: a sale freeing < 50 cash never bought (the 50 trigger still applied)
    seller = {"D": ([(0.4, 5)], [(0.5, 20)]), "R": ([(0.4, 5)], [(0.505, 20)])}       # 20 pairs bid 0.995
    fake = FakeClient({"Held race": seller, "New race": EDGE_01}, balance=1_000)
    fake.held = {fake.ex_of("Held race", "D"): (-5000, 2500.0), fake.ex_of("Held race", "R"): (-5000, 2450.0)}
    r = make_runner(fake, exposure=500)
    r.poll()
    assert [b["legs"][0]["action"] for _, b in r.sent] == ["sell", "buy"]
    assert r.sent[1][1]["legs"][0]["quantity"] == 20


def test_out_of_budget_poll_reads_no_books():
    # out of cash, many entry signals, no held race can fund a swap -> only the cheap reads
    races = {f"Race {i}": EDGE_01 for i in range(6)}
    races["Held race"] = SELLER_0990                        # bids 0.990: cannot fund a 0.990 pair
    fake = FakeClient(races, balance=50_000.5)
    fake.held = held_pairs(fake, "Held race", 100)
    r = make_runner(fake)
    fake.gets.clear()
    r.poll()
    assert r.sent == []
    assert not any(g.endswith("/orderbook") for g in fake.gets), fake.gets
    assert len(fake.gets) <= 3, fake.gets                  # quotes + positions + one balance read


# ---- option B: extra capital only for edge >= 0.015 ---------------------------
EDGE_02_DEEP = {"D": ([(0.5, DEEP)], [(0.99, 5)]), "R": ([(0.52, 300), (0.51, DEEP)], [(0.99, 5)])}  # 0.98 x300, then 0.99


def test_extra_tier_buys_only_edge_0015_levels():
    # core cash used up (0.5 above 50k); 0.02-edge level (300 pairs) may use the extra tier, the 0.01 level may not
    fake = FakeClient({"Big edge": EDGE_02_DEEP}, balance=50_000.5)
    r = make_runner(fake, extra=True)
    r.poll()
    assert len(r.sent) == 1
    legs = r.sent[0][1]["legs"]
    assert legs[0]["quantity"] == 300
    assert sum(l["price"] for l in legs) <= 1 - 0.015 + 1e-9


def test_extra_tier_not_for_small_edges():
    fake = FakeClient({"Small edge": EDGE_01}, balance=50_000.5)
    r = make_runner(fake, extra=True)
    r.poll()
    assert r.sent == []


def test_extra_tier_respects_hard_reserve():
    fake = FakeClient({"Big edge": EDGE_02}, balance=1_500)       # 500 above the hard reserve
    r = make_runner(fake, extra=True)
    r.poll()
    legs = r.sent[0][1]["legs"]
    assert legs[0]["quantity"] * sum(l["price"] for l in legs) <= 500 + 1e-6


def test_extra_tier_disabled_means_drain():
    fake = FakeClient({"Big edge": EDGE_02}, balance=50_000.5)
    r = make_runner(fake, extra=False)
    r.poll()
    assert r.sent == []


def test_exits_run_best_price_first():
    races = {"Aaa race": {"D": ([(0.875, 10)], [(0.875, 50)]), "R": ([(0.12, 10)], [(0.125, 50)])},   # S = 1.000
             "Zzz race": {"D": ([(0.875, 10)], [(0.86, 50)]), "R": ([(0.12, 10)], [(0.125, 50)])}}    # S = 1.015
    fake = FakeClient(races)
    fake.held = {**held_pairs(fake, "Aaa race", 100), **held_pairs(fake, "Zzz race", 100)}
    r = make_runner(fake)
    r.poll()
    assert r.sent[0][1]["legs"][0]["exchangeId"] == fake.ex_of("Zzz race", "D")


def test_no_per_race_cap_when_none():
    fake = FakeClient({"Kansas Senate": EDGE_02})
    fake.held = {fake.ex_of("Kansas Senate", "D"): (-9000, 4500.0), fake.ex_of("Kansas Senate", "R"): (-9000, 4320.0)}
    r = make_runner(fake, race_cap=None, exposure=500)
    r.poll()
    # walk 0.50 + 0.48 = 0.98, slack widens limits to 0.51 + 0.485 = 0.995 -> floor(500 / 0.51) = 980
    assert [l["quantity"] for l in r.sent[0][1]["legs"]] == [980, 980]


# ---- strategy B executor ------------------------------------------------------
# Delaware-like books in YES terms: NO_D bid 0.12 / ask 0.125, NO_R bid 0.84 / ask 0.845
DE = {"D": ([(0.875, 9000)], [(0.88, 9000)]), "R": ([(0.155, 9000)], [(0.16, 9000)])}


def _b_runner(held_d, held_r, balance, kalshi_ok=True):
    import kalshi
    config.B_RACES = {"Delaware Senate": {"event": "SENATEDE-26", "D": "SENATEDE-26-D", "R": "SENATEDE-26-R"}}
    config.B_MIN_FAVOURITE, config.B_TAKE_EDGE, config.B_QUOTE_EDGE = 0.95, 0.05, 0.02
    config.B_RACE_CAP, config.B_TOTAL_CAP_FRAC, config.B_KALSHI_JUMP = None, 0.10, 0.02
    config.B_MAX_ORDERS_PER_ROUND, config.B_CLOSE_REF = 10, 5_000
    kalshi.fair = lambda tickers, max_spread: (
        {"ok": True, "why": "", "p": {"D": 0.986, "R": 0.014}, "mid": {"D": 0.986, "R": 0.014}, "spread": {}}
        if kalshi_ok else {"ok": False, "why": "test"})
    fake = FakeClient({"Delaware Senate": DE}, balance=balance)
    fake.held = {fake.ex_of("Delaware Senate", "D"): (-held_d, 0.12 * held_d),
                 fake.ex_of("Delaware Senate", "R"): (-held_r, 0.84 * held_r)}
    r = make_runner(fake, exposure=500, b_enabled=True)
    return fake, r


def _b_orders(r):
    return [b for _, b in r.sent if "legs" not in b]          # B sends single-leg orders


def test_b_round_sells_rich_leg_within_cap_and_cash():
    fake, r = _b_runner(37_588, 37_588, balance=1_000 + 500)
    r.b.step(r.quotes(), r.positions())
    orders = _b_orders(r)
    ex_d, ex_r = fake.ex_of("Delaware Senate", "D"), fake.ex_of("Delaware Senate", "R")
    sells_d = [o for o in orders if o["exchangeId"] == ex_d and o["action"] == "sell"]
    assert sells_d and all(o["price"] >= 0.014 + 0.05 - 1e-9 for o in sells_d)       # >= fair + take edge
    raising = sum(o["quantity"] for o in orders if (o["exchangeId"], o["action"]) in ((ex_d, "sell"), (ex_r, "buy")))
    total_cap = 0.10 * (1_500 + 0.12 * 37_588 + 0.84 * 37_588)
    assert raising <= total_cap + 1                                                   # one race: all of the cap
    assert sum(o["quantity"] * o["price"] for o in orders if o["action"] == "buy") <= 500 + 1e-9   # cash


def test_b_quotes_never_undercut_best_ask():
    fake, r = _b_runner(37_588, 37_588, balance=1_000)
    r.b.step(r.quotes(), r.positions())
    for o in _b_orders(r):
        if o["action"] == "sell" and o["idempotencyKey"].endswith("b-quote"):
            best_ask = 1 - max(p for p, _ in fake.books[o["exchangeId"]][0])
            assert o["price"] >= best_ask - 1e-9


def test_b_no_orders_when_kalshi_untrusted():
    fake, r = _b_runner(37_588, 37_588, balance=5_000, kalshi_ok=False)
    r.b.step(r.quotes(), r.positions())
    assert _b_orders(r) == []


def test_b_round_runs_once_per_interval():
    fake, r = _b_runner(37_588, 37_588, balance=1_000)
    r.b.step(r.quotes(), r.positions())
    n = len(r.sent)
    r.b.step(r.quotes(), r.positions())                       # same minute: nothing new
    assert len(r.sent) == n


def test_b_total_cap_goes_to_the_biggest_edge_first():
    import kalshi
    # Aaa race: rich leg 0.12 vs fair 0.014 (edge ~0.1); Zzz race: rich leg 0.085 vs fair 0.048 (~0.04)
    small = {"D": ([(0.91, 9000)], [(0.915, 9000)]), "R": ([(0.12, 9000)], [(0.125, 9000)])}
    config.B_RACES = {"Zzz race": {"event": "Z", "D": "Z-D", "R": "Z-R"}, "Aaa race": {"event": "A", "D": "A-D", "R": "A-R"}}
    config.B_MIN_FAVOURITE, config.B_TAKE_EDGE, config.B_QUOTE_EDGE = 0.95, 0.05, 0.02
    config.B_RACE_CAP, config.B_KALSHI_JUMP, config.B_MAX_ORDERS_PER_ROUND = None, 0.02, 10
    probs = {"A": 0.986, "Z": 0.952}
    kalshi.fair = lambda t, m: {"ok": True, "why": "", "p": {"D": probs[t["event"]], "R": 1 - probs[t["event"]]},
                                "mid": {"D": probs[t["event"]], "R": 1 - probs[t["event"]]}, "spread": {}}
    fake = FakeClient({"Zzz race": small, "Aaa race": DE}, balance=1_000)
    fake.held = {fake.ex_of(n, x): (-20_000, 2_000.0) for n in ("Aaa race", "Zzz race") for x in "DR"}
    r = make_runner(fake, exposure=500, b_enabled=True)
    config.B_TOTAL_CAP_FRAC = 3_000 / (1_000 + 8_000)          # total cap 3,000 shares
    r.b.step(r.quotes(), r.positions())
    aaa = {fake.ex_of("Aaa race", x) for x in "DR"}
    first = _b_orders(r)[0]
    assert first["exchangeId"] in aaa                         # biggest edge served first
    assert sum(o["quantity"] for o in _b_orders(r) if o["action"] == "sell") <= 3_000


def _two_race_b(held_big, held_small, frac=0.10, max_orders=10):
    import kalshi
    config.B_RACES = {"Big race": {"event": "B", "D": "B-D", "R": "B-R"}, "Small race": {"event": "S", "D": "S-D", "R": "S-R"}}
    config.B_MIN_FAVOURITE, config.B_TAKE_EDGE, config.B_QUOTE_EDGE = 0.95, 0.05, 0.02
    config.B_RACE_CAP, config.B_KALSHI_JUMP, config.B_MAX_ORDERS_PER_ROUND = None, 0.02, max_orders
    kalshi.fair = lambda t, m: {"ok": True, "why": "", "p": {"D": 0.986, "R": 0.014}, "mid": {"D": 0.986, "R": 0.014}, "spread": {}}
    fake = FakeClient({"Big race": DE, "Small race": DE}, balance=1_000)
    fake.held = {fake.ex_of("Big race", x): (-held_big, 0.5 * held_big) for x in "DR"}
    fake.held.update({fake.ex_of("Small race", x): (-held_small, 0.5 * held_small) for x in "DR"})
    r = make_runner(fake, exposure=500, b_enabled=True)
    config.B_TOTAL_CAP_FRAC = frac
    return fake, r


def _maker_runner(held, other_race_books):
    import kalshi
    config.B_RACES = {"Delaware Senate": {"event": "SENATEDE-26", "D": "SENATEDE-26-D", "R": "SENATEDE-26-R"}}
    config.B_MIN_FAVOURITE, config.B_MAX_ORDERS_PER_ROUND, config.B_TOTAL_CAP_FRAC = 0.95, 10, 0.10
    kalshi.fair = lambda t, m: {"ok": False, "why": "test"}
    fake = FakeClient({"Delaware Senate": DE, "Other race": other_race_books}, balance=1_000)
    fake.held = {fake.ex_of("Delaware Senate", x): (-held, 0.5 * held) for x in "DR"}
    return fake, make_runner(fake, exposure=500, b_enabled=True, maker=True)


CHEAP_OTHER = {"D": ([(0.5, 9000)], [(0.51, 9000)]), "R": ([(0.535, 9000)], [(0.54, 9000)])}   # NO asks 0.5 + 0.465 = 0.965
DEAR_OTHER = {"D": ([(0.5, 9000)], [(0.51, 9000)]), "R": ([(0.53, 9000)], [(0.54, 9000)])}    # NO asks 0.5 + 0.47 = 0.97


def test_maker_quotes_both_legs_at_the_best_asks_when_kalshi_is_untrusted():
    # no Kalshi needed for pair quotes; B itself stays out. Delaware asks sum 0.97 >= other race 0.965 + 0.001
    fake, r = _maker_runner(37_588, CHEAP_OTHER)
    r.b.step(r.quotes(), r.positions())
    sells = [o for o in _b_orders(r) if o["action"] == "sell"]
    assert len(sells) == 2 and {o["quantity"] for o in sells} == {500}
    for o in sells:                                                   # exactly at the best NO ask
        assert abs(o["price"] - (1 - max(p for p, _ in fake.books[o["exchangeId"]][0]))) < 1e-9


def test_maker_favourite_leg_only_up_to_race_cap_plus_500():
    import kalshi
    fake, r = _maker_runner(37_588, CHEAP_OTHER)
    config.MAKER_OVER_CAP = 500
    kalshi.fair = lambda t, m: {"ok": True, "why": "", "p": {"D": 0.991, "R": 0.009}, "mid": {"D": 0.991, "R": 0.009}, "spread": {}}
    ex_d, ex_r = fake.ex_of("Delaware Senate", "D"), fake.ex_of("Delaware Senate", "R")
    fake.held = {ex_d: (-30_000, 3_000.0), ex_r: (-35_000, 29_000.0)}           # exposure 5,000
    config.C_LIMIT = 4_800                                                      # limit 4,800 -> maker up to 5,300
    r.b.step(r.quotes(), r.positions())
    sells = {o["exchangeId"]: o["quantity"] for o in _b_orders(r) if o["idempotencyKey"].endswith("b-maker") and o["action"] == "sell"}
    assert sells == {ex_d: 300, ex_r: 300}
    # a restart does not reset anything: same state -> same 300; 300 more filled on NO_D -> nothing
    fake.held = {ex_d: (-29_700, 2_970.0), ex_r: (-35_000, 29_030.0)}           # same cost basis
    fake.balance = 1_000                                                        # same cash -> same cap
    r.b.next_t = 0; r.sent.clear(); r.quote_live.clear(); r._cash = None
    r.b.step(r.quotes(), r.positions())
    assert not [o for o in _b_orders(r) if o["idempotencyKey"].endswith("b-maker") and o["action"] == "sell"]


def test_maker_no_favourite_asks_on_a_race_already_over_cap_plus_500():
    import kalshi
    fake, r = _maker_runner(37_588, CHEAP_OTHER)
    config.MAKER_OVER_CAP, config.B_TOTAL_CAP_FRAC = 500, 0.01                    # tiny cap, exposure 5,000
    kalshi.fair = lambda t, m: {"ok": True, "why": "", "p": {"D": 0.991, "R": 0.009}, "mid": {"D": 0.991, "R": 0.009}, "spread": {}}
    ex_d, ex_r = fake.ex_of("Delaware Senate", "D"), fake.ex_of("Delaware Senate", "R")
    fake.held = {ex_d: (-30_000, 3_000.0), ex_r: (-35_000, 29_000.0)}
    r.b.step(r.quotes(), r.positions())
    assert not [o for o in _b_orders(r) if o["idempotencyKey"].endswith("b-maker") and o["action"] == "sell"]


def test_maker_not_on_small_holdings():
    fake, r = _maker_runner(300, CHEAP_OTHER)
    r.b.step(r.quotes(), r.positions())
    assert _b_orders(r) == []


def test_maker_compares_with_other_races_not_its_own_pair():
    # only another race at 0.97 (= Delaware's own 0.97): no gain from rotating, so no pair asks
    fake, r = _maker_runner(37_588, DEAR_OTHER)
    r.b.step(r.quotes(), r.positions())
    assert not [o for o in _b_orders(r) if o["action"] == "sell"]


# ---- realtime feed -------------------------------------------------------------
def _batch(rev, prev, books=(), trades=(), resync=False):
    b = {"delivery": {"revision": rev, "previousRevision": prev}, "trades": list(trades), "bookDirty": [],
         "marketSettled": [], "books": list(books)}
    if resync:
        b["resyncRequired"] = True
    return b


def _bk(ex, seq, bids, asks, next_in=60):
    import datetime as dt
    nxt = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=next_in)).isoformat()
    return {"exchangeId": int(ex), "asOf": {"sequence": seq, "at": "2026-10-04T09:00:00.0+00:00"}, "nextExpiryAt": nxt,
            "bids": [{"price": p, "quantity": q} for p, q in bids], "asks": [{"price": p, "quantity": q} for p, q in asks]}


def _feed():
    from realtime_feed import Feed
    f = Feed(client=None, tournament_id=TID, log=lambda *a: None)
    f.healthy = True
    return f


def test_feed_applies_only_newer_books():
    f = _feed()
    f.on_market_batch("7", _batch(1, 0, [_bk(1101, 10, [(0.5, 100)], [(0.51, 100)])]))
    f.on_market_batch("7", _batch(2, 1, [_bk(1101, 9, [(0.4, 100)], [(0.41, 100)])]))     # older version
    assert f.top("1101") == (0.5, 0.51)
    f.on_market_batch("7", _batch(3, 2, [_bk(1101, 11, [(0.45, 100)], [(0.46, 100)])]))
    assert f.top("1101") == (0.45, 0.46)


def test_feed_gap_or_resync_drops_the_market():
    f = _feed()
    f.on_market_batch("7", _batch(1, 0, [_bk(1101, 10, [(0.5, 100)], [(0.51, 100)])]))
    f.on_market_batch("7", _batch(5, 3))                                                  # missed 2..3
    assert f.top("1101") is None
    f.on_market_batch("8", _batch(1, 0, [_bk(1102, 10, [(0.5, 1)], [(0.51, 1)])]))
    f.on_market_batch("8", _batch(2, 1, resync=True))
    assert f.top("1102") is None


def test_feed_fresh_only_before_next_expiry():
    f = _feed()
    f.on_market_batch("7", _batch(1, 0, [_bk(1101, 10, [(0.5, 100)], [(0.51, 100)], next_in=-1)]))
    assert f.book("1101") is not None and f.book("1101", fresh=True) is None


def test_feed_unhealthy_returns_nothing():
    f = _feed()
    f.on_market_batch("7", _batch(1, 0, [_bk(1101, 10, [(0.5, 100)], [(0.51, 100)])]))
    f.healthy = False
    assert f.book("1101") is None


def test_quotes_from_feed_without_our_own_ask():
    # pushed book: YES bids 0.875 (ours, 500) then 0.87 (others); we must see others' best 0.87
    fake = FakeClient({"Delaware Senate": DE}, balance=1_000)
    r = make_runner(fake)
    f = _feed(); r.feed = f; r._bulk_t = 1e18
    ex = fake.ex_of("Delaware Senate", "D")
    f.on_market_batch("7", _batch(1, 0, [_bk(ex, 10, [(0.875, 500), (0.87, 900)], [(0.88, 300)])]))
    r.quote_live[(ex, "sell")] = (0.125, 500, 1e18, "maker")                              # our NO ask = YES bid 0.875
    assert r.quotes()[ex]["bestBid"] == 0.87


def test_reconcile_keeps_an_unchanged_quote_and_reprices_a_changed_one():
    fake, r = _b_runner(37_588, 37_588, balance=1_000)
    b = basket(r, "Delaware Senate"); ex = fake.ex_of("Delaware Senate", "D")
    o = {"leg": "D", "side": "sell", "kind": "quote", "price": 0.125, "qty": 1000, "edge_vs_fair": 0.1}
    r.quote_live[(ex, "sell")] = (0.125, 1000, time.time() + 300, "quote")
    r.b.reconcile({(ex, "sell"): (b, o)}, {ex}); assert _b_orders(r) == []               # unchanged: no write
    r.b.reconcile({(ex, "sell"): (b, {**o, "price": 0.13})}, {ex})
    assert [x["price"] for x in _b_orders(r)] == [0.13]


def test_reconcile_never_posts_a_sell_below_others_best_ask():
    fake, r = _b_runner(37_588, 37_588, balance=1_000)
    b = basket(r, "Delaware Senate"); ex = fake.ex_of("Delaware Senate", "D")
    o = {"leg": "D", "side": "sell", "kind": "quote", "price": 0.115, "qty": 1000, "edge_vs_fair": 0.1}   # best NO ask 0.125
    r.b.reconcile({(ex, "sell"): (b, o)}, {ex})
    assert _b_orders(r) == []


def test_take_never_sells_shares_promised_to_a_resting_quote():
    # 1,000 NO_D held, 800 already offered by our resting quote: a take may sell at most 200
    fake, r = _b_runner(1_000, 1_000, balance=1_000)               # no exposure yet
    config.B_TOTAL_CAP_FRAC = 1.0                                    # plenty of room: a take would sell all 1,000
    ex = fake.ex_of("Delaware Senate", "D")
    r.quote_live[(ex, "sell")] = (0.13, 800, time.time() + 300, "quote")
    r.b.step(r.quotes(), r.positions())
    takes = [o for o in _b_orders(r) if o["exchangeId"] == ex and o["action"] == "sell" and o["idempotencyKey"].endswith("b-take")]
    assert sum(o["quantity"] for o in takes) <= 200


def test_quote_in_a_race_we_left_is_cancelled():
    fake, r = _b_runner(0, 0, balance=1_000)                       # both legs 0: race left
    ex = fake.ex_of("Delaware Senate", "D")
    r.quote_live[(ex, "sell")] = (0.125, 500, time.time() + 300, "maker")
    r.b.step(r.quotes(), r.positions())
    assert (ex, "sell") not in r.quote_live                        # cancelled (dry run clears the state)


def test_resting_buys_reduce_the_cash_for_new_buys():
    fake, r = _b_runner(37_588, 37_588, balance=1_000 + 500)
    other = fake.ex_of("Delaware Senate", "R")
    r.quote_live[("9999", "buy")] = (0.5, 1_000, time.time() + 300, "maker")   # 500 already bid elsewhere
    r.b.step(r.quotes(), r.positions())
    assert not [o for o in _b_orders(r) if o["action"] == "buy"]


def test_maker_stops_where_legs_drift_apart_in_a_race_b_does_not_manage():
    fake, r = _maker_runner(37_588, CHEAP_OTHER)                   # Kalshi untrusted
    ex_d, ex_r = fake.ex_of("Delaware Senate", "D"), fake.ex_of("Delaware Senate", "R")
    fake.held = {ex_d: (-37_088, 3_000.0), ex_r: (-37_588, 3_000.0)}   # one-leg fills: 500 apart already
    r.b.step(r.quotes(), r.positions())
    assert not [o for o in _b_orders(r) if o["idempotencyKey"].endswith("b-maker")]


def test_feed_rest_book_is_fresh_only_briefly():
    import realtime_feed
    f = _feed()
    f.put_rest_book("1101", {"asOf": {"sequence": 5, "at": "2026-10-04T09:00:00Z"}, "marketId": "7",
                             "bids": [{"price": 0.5, "quantity": 10}], "asks": [{"price": 0.51, "quantity": 10}]})
    assert f.book("1101", fresh=True) is not None
    with f.lock:
        f.books["1101"]["next"] = time.time() - 1                 # REST_FRESH_S later
    assert f.book("1101", fresh=True) is None and f.book("1101") is not None


def test_b_unexpected_error_skips_the_round_without_raising():
    fake, r = _b_runner(37_588, 37_588, balance=1_000)
    r.b._step = lambda q, held: 1 / 0
    r.b.step(r.quotes(), r.positions())                        # must not raise


def test_rejected_pair_order_pauses_the_race_instead_of_halting():
    fake = FakeClient({"Kansas Senate": NO_EDGE})
    r = make_runner(fake)
    b = basket(r, "Kansas Senate")
    def reject(path, body, label):
        raise execute.ApiError(400, "VALIDATION_ERROR", "Validation failed for one or more legs.")
    r.order = reject
    assert b.send_pair("sell", 10, [0.5, 0.49], [100, 100]) == 0      # no Halt, nothing traded
    assert b.paused() and b.send_pair("sell", 10, [0.5, 0.49], [100, 100]) == 0


def test_unknown_outcome_still_halts():
    fake = FakeClient({"Kansas Senate": NO_EDGE})
    r = make_runner(fake)
    b = basket(r, "Kansas Senate")
    def boom(path, body, label):
        raise execute.ApiError(503, "SERVICE_UNAVAILABLE", "x")
    r.order = boom
    try:
        b.send_pair("sell", 10, [0.5, 0.49], [100, 100]); assert False, "expected Halt"
    except execute.Halt:
        pass


def test_fix_that_succeeds_on_the_last_try_does_not_halt():
    # live 2026-10-04 12:05: try 3 restored the legs, but the loop only checked at the top of a try
    fake = FakeClient({"Kansas Senate": {"D": ([(0.875, 9000)], [(0.88, 9000)]), "R": ([(0.11, 9000)], [(0.115, 9000)])}})
    r = make_runner(fake); r.live = True
    r.cancel_all = lambda exs: None
    b = basket(r, "Kansas Senate")
    b.leg_order = lambda k, action, qty, price, tag: {"open": False}
    config.FIX_MAX_TRIES = 3
    calls = {"n": 0}
    def shares():
        calls["n"] += 1
        return [100, 100] if calls["n"] >= 3 else [100, 90]     # fixed only by the third order
    b.no_shares = shares
    b.fix_imbalance([100, 90], "buy", [0.125, 0.885], 1.01)    # must return without Halt
    assert not execute.STOP_FILE.exists()


def test_own_order_drops_the_pushed_book():
    fake = FakeClient({"Kansas Senate": NO_EDGE})
    r = make_runner(fake)
    f = _feed(); r.feed = f
    f.on_market_batch("7", _batch(1, 0, [_bk(1101, 10, [(0.5, 100)], [(0.51, 100)])]))
    r.live = True; r.c = type("C", (), {"post": lambda self, p, b: {"data": {"orderId": 1}}})()
    execute.Runner.order(r, "/orders", {"exchangeId": "1101", "action": "sell", "quantity": 1, "price": 0.5},
                         "Kansas Senate:fix-sell")                     # the real method, not the test capture
    assert f.book("1101") is None                               # next read goes to REST


def _c_orders(r, ex=None):
    return [o for o in _b_orders(r) if o["idempotencyKey"].endswith("c-quote") and (ex is None or o["exchangeId"] == ex)]


def test_c_quotes_both_sides_on_a_flat_held_race():
    # Delaware-like, exposure 0: sell the favourite's NO at the best ask, buy the underdog's NO with a raised bid
    fake, r = _b_runner(5_000, 5_000, balance=5_000)
    config.B_TOTAL_CAP_FRAC = 1.0
    r.b.step(r.quotes(), r.positions())
    ex_d, ex_r = fake.ex_of("Delaware Senate", "D"), fake.ex_of("Delaware Senate", "R")
    sells = _c_orders(r, ex_d); buys = _c_orders(r, ex_r)
    assert sells and sells[0]["action"] == "sell" and sells[0]["price"] == 0.125      # at the best NO ask
    assert buys and buys[0]["action"] == "buy" and buys[0]["price"] >= 0.84           # joins (1-tick spread: no room to raise)
    assert buys[0]["price"] < 0.845 and buys[0]["price"] + 0.12 < 1                   # below the ask; pair rule


def test_c_raises_the_cheap_legs_bid_when_the_spread_allows():
    # NO_R bid 0.80 / ask 0.85 (wide): the buy may sit above the best bid, toward Kalshi fair 0.986
    wide = {"D": ([(0.875, 9000)], [(0.88, 9000)]), "R": ([(0.15, 9000)], [(0.20, 9000)])}
    fake, r = _b_runner(5_000, 5_000, balance=5_000)
    fake.books[fake.ex_of("Delaware Senate", "R")] = wide["R"]
    config.B_TOTAL_CAP_FRAC = 1.0
    r.b.step(r.quotes(), r.positions())
    buy = _c_orders(r, fake.ex_of("Delaware Senate", "R"))[0]
    assert 0.80 < buy["price"] < 0.85                                                 # raised, still below the ask


def test_c_above_its_limit_only_cuts_risk():
    # 12,000 extra NO_R (legacy position): no risk-adding quotes, only buy NO_D back / sell NO_R at the touch
    fake, r = _b_runner(10_000, 22_000, balance=5_000)
    config.B_TOTAL_CAP_FRAC = 1.0
    r.b.step(r.quotes(), r.positions())
    ex_d, ex_r = fake.ex_of("Delaware Senate", "D"), fake.ex_of("Delaware Senate", "R")
    got = {(o["exchangeId"], o["action"]) for o in _c_orders(r)}
    assert (ex_d, "sell") not in got and (ex_r, "buy") not in got                     # nothing adds risk
    assert got & {(ex_d, "buy"), (ex_r, "sell")}                                       # risk-cutting quotes
    for o in _c_orders(r):
        if o["action"] == "buy":
            assert o["price"] == 0.12                                                  # joins the best bid
        else:
            assert o["price"] == 0.845                                                 # joins the best ask


def test_c_bids_only_in_a_race_we_do_not_hold():
    fake, r = _b_runner(0, 0, balance=5_000)
    r.b.step(r.quotes(), r.positions())
    orders = _c_orders(r)
    assert orders and all(o["action"] == "buy" for o in orders)                       # nothing to sell


# ---- strategy D (Senate-control stat arb) ---------------------------------------
# U.S. Senate: YES Dem bid 0.675 / ask 0.68 -> NO Dem 0.32 / 0.325 (SUSQ P(R) ~ 0.3225); Kalshi R 0.375
SEN = {"D": ([(0.675, 9000)], [(0.68, 9000)]), "R": ([(0.32, 9000)], [(0.325, 9000)])}
TX = {"D": ([(0.64, 9000)], [(0.645, 9000)]), "R": ([(0.355, 9000)], [(0.36, 9000)])}


def _d_runner(ledger=None, kalshi_r=0.375):
    import kalshi, stat_model, d_executor
    config.D_RACES = ["Texas"]
    config.B_RACES = {"Texas Senate": {"event": "SENATETX-26", "D": "SENATETX-26-D", "R": "SENATETX-26-R"}}
    config.D_KALSHI_EXTRA = {}
    config.D_ENTRY_GAP, config.D_EXIT_GAP, config.D_BAND_FRAC, config.D_MIN_TRADE = 0.03, 0.01, 0.10, 25
    config.D_MAX_ORDERS, config.D_CLIP, config.D_CAPITAL, config.D_INTERVAL_S = 6, 1_000, 10_000, 60
    config.B_MAX_KALSHI_SPREAD = 0.02
    kalshi.market = lambda t: {"yes_bid_dollars": str(kalshi_r - 0.005), "yes_ask_dollars": str(kalshi_r + 0.005)}         if t.startswith("CONTROLS") else {"yes_bid_dollars": "0.35", "yes_ask_dollars": "0.36"}
    stat_model.calibrate = lambda p, target: 0.4
    stat_model.deltas = lambda p, rho: [0.15]
    d_executor.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))   # no tags
    fake = FakeClient({"U.S. Senate": SEN, "Texas Senate": TX}, balance=1_000 + 5_000)
    r = make_runner(fake, d_enabled=True)
    if ledger:
        r.d.ledger = {fake.ex_of(*k): v for k, v in ledger.items()}
    return fake, r


def _d_orders(r):
    return [b for _, b in r.sent if "legs" not in b and b["idempotencyKey"].split("-")[-2] == "d"]


def test_d_enters_long_r_control_when_susq_is_below_kalshi():
    fake, r = _d_runner()
    r.d.step(r.quotes())
    o = _d_orders(r)
    assert len(o) == 1 and o[0]["exchangeId"] == fake.ex_of("U.S. Senate", "D") and o[0]["action"] == "buy"
    assert o[0]["price"] == 0.325                                         # NO on Dem control at the ask


def test_d_hedges_held_control_with_no_on_the_state_republican():
    fake, r = _d_runner(ledger={("U.S. Senate", "D"): 1_000})
    r.d.step(r.quotes())
    h = [o for o in _d_orders(r) if o["exchangeId"] == fake.ex_of("Texas Senate", "R")]
    assert h and h[0]["action"] == "buy" and h[0]["quantity"] == 150      # 1,000 x delta 0.15


def test_d_exits_when_converged():
    fake, r = _d_runner(ledger={("U.S. Senate", "D"): 1_000, ("Texas Senate", "R"): 150}, kalshi_r=0.325)
    r.d.step(r.quotes())
    got = {(o["exchangeId"], o["action"], o["quantity"]) for o in _d_orders(r)}
    assert got == {(fake.ex_of("U.S. Senate", "D"), "sell", 1_000), (fake.ex_of("Texas Senate", "R"), "sell", 150)}


def test_d_ledger_is_hidden_from_the_other_strategies():
    fake, r = _d_runner(ledger={("Texas Senate", "R"): 150})
    ex = fake.ex_of("Texas Senate", "R")
    fake.held = {ex: (-1_150, 500.0), fake.ex_of("Texas Senate", "D"): (-1_000, 600.0)}
    assert r.positions()[ex]["no"] == 1_000                               # A/B/C see 1,000 pairs, not 1,150


def test_a_buys_with_free_cash_while_under_its_cap():
    fake = FakeClient({"Aaa race": EDGE_01}, balance=1_000 + 600)            # 600 free cash, no holdings
    r = make_runner(fake, a_cap=50_000)
    r.poll()
    buys = [b for _, b in r.sent if b["legs"][0]["action"] == "buy"]
    assert buys and buys[0]["legs"][0]["quantity"] * sum(l["price"] for l in buys[0]["legs"]) <= 600 + 1e-6


def test_a_buys_nothing_new_while_over_its_cap():
    fake = FakeClient({"Held race": SELLER_0990, "Aaa race": EDGE_01}, balance=1_000 + 5_000)
    fake.held = {fake.ex_of("Held race", "D"): (-60_000, 30_000.0), fake.ex_of("Held race", "R"): (-60_000, 29_400.0)}
    r = make_runner(fake, a_cap=50_000)                                       # 59,400 at cost > 50,000
    assert r.cash_room() < 0
    r.poll()
    assert r.sent == []                       # no entry (over the cap) and no swap (the held pairs bid 0.99 = no gain)


def test_a_room_is_the_smaller_of_cash_and_cap():
    fake = FakeClient({"Held race": SELLER_0990}, balance=1_000 + 20_000)
    fake.held = {fake.ex_of("Held race", "D"): (-40_000, 20_000.0), fake.ex_of("Held race", "R"): (-40_000, 19_600.0)}
    r = make_runner(fake, a_cap=50_000)                                       # holdings 39,600 -> room 10,400 < cash 20,000
    assert abs(r.cash_room() - 10_400) < 1


def test_arb_trade_allowed_on_b_race_with_unequal_legs():
    fake, r = _b_runner(37_588, 32_588, balance=1_000)
    b = basket(r, "Delaware Senate")
    assert b.send_pair("buy", 10, [0.125, 0.845], [37_588, 32_588]) == 10       # no Halt


def test_unequal_legs_still_halt_outside_b_races():
    fake = FakeClient({"Kansas Senate": NO_EDGE})
    r = make_runner(fake)
    b = basket(r, "Kansas Senate")
    try:
        b.send_pair("buy", 10, [0.5, 0.49], [100, 50])
        assert False, "expected Halt"
    except execute.Halt:
        pass


if __name__ == "__main__":
    import contextlib
    import io
    names = [n for n in dir() if n.startswith("test_")]
    bad = 0
    for n in names:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                globals()[n]()
            print("PASS", n)
        except Exception as e:
            bad += 1
            print("FAIL", n, repr(e))
    print(f"{len(names) - bad}/{len(names)} passed")
    sys.exit(1 if bad else 0)
