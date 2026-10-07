"""Kalshi batch reads + price log (user, 2026-10-07: data for strategy E, the Kalshi-jump breakout).

Every KALSHI_BATCH_S: all Kalshi tickers we map (B races, D's control and extra races) in a few batched
requests, which also fill kalshi.market()'s cache (C and D then stop reading one ticker per request).
Each cycle appends one JSON line to state/prices.jsonl with what changed since the last line:
    {"t": ISO time, "full": bool, "k": {ticker: [yes_bid, yes_ask]}, "s": {exchangeId: [best YES bid, ask]}}
"s" is the SUSQ book the bot itself last saw (without our own orders), so it costs no SUSQ reads.
A full line every PRICE_LOG_FULL_S. Read-only: places no orders; errors are printed and never stop the bot.
"""
import json
import threading
import time
from datetime import datetime, timezone

import config
import kalshi


def tickers():
    out = [m[p] for m in config.B_RACES.values() for p in ("D", "R")]
    out += list(getattr(config, "D_KALSHI_CONTROL", {}).values())
    out += list(getattr(config, "D_KALSHI_EXTRA", {}).values())
    return sorted(set(out))


class PriceRecorder(threading.Thread):
    def __init__(self, runner, path):
        super().__init__(daemon=True, name="price-recorder")
        self.r, self.path = runner, path
        self.tickers = tickers()
        self.last_k, self.last_s, self.last_full, self.last_err = {}, {}, 0.0, 0.0

    def run(self):
        while True:
            t0 = time.time()
            try:
                self.cycle()
            except Exception as e:                       # noqa: BLE001 - the recorder must never stop the bot
                if time.time() - self.last_err > 600:    # at most one line per 10 min
                    print(f"  price recorder: {type(e).__name__}: {str(e)[:120]}")
                    self.last_err = time.time()
            time.sleep(max(1.0, config.KALSHI_BATCH_S - (time.time() - t0)))

    def cycle(self):
        ms = kalshi.batch(self.tickers)
        k = {t: [float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])] for t, m in ms.items()
             if m.get("yes_bid_dollars") is not None and m.get("yes_ask_dollars") is not None}
        q = getattr(self.r, "last_q", None) or {}
        s = {e: [x.get("bestBid"), x.get("bestAsk")] for e, x in q.items()}
        full = time.time() - self.last_full >= config.PRICE_LOG_FULL_S
        dk = k if full else {t: v for t, v in k.items() if self.last_k.get(t) != v}
        ds = s if full else {e: v for e, v in s.items() if self.last_s.get(e) != v}
        self.last_k.update(k)
        self.last_s.update(s)
        if full:
            self.last_full = time.time()
        if dk or ds:
            line = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "full": full, "k": dk, "s": ds}
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, separators=(",", ":")) + "\n")
