"""Merge the bot's new order tags (state/order_tags.jsonl) into the persistent map on the
dashboard-data branch (order_tags.json: orderId -> [strategy, kind, action, race]).
    python tools/merge_tags.py state/order_tags.jsonl order_tags.json
Only ids and strategy labels are stored: no prices or sizes (the fill log itself stays private).
"""
import json
import sys

src, dst = sys.argv[1], sys.argv[2]
try:
    tags = json.load(open(dst, encoding="utf-8"))
except (OSError, ValueError):
    tags = {}
try:
    for line in open(src, encoding="utf-8"):
        t = json.loads(line)
        tags[str(t["orderId"])] = [t["s"], t["k"], t["a"], t["race"]]
except OSError:
    pass
json.dump(tags, open(dst, "w", encoding="utf-8"), separators=(",", ":"))
