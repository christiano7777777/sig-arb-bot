"""Read-only portfolio snapshot for the dashboard (docs/index.html).

Prints JSON: cash, every held NO+NO pair with its current edge, and the portfolio value assuming
every pair pays 1; plus the strategy-B (Kalshi-anchored market making) positions on config.B_RACES.
Three API reads (tournament, positions, bulk prices) + Kalshi's public prices for the B races.
    python tools/snapshot.py > snapshot.json
"""
import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # project root
import config  # noqa: E402
import kalshi  # noqa: E402
from susq_client import SusqClient  # noqa: E402

TITLE = re.compile(r"^Will the (\w+) Party win the (.+?)\??$")


def build(c):
    t = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}")
    cash = t["myBalance"]
    pos = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}/portfolio/positions")
    races = defaultdict(dict)
    for p in pos["positions"]:
        if p["quantity"] and not p["settled"]:
            g = TITLE.match(p["marketTitle"].strip())
            name, party = (g.group(2), g.group(1)) if g else (p["marketTitle"], p["exchangeId"])
            races[name][party] = p

    ids = [p["exchangeId"] for legs in races.values() for p in legs.values()]
    quotes = {}
    for i in range(0, len(ids), 100):
        r = c.get("/exchanges/prices", ids=",".join(ids[i:i + 100]), tournamentId=t["id"])
        quotes.update({q["exchangeId"]: q for q in r["data"]})

    rows, warnings = [], []
    for race, legs in races.items():
        q = {party: -p["quantity"] for party, p in legs.items()}        # NO shares (positive)
        b_race = getattr(config, "B_ENABLED", False) and race in config.B_RACES
        if (len(legs) != 2 or len(set(q.values())) != 1 or min(q.values()) < 0) and not b_race:
            warnings.append(f"{race}: legs {q}")          # B races hold unequal legs on purpose
        pairs = min(max(v, 0) for v in q.values())
        # cost of the PAIRS only: each leg's average cost x pairs (B races hold extra shares on one leg)
        cost = sum(p["costBasis"] * pairs / max(-p["quantity"], 1) for p in legs.values() if -p["quantity"] > 0)
        yes_asks = [quotes.get(p["exchangeId"], {}).get("bestAsk") for p in legs.values()]
        yes_bids = [quotes.get(p["exchangeId"], {}).get("bestBid") for p in legs.values()]
        sell = None if None in yes_asks else round(sum(1 - a for a in yes_asks), 4)  # NO bids sum
        buy = None if None in yes_bids else round(sum(1 - b for b in yes_bids), 4)   # NO asks sum
        rows.append({
            "race": race,
            "pairs": pairs,
            "avg_cost": round(cost / pairs, 4) if pairs else None,
            "sell_sum": sell,                                        # what selling a pair pays now
            "current_edge": None if sell is None else round(1 - sell, 4),  # given up by selling now
            "buy_sum": buy,
            "locked": round(pairs - cost, 2),
            # shares above the pair count (legs briefly unequal), valued at their own average cost
            "unpaired_value": round(sum((max(-p["quantity"], 0) - pairs) * p["costBasis"] / max(-p["quantity"], 1)
                                        for p in legs.values() if -p["quantity"] > pairs), 2),
        })
    rows.sort(key=lambda r: (r["current_edge"] is None, r["current_edge"] if r["current_edge"] is not None else 9))

    total_pairs = sum(r["pairs"] for r in rows)
    activity, recent, b_trades = trade_activity(c)
    tags = load_tags(sys.argv[1] if len(sys.argv) > 1 else None)
    attrib = tagged_fills(c, t["id"], tags)
    # strategy B trades: exact (tagged orders) since tagging began, estimated (single legs) before it
    since_tags = getattr(config, "TAGS_SINCE", "2100-01-01T00:00:00+00:00")
    b_trades = [x for x in b_trades if x["ts"] < since_tags[:19]] + attrib["C"]   # C: Kalshi market making
    b_trades.sort(key=lambda x: x["ts"])
    b = strategy_b_block(races, quotes, b_trades, cash, pos)
    if b is not None:
        b["maker"] = maker_block(attrib["B"])                                          # B: pair maker
        b["attribution"] = {k: len(v) for k, v in attrib.items()}
    return {
        "b": b,
        "activity": activity,
        "recent": recent,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cash": round(cash, 2),
        "reserve": config.RESERVE,
        "hard_reserve": getattr(config, "HARD_RESERVE", config.RESERVE),
        "extra_enabled": getattr(config, "EXTRA_CAPITAL_ENABLED", False),
        "extra_min_edge": getattr(config, "EXTRA_MIN_EDGE", None),
        "initial": t["initialBalance"],
        "pairs": total_pairs,
        "races": len(rows),
        "cost_basis": round(sum(p["costBasis"] for legs in races.values() for p in legs.values()), 2),
        # every NO+NO pair pays 1; unpaired shares (legs briefly unequal) at their cost
        "value_at_settlement": round(cash + total_pairs + sum(r["unpaired_value"] for r in rows), 2),
        "mark_to_market": round(cash + pos["summary"]["totalMarketValue"], 2),
        "warnings": warnings,
        "rows": rows,
    }


CACHE = Path(__file__).resolve().parents[1] / "state" / "trades_cache.json"
WINDOWS_H = (1, 6, 24)


def fetch_trades(c, max_pages=15):
    """Trade legs from the last 24 h. Trades already seen are cached in state/, so after the first
    call only the newest page is read (one API read)."""
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    since = datetime.now(timezone.utc).timestamp() - 24 * 3600
    cursor = None
    for _ in range(max_pages):
        r = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}/portfolio/transactions", limit=200, cursor=cursor)
        page = [t for t in r.get("data", []) if t.get("event_type") == "trade"]
        new = [t for t in page if t["event_id"] not in cache]
        for t in new:
            cache[t["event_id"]] = {k: t[k] for k in ("createdAt", "orderType", "quantity", "price", "marketTitle")}
        oldest = min((datetime.fromisoformat(t["createdAt"].replace("Z", "+00:00")).timestamp() for t in page),
                     default=0)
        pg = r.get("pagination", {})
        if len(new) < len(page) or oldest < since or not pg.get("hasMore"):
            break                      # reached trades we already have, or older than 24 h
        cursor = pg["nextCursor"]
    cache = {k: v for k, v in cache.items()
             if datetime.fromisoformat(v["createdAt"].replace("Z", "+00:00")).timestamp() >= since}
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache), encoding="utf-8")
    return list(cache.values())


def load_tags(path):
    """orderId -> [strategy A/B/M, kind, action, race] (tools/merge_tags.py), or {}."""
    try:
        return json.load(open(path, encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}


EXMAP = Path(__file__).resolve().parents[1] / "state" / "exchange_map.json"
FILLS = Path(__file__).resolve().parents[1] / "state" / "fills_cache.json"


def exchange_map(c):
    """exchangeId -> [race, party]; built once per run from the market list (2-3 reads)."""
    try:
        return json.loads(EXMAP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    from baskets import list_markets
    out = {}
    for m in list_markets(c, config.TOURNAMENT_SLUG):
        g = TITLE.match(m["title"].strip())
        if g and m.get("exchanges"):
            out[m["exchanges"][0]["id"]] = [g.group(2), g.group(1)]
    EXMAP.parent.mkdir(exist_ok=True)
    EXMAP.write_text(json.dumps(out), encoding="utf-8")
    return out


def tagged_fills(c, tid, tags, max_pages=15):
    """Our fills since strategy B went live, attributed by orderId to the strategy that placed the order.
    Fills already seen are cached in state/, so after the first call this is about one read.
    Returns {"A": [...], "B": [...], "M": [...], "untagged": [...]}; untagged fills before tagging began
    are history (counted, not attributed). Fill price is the NO price for our NO-side fills."""
    since = getattr(config, "B_LIVE_SINCE", "2100-01-01T00:00:00+00:00")
    try:
        cache = json.loads(FILLS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    cursor = None
    for _ in range(max_pages):
        r = c.get("/portfolio/fills", tournamentId=tid, limit=200, cursor=cursor)
        page = r.get("data", [])
        new = [f for f in page if str(f["id"]) not in cache]
        for f in new:
            cache[str(f["id"])] = {k: f.get(k) for k in ("orderId", "exchangeId", "price", "quantity", "side", "filledAt")}
        pg = r.get("pagination", {})
        oldest = min((f["filledAt"] for f in page), default="")
        if len(new) < len(page) or oldest < since[:19] or not pg.get("hasMore"):
            break
        cursor = pg["nextCursor"]
    FILLS.parent.mkdir(exist_ok=True)
    FILLS.write_text(json.dumps(cache), encoding="utf-8")
    ex = exchange_map(c)
    out = {"A": [], "B": [], "C": [], "untagged": []}
    for f in sorted(cache.values(), key=lambda f: f["filledAt"]):
        if f["filledAt"] < since[:19]:
            continue
        race, party = ex.get(f["exchangeId"], ["?", "?"])
        tag = tags.get(str(f["orderId"]))
        row = {"ts": f["filledAt"][:19] + "+00:00", "race": race, "party": party, "qty": abs(f["quantity"]),
               "price": round(f["price"], 4) if f["price"] is not None else None,
               "side": (tag[2] if tag else "?").upper(), "kind": tag[1] if tag else "?"}
        # strategy from the order's kind (labels changed on 2026-10-04: maker = B, Kalshi take/quote = C)
        strat = "untagged" if not tag else ("B" if tag[1] == "maker" else "C" if tag[1] in ("take", "quote") else "A")
        out[strat].append(row)
    return out


def maker_block(fills):
    """Pair-maker activity: fills per window and the most recent ones."""
    now = datetime.now(timezone.utc)
    age_h = lambda f: (now - datetime.fromisoformat(f["ts"])).total_seconds() / 3600
    win = {f"{h}h": {"fills": sum(1 for f in fills if age_h(f) <= h),
                     "shares": sum(f["qty"] for f in fills if age_h(f) <= h)} for h in WINDOWS_H}
    return {**win, "recent": fills[-25:][::-1]}


def strategy_b_block(races, quotes, b_trades, cash, pos):
    """Per B race: legs, pairs, shares at risk (pay 0 if the underdog wins), mode, Kalshi fair value,
    and the leftover leg valued at Kalshi fair vs at the SUSQ bid."""
    if not getattr(config, "B_ENABLED", False):
        return None
    out, total_risk, ev_gain = [], 0.0, 0.0
    leftover_fair_total, leftover_cost_total = [0.0], [0.0]
    cap_total = config.B_TOTAL_CAP_FRAC * (cash + sum(p["costBasis"] for legs in races.values() for p in legs.values()))
    held = [race for race in races if race in config.B_RACES]          # B trades every mapped race it holds
    with ThreadPoolExecutor(max_workers=8) as pool:
        fairs = dict(zip(held, pool.map(lambda x: kalshi.fair(config.B_RACES[x], config.B_MAX_KALSHI_SPREAD), held)))
    leg_no = {race: {party[0]: max(-p["quantity"], 0) for party, p in races[race].items()} for race in held}
    pairs_total = sum(min(v.get("D", 0.0), v.get("R", 0.0)) for v in leg_no.values()) or 1.0
    for race in held:
        legs = races[race]
        no = leg_no[race]
        d, r = no.get("D", 0.0), no.get("R", 0.0)
        k = fairs[race]
        p = k.get("p")
        fav = max(p, key=p.get) if p else None
        und = None if fav is None else ("R" if fav == "D" else "D")
        risk = (no.get(und, 0.0) - no.get(fav, 0.0)) if fav else abs(d - r)
        total_risk += max(risk, 0.0)
        left_leg = None if abs(d - r) < 1 else ("D" if d > r else "R")
        bid = None
        if left_leg:
            ex = next((x["exchangeId"] for party, x in legs.items() if party[0] == left_leg), None)
            ya = quotes.get(ex, {}).get("bestAsk")
            bid = None if ya is None else round(1 - ya, 4)
        fair_left = None if not (p and left_leg) else round(1 - p[left_leg], 4)
        extra = abs(d - r)
        if fair_left is not None:
            leftover_fair_total[0] += extra * fair_left
            leftover_cost_total[0] += sum(x["costBasis"] * extra / max(-x["quantity"], 1)
                                          for party, x in legs.items() if party[0] == left_leg)
        out.append({"race": race, "kalshi_event": config.B_RACES[race]["event"], "no_d": d, "no_r": r, "pairs": min(d, r),
                    "cap": round(cap_total * min(d, r) / pairs_total),
                    "at_risk": round(risk), "mode": "left" if d < 1 and r < 1 else ("closing" if min(d, r) < 1 else "holding"),
                    "kalshi_ok": k["ok"], "kalshi_why": k.get("why", ""),
                    "favourite": fav, "p_favourite": None if not p else round(p[fav], 4),
                    "leftover_leg": left_leg, "leftover": extra, "leftover_bid": bid, "leftover_fair": fair_left,
                    "leftover_value_fair": None if fair_left is None else round(extra * fair_left, 2),
                    "leftover_value_bid": None if bid is None else round(extra * bid, 2)})
    # expected gain of B's own trades vs Kalshi fair now (sells: price - fair; buys: fair - price)
    fair_now = {}
    for row in out:
        if row["p_favourite"] is not None:
            pf = row["p_favourite"]
            fair_now[row["race"]] = {row["favourite"]: 1 - pf, ("R" if row["favourite"] == "D" else "D"): pf}
    for t in b_trades:
        f = fair_now.get(t["race"], {}).get(t["party"][:1])
        if f is not None and t.get("price") is not None and t["side"] in ("BUY", "SELL"):
            ev_gain += t["qty"] * ((t["price"] - f) if t["side"] == "SELL" else (f - t["price"]))
    out.sort(key=lambda x: -x["pairs"])
    return {"races": out, "total_at_risk": round(total_risk), "cap_total": round(cap_total), "cap_race": "share of pairs",
            "min_favourite": config.B_MIN_FAVOURITE, "take_edge": config.B_TAKE_EDGE, "quote_edge": config.B_QUOTE_EDGE,
            "expected_gain_vs_kalshi": round(ev_gain, 2), "live_since": config.B_LIVE_SINCE,
            # leftover B shares at Kalshi fair minus at cost (value at settlement counts them at cost)
            "leftover_fair_minus_cost": round(leftover_fair_total[0] - leftover_cost_total[0], 2),
            "recent": [{**t, "ts": t["ts"]} for t in b_trades[-25:][::-1]]}


def trade_activity(c):
    """Pair the legs (same race, side of trade and size, within 5 s), then count buys, exits
    (sold at a NO-bid sum >= 1) and swap sales (sold below 1 to fund a bigger edge)."""
    legs = []
    for t in fetch_trades(c):
        g = TITLE.match(t["marketTitle"].strip())
        legs.append({"ts": datetime.fromisoformat(t["createdAt"].replace("Z", "+00:00")),
                     "act": t["orderType"], "race": g.group(2) if g else t["marketTitle"],
                     "party": g.group(1) if g else "", "qty": abs(t["quantity"]), "px": t["price"]})
    legs.sort(key=lambda x: x["ts"])
    pairs, used = [], set()
    for i, a in enumerate(legs):
        if i in used:
            continue
        for j in range(i + 1, min(i + 8, len(legs))):
            b = legs[j]
            if (j not in used and b["race"] == a["race"] and b["act"] == a["act"] and b["qty"] == a["qty"]
                    and b["party"] != a["party"] and (b["ts"] - a["ts"]).total_seconds() <= 5):
                s = a["px"] + b["px"]
                kind = "buy" if a["act"] == "BUY" else ("exit" if s >= 1 - 1e-9 else "swap")
                pairs.append({"ts": a["ts"], "kind": kind, "race": a["race"], "qty": a["qty"], "sum": s})
                used |= {i, j}
                break
    now = datetime.now(timezone.utc)
    history_h = (now - legs[0]["ts"]).total_seconds() / 3600 if legs else 0
    activity = {"history_hours": round(history_h, 2)}
    for h in WINDOWS_H:
        span = max(min(h, history_h), 1 / 60)          # divide by the time actually covered
        recent_pairs = [p for p in pairs if (now - p["ts"]).total_seconds() <= h * 3600]
        activity[f"{h}h"] = {k: {"orders": sum(p["kind"] == k for p in recent_pairs),
                                 "pairs": sum(p["qty"] for p in recent_pairs if p["kind"] == k),
                                 "per_hour": round(sum(p["kind"] == k for p in recent_pairs) / span, 2)}
                             for k in ("exit", "swap", "buy")}
        activity[f"{h}h"]["covered_hours"] = round(span, 2)
        b = [p for p in recent_pairs if p["kind"] == "buy"]
        bq = sum(p["qty"] for p in b)
        activity[f"{h}h"]["buy_edge"] = round(sum(p["qty"] * (1 - p["sum"]) for p in b) / bq, 4) if bq else None
    activity["one_legged_legs_24h"] = len(legs) - 2 * len(pairs)
    recent = [{"ts": p["ts"].isoformat(timespec="seconds"), "kind": p["kind"], "race": p["race"],
               "pairs": p["qty"], "price": round(p["sum"], 4)} for p in reversed(pairs[-25:])]
    # strategy B trades: single legs (not part of a pair) on B races since B went live
    since = datetime.fromisoformat(getattr(config, "B_LIVE_SINCE", "2100-01-01T00:00:00+00:00"))
    # an arb pair whose legs filled in unequal sizes is not paired above: both parties traded in the same
    # second -> not B (keeps the pre-tagging estimate free of arb legs)
    both = defaultdict(set)
    for i in range(len(legs)):
        if i not in used:
            both[(legs[i]["race"], legs[i]["ts"].replace(microsecond=0))].add(legs[i]["party"])
    used |= {i for i in range(len(legs)) if len(both[(legs[i]["race"], legs[i]["ts"].replace(microsecond=0))]) > 1}
    b_trades = [{"ts": legs[i]["ts"].isoformat(timespec="seconds"), "race": legs[i]["race"], "party": legs[i]["party"],
                 "side": legs[i]["act"], "qty": legs[i]["qty"], "price": round(legs[i]["px"], 4)}
                for i in range(len(legs)) if i not in used and legs[i]["ts"] >= since
                and legs[i]["race"] in getattr(config, "B_RACES", {})]
    return activity, recent, b_trades


if __name__ == "__main__":
    print(json.dumps(build(SusqClient()), indent=1))
