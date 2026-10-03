"""Read-only: cash, pairs held, value if every NO+NO pair pays 1, and the platform mark-to-market. Run: python tools/value.py"""
import re
import sys
from collections import defaultdict

sys.path.insert(0, r"C:\Users\user\Documents\SIG prediction cup")
from susq_client import SusqClient  # noqa: E402

c = SusqClient()
t = c.get("/tournaments/midterm-elections")
cash = t["myBalance"]
p = c.get("/tournaments/midterm-elections/portfolio/positions")
pat = re.compile(r"^Will the (\w+) Party win the (.+?)\??$")
race = defaultdict(dict)
for x in p["positions"]:
    if x["quantity"]:
        g = pat.match(x["marketTitle"].strip())
        race[g.group(2)][g.group(1)] = x
pairs, cost, bad = 0, 0.0, []
for r, legs in race.items():
    q = [-v["quantity"] for v in legs.values()]
    pairs += min(q)
    cost += sum(v["costBasis"] for v in legs.values())
    if len(legs) != 2 or q[0] != q[1] or min(q) < 0:
        bad.append((r, q))
init = t["initialBalance"]
mtm = p["summary"]["totalMarketValue"]
print(f"cash {cash:,.2f}")
print(f"pairs {pairs:,.0f} in {len(race)} races (unequal/YES: {bad or 'none'}), cost basis {cost:,.2f}")
print(f"value if every pair pays 1: {cash + pairs:,.2f} vs initial {init:,.0f} -> {cash + pairs - init:+,.2f}")
print(f"platform mark-to-market: positions {mtm:,.2f} -> total {cash + mtm:,.2f} ({cash + mtm - init:+,.2f})")
