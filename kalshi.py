"""Kalshi fair values for two-party races (public market data, no login, read-only).

For an event with a Democratic (-D) and a Republican (-R) market:
    p_D = mid_D / (mid_D + mid_R)      (removes Kalshi's own overround)
Fair NO prices on SUSQ are then 1 - p_D and 1 - p_R = p_D.
"""
import json
import urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"


def market(ticker):
    with urllib.request.urlopen(f"{BASE}/markets/{ticker}", timeout=5) as r:
        return json.load(r)["market"]


def fair(event, max_spread):
    """Return {"p": {"D": p_D, "R": p_R}, "mid": {...}, "spread": {...}, "ok": bool, "why": str}.
    ok is False (do not trade) if a market cannot be read, has no two-sided quote, or its
    bid-ask spread is wider than max_spread."""
    mid, spread = {}, {}
    for p in ("D", "R"):
        try:
            m = market(f"{event}-{p}")
            bid, ask = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
        except Exception as e:                     # network error or missing quote
            return {"ok": False, "why": f"{event}-{p}: {str(e)[:80]}"}
        if not 0 < bid < ask < 1:
            return {"ok": False, "why": f"{event}-{p}: no two-sided quote ({bid}/{ask})"}
        mid[p], spread[p] = (bid + ask) / 2, ask - bid
    total = mid["D"] + mid["R"]
    out = {"p": {"D": mid["D"] / total, "R": mid["R"] / total}, "mid": mid, "spread": spread, "ok": True, "why": ""}
    if max(spread.values()) > max_spread + 1e-9:
        out.update(ok=False, why=f"Kalshi spread {max(spread.values()):.3f} > {max_spread}")
    return out
