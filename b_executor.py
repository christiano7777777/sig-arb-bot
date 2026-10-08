"""Executor for strategies B and C, run inside the arb bot's process (one rate limiter, one positions
read per poll, one quote reconcile per round). Every B_INTERVAL_S seconds:

  C (strategy_c.py, user 2026-10-04): Kalshi-anchored two-sided market making on every race we hold,
    plus bids in the C_EXTRA_RACES races we do not hold with the biggest SUSQ-Kalshi gap. Quotes only
    (no takes), skewed against our directional inventory; risk-adding quotes only below C_LIMIT per
    race, so today's big positions unwind through the risk-cutting quotes.
  B (pair_maker.py): resting pair quotes on the largest held races (sell pairs at the ask, buy at the bid).

Quotes rest MAKER_LIFE_S and are reconciled each round (kept if unchanged, else cancel + re-post after
checking the current book without our own orders). Errors skip the round; they never stop the arb bot.
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor

import config
import kalshi
import pair_maker
import strategy_c
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
        # races with a Kalshi mapping only (b_race is also true for D's control market, which has none:
        # using b_race here made every B/C round fail with KeyError 'U.S. Senate' from 13:37 to 14:50 UTC)
        self.races = [b for b in runner.baskets if b.name in config.B_RACES]
        missing = set(config.B_RACES) - {b.name for b in self.races}
        print(f"B: {len(self.races)} races mapped to Kalshi (traded while held)"
              + (f"; not found on SUSQ: {sorted(missing)}" if missing else ""))
        self.last_mid, self.next_t = {}, 0.0
        self.kcache = {}            # race -> (time, Kalshi fair)

    def step(self, q, held):
        if time.time() < self.next_t:
            return
        self.next_t = time.time() + config.B_INTERVAL_S
        hb = self.r.health["B"] if hasattr(self.r, "health") else {"rounds": 0, "errors": 0}
        hb["rounds"] += 1
        try:
            self._step(q, held)
            hb["last_ok"] = now_plus(0)
        except ApiError as e:
            print(f"  B: API error, round skipped: {e}")
        except Exception as e:                       # noqa: BLE001 - B must never stop the arb bot
            hb["errors"] += 1
            hb["last_error"] = f"{now_plus(0)} {type(e).__name__}: {e}"[:300]
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

    def fair(self, race, max_age):
        """Kalshi fair value for a race, cached up to max_age seconds."""
        hit = self.kcache.get(race)
        if hit is not None and time.time() - hit[0] < max_age:
            return hit[1]
        k = kalshi.fair(config.B_RACES[race], config.B_MAX_KALSHI_SPREAD)
        self.kcache[race] = (time.time(), k)
        return k

    def _step(self, q, held):
        held = self.r.positions(fresh=True)        # sizes sells from holdings: never from a cached read
        cash = self.r.cached_balance()
        cap_total = config.B_TOTAL_CAP_FRAC * (cash + sum(p["cost"] for p in held.values()))
        # cash: each strategy its CASH_SPLIT share, less what its own resting buys already claim
        spend = self.r.strategy_budget("C")
        spend_b = self.r.strategy_budget("B")
        legs = {b.name: {b.legs[i]["party"]: b.ex[i] for i in (0, 1)} for b in self.races}
        hold = {b.name: {x: held.get(legs[b.name][x], {}).get("no", 0.0) for x in "DR"} for b in self.races}
        active = [(b, legs[b.name], hold[b.name]) for b in self.races
                  if hold[b.name]["D"] >= 1 or hold[b.name]["R"] >= 1]
        others = [b for b in self.races if b not in {a[0] for a in active}]
        # Kalshi: held races every round, the others every 5 min (they only rank the extra races)
        with ThreadPoolExecutor(max_workers=8) as pool:
            fairs = list(pool.map(lambda a: self.fair(a[0].name, 0), active))
            ofairs = list(pool.map(lambda b: self.fair(b.name, 300), others))
        used = 0.0
        for (b, ex, h), k in zip(active, fairs):
            if k["ok"]:
                fav = max(k["p"], key=k["p"].get)
                used += max(h["R" if fav == "D" else "D"] - h[fav], 0.0)
        room = max(0.0, cap_total - used)          # total-cap backstop for risk-adding quotes
        # extra races (not held): bids on the cheap leg where SUSQ sits furthest below Kalshi fair
        extra = []
        for b, k in zip(others, ofairs):
            if not k["ok"]:
                continue
            fav = max(k["p"], key=k["p"].get)
            und = "R" if fav == "D" else "D"
            bid_y = q.get(legs[b.name][und], {}).get("bestBid")           # YES bid -> NO ask of the cheap leg
            if bid_y is not None:
                gap = (1.0 - k["p"][und]) - (1.0 - bid_y)
                if gap >= config.C_QUOTE_EDGE:
                    extra.append((gap, b, k))
        extra = [(b, legs[b.name], hold[b.name], k) for _, b, k in sorted(extra, key=lambda t: -t[0])[:config.C_EXTRA_RACES]]
        # C fast unwind (user, 2026-10-07): sell the excess leg into bids within C_DUMP_GAP of Kalshi fair, in
        # races without pairs (A's pair races keep their leftovers). Cheap pre-check on the bulk quote first.
        dumped = set()
        if getattr(config, "C_DUMP_GAP", None) is not None:
            for (b, ex, h), k in zip(active, fairs):
                if len(dumped) >= config.C_DUMP_PER_ROUND:
                    break
                pair_race = min(h["D"], h["R"]) >= 1      # one-sided legs there: C_DUMP_PAIR_GAP (user, 2026-10-08)
                gap = getattr(config, "C_DUMP_PAIR_GAP", None) if pair_race else config.C_DUMP_GAP
                if gap is None:
                    continue
                if not k["ok"]:                     # Kalshi untrusted (spread too wide): best bid only (user, 2026-10-08)
                    if not getattr(config, "C_DUMP_UNTRUSTED", False) or abs(h["D"] - h["R"]) < 1:
                        continue
                    o = strategy_c.dump_top({x: self.full_book(ex[x]) for x in "DR"}, h)
                    if o is None:
                        continue
                    self.r.cancel_all([ex[o["leg"]]])
                    self.r.b_resting.discard(ex[o["leg"]])
                    self.send(b, ex[o["leg"]], o, o["qty"])
                    dumped.add(b.name)
                    continue
                if strategy_c.dump({x: self.top_book(q, ex[x]) for x in "DR"}, k["p"], h, gap) is None:
                    continue                        # best bid already too far below fair: no book read
                o = strategy_c.dump({x: self.full_book(ex[x]) for x in "DR"}, k["p"], h, gap)
                if o is None:
                    continue
                self.r.cancel_all([ex[o["leg"]]])   # our resting ask on this leg first (clears quote_live)
                self.r.b_resting.discard(ex[o["leg"]])
                self.send(b, ex[o["leg"]], o, o["qty"])
                dumped.add(b.name)
        # C quotes: held races first, then the extra races; cash and the total room shared in that order
        want = {}
        for b, ex, h, k, jump in self.with_jumps([(b, ex, h, k) for (b, ex, h), k in zip(active, fairs)] + extra):
            if not k["ok"]:
                print(f"  C {b.name}: Kalshi not trusted ({k.get('why', '')})")
                continue
            if jump or b.name in dumped or not getattr(config, "C_QUOTES", True):
                continue                            # fair value just moved / dumped this round / C stopped: no quote
            books = {x: self.top_book(q, ex[x]) for x in "DR"}
            res = strategy_c.quotes(books, k["p"], h, spend)
            for o in res["orders"]:
                qty = o["qty"]
                if o["adds"]:
                    qty = min(qty, math.floor(room + 1e-9))
                if o["side"] == "buy":
                    qty = min(qty, math.floor(spend / o["price"] + 1e-9))
                if qty < 1:
                    continue
                if o["adds"]:
                    room -= qty
                if o["side"] == "buy":
                    spend -= qty * o["price"]
                want[(ex[o["leg"]], o["side"])] = (b, {**o, "qty": qty, "kind": "quote"})
        if config.MAKER_ENABLED:
            for key, (b, o) in self.make_pairs(q, active, fairs, {b.name: config.C_LIMIT for b, _, _ in active},
                                               room, spend_b).items():
                if o["side"] == "sell":
                    left = held.get(key[0], {}).get("no", 0.0)
                    o = {**o, "qty": min(o["qty"], math.floor(left + 1e-9))}
                    if o["qty"] < 1:
                        continue
                want.setdefault(key, (b, o))        # C's quote on the same leg and side wins
        # every exchange with a quote of ours, incl. races that were left: stale ones get cancelled
        exchanges = {e for b, ex, h in active for e in ex.values()} | {e for b, ex, h, k in extra for e in ex.values()}
        self.reconcile(want, exchanges | {k[0] for k in self.r.quote_live})

    def with_jumps(self, races):
        """Attach a 'Kalshi just jumped' flag per race (and pull our quotes there when it did)."""
        out = []
        for b, ex, h, k in races:
            jump = False
            if k["ok"]:
                prev = self.last_mid.get(b.name)
                jump = prev is not None and max(abs(k["mid"][x] - prev[x]) for x in "DR") > config.B_KALSHI_JUMP
                self.last_mid[b.name] = k["mid"]
            if jump and self.r.b_resting & set(ex.values()):
                self.r.cancel_all(list(ex.values()))
                self.r.b_resting -= set(ex.values())
            out.append((b, ex, h, k, jump))
        return out

    def make_pairs(self, q, active, fairs, race_cap, room, spend):
        """Pair maker on the largest held races (pair_maker.py): the pair quotes wanted this round,
        {(exchangeId, side): (basket, order)}. Posting is left to reconcile()."""
        # NO ask sum of every race (without our own quotes): the pair a swap could rotate the cash into
        if getattr(config, "A_TOP_N", None) and getattr(self.r, "a_top", None) is not None:
            return self.exit_quotes(q, active)      # focus rotation: B only sells the exit race
        if getattr(config, "EXIT_QUEUE", None):
            return self.queue_exit_quotes(q, active)   # exit queue: B only sells the current exit race
        top = getattr(self.r, "a_top", None)        # A buys only there (A_TOP_N), so only those can take the cash
        pair_ask = {b.name: sum(1 - q[e]["bestBid"] for e in b.ex) for b in self.r.baskets
                    if all(q.get(e, {}).get("bestBid") is not None for e in b.ex) and (top is None or b.name in top)}
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

    def queue_exit_quotes(self, q, active):
        """EXIT_QUEUE (user, 2026-10-08 05:30): asks on both legs of the current exit race for ALL its pairs, at the best
        asks, while its ask sum >= the cheapest pair A could buy elsewhere + ROTATE_MIN_GAIN. No bids anywhere."""
        exiting, want = getattr(self.r, "exiting", None), {}
        if exiting is None:
            return want
        skip = set(config.EXIT_QUEUE) | set(getattr(config, "HOLD_RACES", []))
        cheapest = min((sum(1 - q[e]["bestBid"] for e in b.ex) for b in self.r.baskets
                        if b.name not in skip and all(q.get(e, {}).get("bestBid") is not None for e in b.ex)), default=None)
        for b, ex, h in active:
            if b.name != exiting:
                continue
            books = {x: self.top_book(q, ex[x]) for x in "DR"}
            allp = int(min(h["D"], h["R"]) - self.r.exit_target())    # only the part above the target (5,000)
            if allp < 1:
                continue
            if getattr(config, "EXIT_ASK_ALWAYS", False):
                cheapest = 0.0                      # push the exit (user, 2026-10-08 06:05): asks out whatever else costs
            for o in pair_maker.pair_quotes(books, h, cheapest, 0.0, clip=allp, bids_on=False):
                want[(ex[o["leg"]], o["side"])] = (b, {**o, "kind": "maker", "edge_vs_fair": 0.0})
        return want

    def exit_quotes(self, q, active):
        """Focus rotation (user, 2026-10-08): asks on both legs of the exit race, MAKER_EXIT_CLIP pairs, only while
        its ask sum >= the cheapest pair in the other focus races + ROTATE_MIN_GAIN; pair bids on the other focus
        races (MAKER_FOCUS_BIDS)."""
        exiting, want = getattr(self.r, "exiting", None), {}
        if exiting is None:
            return want
        others = [b for b in self.r.baskets if b.name in self.r.a_top and b.name != exiting
                  and all(q.get(e, {}).get("bestBid") is not None for e in b.ex)]
        cheapest = min((sum(1 - q[e]["bestBid"] for e in b.ex) for b in others), default=None)
        spend = self.r.strategy_budget("B")
        for b, ex, h in active:
            books = {x: self.top_book(q, ex[x]) for x in "DR"}
            if b.name == exiting:                   # asks only
                quotes = pair_maker.pair_quotes(books, h, cheapest, 0.0, clip=config.MAKER_EXIT_CLIP, bids_on=False)
            elif getattr(config, "MAKER_FOCUS_BIDS", False) and b.name in self.r.a_top:
                # user, 2026-10-08: pair bids on the other focus races (no asks: cheapest None), within B's cash,
                # bid sum <= 1 - MIN_EDGE and never while the legs are MAKER_OVER_CAP apart (pair_maker)
                quotes = pair_maker.pair_quotes(books, h, None, spend, clip=config.MAKER_EXIT_CLIP)
            else:
                continue
            for o in quotes:
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
        pre = "b" if o["kind"] == "maker" else "c"                 # B = pair maker, C = Kalshi market making
        body = {"idempotencyKey": self.r.next_key(f"{pre}-{o['kind']}"), "exchangeId": exchange, "side": "no",
                "action": o["side"], "quantity": int(qty), "price": o["price"],
                "expirationDate": now_plus(expiry), "tournamentId": self.r.tour["id"]}
        print(f"  {pre.upper()} {b.name}: {o['kind']} {o['side']} {qty} NO_{o['leg']} @ {o['price']} "
              f"({o['edge_vs_fair']:+.3f} vs Kalshi fair)")
        try:
            r = self.r.order("/orders", body, f"{b.name}:{pre}-{o['kind']}-{o['side']}-{o['leg']}")
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
