"""Build kalshi_map.json: SUSQ race name -> Kalshi market tickers for the Democratic and Republican
outcomes (read-only; SUSQ: 2-3 reads for the market list; Kalshi: public API).
    python tools/build_kalshi_map.py <kalshi_events.json or "-" to download> > kalshi_map.json
Matching by Kalshi event title (2026 cycle only):
    "<State> Senate"     -> "<State> Senate winner?"
    "<State> Governor"   -> "<State> governor winner?"
    "XX-NN House race"   -> "XX-NN House winner?"
A race is kept only if its event has exactly one market whose rules settle on a Democratic win and
one on a Republican win (party-wins settlement, like SUSQ's markets).
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # project root
import config  # noqa: E402
from baskets import list_markets, two_party_baskets  # noqa: E402
from susq_client import SusqClient  # noqa: E402

BASE = "https://api.elections.kalshi.com/trade-api/v2"


def get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def all_events():
    ev, cur = [], ""
    while True:
        d = get(f"{BASE}/events?status=open&limit=200" + (f"&cursor={cur}" if cur else ""))
        ev += d.get("events", [])
        cur = d.get("cursor")
        if not cur:
            return ev
        time.sleep(0.25)


src = sys.argv[1] if len(sys.argv) > 1 else "-"
events = all_events() if src == "-" else json.load(open(src, encoding="utf-8"))
by_title = {}
for e in events:
    t = (e.get("title") or "").strip()
    if "(2028)" in t or "(2030)" in t or not re.search(r"-26($|[A-Z])", e["event_ticker"] + " "):
        if not e["event_ticker"].endswith("-26"):
            continue
    by_title.setdefault(t.lower(), []).append(e["event_ticker"])

races = [b["name"] for b in two_party_baskets(list_markets(SusqClient(), config.TOURNAMENT_SLUG))[0]]
out, missing = {}, []
for race in races:
    m = re.match(r"^([A-Z]{2}-(?:\d{2}|AL)) House race$", race)
    if m:
        want = f"{m.group(1)} house winner?"
    elif race.endswith(" Senate"):
        want = f"{race[:-7]} senate winner?"
    elif race.endswith(" Governor"):
        want = f"{race[:-9]} governor winner?"
    else:
        missing.append((race, "no title rule")); continue
    tickers = by_title.get(want.lower(), [])
    if len(tickers) != 1:
        missing.append((race, f"{len(tickers)} Kalshi events titled '{want}'")); continue
    d = get(f"{BASE}/events/{tickers[0]}?with_nested_markets=true")
    ms = d.get("markets") or d["event"].get("markets", [])
    party = {}
    for mk in ms:
        rule = (mk.get("rules_primary") or "")
        if mk.get("status") not in ("active", "open"):
            continue
        if re.search(r"Democratic (\(DFL\) )?party", rule, re.I):          # Minnesota: "Democratic (DFL) party"
            party.setdefault("D", []).append(mk["ticker"])
        elif re.search(r"Republican party", rule, re.I):
            party.setdefault("R", []).append(mk["ticker"])
    if len(party.get("D", [])) != 1 or len(party.get("R", [])) != 1:
        missing.append((race, f"{tickers[0]}: party markets {party}")); continue
    out[race] = {"event": tickers[0], "D": party["D"][0], "R": party["R"][0]}
    time.sleep(0.2)

json.dump(out, sys.stdout, indent=1, sort_keys=True)
print(f"\nmapped {len(out)} of {len(races)} SUSQ races", file=sys.stderr)
for r, why in missing:
    print(f"  not mapped: {r}: {why}", file=sys.stderr)
