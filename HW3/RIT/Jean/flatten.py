#!/usr/bin/env python3
"""flatten.py -- bring the CRZY position back to zero, in slices, with limit orders at
the touch (never walking the book). Reads rit_config.py. Run: python flatten.py"""
import os, time, requests

try:
    import rit_config as CFG
except ImportError:
    CFG = None
BASE = os.environ.get("RIT_BASE_URL") or getattr(CFG, "BASE_URL", "http://localhost:9999/v1")
USER = os.environ.get("RIT_USER") or getattr(CFG, "USER", None)
PASSWORD = os.environ.get("RIT_PASSWORD") or getattr(CFG, "PASSWORD", None)
API_KEY = os.environ.get("RIT_API_KEY") or getattr(CFG, "API_KEY", None)
TICKERS = ("CRZY_M", "CRZY_A")

s = requests.Session()
if USER:
    s.auth = (USER, PASSWORD or "")
else:
    s.headers.update({"X-API-Key": API_KEY})


def call(method, path, **params):
    while True:
        r = s.request(method, f"{BASE.rstrip('/')}/{path}", params=params, timeout=5)
        if r.status_code == 429:
            time.sleep(0.2)
            continue
        if not r.ok:
            raise SystemExit(f"{method} /{path} -> {r.status_code}: {r.text[:200]}")
        return r.json()


def quotes():
    return {x["ticker"]: x for x in call("GET", "securities") if x["ticker"] in TICKERS}


def net(q):   # RIT reports the aggregated CRZY position on both tickers
    pm, pa = float(q["CRZY_M"].get("position") or 0), float(q["CRZY_A"].get("position") or 0)
    return pm if abs(pm - pa) < 1 else pm + pa


try:
    call("POST", "commands/cancel", all=1)         # nothing resting while we flatten
except SystemExit:
    pass
q = quotes()
print(f"net position before: {net(q):+.0f}")
for _ in range(60):
    q = quotes()
    n = net(q)
    if abs(n) < 1:
        break
    if n > 0:
        t = max(TICKERS, key=lambda x: q[x]["bid"] or 0); action, px, depth = "SELL", q[t]["bid"], q[t]["bid_size"]
    else:
        t = min(TICKERS, key=lambda x: q[x]["ask"] or 1e9); action, px, depth = "BUY", q[t]["ask"], q[t]["ask_size"]
    if not px:
        time.sleep(0.2)
        continue
    qty = int(min(abs(n), max(float(depth or 0), 100), 10000))
    o = call("POST", "orders", ticker=t, type="LIMIT", quantity=qty, action=action, price=round(px, 2))
    time.sleep(0.15)
    try:
        call("DELETE", f"orders/{o['order_id']}")   # cancel whatever did not fill at the touch
    except SystemExit:
        pass
    print(f"  net {n:+.0f} -> {action} {t} {qty} @{px}")
print(f"net position after:  {net(quotes()):+.0f}")
