"""Exit frequency analysis from the full timestamped trade history (read-only)."""
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\user\Documents\SIG prediction cup")
from susq_client import SusqClient  # noqa: E402

HK = timezone(timedelta(hours=8))
TITLE = re.compile(r"^Will the (\w+) Party win the (.+?)\??$")
c = SusqClient()

rows, cursor = [], None
while True:
    r = c.get("/tournaments/midterm-elections/portfolio/transactions", limit=200, cursor=cursor)
    rows += [t for t in r.get("data", []) if t.get("event_type") == "trade"]
    pg = r.get("pagination", {})
    if not pg.get("hasMore"):
        break
    cursor = pg["nextCursor"]

legs = []
for t in rows:
    g = TITLE.match(t["marketTitle"].strip())
    legs.append({"ts": datetime.fromisoformat(t["createdAt"].replace("Z", "+00:00")).astimezone(HK),
                 "act": t["orderType"], "race": g.group(2), "party": g.group(1),
                 "qty": abs(t["quantity"]), "px": t["price"]})
legs.sort(key=lambda x: x["ts"])

# pair legs: same race + action + qty within 5 s
pairs, used = [], set()
for i, a in enumerate(legs):
    if i in used:
        continue
    for j in range(i + 1, min(i + 6, len(legs))):
        b = legs[j]
        if (j not in used and b["race"] == a["race"] and b["act"] == a["act"] and b["qty"] == a["qty"]
                and b["party"] != a["party"] and (b["ts"] - a["ts"]).total_seconds() <= 5):
            pairs.append({"ts": a["ts"], "act": a["act"], "race": a["race"], "qty": a["qty"], "sum": a["px"] + b["px"]})
            used |= {i, j}
            break
unpaired = [legs[i] for i in range(len(legs)) if i not in used]

buys = [p for p in pairs if p["act"] == "BUY"]
sells = [p for p in pairs if p["act"] == "SELL"]
exits = [p for p in sells if p["sum"] >= 1 - 1e-9]
swaps = [p for p in sells if p["sum"] < 1 - 1e-9]
start, end = legs[0]["ts"], legs[-1]["ts"]
hours = (end - start).total_seconds() / 3600

print(f"trade history: {len(legs)} legs, {start:%m-%d %H:%M} -> {end:%m-%d %H:%M} HKT ({hours:.1f} h)")
print(f"pairs: {len(buys)} buys, {len(exits)} exits (S >= 1), {len(swaps)} swap sales (S < 1); one-legged legs: {len(unpaired)}")
for u in unpaired:
    print(f"   one-legged: {u['ts']:%H:%M:%S} {u['act']} {u['race']} {u['party']} {u['qty']:g} @ {u['px']}")

def summary(name, xs):
    if not xs:
        print(f"\n{name}: none")
        return
    q = sum(x["qty"] for x in xs)
    print(f"\n{name}: {len(xs)} orders, {q:,.0f} pairs, {len(xs) / hours:.1f} orders/h, {q / hours:,.0f} pairs/h")
    print(f"   price (pair sum): min {min(x['sum'] for x in xs):.4f}  median {median(x['sum'] for x in xs):.4f}  "
          f"max {max(x['sum'] for x in xs):.4f}")
    gaps = [(b["ts"] - a["ts"]).total_seconds() / 60 for a, b in zip(xs, xs[1:])]
    if gaps:
        print(f"   minutes between orders: median {median(gaps):.1f}, max {max(gaps):.1f}")

summary("EXITS at S >= 1.000", exits)
summary("SWAP SALES (S < 1, funding bigger edges)", swaps)
summary("BUYS", buys)

# per race: exits and holding time (exit time minus the race's earliest still-open buy, FIFO)
print("\nexits per race (FIFO holding time from buy to exit):")
lots = defaultdict(list)          # race -> [[ts, qty]]
held_times = []
per_race = defaultdict(lambda: {"exits": 0, "pairs": 0, "swaps": 0})
for p in pairs:
    if p["act"] == "BUY":
        lots[p["race"]].append([p["ts"], p["qty"]])
        continue
    kind = "exits" if p["sum"] >= 1 - 1e-9 else "swaps"
    per_race[p["race"]][kind] += 1
    if kind == "exits":
        per_race[p["race"]]["pairs"] += p["qty"]
    left = p["qty"]
    while left > 1e-9 and lots[p["race"]]:
        lot = lots[p["race"]][0]
        take = min(left, lot[1])
        if kind == "exits":
            held_times.append(((p["ts"] - lot[0]).total_seconds() / 60, take))
        lot[1] -= take
        left -= take
        if lot[1] <= 1e-9:
            lots[p["race"]].pop(0)
for race, d in sorted(per_race.items(), key=lambda kv: -kv[1]["exits"]):
    if d["exits"]:
        print(f"   {race:<24} exits {d['exits']}  pairs {d['pairs']:,.0f}  (swap sales {d['swaps']})")
if held_times:
    w = sum(q for _, q in held_times)
    avg = sum(m * q for m, q in held_times) / w
    print(f"   pair-weighted average holding time before exit: {avg:.0f} min "
          f"(pairs held from before this history are not counted)")
races_held = {p['race'] for p in buys}
races_exited = {p['race'] for p in exits}
print(f"\nraces ever bought: {len(races_held)}, races with at least one exit: {len(races_exited)}")
