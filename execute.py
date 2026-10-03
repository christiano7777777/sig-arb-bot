"""Executor for NO+NO arbitrage across all two-party races in the tournament.

DRY RUN BY DEFAULT: without --live it sends GET requests only and prints the orders it would send.

    python execute.py                         # dry run, poll until Ctrl-C or a STOP file appears
    python execute.py --once                  # dry run, one poll
    python execute.py --live                  # live
    python execute.py --live --max-baskets 10 # live, at most 10 pairs per attempt

Each poll:
  1. one bulk price read per 100 exchanges + one positions read
  2. races whose top of book shows an exit (NO bids sum >= EXIT_MIN_SUM and pairs held) or an
     entry (NO asks sum <= 1 - MIN_EDGE) get their full books read; exits first, then entries
     from the largest edge down
  3. rotation: if cash above the reserve is used up and a race shows an edge >= ROTATE_ENTRY_EDGE,
     sell held pairs (highest NO-bid sum first) when sell sum - new ask sum >= ROTATE_MIN_GAIN,
     never below their cost, then buy the new pair at a price that keeps that gain
Each attempt (per race):
  a. walk both books, apply caps (cash above reserve, optional per-race cap, unhedged exposure)
  b. ONE atomic multi-leg order on both legs (marketable limits, short expiry)
  c. cancel anything left resting (only if a leg reports open)
  d. re-read positions; if the legs are unequal, even them out at once at the current book
     (cheaper of buying the missing leg / selling the extra one); halt only if the book can't absorb it.
Limits get the slack up to the edge threshold so a one-tick move does not leave one leg unfilled.
Exits sell held pairs the same way, best price first, never more than held, never below cost.
Rotation sales may go below cost as long as the swap nets >= ROTATE_MIN_GAIN per pair.

Kill switch: create a file named STOP in this folder, or press Ctrl-C (live mode cancels
open orders on every exchange the bot traded this run).
"""
import argparse
import datetime as dt
import json
import math
import sys
import time
import uuid
from pathlib import Path

import config
from arb_math import (ceil_to_tick, fill_price, floor_to_tick, no_asks_from_yes_bids, no_bids_from_yes_asks,
                      walk_baskets, walk_exit, widen_limits)
from baskets import list_markets, two_party_baskets
from susq_client import ApiError, SusqClient

HERE = Path(__file__).parent
STOP_FILE = HERE / "STOP"
STATE_DIR = HERE / "state"


class Halt(Exception):
    """Stop the bot and leave the situation for the human."""


def now_plus(seconds):
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fmt_sum(xs):
    return "-" if None in xs else f"{sum(xs):.3f}"


class Runner:
    """Shared state: client, tournament, run id, order sending, positions."""

    def __init__(self, client, live, max_baskets):
        self.c, self.live, self.max_baskets = client, live, max_baskets
        self.run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.attempt = 0
        self.touched = set()
        self._cash = None          # cash above reserve, read once per poll; cleared after every order       # exchanges this run sent orders to (cancelled on shutdown)
        STATE_DIR.mkdir(exist_ok=True)
        t = self.c.get(f"/tournaments/{config.TOURNAMENT_SLUG}")
        self.tour = {"id": t["id"], "slug": t["slug"]}
        found, skipped = two_party_baskets(list_markets(self.c, self.tour["slug"]))
        self.baskets = [Basket(self, b["name"], b["legs"]) for b in found]
        print(f"tournament {self.tour['slug']}  run_id {self.run_id}  mode {'LIVE' if live else 'DRY RUN'}")
        print(f"{len(self.baskets)} two-party races; skipped: " + "; ".join(f"{r} ({why})" for r, why in skipped))

    # ---- reads -----------------------------------------------------------
    def positions(self):
        """exchangeId -> {"no": NO shares, "yes": YES shares, "cost": cost basis}."""
        pos = self.c.get(f"/tournaments/{self.tour['slug']}/portfolio/positions")["positions"]
        out = {}
        for p in pos:
            if p["settled"]:
                continue
            q = p["quantity"]
            out[p["exchangeId"]] = {"no": max(0.0, -q), "yes": max(0.0, q), "cost": p.get("costBasis") or 0.0}
        return out

    def balance(self):
        return self.c.get(f"/tournaments/{self.tour['slug']}")["myBalance"]

    def quotes(self):
        """exchangeId -> best YES bid/ask, via the bulk endpoint (100 exchanges per read)."""
        ids = [e for b in self.baskets for e in b.ex]
        out = {}
        for i in range(0, len(ids), 100):
            r = self.c.get("/exchanges/prices", ids=",".join(ids[i:i + 100]), tournamentId=self.tour["id"])
            for q in r["data"]:
                out[q["exchangeId"]] = q
        return out

    # ---- orders ----------------------------------------------------------
    def next_key(self, tag):
        self.attempt += 1
        return f"{self.run_id}-{self.attempt}-{tag}"

    def order(self, path, body, label):
        """Send an order (live) or print it (dry). The request is saved to disk before sending."""
        with open(STATE_DIR / "orders.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_plus(0), "label": label, "path": path, "body": body,
                                "live": self.live}) + "\n")
        if not self.live:
            print(f"  DRY RUN, would send {path}: {json.dumps(body)}")
            return None
        self.touched.update(l["exchangeId"] for l in body.get("legs", [body]))
        return self.c.post(path, body)

    def cancel_all(self, exchange_ids):
        if not self.live:
            return
        for ex in exchange_ids:
            r = self.c.post("/orders/cancel-all", {"exchangeId": ex, "tournamentId": self.tour["id"]})
            if r.get("cancelled"):
                print(f"  cancelled {r['cancelled']} resting order(s) on exchange {ex}")

    # ---- one poll --------------------------------------------------------
    def poll(self):
        self._cash = None
        q = self.quotes()
        held = self.positions()
        entries, exits = [], []
        for b in self.baskets:
            edge = self.top_edge(q, b)
            if edge is not None and edge >= config.MIN_EDGE - 1e-9:
                entries.append((edge, b))
            asks = [q.get(e, {}).get("bestAsk") for e in b.ex]       # YES asks -> NO bids
            pairs = min(held.get(e, {}).get("no", 0.0) for e in b.ex)
            if (config.EXIT_ENABLED and pairs >= 1 and None not in asks
                    and sum(1 - x for x in asks) >= config.EXIT_MIN_SUM - 1e-9):
                exits.append((sum(1 - x for x in asks), b))
        entries.sort(key=lambda t: -t[0])                            # higher edge first
        exits = [b for _, b in sorted(exits, key=lambda t: -t[0])]  # best sell price first
        print(f"{time.strftime('%H:%M:%S')}  {len(self.baskets)} races: "
              f"{len(entries)} entry signal(s) {[f'{b.name} {e:.3f}' for e, b in entries]}, "
              f"{len(exits)} exit signal(s) {[b.name for b in exits]}")
        # 1) every exit, best price first: exits are never delayed by entry work
        for b in exits:
            if STOP_FILE.exists():
                return
            b.try_once()
        # 2) a few entries, highest edge first. Once one is out of budget, the rest are too
        #    (cash does not grow during entries), so they go straight to the swap queue.
        blocked, tried = [], 0
        for _, b in entries:
            if b in exits:
                continue
            if STOP_FILE.exists():
                return
            if blocked:
                blocked.append(b)
                continue
            if tried >= config.MAX_ENTRIES_PER_POLL:
                break
            if self.cash_room() < config.ROTATE_TRIGGER_CASH:   # out of budget: no book reads needed
                blocked.append(b)
                continue
            tried += 1
            b.try_once()
            if b.cash_blocked:
                blocked.append(b)
        if blocked:
            print(f"  out of budget ({max(self.cash_room(), 0):.2f} above reserve) for {len(blocked)} race(s)")
        # 3) a few swaps, highest edge first
        if config.ROTATE_ENABLED:
            done = 0
            for b in blocked:
                if STOP_FILE.exists() or done >= config.MAX_ROTATIONS_PER_POLL:
                    return
                edge = self.top_edge(q, b)
                if edge is not None and edge >= config.ROTATE_ENTRY_EDGE - 1e-9:
                    if self.rotate(b, q, held):
                        done += 1

    @staticmethod
    def top_edge(q, b):
        """1 - (sum of best NO asks) from the bulk quotes, or None if a leg has no bid."""
        bids = [q.get(e, {}).get("bestBid") for e in b.ex]           # YES bids -> NO asks
        return None if None in bids else 1.0 - sum(1 - x for x in bids)

    def cash_room(self):
        """Cash above the reserve. One balance read per poll; any order clears the cache."""
        if self._cash is None:
            self._cash = self.balance() - config.RESERVE
        return self._cash

    def sellers(self, q, held, b, floor):
        """Held races (other than b) whose best NO bids sum to >= floor, from the bulk quotes."""
        out = []
        for a in self.baskets:
            if a is b:
                continue
            pairs = min(held.get(e, {}).get("no", 0.0) for e in a.ex)
            asks = [q.get(e, {}).get("bestAsk") for e in a.ex]
            if pairs >= 1 and None not in asks:
                s = sum(1 - x for x in asks)
                if s >= floor - 1e-9:
                    out.append((s, a))
        return out

    def rotate(self, b, q, held):
        """Fund race b (edge >= ROTATE_ENTRY_EDGE) by selling held pairs whose current NO-bid sum
        beats b's ask sum by >= ROTATE_MIN_GAIN. Returns True if it did any book reads."""
        # pre-check from the bulk quotes: the cheapest new pair is 1 - top edge, so no held race
        # bidding below that + ROTATE_MIN_GAIN can ever fund it. Then no book is read at all.
        c_top = 1.0 - self.top_edge(q, b)
        possible = self.sellers(q, held, b, c_top + config.ROTATE_MIN_GAIN)
        if not possible:
            return False
        # size the new pair only down to the levels the best seller can pay for:
        # buy levels priced <= best_S - ROTATE_MIN_GAIN (walking deeper would price out every seller)
        best_s = max(s for s, _ in possible)
        edge_needed = max(config.ROTATE_ENTRY_EDGE, 1.0 - (best_s - config.ROTATE_MIN_GAIN))
        p = b.plan(b.books(), min_edge=edge_needed, ignore_cash=True)
        if p is None:
            return True
        qn, _, res_n, _ = p
        # price of a new pair at the book levels it would take (before limit slack is added)
        new_sum = sum(ceil_to_tick(x, config.TICK) for x in res_n["worst_prices"])
        sell_floor = new_sum + config.ROTATE_MIN_GAIN             # held pairs must sell at >= this
        need = qn * res_n["avg_cost"] - max(0.0, self.cash_room())
        if need < 1:
            return True
        cands = self.sellers(q, held, b, sell_floor)
        if not cands:
            print(f"  ROTATE {b.name} (new pair <= {new_sum:.3f}): needs ~{need:.0f}, "
                  f"no held race bids >= {sell_floor:.3f}")
            return True
        print(f"  ROTATE {b.name} (new pair <= {new_sum:.3f}): needs ~{need:.0f}; "
              f"sellers {[f'{a.name} {s:.3f}' for s, a in cands]}")
        sold_min = None
        for _, a in sorted(cands, key=lambda t: -t[0]):           # cheapest to give up first
            if need < 1 or STOP_FILE.exists():
                break
            ex = a.plan_exit(a.books(), min_sum=sell_floor, max_pairs=math.ceil(need / sell_floor),
                             allow_below_cost=True)
            if ex is None:
                continue
            qs, lim_s, res_s, held_a = ex
            print(f"  ROTATE: sell {qs} pairs of {a.name} at {lim_s} (~{res_s['avg_proceeds']:.4f}/pair) "
                  f"to fund {b.name}")
            a.send_pair("sell", qs, lim_s, held_a)
            need -= qs * res_s["avg_proceeds"]
            sold_min = sum(lim_s) if sold_min is None else min(sold_min, sum(lim_s))
        if sold_min is None:
            return True
        # buy only at a price that keeps the swap gain, even if the book moved meanwhile
        # min_cash=1: spend whatever the sales freed, even if it is under the "out of budget" trigger
        b.try_once(min_edge=max(config.ROTATE_ENTRY_EDGE, 1.0 - (sold_min - config.ROTATE_MIN_GAIN)), min_cash=1)
        return True


class Basket:
    """One two-party race: legs[0] = Democratic, legs[1] = Republican."""

    def __init__(self, runner, name, legs):
        self.r, self.name, self.legs = runner, name, legs
        self.ex = [l["exchange_id"] for l in legs]
        self.cash_blocked = False   # set by plan(): out of cash above the reserve

    # ---- reads -----------------------------------------------------------
    def books(self):
        """Per leg: NO asks (to buy) and NO bids (to sell), best first."""
        out = []
        for e in self.ex:
            book = self.r.c.get(f"/exchanges/{e}/orderbook", tournamentId=self.r.tour["id"], depth=200)
            out.append({"asks": no_asks_from_yes_bids(book["bids"]), "bids": no_bids_from_yes_asks(book["asks"])})
        return out

    def holdings(self):
        """(NO shares per leg, YES shares per leg, cost basis of the race)."""
        pos = self.r.positions()
        p = [pos.get(e, {"no": 0.0, "yes": 0.0, "cost": 0.0}) for e in self.ex]
        return [x["no"] for x in p], [x["yes"] for x in p], sum(x["cost"] for x in p)

    def no_shares(self):
        return self.holdings()[0]

    # ---- planning --------------------------------------------------------
    def plan(self, books, min_edge=None, ignore_cash=False, min_cash=None):
        """Entry: return (q, limits, walk result, NO held) or None.

        Sets self.cash_blocked when the cash above the reserve (not the per-race cap) is what stops it.
        ignore_cash=True sizes the trade as if cash were available (used to size a rotation).
        """
        edge = config.MIN_EDGE if min_edge is None else min_edge
        ladders = [b["asks"] for b in books]
        if not all(ladders) or 1.0 - sum(lad[0][0] for lad in ladders) < edge - 1e-9:
            return None
        no, yes, race_cost = self.holdings()
        if any(yes):
            print(f"  {self.name}: skip, holds YES shares (a NO buy would net against them)")
            return None
        race_room = float("inf") if config.PER_RACE_CAP is None else config.PER_RACE_CAP - race_cost
        cash = float("inf") if ignore_cash else self.r.cash_room()
        trigger = config.ROTATE_TRIGGER_CASH if min_cash is None else min_cash
        if cash < trigger and race_room > cash:
            self.cash_blocked = True          # reported once per poll, in Runner.poll
            return None
        max_cost = max(0.0, min(race_room, cash))
        res = walk_baskets(ladders, 1.0, edge, max_baskets=self.r.max_baskets,
                           max_cost=None if max_cost == float("inf") else max_cost)
        q = res["quantity"]
        if q == 0:
            if max_cost < 1:
                print(f"  {self.name}: skip, per-race cap reached")
            return None
        limits = [ceil_to_tick(p, config.TICK) for p in res["worst_prices"]]
        # worst case: every share fills at the limit. Must still keep the required edge.
        if sum(limits) > 1.0 - edge + 1e-9:
            print(f"  {self.name}: skip, on-tick limits {limits} leave edge below {edge}")
            return None
        limits = widen_limits(limits, 1.0 - edge, config.TICK, up=True)
        # if only the most expensive leg fills, the worst-case loss is q * max(limits)
        if config.MAX_UNHEDGED_EXPOSURE is not None:
            q = min(q, math.floor(config.MAX_UNHEDGED_EXPOSURE / max(limits)))
        return (q, limits, res, no) if q > 0 else None

    def plan_exit(self, books, min_sum=None, max_pairs=None, allow_below_cost=False):
        """Exit: sell held pairs while the NO bids sum to >= min_sum (default EXIT_MIN_SUM),
        never below the race's average cost per pair unless allow_below_cost (rotation).
        Returns (q, limits, res, held) or None."""
        if not config.EXIT_ENABLED:
            return None
        min_sum = config.EXIT_MIN_SUM if min_sum is None else min_sum
        ladders = [b["bids"] for b in books]
        if not all(ladders) or sum(lad[0][0] for lad in ladders) < min_sum - 1e-9:
            return None
        held, _, race_cost = self.holdings()
        pairs = math.floor(min(held))                # never sell more than we hold (oversells flip to YES)
        if pairs <= 0:
            return None
        avg_cost = race_cost / min(held)             # cost per pair (both legs)
        floor_sum = min_sum if allow_below_cost else max(min_sum, avg_cost)   # plain exits: no loss on cost
        cap = pairs if self.r.max_baskets is None else min(pairs, self.r.max_baskets)
        if max_pairs is not None:
            cap = min(cap, max_pairs)
        res = walk_exit(ladders, floor_sum, max_baskets=cap)
        q = res["quantity"]
        if q == 0:
            return None
        limits = [floor_to_tick(p, config.TICK) for p in res["worst_prices"]]
        if sum(limits) < floor_sum - 1e-9:
            print(f"  {self.name}: skip exit, on-tick limits {limits} sum below {floor_sum:.4f}")
            return None
        limits = widen_limits(limits, floor_sum, config.TICK, up=False)
        # if only one leg sells, the other leg's shares are left unpaired
        if config.MAX_UNHEDGED_EXPOSURE is not None:
            q = min(q, math.floor(config.MAX_UNHEDGED_EXPOSURE / max(limits)))
        return (q, limits, res, held) if q > 0 else None

    # ---- one attempt -----------------------------------------------------
    def try_once(self, min_edge=None, min_cash=None):
        self.cash_blocked = False
        books = self.books()
        ex = self.plan_exit(books)
        if ex is not None:
            q, limits, res, held = ex
            print(f"  {self.name}: EXIT sell {q} of {min(held):g} pairs, limits {limits}, "
                  f"expected proceeds {res['avg_proceeds']:.4f}/pair")
            self.send_pair("sell", q, limits, held)
            return
        p = self.plan(books, min_edge=min_edge, min_cash=min_cash)
        if p is None:
            if self.cash_blocked:
                return
            buy = [b["asks"][0][0] if b["asks"] else None for b in books]
            sell = [b["bids"][0][0] if b["bids"] else None for b in books]
            print(f"  {self.name}: books show NO asks {buy} sum {fmt_sum(buy)} | "
                  f"NO bids {sell} sum {fmt_sum(sell)} -> no trade")
            return
        q, limits, res, held = p
        print(f"  {self.name}: EDGE {q} pairs, limits {limits}, "
              f"expected cost {res['avg_cost']:.4f}/pair, locked >= {q - q * res['avg_cost']:.3f}")
        self.send_pair("buy", q, limits, held)

    def send_pair(self, action, q, limits, before, exit_target=None):
        """One atomic multi-leg order on both legs, then cancel leftovers and check what filled.
        exit_target: the pair price a sell aimed for (break-even for repairing a one-sided sell)."""
        if abs(before[0] - before[1]) > 1e-9:
            raise Halt(f"{self.name}: legs already unequal before trading: {before}")
        body = {"idempotencyKey": self.r.next_key(action),
                "legs": [{"exchangeId": e, "side": "no", "action": action, "quantity": int(q), "price": px,
                          "expirationDate": now_plus(config.ORDER_EXPIRY_S), "tournamentId": self.r.tour["id"]}
                         for e, px in zip(self.ex, limits)]}
        try:
            r = self.r.order("/orders/multi-leg", body, f"{self.name}:pair-{action}")
            self.r._cash = None
        except ApiError as err:
            # outcome unknown even after the documented retries: stop, cancel, let the human look
            raise Halt(f"{self.name}: pair {action} failed: {err}. Check positions by hand.")
        if r is None:          # dry run stops here
            return
        any_open = False
        for leg in r["results"]:
            d = leg["data"]
            any_open = any_open or d["open"]
            print(f"    leg exch {d['exchangeId']}: {d.get('action', action)} {d.get('side', 'no')} "
                  f"traded {d['quantityTraded']} cost {d['totalCost']} open={d['open']} "
                  f"reason={d.get('terminalReasonCode')}")
        if any_open:
            self.r.cancel_all(self.ex)
        after = self.no_shares()
        moved = [abs(a - b) for a, b in zip(after, before)]
        print(f"    {action} per leg (from positions): {moved}  now holding {after}")
        if abs(after[0] - after[1]) > 1e-9:
            target = sum(limits) if exit_target is None else exit_target
            try:
                self.fix_imbalance(after, action, limits, target)
            except ApiError as err:
                raise Halt(f"{self.name}: error while evening out legs: {err}. Check positions by hand.")

    def leg_order(self, k, action, qty, price, tag):
        body = {"idempotencyKey": self.r.next_key(tag), "exchangeId": self.ex[k], "side": "no",
                "action": action, "quantity": int(qty), "price": price,
                "expirationDate": now_plus(config.ORDER_EXPIRY_S), "tournamentId": self.r.tour["id"]}
        r = self.r.order("/orders", body, f"{self.name}:{tag}")
        self.r._cash = None
        return r

    # ---- unequal legs ----------------------------------------------------
    def fix_imbalance(self, held, action, limits, exit_target):
        """Legs are unequal after an order. Even them out NOW at the current book (never leave it):
        the cheaper of buying the missing leg or selling the extra leg, per share, versus what the
        original order intended. Halts only if the book cannot absorb the fix after FIX_MAX_TRIES."""
        for attempt in range(1, config.FIX_MAX_TRIES + 1):
            e = 0 if held[0] > held[1] else 1           # leg with extra shares
            m = 1 - e
            diff = held[e] - held[m]
            if diff <= 1e-9:
                print("    legs equal again")
                return
            x = math.ceil(diff - 1e-9)
            books = self.books()
            buy_px = fill_price(books[m]["asks"], x)      # buy x of the missing leg
            sell_px = fill_price(books[e]["bids"], x)     # or sell x of the extra leg
            inf = float("inf")
            if action == "buy":                           # e was bought at ~limits[e]
                reform_loss = limits[e] + buy_px - 1.0 if buy_px is not None else inf
                unwind_loss = limits[e] - sell_px if sell_px is not None else inf
            else:                                         # m was sold at ~limits[m], e was not
                reform_loss = buy_px - limits[m] if buy_px is not None else inf
                unwind_loss = exit_target - (limits[m] + sell_px) if sell_px is not None else inf
            pe, pm = self.legs[e]["party"], self.legs[m]["party"]
            print(f"    UNEQUAL in {self.name} (try {attempt}): {diff:g} extra NO on {pe}. "
                  f"buy {pm} @{buy_px} -> {reform_loss:+.4f}/sh, sell {pe} @{sell_px} -> {unwind_loss:+.4f}/sh")
            if reform_loss == inf and unwind_loss == inf:
                break
            if reform_loss <= unwind_loss:
                r = self.leg_order(m, "buy", x, ceil_to_tick(buy_px, config.TICK), "fix-buy")
                k = m
            else:
                r = self.leg_order(e, "sell", x, floor_to_tick(sell_px, config.TICK), "fix-sell")
                k = e
            if r is None:                                 # dry run
                return
            if r.get("open"):
                self.r.cancel_all([self.ex[k]])
            held = self.no_shares()
        STOP_FILE.write_text(f"halted {now_plus(0)}: {self.name}: legs still unequal {held}\n")
        raise Halt(f"{self.name}: legs still unequal {held} after {config.FIX_MAX_TRIES} tries "
                   "(book too thin). Bot halted; STOP file written. Decide by hand.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually send orders")
    ap.add_argument("--max-baskets", type=int, help="optional cap on pairs per attempt")
    ap.add_argument("--once", action="store_true", help="one poll, then exit")
    ap.add_argument("--max-runtime", type=float, help="stop cleanly after this many seconds "
                    "(GitHub Actions jobs are killed at 6 h)")
    args = ap.parse_args()
    started = time.time()
    exit_code = 0

    if args.live:
        missing = [n for n in ("RESERVE", "MAX_UNHEDGED_EXPOSURE") if getattr(config, n) is None]
        if missing:
            raise SystemExit(f"--live refused: set {missing} in config.py")
    if STOP_FILE.exists():
        raise SystemExit(f"STOP file present ({STOP_FILE.read_text().strip()}). Delete it to run.")

    runner = None
    try:
        runner = Runner(SusqClient(), args.live, args.max_baskets)
        while not STOP_FILE.exists():
            try:
                runner.poll()
            except ApiError as e:   # read errors only; order errors become Halt inside send_pair
                print(f"  API error on a read: {e}")
                if e.status == 429:
                    time.sleep(float(getattr(e, "retry_after", None) or 60))
            if args.once:
                break
            if args.max_runtime and time.time() - started > args.max_runtime:
                print(f"max runtime {args.max_runtime:g}s reached")
                break
            time.sleep(config.POLL_INTERVAL_S)
    except KeyboardInterrupt:
        print("Ctrl-C")
    except Halt as h:
        print(f"HALT: {h}")
        exit_code = 3                        # tells the GitHub workflow to disable itself
        if not STOP_FILE.exists():           # keeps run_forever.ps1 from restarting into the same problem
            STOP_FILE.write_text(f"halted {now_plus(0)}: {h}\n")
    finally:
        if args.live and runner is not None and runner.touched:
            runner.cancel_all(sorted(runner.touched))
        print("stopped")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
