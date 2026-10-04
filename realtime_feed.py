"""Realtime feed (Super Market / Supabase): pushed order books and our own account events, kept in a
local cache that the bot reads instead of polling REST. Best-effort by design (see the API docs,
Realtime section): every gap, reconnect or doubt falls back to REST, never to a guess.

  - one background thread with its own asyncio loop; one Supabase client; two private channels:
      tournament:{id}:markets  -> markets_batch (versioned full books per changed exchange), book_dirty,
                                  market_settled
      user:{profile_id}        -> account_batch (our fills / order updates) -> positions are stale
  - books are applied only when newer (asOf.sequence, then asOf.at); REST books can be merged too
  - a revision gap, resyncRequired, a trade without sequence, book_dirty or market_settled drops the
    affected books, so readers go to REST for them
  - expiries are not pushed: a book is "fresh" only until its nextExpiryAt. Signals may use any held
    book; sizing and resting quotes must use fresh() books (else REST)
  - token refreshed before its 3 h expiry; any error -> unhealthy, books cleared, reconnect after a pause
  - healthy == False makes the bot behave exactly as without the feed
"""
import asyncio
import re
import threading
import time
from datetime import datetime

STALE_AFTER_S = 90          # no message for this long while subscribed -> reconnect (the feed is busy)
TOKEN_REFRESH_S = 2.5 * 3600
REST_FRESH_S = 5           # a REST book carries no nextExpiryAt: treat it as fresh this long only


def _epoch(s):
    """ISO time (any number of fractional digits) -> epoch seconds."""
    if s is None:
        return None
    s = s.replace("Z", "+00:00")
    s = re.sub(r"\.(\d+)", lambda g: "." + (g.group(1) + "000000")[:6], s)
    return datetime.fromisoformat(s).timestamp()


def _version(as_of):
    """Comparable engine version (sequence, at) or None."""
    if not as_of:
        return None
    return (as_of.get("sequence") or 0, _epoch(as_of.get("at")) or 0.0)


class Feed:
    def __init__(self, client, tournament_id, log=print):
        self.c, self.tid, self.log = client, tournament_id, log
        self.lock = threading.Lock()
        self.books = {}             # exchangeId (str) -> {"v", "next", "bids", "asks", "market", "recv"}
        self.ex_market = {}         # exchangeId -> marketId (str)
        self.last_rev = {}          # marketId -> last accepted revision
        self.changed = threading.Event()          # any book or account change: wake the bot
        self.positions_dirty = threading.Event()  # our fills / orders changed: re-read positions
        self.healthy = False
        self.last_msg = 0.0
        self.stats = {"batches": 0, "books": 0, "gaps": 0, "resync": 0, "account": 0, "reconnects": 0}
        self._thread = None

    # ---------------- readers (called from the bot thread) ----------------
    def book(self, ex, fresh=False):
        """YES ladders {"bids": [(px, qty)], "asks": [...]} or None. fresh=True: only while no resting
        order in it can have expired (now < nextExpiryAt)."""
        with self.lock:
            if not self.healthy:
                return None
            b = self.books.get(str(ex))
            if b is None or (fresh and b["next"] is not None and time.time() >= b["next"]):
                return None
            return {"bids": list(b["bids"]), "asks": list(b["asks"])}

    def top(self, ex):
        """(bestBid, bestAsk) in YES terms from any held book, or None."""
        b = self.book(ex)
        if b is None:
            return None
        return (b["bids"][0][0] if b["bids"] else None, b["asks"][0][0] if b["asks"] else None)

    def put_rest_book(self, ex, ob):
        """Merge a full REST orderbook (depth=200) by version, so pushes only replace it when newer."""
        self._apply(str(ex), _version(ob.get("asOf")), None,
                    [(l["price"], l["quantity"]) for l in ob.get("bids", [])],
                    [(l["price"], l["quantity"]) for l in ob.get("asks", [])], ob.get("marketId"), rest=True)

    # ---------------- cache updates ----------------
    def _apply(self, ex, v, next_expiry, bids, asks, market, rest=False):
        with self.lock:
            held = self.books.get(ex)
            if held is not None and held["v"] is not None and v is not None and v <= held["v"]:
                return False                       # not newer: keep the higher version
            nxt = time.time() + REST_FRESH_S if rest else _epoch(next_expiry)
            self.books[ex] = {"v": v, "next": nxt, "bids": bids, "asks": asks,
                              "market": str(market) if market is not None else self.ex_market.get(ex), "recv": time.time()}
            if market is not None:
                self.ex_market[ex] = str(market)
            return True

    def _drop_market(self, market_id):
        with self.lock:
            for ex in [e for e, m in self.ex_market.items() if m == str(market_id)]:
                self.books.pop(ex, None)
        self.stats["resync"] += 1

    def _drop_exchange(self, ex):
        with self.lock:
            self.books.pop(str(ex), None)

    def on_market_batch(self, market_id, batch):
        """One market's batch (documented rules): books by version first, then revision/gap logic."""
        market_id = str(market_id)
        self.stats["batches"] += 1
        for bk in batch.get("books") or []:
            ex = str(bk["exchangeId"])
            self.ex_market[ex] = market_id
            if self._apply(ex, _version(bk.get("asOf")), bk.get("nextExpiryAt"),
                           [(l["price"], l["quantity"]) for l in bk.get("bids", [])],
                           [(l["price"], l["quantity"]) for l in bk.get("asks", [])], market_id):
                self.stats["books"] += 1
        d = batch.get("delivery") or {}
        rev, prev = d.get("revision"), d.get("previousRevision")
        last = self.last_rev.get(market_id)
        if batch.get("resyncRequired"):
            if rev is not None and (last is None or rev > last):
                self.last_rev[market_id] = rev
            self._drop_market(market_id)
        elif rev is not None and last is not None and rev <= last:
            pass                                   # duplicate: its books were still applied above
        else:
            if rev is not None:
                self.last_rev[market_id] = rev
            if last is not None and prev is not None and prev > last:
                self.stats["gaps"] += 1
                self._drop_market(market_id)       # missed a batch: REST for this market
            elif any(t.get("sequence") is None for t in batch.get("trades") or []):
                self._drop_market(market_id)
            for s in batch.get("marketSettled") or []:
                self._drop_market(s.get("marketId", market_id))
        self.changed.set()

    def on_markets_batch(self, message):
        self.last_msg = time.time()
        for entry in (message.get("payload") or {}).get("markets", []):
            self.on_market_batch(entry["marketId"], entry["batch"])

    def on_book_dirty(self, message):
        self.last_msg = time.time()
        self._drop_exchange((message.get("payload") or {}).get("exchangeId"))
        self.changed.set()

    def on_market_settled(self, message):
        self.last_msg = time.time()
        self._drop_market((message.get("payload") or {}).get("marketId"))
        self.changed.set()

    def on_account_batch(self, message):
        self.last_msg = time.time()
        p = message.get("payload") or {}
        if p.get("fills") or p.get("orderUpdates") or p.get("settlements") or p.get("refunds"):
            self.stats["account"] += 1
            self.positions_dirty.set()
            self.changed.set()

    def _reset(self, healthy):
        with self.lock:
            self.books.clear()                     # initial / re-subscription: REST is authoritative
            self.last_rev.clear()
            self.healthy = healthy
        self.changed.set()

    # ---------------- connection (background thread) ----------------
    def start(self):
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True, name="realtime")
        self._thread.start()

    async def _main(self):
        from realtime import RealtimeSubscribeStates
        from supabase import acreate_client
        while True:
            sb = None
            try:
                tok = await asyncio.to_thread(self.c.post, "/realtime/token", {})
                if not tok or not tok.get("token"):
                    raise RuntimeError("realtime token response carried no token")
                sb = await acreate_client(tok["supabaseUrl"], tok["anonKey"])
                await sb.realtime.set_auth(tok["token"])
                minted = time.time()
                subscribed = {"markets": False, "user": False}
                failed = []

                def status(name):
                    def cb(state, err):
                        if state == RealtimeSubscribeStates.SUBSCRIBED:
                            subscribed[name] = True
                            if all(subscribed.values()):
                                self.last_msg = time.time()
                                self._reset(True)          # fresh start: books come from REST / pushes
                                self.log(f"  realtime: subscribed ({', '.join(subscribed)})")
                        elif state in (RealtimeSubscribeStates.CHANNEL_ERROR, RealtimeSubscribeStates.CLOSED,
                                       RealtimeSubscribeStates.TIMED_OUT):
                            failed.append(f"{name}: {state} {err}")
                    return cb

                mk = sb.channel(f"tournament:{self.tid}:markets", {"config": {"private": True}})
                mk.on_broadcast("markets_batch", self.on_markets_batch)
                mk.on_broadcast("book_dirty", self.on_book_dirty)
                mk.on_broadcast("market_settled", self.on_market_settled)
                us = sb.channel(tok["channels"]["user"], {"config": {"private": True}})
                us.on_broadcast("account_batch", self.on_account_batch)
                await mk.subscribe(status("markets"))
                await us.subscribe(status("user"))
                while True:
                    await asyncio.sleep(5)
                    if failed:
                        raise RuntimeError("; ".join(failed))
                    if self.healthy and time.time() - self.last_msg > STALE_AFTER_S:
                        raise RuntimeError(f"no message for {STALE_AFTER_S} s")
                    if time.time() - minted > TOKEN_REFRESH_S:
                        raise RuntimeError("token refresh (planned reconnect)")
            except Exception as e:                       # noqa: BLE001 - any failure: REST fallback, retry
                self._reset(False)
                self.stats["reconnects"] += 1
                self.log(f"  realtime: down ({str(e)[:120]}); REST polling until it reconnects")
                if sb is not None:
                    try:
                        await sb.remove_all_channels()
                    except Exception:                    # noqa: BLE001
                        pass
                await asyncio.sleep(15)
