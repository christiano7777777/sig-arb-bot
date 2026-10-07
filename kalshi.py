"""Kalshi fair values for two-party races (public market data, no login, read-only).

For a race with a Democratic and a Republican market (tickers from kalshi_map.json):
    p_D = mid_D / (mid_D + mid_R)      (removes Kalshi's own overround)
Fair NO prices on SUSQ are then 1 - p_D and 1 - p_R = p_D.
"""
import json
import time
import urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"
FRESH_S = 15            # a batch-read market this recent is served from the cache instead of a new request
_CACHE = {}             # ticker -> (epoch, market), filled by batch() (price_recorder thread)


def market(ticker):
    hit = _CACHE.get(ticker)
    if hit is not None and time.time() - hit[0] <= FRESH_S:
        return hit[1]
    with urllib.request.urlopen(f"{BASE}/markets/{ticker}", timeout=5) as r:
        return json.load(r)["market"]


def batch(tickers, chunk=100):
    """Many markets in few requests (public, no key; verified 2026-10-07: 100 tickers per request, no paging).
    Refreshes the cache market() reads from. Returns {ticker: market}."""
    out = {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        with urllib.request.urlopen(f"{BASE}/markets?limit=1000&tickers={','.join(part)}", timeout=10) as r:
            ms = json.load(r).get("markets", [])
        now = time.time()
        for m in ms:
            out[m["ticker"]] = m
            _CACHE[m["ticker"]] = (now, m)
    return out


def fair(tickers, max_spread):
    """Return {"p": {"D": p_D, "R": p_R}, "mid": {...}, "spread": {...}, "ok": bool, "why": str}.
    ok is False (do not trade) if a market cannot be read, has no two-sided quote, or its
    bid-ask spread is wider than max_spread."""
    mid, spread = {}, {}
    for p in ("D", "R"):
        try:
            m = market(tickers[p])
            bid, ask = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
        except Exception as e:                     # network error or missing quote
            return {"ok": False, "why": f"{tickers[p]}: {str(e)[:80]}"}
        if not 0 < bid < ask < 1:
            return {"ok": False, "why": f"{tickers[p]}: no two-sided quote ({bid}/{ask})"}
        mid[p], spread[p] = (bid + ask) / 2, ask - bid
    total = mid["D"] + mid["R"]
    out = {"p": {"D": mid["D"] / total, "R": mid["R"] / total}, "mid": mid, "spread": spread, "ok": True, "why": ""}
    if max(spread.values()) > max_spread + 1e-9:
        out.update(ok=False, why=f"Kalshi spread {max(spread.values()):.3f} > {max_spread}")
    return out
