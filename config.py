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
RESERVE = 50_000             # (legacy, used only when A_CAPITAL_CAP is None) core budget = cash above this
CASH_SPLIT = {"D": 0.0, "A": 1.0, "B": 0.0, "C": 0.0}     # user, 2026-10-08 05:30: all free cash to A's rotation (B only sells)
                             # (2026-10-08 earlier: D 0 / A 0.8 / B 0.2 / C 0)
                             # (2026-10-07: D 0.5 / A 0.3 / B 0.2 / C 0, C's 5% to A)
                             # (2026-10-05: D 0.5 / A 0.25 / B 0.2 / C 0.05) share of the free cash (above
                             # HARD_RESERVE) each strategy may use for new buys, read fresh every time (so cash the
                             # others leave idle is used up step by step). D's missing hedges are funded first.
                             # (2026-10-04: D 0.5 / C 0.3 / B 0.2, A none; C is cut-only now, cash was A's bottleneck)
A_CAPITAL_CAP = None         # user, 2026-10-05: no cap on A (was 50,000 at cost on 2026-10-04)
# Option B (user, 2026-10-04): the other 50k is used too, but only for book levels with edge >= EXTRA_MIN_EDGE,
# so each extra trade pays for an early exit later. To drain back to 50% before the stat-arb starts,
# set EXTRA_CAPITAL_ENABLED = False: the bot stops buying below RESERVE and gets cash back only from
# exits at >= 1.000 (free). HARD_RESERVE stays untouched so the unequal-fill fix always has cash.
EXTRA_CAPITAL_ENABLED = False   # user, 2026-10-04: extra tier off; arb buys only with cash above RESERVE
EXTRA_MIN_EDGE = 0.01      # user, 2026-10-04 (was 0.015): use more of the second 50k
HARD_RESERVE = 100            # user, 2026-10-07 (was 1,000): frees ~900 for A; an unequal-fill fix that must buy may lack cash -> halt
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

# Small-edge bucket (user, 2026-10-07): keep >= A_SMALL_FRAC of A's pairs (at cost) in races whose average
# pair cost is >= 1 - A_SMALL_EDGE (bought at an edge <= 0.01). They sell near 1, so swaps (cheapest to
# exit first) fund new edges from them cheaply, for faster rotation. While the bucket is below target,
# A's new cash buys only small-edge entries, at most the shortfall; bigger edges are swap-only. Swaps may
# sell the bucket (a target, not a floor); it refills from freed cash only, never by swapping deep pairs in.
A_SMALL_EDGE = 0.01
A_SMALL_FRAC = None          # user, 2026-10-08: off with the top-3 focus (small edges were in other races); was 0.20
                             # (13:30 off: unpaced, swaps sold the new pairs within seconds, ~0.015/pair lost)
# Paced small edges (user, 2026-10-07 14:30: "put some money on small edge, but slowly"): small-edge entries get
# first claim on A's cash, but at most A_SMALL_PER_HOUR SUSQies (at limit cost) per rolling hour; all other
# entries are unchanged (highest edge first). A_SMALL_PROTECT: swaps never sell races whose average pair cost
# is >= 1 - A_SMALL_EDGE, so these are not bought and sold again; plain exits at >= 1 still apply.
A_SMALL_PER_HOUR = 300
# Top-N focus (user, 2026-10-08): A buys and swaps only in its N largest races (pairs at cost, recomputed every
# poll); races outside them are sold only by plain exits (bids sum >= 1, "0 edge") and never fund swaps.
A_TOP_N = None               # user, 2026-10-08 05:30: focus rotation replaced by EXIT_QUEUE (was 3)
# Exit queue (user, 2026-10-08 05:30): the first race here that still has pairs is exited (B asks for all its pairs,
# A sells it into the bids whenever the cash can buy any cheaper pair); never bought. HOLD_RACES: no buys, no sells.
EXIT_QUEUE = ["Alaska Senate", "Delaware Senate"]
HOLD_RACES = ["MN-05 House race"]
# Focus rotation (user, 2026-10-08): the focus is A_TOP_N races saved in state/focus.json; B sells the exit race
# (smallest current edge) until it is gone, then one race with the largest total edge on its book is taken in.
A_SMALL_POT = None           # user, 2026-10-08 05:30: no cap (was 10,000 for the non-big-3 pot)
A_NONFOCUS_MAX_EDGE = None   # user, 2026-10-08 05:30: no edge cap (was 0.03)
INTAKE_SHORTLIST = 5         # races (best top-of-book edge) whose books are read to pick the next focus race
MAKER_EXIT_CLIP = 2_000      # B's ask size per leg on the exit race (user: bigger clips)
MAKER_FOCUS_BIDS = False     # user, 2026-10-08 05:30: no B bids anywhere (was True)
A_SMALL_PROTECT = True
# D paused for A (user, 2026-10-07): while A's small-edge bucket is below this share of A's pairs at cost,
# D's CASH_SPLIT share goes to A. D still exits and buys missing hedges (those draw on all free cash), but
# takes no new position. D gets its share back once the bucket is >= this (re-checked every poll).
D_PAUSE_UNTIL_A_SMALL = None  # user, 2026-10-07 13:30: off with the bucket (was 0.10); D has its 50% again

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
# --- Strategy D: Senate-control stat arb (user, 2026-10-04; strategy_d.py, stat_model.py) ---
# Trade SUSQ "U.S. Senate" toward Kalshi's control price, delta-hedged with the state Senate races:
# delta_i = dP(R control)/dp_i under a national-swing model calibrated to Kalshi's control price.
D_ENABLED = True
D_FROZEN = True              # user, 2026-10-08: D stopped, no orders; its hedged position is held to settlement (ledger kept)
D_CLOSE = True               # user, 2026-10-08 05:30: D flat: sell every share in D's ledger into the best bids (overrides frozen)
D_CAPITAL = 10_000           # SUSQies for D (control leg + hedges, at cost); starts with whatever cash is free
D_ENTRY_GAP = 0.03           # enter when |Kalshi - SUSQ| on Republican control >= this
D_EXIT_GAP = 0.01            # exit everything once it is <= this
D_BAND_FRAC = 0.10           # rebalance a hedge only when it is off its own target by > this x that target
D_MIN_TRADE = 25             # ... and by at least this many shares
D_INTERVAL_S = 60            # one D round per minute
D_MAX_ORDERS = 6             # orders per round (largest deviations first)
D_CLIP = 1_000               # control-leg shares per round
D_CONTROL_RACE = "U.S. Senate"
D_KALSHI_CONTROL = {"D": "CONTROLS-2026-D", "R": "CONTROLS-2026-R"}
# Kalshi batch reads + price log for strategy E research (user, 2026-10-07; price_recorder.py)
KALSHI_BATCH_S = 10          # all mapped tickers in ~3 requests every 10 s; None = off (C/D read per ticker again)
PRICE_LOG_FULL_S = 600       # state/prices.jsonl: changes every cycle, a full line every 10 min
D_RACES = ["Alabama", "Alaska", "Arkansas", "Colorado", "Delaware", "Florida", "Georgia", "Idaho", "Illinois",
           "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Massachusetts", "Michigan", "Minnesota",
           "Mississippi", "Montana", "Nebraska", "New Hampshire", "New Jersey", "New Mexico", "North Carolina",
           "Ohio", "Oklahoma", "Oregon", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas",
           "Virginia", "West Virginia", "Wyoming"]   # the 35 seats up in 2026 (+ " Senate")
D_KALSHI_EXTRA = {"Montana Senate": "SENATEMT-26-R", "Nebraska Senate": "SENATENE-26-R",
                  "Ohio Senate": "SENATEOHS-26-R"}   # P(R wins) for the 3 races SUSQ does not list
D_LIVE_SINCE = "2026-10-04T13:30:00+00:00"   # D's fills counted from here (ledger rebuild)

# --- Strategy E: Kalshi-jump breakout (user, 2026-10-07; strategy_e.py, e_executor.py) ---
# Needs the Kalshi batch cache (KALSHI_BATCH_S) and runs live only. Own ledger and own cash, independent of A.
E_ENABLED = False            # user, 2026-10-08 05:30: E off (no positions; its reserve goes back to the pool)
E_LIVE_SINCE = "2026-10-07T15:29:00+00:00"   # E's allotment grows from here; E's fills counted from here
E_CAPITAL = 10_000           # user: 10k, no other risk limit
E_FILL_PER_HOUR = 150        # user, 2026-10-08 18:00: refill 150/h toward E_CAPITAL (was 1e9 = full 10k, 500/h before)
E_ALLOT_BASE = 3_000         # user, 2026-10-08 18:00: E's reserve 3k now (was 10k: 6.6k idle while E had no signal)
E_FILL_SINCE = "2026-10-07T18:01:00+00:00"   # allotment = min(E_CAPITAL, E_ALLOT_BASE + E_FILL_PER_HOUR x hours since this)
E_INTERVAL_S = 10            # one E round per Kalshi batch refresh
E_JUMP = 0.03                # Kalshi fair of a leg (NO price) up at least this ...
E_JUMP_WINDOW_S = 60         # ... within this long
E_BASELINE_S = 7_200         # usual gap fair - SUSQ mid = median over the last 2 h
E_MIN_HISTORY_S = 1_800      # no entries in a race until 30 min of history (e.g. after a restart)
E_MARGIN = 0.01              # buy only at <= (where SUSQ goes if it follows) - this
E_EXIT_SLACK = 0.005         # caught up: best bid >= that target (now) - this

# --- Strategy C: Kalshi-anchored two-sided market making (user, 2026-10-04; strategy_c.py) ---
C_LIMIT = 2_000            # directional inventory per race: risk-adding quotes only below this
C_SKEW = 0.10              # reservation price moves this far from Kalshi fair at C_LIMIT of exposure
C_SKEW_MAX = 0.25          # ... capped here (big legacy positions: strongest pull back toward flat)
C_QUOTE_EDGE = 0.02        # quotes at least this far from the reservation price
C_CLIP = 500               # shares per quote
C_CUT_ONLY = True          # user, 2026-10-05: C only unwinds what it holds (no new inventory, no bids in other races)
C_QUOTES = False           # user, 2026-10-08: C stopped, no quotes anywhere (stale ones are cancelled)
C_DUMP_GAP = 0.30          # user, 2026-10-08 05:30 "flat": any bid within 0.30 of Kalshi fair (was 0.15); keeps out of empty books
                           # (2026-10-08 earlier: 0.15, "sell everything now" (was 0.03): any bid within 0.15 of Kalshi fair, so a
                           # thin book is not sold into far below fair; user, 2026-10-07: sell C's excess leg into the bids while they are within this of Kalshi
                           # fair (races without pairs only; the rest keeps unwinding at the best ask). None = off
C_DUMP_PAIR_GAP = 0.15      # user, 2026-10-08 05:30: neutralize uneven legs (excess leg into bids within 0.15 of fair), not on
                             # the exit race (was 0.0: only at >= Kalshi fair)
C_DUMP_PER_ROUND = 2       # races dumped per B round (each = cancel + order, inside the 30 writes/min budget)
C_EXTRA_RACES = 5          # races we do not hold: bids on the cheap leg in the 5 with the biggest gap
B_SKEW = 0.02             # holding: reservation price moves this far from fair at full race-cap exposure (user: slight)
B_TOTAL_CAP_FRAC = 0.30    # max shares at risk over all races, as a fraction of portfolio value (user: 30%)
MAKER_ENABLED = True       # pair maker (pair_maker.py): resting two-sided pair quotes on the largest held races
MAKER_RACES = 3            # user, 2026-10-08: top 3 only (was 10); how many held races (largest by pairs) get pair quotes
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
