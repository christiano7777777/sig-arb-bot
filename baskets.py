"""Discover NO+NO baskets from the tournament's market titles.

Titles look like "Will the Democratic Party win the Kansas Senate?". Markets are grouped by
race ("Kansas Senate"); a race becomes a basket only if it has exactly two party markets,
one Democratic and one Republican, both open and binary. At most one of them can win, so
NO on both pays at least 1.
"""
import re
from collections import defaultdict

import config

TITLE = re.compile(r"^Will the (\w+) Party win the (.+?)\??$")


def list_markets(client, slug):
    rows, cursor = [], None
    while True:
        r = client.get(f"/tournaments/{slug}/markets", limit=100, cursor=cursor)
        rows += r["data"]
        if not r["pagination"]["hasMore"]:
            return rows
        cursor = r["pagination"]["nextCursor"]


def two_party_baskets(markets):
    """Return (baskets, skipped) where each basket is {"name", "legs": [D leg, R leg]}."""
    races = defaultdict(list)
    for m in markets:
        g = TITLE.match(m["title"].strip())
        if g:
            races[g.group(2)].append((g.group(1), m))

    baskets, skipped = [], []
    for race, entries in sorted(races.items()):
        parties = sorted(p for p, _ in entries)
        if parties != ["Democratic", "Republican"]:
            skipped.append((race, "parties: " + "/".join(parties)))
            continue
        if race in config.RACE_DENYLIST:
            skipped.append((race, "on RACE_DENYLIST"))
            continue
        if any(m["status"] != "open" or len(m["exchanges"]) != 1 for _, m in entries):
            skipped.append((race, "not open / not binary"))
            continue
        legs = [{"party": p[0], "market_id": m["id"], "exchange_id": m["exchanges"][0]["id"], "title": m["title"]}
                for p, m in sorted(entries, key=lambda e: e[0])]          # D first, then R
        baskets.append({"name": race, "legs": legs})
    return baskets, skipped
