"""Minimal client for the Super Market API.

* The key is read from the SUSQ_API_KEY environment variable and is never printed.
* Every request/response is appended to logs/requests-YYYYMMDD.jsonl.
  Headers are not logged at all, and the key string is scrubbed from any text as a backstop.
"""
import datetime as dt
import json
import os
import time
from collections import deque
from pathlib import Path

import requests
import truststore

from config import BASE_URL

# Verify TLS against the Windows certificate store: Python's bundled CA list
# rejected this site's chain on this machine (2026-10-04). Verification stays on.
truststore.inject_into_ssl()

LOG_DIR = Path(__file__).parent / "logs"

# Per-account budget is 100 reads + 30 writes per minute; stay a little below it.
BUDGET_PER_MIN = {"read": 90, "write": 27}


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code = status, code


class SusqClient:
    def __init__(self, base_url=BASE_URL):
        # strip whitespace and an invisible byte-order mark (PowerShell pipes add one)
        self._key = (os.environ.get("SUSQ_API_KEY") or "").strip().lstrip("﻿").strip()
        if not self._key:
            raise SystemExit("SUSQ_API_KEY is not set. See .env.example.")
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self._key}"
        self._sent = {"read": deque(), "write": deque()}   # send times in the last 60 s
        LOG_DIR.mkdir(exist_ok=True)

    def _throttle(self, kind):
        """Sleep just long enough to stay inside the per-minute budget."""
        q = self._sent[kind]
        while True:
            now = time.time()
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) < BUDGET_PER_MIN[kind]:
                q.append(now)
                return
            time.sleep(60 - (now - q[0]) + 0.05)

    # ---- logging -------------------------------------------------------
    def _scrub(self, text):
        return text.replace(self._key, "***REDACTED***") if text else text

    def _log(self, record):
        path = LOG_DIR / f"requests-{dt.date.today():%Y%m%d}.jsonl"
        line = self._scrub(json.dumps(record, default=str))
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ---- requests ------------------------------------------------------
    def _send(self, method, path, params=None, body=None):
        url = f"{self.base_url}{path}"
        self._throttle("read" if method == "GET" else "write")
        t0 = time.time()
        try:
            resp = self.session.request(method, url, params=params, json=body, timeout=30)
        except requests.RequestException as exc:
            # network trouble: treated like a 5xx (reads skip the poll, order POSTs retry with the same key)
            self._log({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "method": method, "path": path,
                       "params": params, "request": body, "status": 599, "body": repr(exc)[:500]})
            e = ApiError(599, "NETWORK", repr(exc)[:200])
            e.retry_after = None
            raise e from None
        self._log({
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "method": method, "path": path, "params": params, "request": body,
            "status": resp.status_code, "ms": round((time.time() - t0) * 1000),
            # orders and errors are logged in full; successful reads only briefly (they run 24/7)
            "body": resp.text[:20000] if (method != "GET" or not resp.ok) else resp.text[:300],
        })
        if resp.ok:
            return resp.json()
        try:
            err = resp.json()["error"]
            code, msg = err.get("code"), err.get("message")
            if err.get("details"):                 # e.g. which leg failed validation and why
                msg = f"{msg} details={json.dumps(err['details'])[:600]}"
        except (ValueError, KeyError, TypeError):
            code, msg = "HTTP", resp.text[:300]
        e = ApiError(resp.status_code, code, msg)
        e.retry_after = resp.headers.get("Retry-After")
        raise e

    def get(self, path, **params):
        params = {k: v for k, v in params.items() if v is not None}
        return self._send("GET", path, params=params)

    def post(self, path, body, max_tries=6):
        """POST with the retry rules from the API docs.

        Retries only what the docs call safe: 429, 409 REQUEST_IN_FLIGHT, 502 ORDER_STATUS_UNKNOWN
        and 5xx. Order bodies carry an idempotencyKey, and every retry sends the identical body,
        so a retry can never place a second order. Other 4xx errors are raised immediately.
        """
        delay = 0.5
        for attempt in range(1, max_tries + 1):
            try:
                return self._send("POST", path, body=body)
            except ApiError as e:
                retryable = e.status == 429 or e.status >= 500 or e.code == "REQUEST_IN_FLIGHT"
                if not retryable or attempt == max_tries:
                    raise
                if e.code == "REQUEST_IN_FLIGHT":
                    wait = 90
                elif e.retry_after:
                    wait = float(e.retry_after)
                else:
                    wait = delay
                    delay *= 2
                print(f"  {e} -> retrying same request in {wait:g}s (try {attempt + 1}/{max_tries})")
                time.sleep(wait)
