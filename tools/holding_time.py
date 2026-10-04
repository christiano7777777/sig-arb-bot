"""Holding time of NO+NO pairs, entry -> exit / swap sale, from the saved fill history.
Per race, pairs held = min(NO shares Dem, NO shares Rep). Fills with the same race and createdAt
(one multi-leg order) form one event. When pairs held rises, a lot opens; when it falls, lots are
closed (FIFO, LIFO also shown). A closing event is an EXIT if its sell legs sum to >= 1, a SWAP
sale if < 1, and REBALANCE if only one leg traded (evening out a short fill)."""
import json, re, sys
from collections import defaultdict
from datetime import datetime
from statistics import median
TITLE = re.compile(r"^Will the (\w+) Party win the (.+?)\??$")
rows = json.load(open(sys.argv[1]))
tr = sorted((r for r in rows if r.get("event_type") == "trade"), key=lambda r: r["createdAt"])
ev = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))     # (time, race) -> party -> [signed qty, qty*px]
order = []
for r in tr:
    g = TITLE.match(r["marketTitle"].strip()); race, party = g.group(2), g.group(1)
    k = (r["createdAt"], race)
    if k not in ev: order.append(k)
    q = abs(r["quantity"]) * (1 if r["orderType"] == "BUY" else -1)
    ev[k][party][0] += q; ev[k][party][1] += abs(q) * r["price"]
ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
def run(lifo):
    no = defaultdict(lambda: defaultdict(float)); lots = defaultdict(list); closed = []
    for k in order:
        t, race = k; before = min(no[race]["Democratic"], no[race]["Republican"])
        for p, (q, _) in ev[k].items(): no[race][p] += q
        after = min(no[race]["Democratic"], no[race]["Republican"]); d = after - before
        if d > 1e-9: lots[race].append([ts(t), d])
        elif d < -1e-9:
            sells = {p: v for p, v in ev[k].items() if v[0] < 0}
            if len(sells) == 2: S = sum(v[1] / -v[0] for v in sells.values()); kind = "exit" if S >= 1 - 1e-9 else "swap"
            else: S, kind = None, "rebalance"
            left = -d
            while left > 1e-9 and lots[race]:
                lot = lots[race][-1 if lifo else 0]; take = min(left, lot[1])
                closed.append((kind, race, (ts(t) - lot[0]).total_seconds() / 60, take, S))
                lot[1] -= take; left -= take
                if lot[1] <= 1e-9: lots[race].pop(-1 if lifo else 0)
    return closed, lots, no
def wq(xs, q):   # pair-weighted quantile of holding minutes
    xs = sorted(xs); tot = sum(w for _, w in xs); c = 0
    for m, w in xs:
        c += w
        if c >= q * tot: return m
end = ts(tr[-1]["createdAt"])
print(f"fills: {len(tr)}  {tr[0]['createdAt']} -> {tr[-1]['createdAt']}  events: {len(order)}")
for lifo in (False, True):
    closed, lots, no = run(lifo)
    print(f"\n=== {'LIFO' if lifo else 'FIFO'} ===")
    for kind in ("exit", "swap", "rebalance"):
        xs = [(m, q) for k, _, m, q, _ in closed if k == kind]
        if not xs: print(f"{kind:9}: none"); continue
        tot = sum(q for _, q in xs); mean = sum(m * q for m, q in xs) / tot
        print(f"{kind:9}: {tot:>9,.0f} pairs  mean {mean:6.1f} min  p10 {wq(xs,.1):6.1f}  median {wq(xs,.5):6.1f}  "
              f"p90 {wq(xs,.9):6.1f}  max {max(m for m,_ in xs):6.1f}")
    op = [((end - t0).total_seconds() / 60, q) for L in lots.values() for t0, q in L]
    print(f"still open: {sum(q for _,q in op):,.0f} pairs in {sum(1 for L in lots.values() if L)} races; "
          f"age median {wq(op,.5):.0f} min, oldest {max(m for m,_ in op):.0f} min")
    if not lifo:
        held = {r: min(v['Democratic'], v['Republican']) for r, v in no.items()}
        print("pairs held now (check vs dashboard 103,851):", f"{sum(held.values()):,.0f}")
        # holding time buckets for swaps
        sw = [(m, q) for k, _, m, q, _ in closed if k == "swap"]; T = sum(q for _, q in sw)
        for lo, hi in [(0,5),(5,15),(15,30),(30,60),(60,120),(120,240),(240,1e9)]:
            s = sum(q for m, q in sw if lo <= m < hi); print(f"   swap held {lo:>3}-{'' if hi>1e8 else hi:<4} min: {s/T:6.1%}")
        # by race
        br = defaultdict(lambda: [0,0.0])
        for k, r, m, q, _ in closed:
            if k == "swap": br[r][0] += q; br[r][1] += m*q
        print("   swap holding by race (top 8 by pairs):")
        for r,(q,mq) in sorted(br.items(), key=lambda x:-x[1][0])[:8]: print(f"     {r:<24}{q:>9,.0f} pairs  mean {mq/q:6.1f} min")

# Convention-free check (Little's law): mean time in system = time-average inventory / outflow rate
no = defaultdict(lambda: defaultdict(float)); inv = 0.0; area = 0.0; out = 0.0; prev = ts(order[0][0])
for k in order:
    t, race = k; tt = ts(t); area += inv * (tt - prev).total_seconds() / 60; prev = tt
    b = min(no[race]["Democratic"], no[race]["Republican"])
    for p, (q, _) in ev[k].items(): no[race][p] += q
    d = min(no[race]["Democratic"], no[race]["Republican"]) - b; inv += d
    if d < 0: out += -d
T = (prev - ts(order[0][0])).total_seconds() / 60
print(f"\nLittle's law: avg inventory {area/T:,.0f} pairs over {T/60:.1f} h, outflow {out/T:,.1f} pairs/min "
      f"-> mean holding ~ {area/out:.0f} min (biased: open pairs at the end are not counted as outflow)")
