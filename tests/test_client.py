"""Offline tests of SusqClient.post's order-expiry handling (stubbed HTTP, no network).
Run: python tests/test_client.py"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("SUSQ_API_KEY", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import susq_client  # noqa: E402


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.ok = 200 <= status < 300
        self.text = json.dumps(body)

    def json(self):
        return self._body


def client(responses, throttle_wait=0.0):
    """Client whose session returns `responses` in order and records each request body; the write
    throttle 'waits' throttle_wait seconds by moving a fake clock (the expiry must follow it)."""
    c = susq_client.SusqClient()
    sent, clock = [], {"offset": 0.0}
    real_time = time.time
    susq_client.time.time = lambda: real_time() + clock["offset"]
    c._throttle = lambda kind: clock.__setitem__("offset", clock["offset"] + throttle_wait)
    c._log = lambda rec: None

    class Session:
        def request(self, method, url, params=None, json=None, timeout=None):
            sent.append({"body": __import__("copy").deepcopy(json), "at": susq_client.time.time()})
            return responses.pop(0)
    c.session = Session()
    return c, sent, clock


def order(ttl=10):
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {"idempotencyKey": "k1", "legs": [{"exchangeId": "1", "expirationDate": exp}, {"exchangeId": "2", "expirationDate": exp}]}


def exp_of(leg):
    return dt.datetime.fromisoformat(leg["expirationDate"].replace("Z", "+00:00")).timestamp()


def test_expiry_restamped_after_a_long_budget_wait():
    # the write budget holds the order 25 s; its 10 s expiry must still be ~10 s ahead when sent
    c, sent, _ = client([Resp(200, {"results": []})], throttle_wait=25)
    c.post("/orders/multi-leg", order())
    s = sent[0]
    for leg in s["body"]["legs"]:
        assert 8 <= exp_of(leg) - s["at"] <= 11


def test_after_429_new_key_and_fresh_expiry():
    orig_sleep = susq_client.time.sleep
    susq_client.time.sleep = lambda s: None
    try:
        c, sent, clock = client([Resp(429, {"error": {"code": "RATE_LIMITED", "message": "x"}}, {"Retry-After": "60"}),
                                 Resp(200, {"results": []})])
        body = order()
        c.post("/orders/multi-leg", body)
        assert sent[0]["body"]["idempotencyKey"] == "k1" and sent[1]["body"]["idempotencyKey"] == "k1-r1"
    finally:
        susq_client.time.sleep = orig_sleep


def test_after_5xx_the_identical_request_is_resent():
    orig_sleep = susq_client.time.sleep
    susq_client.time.sleep = lambda s: None
    try:
        c, sent, _ = client([Resp(503, {"error": {"code": "SERVICE_UNAVAILABLE", "message": "x"}}),
                             Resp(200, {"results": []})])
        c.post("/orders/multi-leg", order())
        assert sent[0]["body"] == sent[1]["body"]          # same key and same expiry: no double order
    finally:
        susq_client.time.sleep = orig_sleep


if __name__ == "__main__":
    real = susq_client.time.time
    names = [n for n in dir() if n.startswith("test_")]
    bad = 0
    for n in names:
        try:
            globals()[n]()
            print("PASS", n)
        except Exception as e:
            bad += 1
            print("FAIL", n, repr(e))
        finally:
            susq_client.time.time = real
    print(f"{len(names) - bad}/{len(names)} passed")
    sys.exit(1 if bad else 0)
