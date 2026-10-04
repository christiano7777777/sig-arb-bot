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
REJECT_PAUSE_S = 60         # a race whose pair order was rejected (4xx: nothing traded) is skipped this long
FIX_MAX_TRIES = 3            # then halt (only if the book cannot absorb the fix)

# Exit: sell held NO+NO pairs when the NO bids sum to >= EXIT_MIN_SUM, to free capital
# (user chose 1.000 on 2026-10-04: capital is wanted for other markets)
EXIT_ENABLED = True
EXIT_MIN_SUM = 1.000

# Rotation (user, 2026-10-04): when out of budget and a race shows an entry edge >= ROTATE_ENTRY_EDGE,
# sell held pairs to fund it, cheapest-to-exit first (highest NO-bid sum S), only if
#   S - (ask sum of the new pair) >= ROTATE_MIN_GAIN      (net gain per pair swapped)
# Swaps may sell below the held pair's cost (user, 2026-10-04); plain exits never do.
# Sell first, always (user, 2026-10-04): every swap must release cash. The buy is at most the pairs
# sold, at <= the sale price - ROTATE_MIN_GAIN, paid only from that sale's proceeds.
ROTATE_ENABLED = True
ROTATE_ENTRY_EDGE = MIN_EDGE  # user, 2026-10-04: swap whenever the swap itself earns > 0
ROTATE_MIN_GAIN = 0.001   # user, 2026-10-04 (on the 0.005 tick this equals any gain > 0)
ROTATE_TRIGGER_CASH = 50     # "out of budget" = less than this above the reserve

# Exits are immediate (user, 2026-10-04): each poll runs ALL exits first, then only a few entries and
# swaps, so the next exit check is never more than a few seconds away.
MAX_ENTRIES_PER_POLL = 4
MAX_ROTATIONS_PER_POLL = 2
ROTATE_MAX_PER_MIN = 6       # with ~1 poll/s: at most this many swap attempts a minute (30 writes/min budget)

# --- Strategy B: Kalshi-anchored trading on held races (user, 2026-10-04) ---
# Fair value of each leg from Kalshi's mid (overround removed). Sell the leg that is rich on SUSQ,
# buy the leg that is cheap, within caps. Quotes never make the pair worse for others: our NO ask
# only at/above the leg's best NO ask; our NO bid + the other leg's best NO bid < 1.
# A race stays active while either leg has shares; it is left only when both legs are 0.
B_ENABLED = True           # user, 2026-10-04: deploy live on the 4 races
TAGS_SINCE = "2026-10-04T09:30:00+00:00"   # orders tagged by strategy from here (see Runner.tag_orders)
B_LIVE_SINCE = "2026-10-04T07:34:30+00:00"   # first live B round (dashboard counts B trades from here)
B_INTERVAL_S = 20          # one B round per 20 s (realtime); quotes rest MAKER_LIFE_S and are re-posted only on change
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
# --- Strategy C: Kalshi-anchored two-sided market making (user, 2026-10-04; strategy_c.py) ---
C_LIMIT = 2_000            # directional inventory per race: risk-adding quotes only below this
C_SKEW = 0.10              # reservation price moves this far from Kalshi fair at C_LIMIT of exposure
C_SKEW_MAX = 0.25          # ... capped here (big legacy positions: strongest pull back toward flat)
C_QUOTE_EDGE = 0.02        # quotes at least this far from the reservation price
C_CLIP = 500               # shares per quote
C_EXTRA_RACES = 5          # races we do not hold: bids on the cheap leg in the 5 with the biggest gap
B_SKEW = 0.02             # holding: reservation price moves this far from fair at full race-cap exposure (user: slight)
B_TOTAL_CAP_FRAC = 0.30    # max shares at risk over all races, as a fraction of portfolio value (user: 30%)
MAKER_ENABLED = True       # pair maker (pair_maker.py): resting two-sided pair quotes on the largest held races
MAKER_RACES = 6            # how many held races (largest by pairs) get pair quotes
MAKER_MIN_PAIRS = 500      # ... and only races holding at least this many pairs
MAKER_CLIP = 500           # pairs per side per race
MAKER_LIFE_S = 300         # quotes rest this long; re-posted only on expiry or when the best price moves
MAKER_OVER_CAP = 500       # user: a one-leg fill on the favourite may take a race at most this far over its cap
                           # (strict race cap + 500, no memory: restarts cannot ratchet it)
MAKER_MAX_ORDERS = 8       # maker writes per round (cancels + posts), inside the 30 writes/min budget
B_MAX_ORDERS_PER_ROUND = 10  # keeps B inside the 30 writes/min account budget it shares with the arb

# Execution
ORDER_EXPIRY_S = 10          # short expiry on every order = home-made IOC (API has no IOC flag)
REALTIME_ENABLED = True    # realtime feed (realtime_feed.py); False = pure REST polling as before
POLL_MIN_S = 1.0           # with the feed: polls wake on pushed changes, at most one per this many seconds
BULK_REFRESH_S = 30        # with the feed: REST bulk quotes only this often (for exchanges without a pushed book)
POSITIONS_REFRESH_S = 30   # with the feed: poll-level positions/cash re-read this often or after our own fills
POLL_INTERVAL_S = 5          # one poll = 3 bulk price reads + 1 positions read
