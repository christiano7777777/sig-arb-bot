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
import pair_maker  # noqa: E402
import strategy_c  # noqa: E402
from susq_client import SusqClient  # noqa: E402

TITLE = re.compile(r"^Will the (\w+) Party win the (.+?)\??$")


def build(c):
    t = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}")
    cash = t["myBalance"]
    pos = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}/portfolio/positions")
    tags = load_tags(sys.argv[1] if len(sys.argv) > 1 else None)
    attrib = tagged_fills(c, t["id"], tags)
    d_ledger = {}                                    # strategy D's NO shares per exchange (from its own fills)
    for f in attrib["D"]:
        d_ledger[f["ex"]] = d_ledger.get(f["ex"], 0) + (f["qty"] if f["side"] == "BUY" else -f["qty"])
    d_ledger = {e: q for e, q in d_ledger.items() if q > 0}
    races = defaultdict(dict)
    for p in pos["positions"]:
        if p["exchangeId"] in d_ledger:              # A/B/C views exclude D (its block shows them)
            share = d_ledger[p["exchangeId"]] / max(-p["quantity"], 1)
            p = {**p, "quantity": min(p["quantity"] + d_ledger[p["exchangeId"]], 0),
                 "costBasis": (p.get("costBasis") or 0) * max(0.0, 1 - share)}
        if p["quantity"] and not p["settled"]:
            g = TITLE.match(p["marketTitle"].strip())
            name, party = (g.group(2), g.group(1)) if g else (p["marketTitle"], p["exchangeId"])
            races[name][party] = p

    ids = [p["exchangeId"] for legs in races.values() for p in legs.values()]
    # both legs of every held C race (C quotes the leg we do not hold too)
    exmap = exchange_map(c)
    legs_of = {}
    for ex, (race, party) in exmap.items():
        legs_of.setdefault(race, {})[party[0]] = ex
    ids = sorted(set(ids) | {e for race in races for e in legs_of.get(race, {}).values()})   # both legs of every held race
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
        # a pair needs BOTH legs: a race holding one leg only (e.g. C's leftover after A swapped its pairs
        # out) has 0 pairs; before this fix such a leg was counted as pairs worth 1 (+~2.6k on 2026-10-04)
        pairs = min(max(v, 0) for v in q.values()) if len(q) == 2 else 0
        # cost of the PAIRS only: each leg's average cost x pairs (B races hold extra shares on one leg)
        cost = sum(p["costBasis"] * pairs / max(-p["quantity"], 1) for p in legs.values() if -p["quantity"] > 0)
        # pair prices from BOTH legs of the race (a race holding one leg only would otherwise show that
        # leg's price as the 'pair' price: Hawaii Governor read 0.04 instead of 0.955 on 2026-10-04)
        both = list(legs_of.get(race, {}).values()) if len(legs_of.get(race, {})) == 2 else [p["exchangeId"] for p in legs.values()]
        yes_asks = [quotes.get(e, {}).get("bestAsk") for e in both] if len(both) == 2 else [None]
        yes_bids = [quotes.get(e, {}).get("bestBid") for e in both] if len(both) == 2 else [None]
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
    # strategy B trades: exact (tagged orders) since tagging began, estimated (single legs) before it
    since_tags = getattr(config, "TAGS_SINCE", "2100-01-01T00:00:00+00:00")
    b_trades = [x for x in b_trades if x["ts"] < since_tags[:19]] + attrib["C"]   # C: Kalshi market making
    b_trades.sort(key=lambda x: x["ts"])
    b = strategy_b_block(races, quotes, b_trades, cash, pos)
    if b is not None:
        add_c_view(b, quotes, legs_of, cash)
        b["maker_view"] = maker_view(c, t["id"], races, legs_of, quotes, b, cash)
    if b is not None:
        b["maker"] = maker_block(attrib["B"])                                          # B: pair maker
        b["attribution"] = {k: len(v) for k, v in attrib.items()}
    d_view = strategy_d_block(c, quotes_all(c, t["id"], legs_of), legs_of, d_ledger, attrib["D"])
    d_cost = (d_view or {}).get("holdings_cost") or 0.0
    s_series, s_state = strategy_series(fetch_all_trades(c), attrib)
    s_now = strategy_now(c, t["id"], s_series, s_state, legs_of, quotes)
    d_fair_minus_cost = ((d_view or {}).get("holdings_fair") or 0.0) - d_cost if d_view and "holdings_fair" in d_view else 0.0
    return {
        "strategy_series": s_series,             # since the Cup began: A pairs 1 / legs at cost; B, C, D at cost (past)
        "strategy_now": s_now,                   # now: A as above; B, C, D at the current SUSQ market value (mid)
        "d": d_view,
        "b": b,
        "activity": activity,
        "recent": recent,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cash": round(cash, 2),
        "reserve": config.RESERVE,
        # strategy A's pairs at cost (D excluded) vs its capital cap (user: at most 50,000)
        "a_capital": round(sum(r["pairs"] * (r["avg_cost"] or 0) for r in rows), 2),
        "a_cap": getattr(config, "A_CAPITAL_CAP", None),
        "hard_reserve": getattr(config, "HARD_RESERVE", config.RESERVE),
        "extra_enabled": getattr(config, "EXTRA_CAPITAL_ENABLED", False),
        "extra_min_edge": getattr(config, "EXTRA_MIN_EDGE", None),
        "initial": t["initialBalance"],
        "pairs": total_pairs,
        "races": len(rows),
        "cost_basis": round(sum(p["costBasis"] for legs in races.values() for p in legs.values()), 2),
        # every NO+NO pair pays 1; unpaired shares (legs briefly unequal) at their cost
        # D's shares are taken out of the A/B/C views, so they are added back here (at cost, like C's positions)
        "value_at_settlement": round(cash + total_pairs + sum(r["unpaired_value"] for r in rows) + d_cost, 2),
        "value_fair": round(cash + total_pairs + sum(r["unpaired_value"] for r in rows) + d_cost
                            + ((b or {}).get("leftover_fair_minus_cost") or 0) + d_fair_minus_cost, 2),
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
    out = {"A": [], "B": [], "C": [], "D": [], "untagged": []}
    for f in sorted(cache.values(), key=lambda f: f["filledAt"]):
        if f["filledAt"] < since[:19]:
            continue
        race, party = ex.get(f["exchangeId"], ["?", "?"])
        tag = tags.get(str(f["orderId"]))
        row = {"ts": f["filledAt"][:19] + "+00:00", "race": race, "party": party, "qty": abs(f["quantity"]),
               "price": round(f["price"], 4) if f["price"] is not None else None,
               "side": (tag[2] if tag else "?").upper(), "kind": tag[1] if tag else "?", "ex": f["exchangeId"]}
        # strategy from the order's kind (labels changed on 2026-10-04: maker = B, Kalshi take/quote = C)
        strat = ("untagged" if not tag else "D" if tag[0] == "D" else "B" if tag[1] == "maker"
                 else "C" if tag[1] in ("take", "quote") else "A")
        out[strat].append(row)
    return out


def maker_block(fills):
    """Pair-maker activity: fills per window and the most recent ones."""
    now = datetime.now(timezone.utc)
    age_h = lambda f: (now - datetime.fromisoformat(f["ts"])).total_seconds() / 3600
    win = {f"{h}h": {"fills": sum(1 for f in fills if age_h(f) <= h),
                     "shares": sum(f["qty"] for f in fills if age_h(f) <= h)} for h in WINDOWS_H}
    return {**win, "recent": fills[-25:][::-1]}


def quotes_all(c, tid, legs_of):
    """Best YES bid/ask for the Senate-control market and the 35 state Senate races (bulk, 1 read)."""
    names = [config.D_CONTROL_RACE] + [f"{s} Senate" for s in config.D_RACES]
    ids = [e for n in names for e in legs_of.get(n, {}).values()]
    r = c.get("/exchanges/prices", ids=",".join(ids), tournamentId=tid) if ids else {"data": []}
    return {q["exchangeId"]: q for q in r["data"]}


ALLTRADES = Path(__file__).resolve().parents[1] / "state" / "trades_all.json"


def fetch_all_trades(c, max_pages=60):
    """Every trade since the Cup began (transactions endpoint, has BUY/SELL). Cached in state/ without
    pruning: the first call pages through the whole history, later calls read about one page."""
    try:
        cache = json.loads(ALLTRADES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    cursor = None
    for _ in range(max_pages):
        r = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}/portfolio/transactions", limit=200, cursor=cursor)
        page = [t for t in r.get("data", []) if t.get("event_type") == "trade"]
        new = [t for t in page if t["event_id"] not in cache]
        for t in new:
            cache[t["event_id"]] = {k: t[k] for k in ("createdAt", "orderType", "quantity", "price", "marketTitle")}
        pg = r.get("pagination", {})
        if (page and len(new) < len(page)) or not pg.get("hasMore"):
            break
        cursor = pg["nextCursor"]
    ALLTRADES.parent.mkdir(exist_ok=True)
    ALLTRADES.write_text(json.dumps(cache), encoding="utf-8")
    return list(cache.values())


def strategy_series(trades, attrib, step_min=10):
    """Value of each strategy since the Cup began, on a 10-minute grid plus now: its own cash flow plus
    what it holds, valued like the main 'value at settlement' (a pair pays 1, a single leg at its cost).
    Who traded: order tags since TAGS_SINCE (exact); before B went live everything was A; in between,
    two-leg pair trades are A and single legs on Kalshi-mapped races are C (estimated)."""
    from datetime import timedelta
    tagged = {}
    for s in ("A", "B", "C", "D"):
        for f in attrib.get(s, []):
            tagged[(f["ts"][:19], f["race"], f["party"], f["qty"], round(f["price"] or 0, 4))] = s
    legs = []
    for t in trades:
        g = TITLE.match(t["marketTitle"].strip())
        legs.append({"ts": t["createdAt"][:19], "act": t["orderType"], "race": g.group(2) if g else t["marketTitle"],
                     "party": g.group(1) if g else "?", "qty": abs(t["quantity"]), "px": t["price"]})
    legs.sort(key=lambda x: x["ts"])
    b_live = getattr(config, "B_LIVE_SINCE", "2100")[:19]
    tags_since = getattr(config, "TAGS_SINCE", "2100")[:19]
    by_sec = defaultdict(set)
    for x in legs:
        by_sec[(x["race"], x["ts"])].add(x["party"])
    for x in legs:
        key = (x["ts"], x["race"], x["party"], x["qty"], round(x["px"], 4))
        if key in tagged:
            x["s"] = tagged[key]
        elif x["ts"] < b_live or x["ts"] >= tags_since:
            x["s"] = "A"
        else:                                              # 07:34-09:30: pairs are A, single legs on C races are C
            x["s"] = "A" if len(by_sec[(x["race"], x["ts"])]) > 1 or x["race"] not in config.B_RACES else "C"
    state = {s: {"cash": 0.0, "legs": defaultdict(lambda: [0.0, 0.0])} for s in ("A", "B", "C", "D")}

    def value(st):
        v = st["cash"]
        races = defaultdict(dict)
        for (race, party), (q, cost) in st["legs"].items():
            races[race][party] = (q, cost)
        for legs_ in races.values():
            qs = [q for q, _ in legs_.values()]
            pairs = min(qs) if len(legs_) == 2 and min(qs) > 0 else 0
            v += pairs
            for q, cost in legs_.values():
                if q > 0:
                    v += (q - pairs) * cost / q                  # the rest of the leg at its average cost
                elif q < 0:
                    v += q * (cost / q if q else 0)              # sold more than bought here: owed at sale cost
        return v

    out, i = [], 0
    if not legs:
        return out, state
    t = datetime.fromisoformat(legs[0]["ts"] + "+00:00").replace(minute=(int(legs[0]["ts"][14:16]) // step_min) * step_min, second=0)
    end = datetime.now(timezone.utc)
    while True:
        stamp = t.isoformat()[:19]
        while i < len(legs) and legs[i]["ts"] <= stamp:
            x = legs[i]; st = state[x["s"]]; leg = st["legs"][(x["race"], x["party"])]
            if x["act"] == "BUY":
                st["cash"] -= x["qty"] * x["px"]; leg[0] += x["qty"]; leg[1] += x["qty"] * x["px"]
            else:
                st["cash"] += x["qty"] * x["px"]
                if leg[0] > 0:
                    cut = min(x["qty"], leg[0]); leg[1] -= leg[1] * cut / leg[0]; leg[0] -= cut
                    if x["qty"] > cut:
                        leg[0] -= x["qty"] - cut; leg[1] -= (x["qty"] - cut) * x["px"]
                else:
                    leg[0] -= x["qty"]; leg[1] -= x["qty"] * x["px"]
            i += 1
        out.append({"t": t.isoformat(), **{s: round(value(state[s]), 2) for s in state}})
        if t >= end:
            break
        t = min(t + timedelta(minutes=step_min), end)
    return out, state


def strategy_now(c, tid, series, state, legs_of, quotes):
    """Value of each strategy now. A: like the main curve (a pair pays 1, single legs at cost). B, C, D:
    current market value: cash flow + the NO shares each holds at the SUSQ mid (user, 2026-10-04)."""
    if not series:
        return {}
    need = sorted({legs_of.get(race, {}).get(party[0]) for s in ("B", "C", "D")
                   for (race, party), (q, _) in state[s]["legs"].items() if q} - {None} - set(quotes))
    for i in range(0, len(need), 100):
        r = c.get("/exchanges/prices", ids=",".join(need[i:i + 100]), tournamentId=tid)
        quotes.update({x["exchangeId"]: x for x in r["data"]})
    out = {"A": series[-1]["A"]}
    for s in ("B", "C", "D"):
        v = state[s]["cash"]
        for (race, party), (q, cost) in state[s]["legs"].items():
            x = quotes.get(legs_of.get(race, {}).get(party[0]), {})
            if q and x.get("bestBid") is not None and x.get("bestAsk") is not None:
                v += q * (1 - (x["bestBid"] + x["bestAsk"]) / 2)      # NO mid from the YES book
            elif q:
                v += cost                                            # no quote: keep it at cost
        out[s] = round(v, 2)
    return out


def d_cost_basis(fills, until=None):
    """Cost of what D still holds, per exchange (average cost; sells release cost pro rata)."""
    qty, cost = {}, {}
    for f in sorted(fills, key=lambda f: f["ts"]):
        if until is not None and f["ts"] > until:
            break
        e, q, px = f["ex"], f["qty"], f["price"] or 0
        if f["side"] == "BUY":
            qty[e] = qty.get(e, 0) + q; cost[e] = cost.get(e, 0) + q * px
        elif qty.get(e, 0) > 0:
            cut = min(q, qty[e]) / qty[e]
            cost[e] -= cost[e] * cut; qty[e] -= min(q, qty[e])
    return cost


def strategy_d_block(c, quotes, legs_of, ledger, fills):
    """Strategy D: model (rho, deltas) recomputed from Kalshi, D's ledger vs its hedge targets, its trades
    and P&L (cash flow + holdings at the SUSQ bid, and at Kalshi fair)."""
    if not getattr(config, "D_ENABLED", False):
        return None
    import stat_model
    races = [f"{s} Senate" for s in config.D_RACES]
    tick = {r: (config.B_RACES[r]["R"] if r in config.B_RACES else config.D_KALSHI_EXTRA.get(r)) for r in races}
    mid = lambda m: (float(m["yes_bid_dollars"]) + float(m["yes_ask_dollars"])) / 2
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            ks = list(pool.map(lambda r: kalshi.market(tick[r]), races))
        k_ctrl = kalshi.market(config.D_KALSHI_CONTROL["R"])
    except Exception as e:                                          # noqa: BLE001
        return {"error": f"Kalshi: {e}"}
    p = [mid(m) for m in ks]
    k_r = mid(k_ctrl)
    rho = stat_model.calibrate(p, k_r)
    dl = stat_model.deltas(p, rho) if rho is not None else [None] * len(p)
    noq = lambda e: {"bid": round(1 - quotes[e]["bestAsk"], 4) if quotes.get(e, {}).get("bestAsk") is not None else None,
                     "ask": round(1 - quotes[e]["bestBid"], 4) if quotes.get(e, {}).get("bestBid") is not None else None}
    ctrl = legs_of.get(config.D_CONTROL_RACE, {})
    ctrl_book = {x: noq(ctrl[x]) for x in ctrl}
    susq_r = None if not ctrl_book.get("D") or None in ctrl_book["D"].values() else (ctrl_book["D"]["bid"] + ctrl_book["D"]["ask"]) / 2
    n_d, n_r = ledger.get(ctrl.get("D"), 0), ledger.get(ctrl.get("R"), 0)
    direction = 1 if n_d > 0 else -1 if n_r > 0 else 0
    n = n_d or n_r
    hedge_leg = "R" if direction >= 0 else "D"
    rows = []
    for r, pr, d in zip(races, p, dl):
        legs = legs_of.get(r, {})
        rows.append({"race": r, "p_r": round(pr, 4), "delta": None if d is None else round(d, 4), "on_susq": len(legs) == 2,
                     "target": None if d is None or len(legs) != 2 else round(n * d),
                     "held": ledger.get(legs.get(hedge_leg), 0) if len(legs) == 2 else 0,
                     "book": noq(legs[hedge_leg]) if legs else None})
    rows.sort(key=lambda x: -(x["delta"] or 0))
    # P&L: cash flow of D's fills + what it holds now, at the SUSQ bid and at Kalshi fair
    flow = sum((-1 if f["side"] == "BUY" else 1) * f["qty"] * (f["price"] or 0) for f in fills)
    fair_no = {}
    for (r, pr) in zip(races, p):
        for x, e in legs_of.get(r, {}).items():
            fair_no[e] = pr if x == "D" else 1 - pr                     # NO on Dem pays if R wins
    for x, e in ctrl.items():
        fair_no[e] = k_r if x == "D" else 1 - k_r
    bid_val = sum(q * (noq(e)["bid"] or 0) for e, q in ledger.items())
    fair_val = sum(q * fair_no.get(e, 0) for e, q in ledger.items())
    cost_val = sum(d_cost_basis(fills).values())
    return {"kalshi_r": round(k_r, 4), "susq_r": None if susq_r is None else round(susq_r, 4),
            "gap": None if susq_r is None else round(k_r - susq_r, 4), "rho": None if rho is None else round(rho, 4),
            "direction": direction, "control": n, "entry_gap": config.D_ENTRY_GAP, "exit_gap": config.D_EXIT_GAP,
            "capital": config.D_CAPITAL, "band": config.D_BAND_FRAC, "races": rows,
            "pnl_bid": round(flow + bid_val, 2), "pnl_fair": round(flow + fair_val, 2),
            "holdings_cost": round(cost_val, 2), "holdings_fair": round(fair_val, 2),
            "recent": [{k: f[k] for k in ("ts", "race", "party", "side", "qty", "price", "kind")} for f in fills[-25:][::-1]]}


def maker_view(c, tid, races, legs_of, quotes, b, cash):
    """Strategy B (pair maker) per race it quotes: the largest mapped held races (as the bot picks them),
    their books, pair ask / bid sums, the cheapest pair in another race, and the quotes B wants now."""
    allx = sorted({e for legs in legs_of.values() for e in legs.values()} - set(quotes))
    for i in range(0, len(allx), 100):
        r = c.get("/exchanges/prices", ids=",".join(allx[i:i + 100]), tournamentId=tid)
        quotes.update({x["exchangeId"]: x for x in r["data"]})
    noq = lambda e: {"bid": round(1 - quotes[e]["bestAsk"], 4) if quotes.get(e, {}).get("bestAsk") is not None else None,
                     "ask": round(1 - quotes[e]["bestBid"], 4) if quotes.get(e, {}).get("bestBid") is not None else None}
    pair_ask = {}
    for race, legs in legs_of.items():
        if set(legs) == {"D", "R"}:
            a = [noq(legs[x])["ask"] for x in "DR"]
            if None not in a:
                pair_ask[race] = round(sum(a), 4)
    held = []
    for race, legs in races.items():
        if race in config.B_RACES and set(legs_of.get(race, {})) == {"D", "R"}:
            no = {party[0]: max(-p["quantity"], 0) for party, p in legs.items()}
            pairs = min(no.get("D", 0), no.get("R", 0))
            if pairs >= config.MAKER_MIN_PAIRS:
                held.append((pairs, race, no))
    held.sort(reverse=True)
    c_rows = {r["race"]: r for r in (b or {}).get("races", [])}
    spend = max(0.0, (cash - config.HARD_RESERVE) * (getattr(config, "CASH_SPLIT", {}) or {}).get("B", 1.0))
    out = []
    for pairs, race, no in held[:config.MAKER_RACES]:
        legs = legs_of[race]
        books = {x: noq(legs[x]) for x in "DR"}
        cheapest = min((v for n, v in pair_ask.items() if n != race), default=None)
        cr = c_rows.get(race, {})
        fav, room = "either", max(0.0, config.MAKER_OVER_CAP - abs(no.get("D", 0) - no.get("R", 0)))
        if cr.get("p_favourite") is not None and cr["p_favourite"] >= config.B_MIN_FAVOURITE:
            fav = cr["favourite"]; und = "R" if fav == "D" else "D"
            exposure = max(no.get(und, 0) - no.get(fav, 0), 0)
            room = max(0.0, config.C_LIMIT + config.MAKER_OVER_CAP - exposure)
        bk = {x: {"bids": [(books[x]["bid"], 1)] if books[x]["bid"] is not None else [],
                  "asks": [(books[x]["ask"], 1)] if books[x]["ask"] is not None else []} for x in "DR"}
        want = pair_maker.pair_quotes(bk, {"D": no.get("D", 0), "R": no.get("R", 0)}, cheapest, spend, fav, room)
        ask_sum = None if None in (books["D"]["ask"], books["R"]["ask"]) else round(books["D"]["ask"] + books["R"]["ask"], 4)
        bid_sum = None if None in (books["D"]["bid"], books["R"]["bid"]) else round(books["D"]["bid"] + books["R"]["bid"], 4)
        out.append({"race": race, "pairs": pairs, "book": books, "ask_sum": ask_sum, "bid_sum": bid_sum,
                    "cheapest_other": cheapest, "quotes": want})
    return {"races": out, "clip": config.MAKER_CLIP, "min_pairs": config.MAKER_MIN_PAIRS, "n_races": config.MAKER_RACES}


def add_c_view(b, quotes, legs_of, cash):
    """Strategy C's view of each held race (strategy_c.py, the same code the bot runs): state, exposure vs
    C_LIMIT, Kalshi fair and SUSQ book on both legs, reservation prices, and the quotes C wants now."""
    def no_book(ex):
        x = quotes.get(ex, {})
        return {"bids": [(round(1 - x["bestAsk"], 4), 1)] if x.get("bestAsk") is not None else [],
                "asks": [(round(1 - x["bestBid"], 4), 1)] if x.get("bestBid") is not None else []}
    spend = max(0.0, cash - config.HARD_RESERVE)
    total, above = 0.0, 0.0
    for row in b["races"]:
        legs = legs_of.get(row["race"], {})
        if row["p_favourite"] is None or len(legs) != 2:
            row["c"] = {"state": "no Kalshi" if row["p_favourite"] is None else "?"}
            continue
        fav = row["favourite"]
        p = {fav: row["p_favourite"], ("R" if fav == "D" else "D"): 1 - row["p_favourite"]}
        books = {x: no_book(legs[x]) for x in "DR"}
        h = {"D": row["no_d"], "R": row["no_r"]}
        res = strategy_c.quotes(books, p, h, spend)
        e = res["exposure"]
        total += max(e, 0); above += max(e - config.C_LIMIT, 0)
        adds = any(o["adds"] for o in res["orders"]); cuts = any(not o["adds"] for o in res["orders"])
        state = ("over limit: cutting only" if e >= config.C_LIMIT else "two-sided" if adds and cuts
                 else "adding" if adds else "cutting" if cuts else "idle (SUSQ near fair)")
        row["c"] = {"state": state, "exposure": round(e), "limit": config.C_LIMIT,
                    "fair": {x: round(1 - p[x], 4) for x in "DR"},
                    "book": {x: {"bid": books[x]["bids"][0][0] if books[x]["bids"] else None,
                                 "ask": books[x]["asks"][0][0] if books[x]["asks"] else None} for x in "DR"},
                    "reservation": res["reservation"],
                    "quotes": [{k: o[k] for k in ("leg", "side", "price", "qty", "adds")} for o in res["orders"]]}
    b["c_summary"] = {"exposure": round(total), "above_limit": round(above), "limit": config.C_LIMIT,
                      "skew": config.C_SKEW, "quote_edge": config.C_QUOTE_EDGE, "extra_races": config.C_EXTRA_RACES}


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
