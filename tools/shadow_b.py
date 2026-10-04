"""Strategy B in SHADOW mode: reads only, never sends an order. Every INTERVAL seconds it reads
Kalshi fair values and the SUSQ books + positions of the B_RACES, runs strategy_b.decide, and
appends what it WOULD do to a JSONL log (one line per race per tick).
    python tools/shadow_b.py <out.jsonl> [minutes] [interval_s]
SUSQ reads per tick: 1 positions + 1 balance + 2 books per race (10 for 4 races); the live bot
shares the 100 reads/min budget, so keep the interval >= 60 s while it runs.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # project root
import config  # noqa: E402
import kalshi  # noqa: E402
import strategy_b  # noqa: E402
from arb_math import no_asks_from_yes_bids, no_bids_from_yes_asks  # noqa: E402
from baskets import list_markets, two_party_baskets  # noqa: E402
from susq_client import ApiError, SusqClient  # noqa: E402

out_path = sys.argv[1]
minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 60
interval = int(sys.argv[3]) if len(sys.argv) > 3 else 60

c = SusqClient()
tour = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}")
legs = {b["name"]: {l["party"]: l["exchange_id"] for l in b["legs"]}
        for b in two_party_baskets(list_markets(c, config.TOURNAMENT_SLUG))[0] if b["name"] in config.B_RACES}
missing = set(config.B_RACES) - set(legs)
if missing:
    raise SystemExit(f"races not found on SUSQ: {missing}")
last_mid = {}
end = time.time() + minutes * 60


def tick():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pos = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}/portfolio/positions")["positions"]
    no = {p["exchangeId"]: max(0.0, -p["quantity"]) for p in pos if not p["settled"]}
    cost = sum(p.get("costBasis") or 0.0 for p in pos if not p["settled"])
    cash = c.get(f"/tournaments/{config.TOURNAMENT_SLUG}")["myBalance"]
    cap_total = config.B_TOTAL_CAP_FRAC * (cash + cost)          # ~ portfolio value
    races, used = {}, 0.0
    for race, event in config.B_RACES.items():
        k = kalshi.fair(event, config.B_MAX_KALSHI_SPREAD)
        held = {x: no.get(legs[race][x], 0.0) for x in "DR"}
        books = {}
        for x in "DR":
            ob = c.get(f"/exchanges/{legs[race][x]}/orderbook", tournamentId=tour["id"], depth=50)
            books[x] = {"asks": no_asks_from_yes_bids(ob["bids"]), "bids": no_bids_from_yes_asks(ob["asks"])}
            time.sleep(0.5)
        jump = False
        if k["ok"]:
            prev = last_mid.get(race)
            jump = prev is not None and max(abs(k["mid"][x] - prev[x]) for x in "DR") > config.B_KALSHI_JUMP
            last_mid[race] = k["mid"]
        races[race] = (k, held, books, jump)
        if k.get("ok"):
            fav = max(k["p"], key=k["p"].get)
            und = "R" if fav == "D" else "D"
            used += max(held[und] - held[fav], 0.0)
    room_total = max(0.0, cap_total - used)
    with open(out_path, "a", encoding="utf-8") as f:
        for race, (k, held, books, jump) in races.items():
            res = strategy_b.decide(books, k.get("p", {"D": 0.5, "R": 0.5}), held, room_total,
                                    kalshi_ok=k["ok"], kalshi_jump=jump)
            if k.get("ok"):                     # later races only get the room this race left over
                fav = max(k["p"], key=k["p"].get)
                room_total -= sum(o["qty"] for o in res["orders"]
                                  if (o["leg"] == fav) == (o["side"] == "sell"))   # sell NO_f / buy NO_u
            f.write(json.dumps({"t": now, "race": race, "kalshi": k, "held": held, "jump": jump,
                                "top": {x: {"bid": books[x]["bids"][:1], "ask": books[x]["asks"][:1]} for x in "DR"},
                                "cash": cash, "cap_total": round(cap_total), **res}) + "\n")
            summary = "; ".join(f"{o['kind']} {o['side']} {o['qty']} NO_{o['leg']} @{o['price']} "
                                f"({o['edge_vs_fair']:+.3f} vs fair)" for o in res["orders"]) or res["why"] or "nothing"
            print(f"{now} {race:<22} held D/R {held['D']:.0f}/{held['R']:.0f}  {summary}")


while time.time() < end:
    t0 = time.time()
    try:
        tick()
    except ApiError as e:                       # e.g. 429 while the live bot is busy: skip this tick
        print(f"{datetime.now(timezone.utc):%H:%M:%S} tick skipped: {e}")
    time.sleep(max(0, interval - (time.time() - t0)))
