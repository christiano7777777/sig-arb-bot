"""Offline tests of the multi-race executor against a fake API (no network).
Run: python tests/test_execute_dry.py"""
import os
import sys
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
                denylist=()):
    # pin every setting the tests rely on, so editing config.py cannot silently change a test
    config.MIN_EDGE, config.MAX_UNHEDGED_EXPOSURE = min_edge, exposure
    config.MAX_CAPITAL_PER_RUN, config.RESERVE, config.PER_RACE_CAP = capital, reserve, race_cap
    config.EXIT_ENABLED, config.EXIT_MIN_SUM, config.TOURNAMENT_SLUG = True, 1.0, SLUG
    config.RACE_DENYLIST = set(denylist)
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
    assert [l["quantity"] for l in r.sent[0][1]["legs"]] == [101, 101]     # (5000 - 4900) / 0.99


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
    (p1, sell), (p2, buy) = r.sent
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


def test_rotation_buys_even_when_sale_frees_under_50():
    # seller only has 20 pairs at 0.995 -> frees ~20 cash (< 50 trigger); the buy must still happen
    seller = {"D": ([(0.4, 5)], [(0.5, 20)]), "R": ([(0.4, 5)], [(0.505, 20)])}
    fake = FakeClient({"Held race": seller, "New race": EDGE_01}, balance=50_000.5)
    fake.held = {fake.ex_of("Held race", "D"): (-5000, 2500.0), fake.ex_of("Held race", "R"): (-5000, 2450.0)}
    r = make_runner(fake, exposure=500)
    r.poll()
    assert [b["legs"][0]["action"] for _, b in r.sent] == ["sell", "buy"]
    assert r.sent[1][1]["legs"][0]["quantity"] == 20      # (0.5 + 20 * 0.995) / 0.99 -> 20


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
