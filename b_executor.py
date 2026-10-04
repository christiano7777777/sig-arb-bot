"""Strategy B executor: Kalshi-anchored orders on config.B_RACES, run inside the arb bot's process
(one rate limiter, one positions read per poll). Rules: strategy_b.py. Live since 2026-10-04 (user).

Races: every race in config.B_RACES (kalshi_map.json) that holds shares on either leg (user, 2026-10-04).
Caps: total = B_TOTAL_CAP_FRAC of portfolio value; each race gets the total x its share of the pairs
held in those races (bigger holdings, bigger position). At most B_MAX_ORDERS_PER_ROUND orders per round.
Then the pair maker (pair_maker.py) keeps resting pair quotes at the touch on the largest held races.

Every B_INTERVAL_S seconds, per race:
  1. fair value from Kalshi (public API); no trading on that race if it is not trusted
  2. top of book from the poll's bulk quotes; full book read only for a leg that shows a take
  3. takes: marketable limit at the worst level still >= TAKE_EDGE from fair, 10 s expiry, any
     remainder cancelled at once (a resting remainder would undercut the best ask)
  4. quotes: posted only when they sit at the best price (a quote behind the touch cannot fill and
     only costs writes); they expire before the next round, so no cancels are needed
Buys are limited by cash above HARD_RESERVE. B errors skip the round; they never halt the arb bot.
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor

import config
import kalshi
import pair_maker
import strategy_b
from arb_math import no_asks_from_yes_bids, no_bids_from_yes_asks
from susq_client import ApiError

INF = float("inf")


def now_plus(seconds):
    import datetime as dt
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class BExecutor:
    def __init__(self, runner):
        self.r = runner
        self.races = [b for b in runner.baskets if b.b_race]
        missing = set(config.B_RACES) - {b.name for b in self.races}
        print(f"B: {len(self.races)} races mapped to Kalshi (traded while held)"
              + (f"; not found on SUSQ: {sorted(missing)}" if missing else ""))
        self.last_mid, self.next_t = {}, 0.0

    def step(self, q, held):
        if time.time() < self.next_t:
            return
        self.next_t = time.time() + config.B_INTERVAL_S
        try:
            self._step(q, held)
        except ApiError as e:
            print(f"  B: API error, round skipped: {e}")
        except Exception as e:                       # noqa: BLE001 - B must never stop the arb bot
            print(f"  B: unexpected error, round skipped: {type(e).__name__}: {e}")

    @staticmethod
    def top_book(q, ex):
        """NO ladders from the bulk quote (best level only, size unknown)."""
        x = q.get(ex, {})
        bid_y, ask_y = x.get("bestBid"), x.get("bestAsk")
        return {"asks": [(round(1 - bid_y, 6), INF)] if bid_y is not None else [],
                "bids": [(round(1 - ask_y, 6), INF)] if ask_y is not None else []}

    def full_book(self, ex):
        """NO ladders of a leg: fresh pushed book or REST, without our own resting quotes."""
        bids, asks = self.r.levels(ex, fresh=True)
        return {"asks": no_asks_from_yes_bids([{"price": p, "quantity": q} for p, q in bids]),
                "bids": no_bids_from_yes_asks([{"price": p, "quantity": q} for p, q in asks])}

    def _step(self, q, held):
        held = self.r.positions(fresh=True)        # B sizes sells from holdings: never from a cached read
        cash = self.r.cached_balance()
        cap_total = config.B_TOTAL_CAP_FRAC * (cash + sum(p["cost"] for p in held.values()))
        state = []
        used = 0.0
        active = []                                               # races with shares on either leg
        for b in self.races:
            ex = {b.legs[i]["party"]: b.ex[i] for i in (0, 1)}
            h = {x: held.get(ex[x], {}).get("no", 0.0) for x in "DR"}
            if h["D"] >= 1 or h["R"] >= 1:
                active.append((b, ex, h))
        with ThreadPoolExecutor(max_workers=8) as pool:           # Kalshi in parallel: the arb loop waits
            fairs = list(pool.map(lambda a: kalshi.fair(config.B_RACES[a[0].name], config.B_MAX_KALSHI_SPREAD), active))
        pairs_total = sum(min(h["D"], h["R"]) for _, _, h in active) or 1.0
        race_cap = {b.name: cap_total * min(h["D"], h["R"]) / pairs_total for b, _, h in active}
        for (b, ex, h), k in zip(active, fairs):
            jump = False
            if k["ok"]:
                prev = self.last_mid.get(b.name)
                jump = prev is not None and max(abs(k["mid"][x] - prev[x]) for x in "DR") > config.B_KALSHI_JUMP
                self.last_mid[b.name] = k["mid"]
                fav = max(k["p"], key=k["p"].get)
                used += max(h["R" if fav == "D" else "D"] - h[fav], 0.0)
            state.append((b, ex, h, k, jump))
        room = max(0.0, cap_total - used)
        # resting buy quotes already claim cash (the engine only checks each exchange on its own)
        live_buys = sum(px * qty for (e, side), (px, qty, _, _) in self.r.quote_live.items() if side == "buy")
        spend = max(0.0, cash - config.HARD_RESERVE - live_buys)  # cash B may use for new buys this round
        # shares already promised to resting sell quotes, and sold by takes this round, per exchange
        promised = {e: qty for (e, side), (px, qty, _, _) in self.r.quote_live.items() if side == "sell"}
        sold_now = {}
        # 1) candidate orders per race (race cap applied inside decide; total cap allocated below)
        cands = []
        for b, ex, h, k, jump in state:
            if jump and self.r.b_resting & set(ex.values()):
                self.r.cancel_all(list(ex.values()))              # fair value moved: pull our quotes
                self.r.b_resting -= set(ex.values())
            books = {x: self.top_book(q, ex[x]) for x in "DR"}
            p = k.get("p", {"D": 0.5, "R": 0.5})
            res = strategy_b.decide(books, p, h, INF, kalshi_ok=k["ok"], kalshi_jump=jump, cash=spend,
                                    race_cap=race_cap[b.name])
            if not res["orders"]:
                if k["ok"] is False:
                    print(f"  B {b.name}: {res['why']} ({k.get('why', '')})")
                continue
            take_legs = {o["leg"] for o in res["orders"] if o["kind"] == "take"}
            if take_legs:                                         # size takes from the real book
                for x in take_legs:
                    books[x] = self.full_book(ex[x])
                res = strategy_b.decide(books, p, h, INF, kalshi_ok=k["ok"], kalshi_jump=jump, cash=spend,
                                    race_cap=race_cap[b.name])
            fav = max(p, key=p.get)
            fair = {x: 1.0 - p[x] for x in "DR"}
            for o in res["orders"]:
                top = books[o["leg"]]["asks" if o["side"] == "sell" else "bids"]
                if o["kind"] == "quote" and (not top or abs(o["price"] - top[0][0]) > 1e-9
                                             and not (o["side"] == "buy" and o["price"] > top[0][0])):
                    continue                                      # behind the touch: would not fill
                # priority = edge vs fair at the best level we would trade (takes) or at our quote
                if o["kind"] == "take":
                    lvl = books[o["leg"]]["bids" if o["side"] == "sell" else "asks"][0][0]
                    edge = lvl - fair[o["leg"]] if o["side"] == "sell" else fair[o["leg"]] - lvl
                else:
                    edge = o["edge_vs_fair"]
                raises = (o["leg"] == fav) == (o["side"] == "sell")
                if res.get("mode") == "closing" and o["side"] == "sell":
                    raises = False                                # unwinding never needs cap room
                cands.append((edge, o["kind"] == "take", b, ex[o["leg"]], o, raises))
        # 2) hand out the total cap and the cash: takes first, each group by largest edge.
        #    Takes are sent now; quotes are collected and reconciled with what is already resting.
        sent, want = 0, {}
        for edge, _, b, exchange, o, raises in sorted(cands, key=lambda c: (not c[1], -c[0])):
            qty = o["qty"]
            if raises:
                qty = min(qty, math.floor(room + 1e-9))
            if o["side"] == "buy":
                qty = min(qty, math.floor(spend / o["price"] + 1e-9))
            if qty < 1:
                continue
            if raises:
                room -= qty
            if o["side"] == "buy":
                spend -= qty * o["price"]
            if o["side"] == "sell":                # never sell more than held (an oversell turns into YES)
                have = held.get(exchange, {}).get("no", 0.0) - sold_now.get(exchange, 0.0)
                if o["kind"] == "take":
                    have -= promised.get(exchange, 0.0)
                qty = min(qty, math.floor(have + 1e-9))
                if qty < 1:
                    continue
            if o["kind"] == "take":
                if sent < config.B_MAX_ORDERS_PER_ROUND:
                    self.send(b, exchange, o, qty)
                    sent += 1
                    if o["side"] == "sell":
                        sold_now[exchange] = sold_now.get(exchange, 0.0) + qty
            elif (exchange, o["side"]) not in want:
                want[(exchange, o["side"])] = (b, {**o, "qty": qty})
        if config.MAKER_ENABLED:
            for key, (b, o) in self.make_pairs(q, active, fairs, race_cap, room, spend).items():
                if o["side"] == "sell":
                    left = held.get(key[0], {}).get("no", 0.0) - sold_now.get(key[0], 0.0)
                    o = {**o, "qty": min(o["qty"], math.floor(left + 1e-9))}
                    if o["qty"] < 1:
                        continue
                want.setdefault(key, (b, o))        # B's own quote on the same leg and side wins
        # every exchange with a quote of ours, incl. races that were left (both legs 0): stale ones get cancelled
        self.reconcile(want, {e for b, ex, h in active for e in ex.values()} | {k[0] for k in self.r.quote_live})

    def make_pairs(self, q, active, fairs, race_cap, room, spend):
        """Pair maker on the largest held races (pair_maker.py): the pair quotes wanted this round,
        {(exchangeId, side): (basket, order)}. Posting is left to reconcile()."""
        # NO ask sum of every race (without our own quotes): the pair a swap could rotate the cash into
        pair_ask = {b.name: sum(1 - q[e]["bestBid"] for e in b.ex) for b in self.r.baskets
                    if all(q.get(e, {}).get("bestBid") is not None for e in b.ex)}
        big = sorted(((b, ex, h, k) for (b, ex, h), k in zip(active, fairs) if min(h.values()) >= config.MAKER_MIN_PAIRS),
                     key=lambda t: -min(t[2].values()))[:config.MAKER_RACES]
        want = {}
        for b, ex, h, k in big:
            # race B does not manage (Kalshi untrusted / no clear favourite): legs may drift apart by at most
            # MAKER_OVER_CAP through one-leg fills, then the maker stops quoting it
            fav, rroom = "either", max(0.0, config.MAKER_OVER_CAP - abs(h["D"] - h["R"]))
            if k["ok"] and max(k["p"].values()) >= config.B_MIN_FAVOURITE:
                fav = max(k["p"], key=k["p"].get)
                und = "R" if fav == "D" else "D"
                exposure = max(h[und] - h[fav], 0.0)
                # (1, user 2026-10-04): a one-leg fill on the favourite may take the race at most
                # MAKER_OVER_CAP over its cap. No memory, so restarts cannot ratchet it up.
                limit = race_cap[b.name] + config.MAKER_OVER_CAP
                rroom = max(0.0, min(limit - exposure, max(room, 0.0) + config.MAKER_OVER_CAP))
            books = {x: self.top_book(q, ex[x]) for x in "DR"}
            cheapest = min((v for n, v in pair_ask.items() if n != b.name), default=None)   # another race
            for o in pair_maker.pair_quotes(books, h, cheapest, spend, fav, rroom):
                if o["side"] == "buy":
                    spend -= o["qty"] * o["price"]
                want[(ex[o["leg"]], o["side"])] = (b, {**o, "kind": "maker", "edge_vs_fair": 0.0})
        return want

    def reconcile(self, want, exchanges):
        """Keep resting quotes that are still wanted at the same price and about the same size; cancel the
        rest (cancel-all per exchange) and post what is missing, after checking against the current book
        (without our own orders) that a sell is not below the best NO ask and a buy does not cross it."""
        now, writes = time.time(), 0
        budget = config.B_MAX_ORDERS_PER_ROUND + config.MAKER_MAX_ORDERS
        for e in exchanges:
            live = {k: v for k, v in self.r.quote_live.items() if k[0] == e}
            stale = [k for k, (px, qty, exp, _) in live.items()
                     if k not in want or abs(want[k][1]["price"] - px) > 1e-9 or exp < now + 5
                     or not 0.8 * qty <= want[k][1]["qty"] <= 1.25 * qty]
            if stale and writes < budget:
                self.r.cancel_all([e])              # clears quote_live for e
                self.r.b_resting.discard(e)
                writes += 1
        for key, (b, o) in want.items():
            if key in self.r.quote_live or writes >= budget:
                continue                            # already resting as wanted (or out of writes)
            bids, asks = self.r.levels(key[0], fresh=True)
            best_no_ask = round(1 - bids[0][0], 6) if bids else None    # others' best NO ask
            if best_no_ask is not None and (o["side"] == "sell" and o["price"] < best_no_ask - 1e-9
                                            or o["side"] == "buy" and o["price"] >= best_no_ask - 1e-9):
                print(f"  B {b.name}: skip {o['kind']} {o['side']} NO_{o['leg']} @ {o['price']}: "
                      f"book moved (best NO ask {best_no_ask})")
                continue
            self.send(b, key[0], o, o["qty"])
            writes += 1

    def send(self, b, exchange, o, qty):
        expiry = config.ORDER_EXPIRY_S if o["kind"] == "take" else config.MAKER_LIFE_S   # quotes rest; reconcile() keeps them current
        body = {"idempotencyKey": self.r.next_key(f"b-{o['kind']}"), "exchangeId": exchange, "side": "no",
                "action": o["side"], "quantity": int(qty), "price": o["price"],
                "expirationDate": now_plus(expiry), "tournamentId": self.r.tour["id"]}
        print(f"  B {b.name}: {o['kind']} {o['side']} {qty} NO_{o['leg']} @ {o['price']} "
              f"({o['edge_vs_fair']:+.3f} vs Kalshi fair)")
        try:
            r = self.r.order("/orders", body, f"{b.name}:b-{o['kind']}-{o['side']}-{o['leg']}")
        except ApiError as e:
            print(f"    B order failed (round continues): {e}")
            return
        self.r._cash = None
        if r is None:                                             # dry run
            return
        d = r.get("data", r)
        print(f"    traded {d.get('quantityTraded')} open={d.get('open')} reason={d.get('terminalReasonCode')}")
        if o["kind"] == "take" and d.get("open"):
            self.r.cancel_all([exchange])                         # no resting remainder below the best ask
        elif o["kind"] in ("quote", "maker") and d.get("open"):
            self.r.b_resting.add(exchange)                        # the arb cancels these before trading here
            rest = qty - (d.get("quantityTraded") or 0)
            self.r.quote_live[(exchange, o["side"])] = (o["price"], rest, time.time() + expiry, o["kind"])
