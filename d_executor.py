"""Strategy D executor: Senate-control stat arb (strategy_d.py), run inside the arb bot's process.

Every D_INTERVAL_S: Kalshi control price + the 35 races' P(R) -> calibrate the national-swing model ->
deltas -> strategy_d.plan -> marketable limit orders (10 s expiry, remainder cancelled).

D keeps its own LEDGER of NO shares per exchange, so its control position and hedges are invisible to
A, B and C (Runner.positions subtracts it): otherwise A's leg fixes and C's risk logic would trade
against D's hedges. The ledger is updated from every D order's fill and rebuilt at start-up from D's
order tags (order_tags.json on the dashboard-data branch) plus our fills since D_LIVE_SINCE.
"""
import json
from pathlib import Path
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import config
import kalshi
import stat_model
import strategy_d
from susq_client import ApiError

TAGS_URL = "https://raw.githubusercontent.com/christiano7777777/sig-arb-bot/dashboard-data/order_tags.json"
TAGS_API = "https://api.github.com/repos/christiano7777777/sig-arb-bot/contents/order_tags.json?ref=dashboard-data"
MANUAL_TAGS = Path(__file__).parent / "tools" / "manual_tags.json"


def now_plus(seconds):
    import datetime as dt
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_tags(runner):
    """orderId -> [strategy, kind, action, race]: published tags, hand-fixed tags, this run's tags."""
    tags = {}
    for url in (TAGS_API, TAGS_URL):                   # the API is never stale; raw.githubusercontent caches ~5 min
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.raw"})
            tags = json.load(urllib.request.urlopen(req, timeout=10))
            break
        except Exception:                               # noqa: BLE001 - no tags yet: nothing to rebuild
            continue
    try:                                                # tags fixed by hand (orders whose tag was lost)
        tags.update(json.load(open(MANUAL_TAGS, encoding="utf-8")))
    except (OSError, ValueError):
        pass
    try:
        for line in open(runner.state_dir / "order_tags.jsonl", encoding="utf-8"):
            t = json.loads(line)
            tags[str(t["orderId"])] = [t["s"], t["k"], t["a"], t["race"]]
    except (OSError, AttributeError):
        pass
    return tags


class DExecutor:
    def __init__(self, runner):
        self.r = runner
        by_name = {b.name: b for b in runner.baskets}
        self.ctrl = by_name.get(config.D_CONTROL_RACE)
        # hedgeable races: on SUSQ and mapped to Kalshi; the 3 others only feed the model
        self.hedge = {f"{s} Senate": by_name[f"{s} Senate"] for s in config.D_RACES if f"{s} Senate" in by_name}
        self.k_tickers = {}
        for s in config.D_RACES:
            race = f"{s} Senate"
            if race in config.B_RACES:
                self.k_tickers[race] = config.B_RACES[race]["R"]
            elif race in config.D_KALSHI_EXTRA:
                self.k_tickers[race] = config.D_KALSHI_EXTRA[race]
        self.ex_key = {}                                   # exchangeId -> ("ctrl" | race, "D" | "R")
        if self.ctrl:
            for l in self.ctrl.legs:
                self.ex_key[l["exchange_id"]] = ("ctrl", l["party"])
        for race, b in self.hedge.items():
            for l in b.legs:
                self.ex_key[l["exchange_id"]] = (race, l["party"])
        self.ledger = {}                                   # exchangeId -> D's NO shares
        self.next_t, self.last = 0.0, {}
        missing = [f"{s} Senate" for s in config.D_RACES if f"{s} Senate" not in self.k_tickers]
        print(f"D: control market {'found' if self.ctrl else 'MISSING'}, {len(self.hedge)} hedgeable races, "
              f"{len(self.k_tickers)}/35 with Kalshi prices" + (f" (missing {missing})" if missing else ""))
        self.rebuild()

    # ---------------- ledger ----------------
    def rebuild(self):
        """D's holdings = sum of D-tagged fills since D_LIVE_SINCE (buys +, sells -)."""
        tags = load_tags(self.r)
        d_orders = {oid: t for oid, t in tags.items() if t[0] == "D"}
        if not d_orders:
            print("  D: ledger empty (no D orders yet)")
            return
        led, cursor = {}, None
        for _ in range(30):
            r = self.r.c.get("/portfolio/fills", tournamentId=self.r.tour["id"], limit=200, cursor=cursor)
            page = r.get("data", [])
            for f in page:
                t = d_orders.get(str(f.get("orderId")))
                if t and f["filledAt"] >= config.D_LIVE_SINCE[:19]:
                    led[f["exchangeId"]] = led.get(f["exchangeId"], 0) + (abs(f["quantity"]) if t[2] == "buy" else -abs(f["quantity"]))
            pg = r.get("pagination", {})
            if not pg.get("hasMore") or min((f["filledAt"] for f in page), default="") < config.D_LIVE_SINCE[:19]:
                break
            cursor = pg["nextCursor"]
        self.ledger = {e: q for e, q in led.items() if q > 0}
        print(f"  D: ledger rebuilt from {len(d_orders)} tagged orders: {self.ledger}")

    def view(self):
        """Ledger as {("ctrl"|race, leg): shares} for strategy_d.plan."""
        return {self.ex_key[e]: q for e, q in self.ledger.items() if e in self.ex_key}

    # ---------------- one round ----------------
    def step(self, q):
        if self.ctrl is None or time.time() < self.next_t:
            return
        self.next_t = time.time() + config.D_INTERVAL_S
        hd = self.r.health["D"] if hasattr(self.r, "health") else {"rounds": 0, "errors": 0}
        hd["rounds"] += 1
        try:
            self._step(q)
            hd["last_ok"] = now_plus(0)
        except ApiError as e:
            print(f"  D: API error, round skipped: {e}")
        except Exception as e:                          # noqa: BLE001 - D must never stop the bot
            hd["errors"] += 1
            hd["last_error"] = f"{now_plus(0)} {type(e).__name__}: {e}"[:300]
            print(f"  D: unexpected error, round skipped: {type(e).__name__}: {e}")

    def _step(self, q):
        races = list(self.k_tickers)
        with ThreadPoolExecutor(max_workers=8) as pool:
            ks = list(pool.map(lambda r: kalshi.market(self.k_tickers[r]), races))
            ctrl_k = pool.submit(lambda: (kalshi.market(config.D_KALSHI_CONTROL["R"]))).result()
        mid = lambda m: (float(m["yes_bid_dollars"]) + float(m["yes_ask_dollars"])) / 2
        p = {r: mid(m) for r, m in zip(races, ks)}
        if len(p) < len(config.D_RACES):
            print(f"  D: only {len(p)}/{len(config.D_RACES)} race prices, round skipped"); return
        k_r = mid(ctrl_k)
        if float(ctrl_k["yes_ask_dollars"]) - float(ctrl_k["yes_bid_dollars"]) > config.B_MAX_KALSHI_SPREAD:
            print("  D: Kalshi control spread too wide, round skipped"); return
        order = [f"{s} Senate" for s in config.D_RACES]
        plist = [p[r] for r in order]
        rho = stat_model.calibrate(plist, k_r)
        if rho is None:
            print(f"  D: model cannot reproduce Kalshi control {k_r:.3f}, round skipped"); return
        dl = dict(zip(order, stat_model.deltas(plist, rho)))
        top = lambda e: {"bid": (round(1 - q[e]["bestAsk"], 6) if q.get(e, {}).get("bestAsk") is not None else None),
                         "ask": (round(1 - q[e]["bestBid"], 6) if q.get(e, {}).get("bestBid") is not None else None)}
        ctrl_book = {l["party"]: top(l["exchange_id"]) for l in self.ctrl.legs}
        hedge_books = {r: {l["party"]: top(l["exchange_id"]) for l in b.legs} for r, b in self.hedge.items()}
        deltas = {r: d for r, d in dl.items() if r in self.hedge}
        # cash: D's CASH_SPLIT share (capped by D_CAPITAL); its missing hedges may draw on all free cash
        led = self.view()
        held_ctrl = max(led.get(("ctrl", "D"), 0), led.get(("ctrl", "R"), 0))
        hleg = "R" if led.get(("ctrl", "D"), 0) >= led.get(("ctrl", "R"), 0) else "D"
        deficit = sum(max(0.0, held_ctrl * d - led.get((r, hleg), 0)) * (hedge_books[r][hleg]["ask"] or 1.0)
                      for r, d in deltas.items() if r in hedge_books)
        live_buys = sum(px * qty for (e, side), (px, qty, _, _) in self.r.quote_live.items() if side == "buy")
        free = max(0.0, self.r.free_cash() - live_buys)
        committed = sum(self.ledger.values()) * 0.5                 # rough cost of what D holds (cap check)
        budget = min(max(min(self.r.strategy_budget("D"), config.D_CAPITAL - committed), deficit), free)
        res = strategy_d.plan(k_r, ctrl_book, deltas, hedge_books, self.view(), budget)
        self.last = {"rho": rho, "k_r": k_r, "gap": res["gap"], "direction": res["direction"], "deltas": dl,
                     "target_n": res["target_n"], "hedge_targets": res["hedge_targets"]}
        print(f"  D: Kalshi R control {k_r:.3f}, SUSQ {strategy_d.susq_p_r(ctrl_book)}, gap {res['gap']}, rho {rho:.3f}, "
              f"direction {res['direction']}, N {res['target_n']}, ledger {self.view()}")
        for o in res["orders"]:
            b = self.ctrl if o["race"] == "ctrl" else self.hedge[o["race"]]
            ex = next(l["exchange_id"] for l in b.legs if l["party"] == o["leg"])
            self.send(b, ex, o)

    def send(self, b, ex, o):
        if self.r.live and ex in self.r.b_resting:
            self.r.cancel_all([ex])                      # never trade against our own B/C quotes
            self.r.b_resting.discard(ex)
        body = {"idempotencyKey": self.r.next_key(f"d-{o['kind']}"), "exchangeId": ex, "side": "no",
                "action": o["side"], "quantity": int(o["qty"]), "price": o["price"],
                "expirationDate": now_plus(config.ORDER_EXPIRY_S), "tournamentId": self.r.tour["id"]}
        print(f"  D {b.name}: {o['kind']} {o['side']} {o['qty']} NO_{o['leg']} @ {o['price']}")
        try:
            r = self.r.order("/orders", body, f"{b.name}:d-{o['kind']}-{o['side']}-{o['leg']}")
        except ApiError as e:
            print(f"    D order failed (round continues): {e}")
            return
        self.r._cash = None
        if r is None:                                    # dry run
            return
        d = r.get("data", r)
        traded = d.get("quantityTraded") or 0
        self.ledger[ex] = max(0, self.ledger.get(ex, 0) + (traded if o["side"] == "buy" else -traded))
        print(f"    traded {traded} open={d.get('open')}")
        if d.get("open"):
            self.r.cancel_all([ex])                      # marketable only: no resting remainder
