"""Settings for the arbitrage scanner / executor.

A "basket" is a set of legs that together guarantee a minimum payout per unit.
Two-party race: buy NO on the Republican market and NO on the Democratic market.
At most one party wins, so at least one NO pays 1 -> payout >= 1 per pair.
"""

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
RESERVE = 50_000             # SUSQies never touched
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
ROTATE_ENABLED = True
ROTATE_ENTRY_EDGE = 0.01
ROTATE_MIN_GAIN = 0.001   # user, 2026-10-04 (on the 0.005 tick this equals any gain > 0)
ROTATE_TRIGGER_CASH = 50     # "out of budget" = less than this above the reserve

# Exits are immediate (user, 2026-10-04): each poll runs ALL exits first, then only a few entries and
# swaps, so the next exit check is never more than a few seconds away.
MAX_ENTRIES_PER_POLL = 4
MAX_ROTATIONS_PER_POLL = 2

# Execution
ORDER_EXPIRY_S = 10          # short expiry on every order = home-made IOC (API has no IOC flag)
POLL_INTERVAL_S = 5          # one poll = 3 bulk price reads + 1 positions read
