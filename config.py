"""Settings for the arbitrage scanner / executor.

A "basket" is a set of legs that together guarantee a minimum payout per unit.
Two-party race: buy NO on the Republican market and NO on the Democratic market.
At most one party wins, so at least one NO pays 1 -> payout >= 1 per pair.
"""
import json as _json
from pathlib import Path as _Path


BASE_URL = "https://sig.thesuper.market/api/v1"
TOURNAMENT_SLUG = "midterm-elections"

# Races are discovered from market titles (baskets.py): every race with exactly one Democratic
# and one Republican market (user, 2026-10-04: "all 2 party races only"). 3-party races are skipped.
RACE_DENYLIST = set()   # e.g. {"Alaska Senate", "Georgia Senate"} to exclude a race

# scan.py (the original read-only RI scanner) still uses this
BASKETS = [
    {
        "name": "RI Senate NO+NO",
        "market_ids": [387, 388],   # which is R and which is D is checked at runtime
        "side": "no",
        "min_payout": 1.0,
    },
]

TICK = 0.005        # limit prices must sit on this grid (0.005 .. 0.995)
MIN_EDGE = 0.005    # trade only if 1 - cost >= MIN_EDGE per pair (set back to 0.005 on 2026-10-04)
TRADE_AT_ZERO_EDGE = False  # scan.py only

# --- Capital (user, 2026-10-04) ---
# The arb strategy may use only the cash above RESERVE (initial 100,000 -> 50,000 for this strategy).
# The budget is read from the live balance before every entry, so exits automatically free it again.
RESERVE = 50_000             # core budget = cash above this, for any edge >= MIN_EDGE
# Option B (user, 2026-10-04): the other 50k is used too, but only for book levels with edge >= EXTRA_MIN_EDGE,
# so each extra trade pays for an early exit later. To drain back to 50% before the stat-arb starts,
# set EXTRA_CAPITAL_ENABLED = False: the bot stops buying below RESERVE and gets cash back only from
# exits at >= 1.000 (free). HARD_RESERVE stays untouched so the unequal-fill fix always has cash.
EXTRA_CAPITAL_ENABLED = False   # user, 2026-10-04: extra tier off; arb buys only with cash above RESERVE
EXTRA_MIN_EDGE = 0.01      # user, 2026-10-04 (was 0.015): use more of the second 50k
HARD_RESERVE = 1_000
PER_RACE_CAP = None          # no per-race cap (user, 2026-10-04: higher edge gets priority instead)
MAX_UNHEDGED_EXPOSURE = 500  # worst-case SUSQies on an unpaired leg per attempt (~1% of strategy capital)

# Unequal legs (user, 2026-10-04: "NEVER DO UNEQUAL FILL")
# Prevention: limits get the slack up to the edge threshold (arb_math.widen_limits).
# If legs still end up unequal, they are evened out at once at the current book, by the cheaper of
# buying the missing leg or selling the extra one (a small realised loss is accepted).
FIX_MAX_TRIES = 3            # then halt (only if the book cannot absorb the fix)

# Exit: sell held NO+NO pairs when the NO bids sum to >= EXIT_MIN_SUM, to free capital
# (user chose 1.000 on 2026-10-04: capital is wanted for other markets)
EXIT_ENABLED = True
EXIT_MIN_SUM = 1.000

# Rotation (user, 2026-10-04): when out of budget and a race shows an entry edge >= ROTATE_ENTRY_EDGE,
# sell held pairs to fund it, cheapest-to-exit first (highest NO-bid sum S), only if
#   S - (ask sum of the new pair) >= ROTATE_MIN_GAIN      (net gain per pair swapped)
# Swaps may sell below the held pair's cost (user, 2026-10-04); plain exits never do.
# Buy first, then sell (2026-10-04): selling first left pairs sold below 1 with no buy when the new
# race's book moved (6% of swapped pairs on 2026-10-03/04). The buy is paid from cash above
# HARD_RESERVE, at most ROTATE_MAX_SPEND per swap, and sized to what the sellers' books can absorb;
# the sale then refills the cash. If the sale falls short, the extra pairs are kept (bought at an edge).
ROTATE_ENABLED = True
ROTATE_MAX_SPEND = 2_000
ROTATE_ENTRY_EDGE = MIN_EDGE  # user, 2026-10-04: swap whenever the swap itself earns > 0
ROTATE_MIN_GAIN = 0.001   # user, 2026-10-04 (on the 0.005 tick this equals any gain > 0)
ROTATE_TRIGGER_CASH = 50     # "out of budget" = less than this above the reserve

# Exits are immediate (user, 2026-10-04): each poll runs ALL exits first, then only a few entries and
# swaps, so the next exit check is never more than a few seconds away.
MAX_ENTRIES_PER_POLL = 4
MAX_ROTATIONS_PER_POLL = 2

# --- Strategy B: Kalshi-anchored trading on held races (user, 2026-10-04) ---
# Fair value of each leg from Kalshi's mid (overround removed). Sell the leg that is rich on SUSQ,
# buy the leg that is cheap, within caps. Quotes never make the pair worse for others: our NO ask
# only at/above the leg's best NO ask; our NO bid + the other leg's best NO bid < 1.
# A race stays active while either leg has shares; it is left only when both legs are 0.
B_ENABLED = True           # user, 2026-10-04: deploy live on the 4 races
B_LIVE_SINCE = "2026-10-04T07:34:30+00:00"   # first live B round (dashboard counts B trades from here)
B_INTERVAL_S = 60          # one B round per minute (quotes expire before the next round)
# SUSQ race -> Kalshi market tickers {"event", "D", "R"}, built by tools/build_kalshi_map.py (party-wins
# settlement checked). B trades every mapped race that holds shares on either leg (user, 2026-10-04).
B_RACES = _json.loads((_Path(__file__).parent / "kalshi_map.json").read_text(encoding="utf-8"))
B_MIN_FAVOURITE = 0.95     # trade a race only if Kalshi gives the favourite >= this
B_TAKE_EDGE = 0.05         # take liquidity when SUSQ price is >= this far from fair (to be set from data)
B_QUOTE_EDGE = 0.02        # rest quotes at least this far from fair (to be set from data)
B_MAX_KALSHI_SPREAD = 0.02 # Kalshi bid-ask wider than this -> fair value not trusted, no trading
B_KALSHI_JUMP = 0.02       # Kalshi mid moved more than this since the last read -> pull quotes
B_RACE_CAP = None          # per-race cap = total cap x the race's share of pairs held in B races (user, 2026-10-04)
B_CLOSE_REF = 5_000        # closing: leftover size at which the buy-back bid is zero
B_CLOSE_CLIP = 500         # closing: shares offered at the best ask per round (slow unwind)
B_CLOSE_BID_RATIO = 0.5    # closing: buy-back bid = ratio * clip * (1 - leftover / B_CLOSE_REF)
B_SKEW = 0.05             # holding: reservation price moves this far from fair at full race-cap exposure
B_TOTAL_CAP_FRAC = 0.30    # max shares at risk over all races, as a fraction of portfolio value (user: 30%)
B_MAX_ORDERS_PER_ROUND = 10  # keeps B inside the 30 writes/min account budget it shares with the arb

# Execution
ORDER_EXPIRY_S = 10          # short expiry on every order = home-made IOC (API has no IOC flag)
POLL_INTERVAL_S = 5          # one poll = 3 bulk price reads + 1 positions read
