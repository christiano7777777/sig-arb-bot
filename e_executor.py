"""Strategy E executor: Kalshi-jump breakout (strategy_e.py), run inside the arb bot's process.

E keeps its own LEDGER of NO shares per exchange (Runner.positions subtracts it, like D's), so A, B and C
never see or sell E's shares, and E never trades out of A's pairs. E's cash is its own too:
    E cash = allotment - E's buys + E's sells,   allotment = min(E_CAPITAL, E_FILL_PER_HOUR x hours live)
and the other strategies only see the free cash left after E's cash (Runner.free_cash), so the 10k builds
up slowly instead of being spent by A first. Ledger and cash flow are rebuilt at start-up from E's order tags
(like D's); entry details (Kalshi fair at entry, usual gap) are not, so a rebuilt position exits only on
"caught up", with the usual gap taken from fresh history.
Every E_INTERVAL_S, from the Kalshi batch cache (price_recorder) and the SUSQ books the bot already has.
"""
import time
from collections import deque
from datetime import datetime

import config
import kalshi
import strategy_e
from arb_math import no_asks_from_yes_bids, no_bids_from_yes_asks
from d_executor import load_tags, now_plus
from susq_client import ApiError


class EExecutor:
    def __init__(self, runner):
        self.r = runner
        self.races = [b for b in runner.baskets if b.name in config.B_RACES]
        self.hist = {}            # exchangeId -> deque[(t, Kalshi fair NO, SUSQ mid NO)]
        self.pos = {}             # exchangeId -> {"race", "leg", "entry_fair", "baseline", "t"}
        self.ledger = {}          # exchangeId -> E's NO shares
        self.flow = 0.0           # E's own cash flow: sells - buys (fill prices after a rebuild, limits in this run)
        self.next_t = 0.0
        self.since = datetime.fromisoformat(config.E_LIVE_SINCE).timestamp()
        print(f"E: {len(self.races)} races, capital {config.E_CAPITAL:,.0f} at {config.E_FILL_PER_HOUR:,.0f}/h "
              f"from {config.E_LIVE_SINCE}")
        self.rebuild()

    # ---------------- ledger and cash ----------------
    def rebuild(self):
        """E's holdings and cash flow = E-tagged fills since E_LIVE_SINCE (buys +, sells -)."""
        tags = load_tags(self.r)
        e_orders = {oid: t for oid, t in tags.items() if t[0] == "E"}
        if not e_orders:
            print("  E: ledger empty (no E orders yet)")
            return
        led, flow, cursor = {}, 0.0, None
        for _ in range(30):
            r = self.r.c.get("/portfolio/fills", tournamentId=self.r.tour["id"], limit=200, cursor=cursor)
            page = r.get("data", [])
            for f in page:
                t = e_orders.get(str(f.get("orderId")))
                if t and f["filledAt"] >= config.E_LIVE_SINCE[:19]:
                    n, px = abs(f["quantity"]), f.get("price") or 0.0
                    led[f["exchangeId"]] = led.get(f["exchangeId"], 0) + (n if t[2] == "buy" else -n)
                    flow += -n * px if t[2] == "buy" else n * px
            pg = r.get("pagination", {})
            if not pg.get("hasMore") or min((f["filledAt"] for f in page), default="") < config.E_LIVE_SINCE[:19]:
                break
            cursor = pg["nextCursor"]
        self.ledger = {e: n for e, n in led.items() if n > 0}
        self.flow = flow
        names = {b.ex[i]: (b.name, b.legs[i]["party"]) for b in self.races for i in (0, 1)}
        for e in self.ledger:
            race, leg = names.get(e, ("?", "?"))
            self.pos[e] = {"race": race, "leg": leg, "entry_fair": None, "baseline": None, "t": time.time()}
        print(f"  E: ledger rebuilt from {len(e_orders)} tagged orders: {self.ledger}, cash flow {flow:,.2f}")

    def allotment(self):
        hours = max(0.0, (time.time() - self.since) / 3600)
        return min(config.E_CAPITAL, config.E_FILL_PER_HOUR * hours)

    def cash(self):
        """E's own cash: the allotment less what E's trades have spent (net)."""
        return max(0.0, self.allotment() + self.flow)

    def reserved(self):
        """Cash the other strategies must leave for E (Runner.free_cash)."""
        return max(0.0, min(self.cash(), self.r.cached_balance() - config.HARD_RESERVE))

    # ---------------- one round ----------------
    def step(self, q):
        if time.time() < self.next_t:
            return
        self.next_t = time.time() + config.E_INTERVAL_S
        he = self.r.health.setdefault("E", {"rounds": 0, "errors": 0, "last_error": None})
        he["rounds"] += 1
        try:
            self._step(q)
            he["last_ok"] = now_plus(0)
        except ApiError as e:
            print(f"  E: API error, round skipped: {e}")
        except Exception as e:                          # noqa: BLE001 - E must never stop the bot
            he["errors"] += 1
            he["last_error"] = f"{now_plus(0)} {type(e).__name__}: {e}"[:300]
            print(f"  E: unexpected error, round skipped: {type(e).__name__}: {e}")

    def _step(self, q):
        now = time.time()
        for b in self.races:
            k = kalshi.fair_cached(config.B_RACES[b.name], config.B_MAX_KALSHI_SPREAD)
            if not k["ok"]:
                continue
            for i in (0, 1):
                ex, leg = b.ex[i], b.legs[i]["party"]
                yes = q.get(ex, {})
                bid = round(1 - yes["bestAsk"], 6) if yes.get("bestAsk") is not None else None   # NO bid
                ask = round(1 - yes["bestBid"], 6) if yes.get("bestBid") is not None else None   # NO ask
                fair = 1.0 - k["p"][leg]
                h = self.hist.setdefault(ex, deque())
                if ex in self.ledger:
                    self.manage(b, ex, leg, h, now, fair, bid)
                elif self.cash() >= config.ROTATE_TRIGGER_CASH:
                    sig = strategy_e.signal(list(h), now, fair, ask)
                    if sig is not None:
                        self.enter(b, ex, leg, fair, sig)
                h.append((now, fair, (bid + ask) / 2 if bid is not None and ask is not None else None))
                while h and h[0][0] < now - config.E_BASELINE_S - config.E_JUMP_WINDOW_S:
                    h.popleft()

    def books(self, ex):
        """NO bids and asks of a leg (others' orders only), best first."""
        bids, asks = self.r.levels(ex, fresh=True)
        return (no_bids_from_yes_asks([{"price": p, "quantity": n} for p, n in asks]),
                no_asks_from_yes_bids([{"price": p, "quantity": n} for p, n in bids]))

    def enter(self, b, ex, leg, fair, sig):
        _, asks = self.books(ex)
        cash = min(self.cash(), self.r.cached_balance() - config.HARD_RESERVE)
        qty, worst = strategy_e.size_buy(asks, sig["limit"], cash)
        print(f"  E {b.name}: Kalshi NO_{leg} fair {fair:.3f} (+{sig['jump']:.3f}), usual gap {sig['baseline']:+.3f}, "
              f"SUSQ should go to {sig['target']:.3f}: buy {qty} @ <= {sig['limit']} (E cash {cash:,.0f})")
        if qty < 1:
            return
        if self.send(b, ex, leg, "buy", qty, sig["limit"], "entry"):
            self.pos[ex] = {"race": b.name, "leg": leg, "entry_fair": fair, "baseline": sig["baseline"], "t": time.time()}

    def manage(self, b, ex, leg, h, now, fair, bid):
        p = self.pos.setdefault(ex, {"race": b.name, "leg": leg, "entry_fair": None, "baseline": None, "t": now})
        if p["baseline"] is None and h and h[0][0] <= now - config.E_MIN_HISTORY_S:
            p["baseline"] = strategy_e.baseline(list(h), now)   # rebuilt after a restart: usual gap from fresh history
        x = strategy_e.exit_rule(p, fair, bid)
        if x is None:
            return
        bids, _ = self.books(ex)
        qty, worst = strategy_e.size_sell(bids, x["floor"], self.ledger[ex])
        print(f"  E {b.name}: exit NO_{leg} ({x['why']}): Kalshi fair {fair:.3f}, bid {bid}, sell {qty} @ >= {x['floor']}")
        if qty >= 1:
            self.send(b, ex, leg, "sell", qty, x["floor"], "exit")
            if ex not in self.ledger:
                self.pos.pop(ex, None)

    def send(self, b, ex, leg, side, qty, price, kind):
        if self.r.live and ex in self.r.b_resting:
            self.r.cancel_all([ex])                     # never trade against our own B/C quotes
            self.r.b_resting.discard(ex)
        body = {"idempotencyKey": self.r.next_key(f"e-{kind}"), "exchangeId": ex, "side": "no", "action": side,
                "quantity": int(qty), "price": price, "expirationDate": now_plus(config.ORDER_EXPIRY_S),
                "tournamentId": self.r.tour["id"]}
        try:
            r = self.r.order("/orders", body, f"{b.name}:e-{kind}-{side}-{leg}")
        except ApiError as e:
            print(f"    E order failed: {e}")
            return 0
        self.r._cash = None
        if r is None:                                   # dry run: counted as filled at the limit
            traded = qty
        else:
            d = r.get("data", r)
            traded = d.get("quantityTraded") or 0
            if d.get("open"):
                self.r.cancel_all([ex])                 # marketable only: no resting remainder
        if traded:
            self.ledger[ex] = self.ledger.get(ex, 0) + (traded if side == "buy" else -traded)
            if self.ledger[ex] <= 0:
                del self.ledger[ex]
            self.flow += -traded * price if side == "buy" else traded * price
            print(f"    E traded {traded} {side} @ limit {price} | E holds {self.ledger.get(ex, 0)} NO_{leg}, "
                  f"cash flow {self.flow:,.2f}, E cash {self.cash():,.0f}")
        return traded
