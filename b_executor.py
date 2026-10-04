"""Strategy B executor: Kalshi-anchored orders on config.B_RACES, run inside the arb bot's process
(one rate limiter, one positions read per poll). Rules: strategy_b.py. Live since 2026-10-04 (user).

Races: every race in config.B_RACES (kalshi_map.json) that holds shares on either leg (user, 2026-10-04).
Caps: total = B_TOTAL_CAP_FRAC of portfolio value; each race gets the total x its share of the pairs
held in those races (bigger holdings, bigger position). At most B_MAX_ORDERS_PER_ROUND orders per round.

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

    @staticmethod
    def top_book(q, ex):
        """NO ladders from the bulk quote (best level only, size unknown)."""
        x = q.get(ex, {})
        bid_y, ask_y = x.get("bestBid"), x.get("bestAsk")
        return {"asks": [(round(1 - bid_y, 6), INF)] if bid_y is not None else [],
                "bids": [(round(1 - ask_y, 6), INF)] if ask_y is not None else []}

    def full_book(self, ex):
        ob = self.r.c.get(f"/exchanges/{ex}/orderbook", tournamentId=self.r.tour["id"], depth=200)
        return {"asks": no_asks_from_yes_bids(ob["bids"]), "bids": no_bids_from_yes_asks(ob["asks"])}

    def _step(self, q, held):
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
        spend = max(0.0, cash - config.HARD_RESERVE)              # cash B may use for buys this round
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
        # 2) hand out the total cap and the cash: takes first, each group by largest edge
        sent = 0
        for edge, _, b, exchange, o, raises in sorted(cands, key=lambda c: (not c[1], -c[0])):
            if sent >= config.B_MAX_ORDERS_PER_ROUND:
                break
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
            self.send(b, exchange, o, qty)
            sent += 1

    def send(self, b, exchange, o, qty):
        expiry = config.ORDER_EXPIRY_S if o["kind"] == "take" else max(5, config.B_INTERVAL_S - 5)
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
        elif o["kind"] == "quote" and d.get("open"):
            self.r.b_resting.add(exchange)
