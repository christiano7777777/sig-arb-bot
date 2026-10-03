# NO+NO arbitrage bot for an election prediction market

A small Python bot that finds and trades a structural arbitrage on binary election markets,
built for the Susquehanna Predictions Cup (an academic trading competition with virtual currency, "SUSQies").

## The arbitrage
Each race (e.g. "Kansas Senate") has two binary markets: *Will the Democratic Party win?* and
*Will the Republican Party win?*. At most one of them resolves YES, so holding **one NO share of each**
pays at least 1 at settlement (exactly 1 if either party wins, 2 if neither does).

| | |
|---|---|
| Buy a pair | when `ask_NO(D) + ask_NO(R) <= 1 - 0.005` -> locked profit >= 0.005 per pair |
| Sell a held pair | when `bid_NO(D) + bid_NO(R) >= 1` -> at least the settlement value, paid now |
| Swap (rotation) | when out of capital: sell held pairs at `S`, buy a new pair at `C`, only if `S - C >= 0.005` |

Order books are quoted in YES terms, so `ask_NO = 1 - bid_YES` and `bid_NO = 1 - ask_YES`.

## How it trades
- **Discovery:** every race with exactly one Democratic and one Republican market (114 races); 3-party races are skipped.
- **Scanning:** best bid/ask for all ~230 markets in 3 bulk requests per poll; full order books only for races that signal.
- **Priority:** exits first (best price first), then entries from the largest edge down; swaps fund the largest edges first,
  selling the held pairs that are cheapest to give up (highest current bid sum).
- **Sizing:** walk both order books level by level with equal quantity on both legs, as long as each extra pair keeps the edge;
  capped by the cash above a reserve and by a worst-case one-legged exposure of 500 per attempt.
- **Execution:** one atomic multi-leg order for both legs (marketable limits, 10 s expiry), then positions are re-read.

## Safety
- **Legs are never left unequal.** Limits get the slack up to the edge threshold so a one-tick move still fills both legs;
  if a leg still fills short, the bot evens the legs out immediately at the current book (cheaper of buying the missing
  leg or selling the extra one), and halts only if the book cannot absorb it.
- Idempotency keys on every order (a retry can never double-trade), rate limiter under the API budget,
  dry-run mode by default, kill switch, every request logged with the API key removed.

## Files
| File | What it does |
|---|---|
| `arb_math.py` | pure functions: book conversion, two-book walk, exit walk, tick rounding, limit slack |
| `baskets.py` | race discovery from market titles |
| `execute.py` | the executor (dry run by default; `--live` to trade) |
| `scan.py` | read-only scanner for one race |
| `susq_client.py` | API client: auth from env, logging with key redaction, retries, rate limiter |
| `tests/` | unit tests and executor tests against a fake API (no network, no orders) |
| `.github/workflows/` | CI tests; `arb-bot.yml` runs the live bot |

## Running
```
pip install -r requirements.txt
export SUSQ_API_KEY=...            # never commit it; on GitHub it is a repository secret
python execute.py --once           # dry run: prints the orders it would send
python execute.py --live           # live
```
Tests: `python tests/test_execute_dry.py` and the unit tests in `tests/test_arb_math.py`.

## Limitations
- Profit is locked only if each race has a single winner and settles at its true result.
- The platform has no fill-or-kill order type, so equal fills are enforced after the fact, not guaranteed up front.
- Arbitrage of this kind exists here because the market is a thin, virtual-currency competition; it is a design and
  execution exercise, not a claim about real-money markets.
