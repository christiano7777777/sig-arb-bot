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
     swap held pairs (highest NO-bid sum first) into it when sell sum - new ask sum >= ROTATE_MIN_GAIN:
     buy the new pair FIRST (cash above HARD_RESERVE, sized to what the sellers can absorb), then
     sell only as many held pairs as were bought, at a price that keeps that gain
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
from b_executor import BExecutor
from realtime_feed import Feed
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
        self.touched = set()       # exchanges this run sent orders to (cancelled on shutdown)
        self._cash = None          # cash above reserve, read once per poll; cleared after every order
        STATE_DIR.mkdir(exist_ok=True)
        t = self.c.get(f"/tournaments/{config.TOURNAMENT_SLUG}")
        self.tour = {"id": t["id"], "slug": t["slug"]}
        found, skipped = two_party_baskets(list_markets(self.c, self.tour["slug"]))
        self.baskets = [Basket(self, b["name"], b["legs"]) for b in found]
        print(f"tournament {self.tour['slug']}  run_id {self.run_id}  mode {'LIVE' if live else 'DRY RUN'}")
        print(f"{len(self.baskets)} two-party races; skipped: " + "; ".join(f"{r} ({why})" for r, why in skipped))
        self.b_resting = set()     # exchanges with a resting strategy-B / pair-maker quote
        self.quote_live = {}       # (exchangeId, side) -> (price, qty, expiry epoch, owner) of our resting quotes
        self.b = BExecutor(self) if config.B_ENABLED else None
        # realtime feed (2026-10-04): pushed books + our account events; REST stays the fallback
        self.feed = None
        self._bulk, self._bulk_t = {}, 0.0            # last REST bulk quotes and when they were read
        self._pos, self._pos_t = None, 0.0            # positions cache for the poll (orders re-read fresh)
        self._cash_t = 0.0
        self._no_swap = {}                            # swap attempts that found nothing (see poll)
        self._swaps = []                              # times of recent swap attempts (ROTATE_MAX_PER_MIN)
        if getattr(config, "REALTIME_ENABLED", False):
            self.feed = Feed(SusqClient(), self.tour["id"])    # own client: the token mint is its only call
            self.feed.start()

    # ---- reads -----------------------------------------------------------
    def feed_live(self):
        return self.feed is not None and self.feed.healthy

    def positions(self, fresh=True):
        """exchangeId -> {"no": NO shares, "yes": YES shares, "cost": cost basis}.
        fresh=False (the poll): reuse the last read unless the feed reported our own fills/orders or it
        is older than POSITIONS_REFRESH_S. Order paths always read fresh."""
        if not fresh and self.feed_live() and self._pos is not None and not self.feed.positions_dirty.is_set()                 and time.time() - self._pos_t < config.POSITIONS_REFRESH_S:
            return self._pos
        if self.feed is not None:
            self.feed.positions_dirty.clear()
        pos = self.c.get(f"/tournaments/{self.tour['slug']}/portfolio/positions")["positions"]
        out = {}
        for p in pos:
            if p["settled"]:
                continue
            q = p["quantity"]
            out[p["exchangeId"]] = {"no": max(0.0, -q), "yes": max(0.0, q), "cost": p.get("costBasis") or 0.0}
        self._pos, self._pos_t = out, time.time()
        return out

    def balance(self):
        return self.c.get(f"/tournaments/{self.tour['slug']}")["myBalance"]

    def quotes(self):
        """exchangeId -> best YES bid/ask. With the feed live: from its pushed books, falling back to
        the last REST bulk read for exchanges it holds no book for; the bulk read is refreshed every
        BULK_REFRESH_S. Without the feed: the bulk endpoint every poll (100 exchanges per read)."""
        ids = [e for b in self.baskets for e in b.ex]
        if self.feed_live():
            if time.time() - self._bulk_t > config.BULK_REFRESH_S:
                self._bulk, self._bulk_t = self.bulk_quotes(ids), time.time()
            out = {}
            for e in ids:
                fb = self.feed.book(e)
                if fb is None:
                    out[e] = self._bulk.get(e, {})
                    continue
                bids, asks = self.minus_own(e, fb["bids"], fb["asks"])
                out[e] = {"exchangeId": e, "bestBid": bids[0][0] if bids else None, "bestAsk": asks[0][0] if asks else None}
            return out
        return self.bulk_quotes(ids)

    def minus_own(self, e, bids, asks):
        """YES ladders [(px, qty)] of exchange e without our own resting quotes, so the bot never treats
        itself as the market (joins its own lone ask, or reads its own pair quotes as an arb)."""
        def sub(levels, px, qty):
            out = []
            for p, q in levels:
                if abs(p - px) < 1e-9:
                    q -= qty
                if q > 1e-9:
                    out.append((p, q))
            return out
        for (ex, side), (price, qty, _, _) in self.quote_live.items():
            if ex == e and side == "sell":          # our NO ask at a = a YES bid at 1 - a
                bids = sub(bids, round(1 - price, 6), qty)
            elif ex == e and side == "buy":         # our NO bid at b = a YES ask at 1 - b
                asks = sub(asks, round(1 - price, 6), qty)
        return bids, asks

    def levels(self, e, fresh=True):
        """Full YES ladders of exchange e without our own quotes: the feed's book if live (and, with
        fresh=True, before any of its resting orders could expire), else REST (merged into the feed)."""
        fb = self.feed.book(e, fresh=fresh) if self.feed_live() else None
        if fb is not None:
            bids, asks = fb["bids"], fb["asks"]
        else:
            book = self.c.get(f"/exchanges/{e}/orderbook", tournamentId=self.tour["id"], depth=200)
            if self.feed is not None:
                self.feed.put_rest_book(e, book)
            bids = [(l["price"], l["quantity"]) for l in book["bids"]]
            asks = [(l["price"], l["quantity"]) for l in book["asks"]]
        return self.minus_own(e, bids, asks)

    def bulk_quotes(self, ids):
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
        self._pos_t = 0.0                       # our own order: the next poll re-reads positions
        try:
            r = self.c.post(path, body)
        finally:
            if self.feed is not None:            # our own trade changes the book before the next push:
                for l in body.get("legs", [body]):   # read REST for it until a newer push arrives
                    self.feed._drop_exchange(l["exchangeId"])
        self.tag_orders(r, body, label)
        return r

    @staticmethod
    def strategy_of(label):
        """Which strategy sent an order, from its label: A = NO+NO arbitrage (pair buys, exits, swaps,
        leg fixes), B = Kalshi-anchored trading (takes, quotes, closing), M = pair maker."""
        tag = label.split(":")[-1]
        if tag.startswith("b-maker"):
            return "M", "maker"
        if tag.startswith("b-take"):
            return "B", "take"
        if tag.startswith("b-quote"):
            return "B", "quote"
        if tag.startswith("fix"):
            return "A", "fix"
        return "A", tag.replace("pair-", "")      # buy / sell (exit or swap)

    def tag_orders(self, r, body, label):
        """Record orderId -> strategy for every order placed (state/order_tags.jsonl), so the dashboard
        can attribute each fill (GET /portfolio/fills carries orderId) to the strategy that sent it."""
        try:
            legs = body.get("legs", [body])
            datas = [x.get("data", x) for x in (r.get("results") or [r.get("data", r)])]
            strat, kind = self.strategy_of(label)
            with open(STATE_DIR / "order_tags.jsonl", "a", encoding="utf-8") as f:
                for leg, d in zip(legs, datas):
                    if d.get("orderId") is not None:
                        f.write(json.dumps({"orderId": d["orderId"], "s": strat, "k": kind, "a": leg["action"],
                                            "race": label.split(":")[0], "ex": leg["exchangeId"], "ts": now_plus(0)}) + "\n")
        except Exception as e:                  # noqa: BLE001 - bookkeeping must never break trading
            print(f"  (order tag not recorded: {e})")

    def cancel_all(self, exchange_ids):
        for ex in exchange_ids:                 # cancel-all removes our quotes there too (state also in dry runs)
            for key in [k for k in self.quote_live if k[0] == ex]:
                del self.quote_live[key]
        if not self.live:
            return
        for ex in exchange_ids:
            r = self.c.post("/orders/cancel-all", {"exchangeId": ex, "tournamentId": self.tour["id"]})
            if r.get("cancelled"):
                print(f"  cancelled {r['cancelled']} resting order(s) on exchange {ex}")

    # ---- one poll --------------------------------------------------------
    def poll(self):
        if not self.feed_live() or (self.feed.positions_dirty.is_set()
                                    or time.time() - self._cash_t > config.POSITIONS_REFRESH_S):
            self._cash = None                     # (orders clear it too)
        q = self.quotes()
        held = self.positions(fresh=False)
        if self.b is not None:      # strategy B first: its buys are worth more than an arb entry
            self.b.step(q, held)
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
            if self.out_of_budget(self.top_edge(q, b)):        # out of budget: no book reads needed
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
                    # with ~1 poll/s: do not retry a swap that found nothing until its race's quotes or
                    # our holdings change, or 30 s pass (saves the book reads)
                    key = (b.name, tuple((q.get(e, {}).get("bestBid"), q.get(e, {}).get("bestAsk")) for e in b.ex),
                           self._pos_t)
                    if self._no_swap.get(key, 0) > time.time() - 30:
                        continue
                    self._swaps = [t for t in self._swaps if t > time.time() - 60]
                    if len(self._swaps) >= config.ROTATE_MAX_PER_MIN:
                        return                      # write budget: B, maker and fixes need room too
                    self._swaps.append(time.time())
                    if self.rotate(b, q, held):
                        done += 1
                        self._no_swap[key] = time.time()   # cleared by any change in the key

    @staticmethod
    def top_edge(q, b):
        """1 - (sum of best NO asks) from the bulk quotes, or None if a leg has no bid."""
        bids = [q.get(e, {}).get("bestBid") for e in b.ex]           # YES bids -> NO asks
        return None if None in bids else 1.0 - sum(1 - x for x in bids)

    def cached_balance(self):
        """Account cash. One balance read per poll (feed live: per POSITIONS_REFRESH_S or after our own
        fills); any order clears the cache."""
        if self._cash is None:
            self._cash = self.balance()
            self._cash_t = time.time()
        return self._cash

    def cash_room(self):
        """Core budget: cash above RESERVE (usable for any edge >= MIN_EDGE)."""
        return self.cached_balance() - config.RESERVE

    def extra_room(self):
        """Extra budget: cash above HARD_RESERVE, usable only for edges >= EXTRA_MIN_EDGE (option B)."""
        return self.cached_balance() - config.HARD_RESERVE if config.EXTRA_CAPITAL_ENABLED else float("-inf")

    def out_of_budget(self, edge):
        """True if neither budget can pay for an entry at this (top-of-book) edge."""
        if self.cash_room() >= config.ROTATE_TRIGGER_CASH:
            return False
        return not (edge is not None and edge >= config.EXTRA_MIN_EDGE - 1e-9
                    and self.extra_room() >= config.ROTATE_TRIGGER_CASH)

    def sellers(self, q, held, b, floor):
        """Held races (other than b) whose best NO bids sum to >= floor, from the bulk quotes."""
        out = []
        for a in self.baskets:
            if a is b:
                continue
            pairs = min(held.get(e, {}).get("no", 0.0) for e in a.ex)
            asks = [q.get(e, {}).get("bestAsk") for e in a.ex]
            if pairs >= 1 and None not in asks and not a.paused():
                s = sum(1 - x for x in asks)
                if s >= floor - 1e-9:
                    out.append((s, a))
        return out

    def rotate(self, b, q, held):
        """Swap into race b (edge >= ROTATE_ENTRY_EDGE) out of held pairs whose current NO-bid sum
        beats b's ask sum by >= ROTATE_MIN_GAIN. Order (user, 2026-10-04):
          - enough cash above HARD_RESERVE for the whole swap -> buy first, then sell only what was
            bought (a failed buy changes nothing; a short sale leaves extra pairs bought at an edge)
          - otherwise -> sell first, then buy with what the sale freed, at a price that keeps the gain
        Returns True if it did any book reads."""
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
        p = b.plan(b.books(), min_edge=edge_needed, budget=float("inf"))
        if p is None:
            return True
        qn, lim_n, _, held_b = p
        # worst case paid per new pair = the limit sum (slack included), so held pairs must sell at
        # >= that + ROTATE_MIN_GAIN for the swap to keep its gain whatever the buy fills at
        sell_floor = sum(lim_n) + config.ROTATE_MIN_GAIN
        # read the sellers' books first and plan the sales, so the swap is no bigger than they absorb
        sales, left = [], qn
        for _, a in sorted(self.sellers(q, held, b, sell_floor), key=lambda t: -t[0]):   # cheapest to give up first
            if left < 1:
                break
            ex = a.plan_exit(a.books(), min_sum=sell_floor, max_pairs=left, allow_below_cost=True)
            if ex is not None:
                sales.append((a, ex))
                left -= ex[0]
        q_swap = qn - left
        if q_swap < 1:
            print(f"  ROTATE {b.name}: no held race can sell at >= {sell_floor:.3f}")
            return True
        budget = min(config.ROTATE_MAX_SPEND, self.cached_balance() - config.HARD_RESERVE)
        names = [a.name for a, _ in sales]
        if budget >= q_swap * sum(lim_n):
            print(f"  ROTATE {b.name}: BUY FIRST {q_swap} pairs at {lim_n} (<= {sum(lim_n):.3f}/pair), "
                  f"then sell at >= {sell_floor:.3f} from {names}")
            left = b.send_pair("buy", q_swap, lim_n, held_b)
            for a, (qs, lim_s, res_s, held_a) in sales:
                if left < 1 or STOP_FILE.exists():
                    break
                qs = min(qs, math.floor(left + 1e-9))
                print(f"  ROTATE: sell {qs} pairs of {a.name} at {lim_s} (~{res_s['avg_proceeds']:.4f}/pair) "
                      f"for {b.name}")
                left -= a.send_pair("sell", qs, lim_s, held_a)
            if left >= 1:
                print(f"  ROTATE {b.name}: {left:g} new pairs not matched by a sale; kept (bought at an edge)")
            return True
        print(f"  ROTATE {b.name}: SELL FIRST (cash {max(budget, 0):.0f} < swap ~{q_swap * sum(lim_n):.0f}) "
              f"{q_swap} pairs at >= {sell_floor:.3f} from {names}, then buy at <= {sum(lim_n):.3f}")
        sold = 0
        for a, (qs, lim_s, res_s, held_a) in sales:
            if STOP_FILE.exists():
                break
            print(f"  ROTATE: sell {qs} pairs of {a.name} at {lim_s} (~{res_s['avg_proceeds']:.4f}/pair) "
                  f"to fund {b.name}")
            sold += a.send_pair("sell", qs, lim_s, held_a)
        if sold < 1:
            return True
        # buy with what the sale freed, never above the price that keeps the swap gain
        self._cash = None
        budget = self.cached_balance() - config.HARD_RESERVE
        # min_cash=1: spend whatever the sale freed, even under the 50 "out of budget" trigger
        p = b.plan(b.books(), min_edge=max(config.ROTATE_ENTRY_EDGE, 1.0 - sum(lim_n)), budget=budget, min_cash=1)
        if p is None:
            print(f"  ROTATE {b.name}: sold {sold:g} pairs but the new pair is no longer <= {sum(lim_n):.3f}")
            return True
        qb, lim_b, _, held_b = p
        b.send_pair("buy", min(qb, math.floor(sold + 1e-9)), lim_b, held_b)
        return True


class Basket:
    """One two-party race: legs[0] = Democratic, legs[1] = Republican."""

    def __init__(self, runner, name, legs):
        self.r, self.name, self.legs = runner, name, legs
        self.ex = [l["exchange_id"] for l in legs]
        self.cash_blocked = False   # set by plan(): out of cash above the reserve
        self.paused_until = 0.0     # after a rejected pair order: leave this race alone until then
        # strategy B race: may hold unequal legs on purpose; arb trades keep that imbalance unchanged
        self.b_race = config.B_ENABLED and name in config.B_RACES

    # ---- reads -----------------------------------------------------------
    def books(self):
        """Per leg: NO asks (to buy) and NO bids (to sell), best first."""
        out = []
        for e in self.ex:
            bids, asks = self.r.levels(e, fresh=True)   # fresh pushed book or REST, without our own quotes
            out.append({"asks": no_asks_from_yes_bids([{"price": p, "quantity": q} for p, q in bids]),
                        "bids": no_bids_from_yes_asks([{"price": p, "quantity": q} for p, q in asks])})
        return out

    def holdings(self):
        """(NO shares per leg, YES shares per leg, cost basis of the race)."""
        pos = self.r.positions()
        p = [pos.get(e, {"no": 0.0, "yes": 0.0, "cost": 0.0}) for e in self.ex]
        return [x["no"] for x in p], [x["yes"] for x in p], sum(x["cost"] for x in p)

    def no_shares(self):
        return self.holdings()[0]

    # ---- planning --------------------------------------------------------
    def plan(self, books, min_edge=None, min_cash=None, budget=None):
        """Entry: return (q, limits, walk result, NO held) or None.

        Sets self.cash_blocked when the cash above the reserve (not the per-race cap) is what stops it.
        budget: spend at most this much and ignore the reserve tiers (used by a swap's buy).
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
        trigger = config.ROTATE_TRIGGER_CASH if min_cash is None else min_cash
        cash = budget if budget is not None else self.r.cash_room()
        if cash < trigger and budget is None:
            # option B: below the 50k reserve, only book levels with edge >= EXTRA_MIN_EDGE may be bought,
            # paid from the extra tier (cash above HARD_RESERVE)
            top = 1.0 - sum(lad[0][0] for lad in ladders)
            if top >= config.EXTRA_MIN_EDGE - 1e-9 and self.r.extra_room() >= trigger:
                edge = max(edge, config.EXTRA_MIN_EDGE)
                cash = self.r.extra_room()
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
        # budget check at the worst case (every share at its limit), so slack can never overspend
        if max_cost != float("inf"):
            q = min(q, math.floor(max_cost / sum(limits) + 1e-9))
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

    def paused(self):
        return time.time() < self.paused_until

    def send_pair(self, action, q, limits, before, exit_target=None):
        """One atomic multi-leg order on both legs, then cancel leftovers and check what filled.
        exit_target: the pair price a sell aimed for (break-even for repairing a one-sided sell)."""
        if self.paused():
            return 0
        base = before[0] - before[1]                 # imbalance to keep (0 except on strategy B races)
        if abs(base) > 1e-9 and not self.b_race:
            raise Halt(f"{self.name}: legs already unequal before trading: {before}")
        if self.b_race and self.r.live and self.r.b_resting & set(self.ex):
            self.r.cancel_all(self.ex)               # our own B quotes must not trade against this order
            self.r.b_resting -= set(self.ex)
        body = {"idempotencyKey": self.r.next_key(action),
                "legs": [{"exchangeId": e, "side": "no", "action": action, "quantity": int(q), "price": px,
                          "expirationDate": now_plus(config.ORDER_EXPIRY_S), "tournamentId": self.r.tour["id"]}
                         for e, px in zip(self.ex, limits)]}
        try:
            r = self.r.order("/orders/multi-leg", body, f"{self.name}:pair-{action}")
            self.r._cash = None
        except ApiError as err:
            if 400 <= err.status < 500 and err.status not in (408, 429):
                # rejected before execution (multi-leg is all-or-nothing; 4xx is not retried): nothing
                # traded. Skip this race for a minute instead of halting (2026-10-04 live halt: 400
                # VALIDATION_ERROR on a pair sell whose holdings were sufficient).
                print(f"    REJECTED, nothing traded: {err}. {self.name} paused {config.REJECT_PAUSE_S} s")
                self.paused_until = time.time() + config.REJECT_PAUSE_S
                return 0
            # outcome unknown even after the documented retries: stop, cancel, let the human look
            raise Halt(f"{self.name}: pair {action} failed: {err}. Check positions by hand.")
        if r is None:          # dry run stops here (counted as a full fill)
            return q
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
        if abs((after[0] - after[1]) - base) > 1e-9:
            target = sum(limits) if exit_target is None else exit_target
            try:
                self.fix_imbalance(after, action, limits, target, base)
            except ApiError as err:
                raise Halt(f"{self.name}: error while evening out legs: {err}. Check positions by hand.")
            after = self.no_shares()
        return abs(min(after) - min(before))       # pairs actually bought / sold

    def leg_order(self, k, action, qty, price, tag):
        body = {"idempotencyKey": self.r.next_key(tag), "exchangeId": self.ex[k], "side": "no",
                "action": action, "quantity": int(qty), "price": price,
                "expirationDate": now_plus(config.ORDER_EXPIRY_S), "tournamentId": self.r.tour["id"]}
        r = self.r.order("/orders", body, f"{self.name}:{tag}")
        self.r._cash = None
        return r

    # ---- unequal legs ----------------------------------------------------
    def fix_imbalance(self, held, action, limits, exit_target, base=0.0):
        """Legs are unequal after an order. Even them out NOW at the current book (never leave it):
        the cheaper of buying the missing leg or selling the extra leg, per share, versus what the
        original order intended. Halts only if the book cannot absorb the fix after FIX_MAX_TRIES.
        base: the imbalance (leg 0 - leg 1) the race had before the order; restored, not zeroed."""
        for attempt in range(1, config.FIX_MAX_TRIES + 1):
            d = held[0] - held[1] - base
            e = 0 if d > 0 else 1                       # leg with extra shares (relative to base)
            m = 1 - e
            diff = abs(d)
            if diff <= 1e-9:
                print("    legs back to their intended balance")
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
            try:
                if reform_loss <= unwind_loss:
                    r = self.leg_order(m, "buy", x, ceil_to_tick(buy_px, config.TICK), "fix-buy")
                    k = m
                else:
                    r = self.leg_order(e, "sell", x, floor_to_tick(sell_px, config.TICK), "fix-sell")
                    k = e
            except ApiError as err:
                if 400 <= err.status < 500 and err.status not in (408, 429):
                    print(f"    fix order rejected, nothing traded ({err}); retrying")
                    held = self.no_shares()
                    continue                    # next try re-reads the book and re-prices
                raise
            if r is None:                                 # dry run
                return
            if r.get("open"):
                self.r.cancel_all([self.ex[k]])
            held = self.no_shares()
        if abs(held[0] - held[1] - base) <= 1e-9:      # the last try fixed it (live halt 12:05: it had)
            print("    legs back to their intended balance")
            return
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
            if runner.feed_live():
                runner.feed.changed.wait(timeout=config.POLL_INTERVAL_S)   # wake on pushed changes
                runner.feed.changed.clear()
                time.sleep(config.POLL_MIN_S)                              # but at most ~1 poll / s
            else:
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
