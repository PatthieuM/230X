#!/usr/bin/env python3
"""
algo1_v3.py -- ALGO1 arbitrage for the graded run. Built from two test sessions.

Evidence from the test server (10 Sep 2026):
  v1  market orders, fixed 1,000 shares, sleep 0.2 s:   125 pairs, -$39.9k.
      83% of the loss: re-firing on quotes the server had not refreshed, into a book
      we had just emptied; 17%: 1,000-share market orders walking a touch that only
      held a few hundred shares. Latency was NOT the problem: when depth existed the
      fills matched the quoted prices to the cent.
  v2  marketable limit legs at the touch:  matched pairs earned exactly edge x size
      (+40 ... +320 each, realized P&L +2,095 in ~2 minutes), BUT
      - the second leg often filled nothing: the quote is gone within tens of ms,
        mostly on CRZY_M (the venue everybody hedges on);
      - legs that we "cancelled" were in fact filled by other bots' market orders a
        moment later, so the position drifted to -10,423 without the code knowing,
        and the mark-to-market on that inventory swung P&L by thousands;
      - repairs done with market orders walked the thin book (-340 on 2,000 shares);
      - RIT reports the AGGREGATED CRZY position on both tickers (Portfolio showed
        the same -10,423 twice): summing them doubles it.

Design rules that follow:
  R1  Limit orders only, priced at the touch. Nothing we send can fill worse than
      the quote we saw. (Case brief Q4: a market order is a blank cheque in a thin book.)
  R2  One leg at a time, the fragile one first (default: the CRZY_M leg). If it fills
      nothing, the pair is abandoned at zero cost. The second leg is sent for exactly
      the quantity the first one filled.
  R3  Every order is read back after its cancel; fills that happened in between count.
  R4  The server's aggregated position is the truth. Every loop: if |net| exceeds
      POSITION_TOLERANCE, cancel everything and flatten in limit slices before trading.
  R5  Never fire twice on the same quote snapshot.
  R6  Only trade crosses wider than MIN_EDGE: a missed second leg costs about one
      spread to repair, so the edge must exceed the spread for positive expectation.
      (Case brief Q1: MIN_EDGE is where a per-share cost goes.)
  R7  Stop opening pairs at STOP_TICK and flatten, so the run ends with no inventory.
  R8  Every attempt -- filled, abandoned, repaired -- is logged for the HW03 write-up.

Run:  python algo1_v3.py      Stop: Ctrl+C once (then python flatten.py if Trader Info is not 0/0)
"""

import csv
import os
import signal
import sys
import time
from datetime import datetime

import requests

try:
    import rit_config as CFG
except ImportError:
    CFG = None

# ----------------------------- settings -------------------------------------
TICKER_M = "CRZY_M"
TICKER_A = "CRZY_A"
QUANTITY_MAX = 1000        # max shares per leg; actual size = min(this, depth at both touches)
MIN_QUANTITY = 100         # skip crosses thinner than this
MIN_EDGE = 0.03            # trade only if bid - ask > MIN_EDGE (repair of a missed leg costs ~1 spread)
LEG_ORDER = "M"            # which leg goes first: "M" / "A" (venue), "thin" (smaller size), "buy", "sell"
FILL_WAIT = 0.05           # seconds to wait for a limit leg before cancelling the remainder
POLL_FILL = 0.05
MAX_POSITION = 20000       # |net position| cap (case limit is 25,000, aggregated across venues)
POSITION_TOLERANCE = 100   # guard: flatten if the server reports |net| above this between pairs
REPAIR_TRIES = 6           # limit slices attempted right after a pair before leaving it to the guard
SLEEP_AFTER_TRADE = 0.05
START_TICK = 3
STOP_TICK = 290            # no new pairs after this tick ...
FLATTEN_TICK = 291         # ... and flatten from this tick on
# -----------------------------------------------------------------------------

BASE_URL = os.environ.get("RIT_BASE_URL") or getattr(CFG, "BASE_URL", "http://localhost:9999/v1")
USER = os.environ.get("RIT_USER") or getattr(CFG, "USER", None)
PASSWORD = os.environ.get("RIT_PASSWORD") or getattr(CFG, "PASSWORD", None)
API_KEY = os.environ.get("RIT_API_KEY") or getattr(CFG, "API_KEY", None)

shutdown = False


class ApiException(Exception):
    pass


def signal_handler(signum, frame):
    global shutdown
    shutdown = True
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    print("\nCtrl+C: stopping after this loop.")


# ----------------------------- API layer ------------------------------------
def make_session():
    s = requests.Session()
    if USER:
        s.auth = (USER, PASSWORD or "")
    else:
        s.headers.update({"X-API-Key": API_KEY})
    return s


def request(s, method, endpoint, params=None):
    url = f"{BASE_URL.rstrip('/')}/{endpoint}"
    while True:
        resp = s.request(method, url, params=params, timeout=5)
        if resp.status_code == 429:
            try:
                wait = float(resp.headers.get("Retry-After") or resp.json().get("wait", 0.1))
            except Exception:
                wait = 0.1
            time.sleep(wait)
            continue
        if resp.status_code == 401:
            raise ApiException("401 Unauthorized: check rit_config.py")
        if not resp.ok:
            raise ApiException(f"{method} /{endpoint} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()


def get_case(s):
    return request(s, "GET", "case")


def get_quotes(s):
    out = {}
    for sec in request(s, "GET", "securities"):
        out[sec["ticker"]] = {
            "bid": sec.get("bid") or 0.0, "ask": sec.get("ask") or 0.0,
            "bid_size": float(sec.get("bid_size") or 0), "ask_size": float(sec.get("ask_size") or 0),
            "position": float(sec.get("position") or 0.0)}
    return out


def net_position(q):
    """RIT aggregates CRZY across venues and reports the same number on both tickers."""
    pm, pa = q[TICKER_M]["position"], q[TICKER_A]["position"]
    return pm if abs(pm - pa) < 1 else pm + pa


def cancel_all(s):
    try:
        request(s, "POST", "commands/cancel", params={"all": 1})
    except ApiException:
        pass


def limit_order(s, ticker, action, qty, price):
    try:
        return request(s, "POST", "orders", params={"ticker": ticker, "type": "LIMIT",
                                                    "quantity": int(qty), "action": action,
                                                    "price": round(price, 2)})
    except ApiException as e:
        print(f"  !! order rejected: {ticker} {action} {qty} @{price}: {e}")
        return None


def settle(s, order):
    """Let a limit leg fill briefly, cancel the rest, then READ BACK the final fill
    (R3: a fill that lands between our last look and the cancel still counts)."""
    if order is None:
        return 0.0, None
    oid = order.get("order_id")
    filled = float(order.get("quantity_filled") or 0)
    vwap = order.get("vwap")
    status = order.get("status")
    deadline = time.perf_counter() + FILL_WAIT
    while status not in ("TRANSACTED", "CANCELLED") and time.perf_counter() < deadline:
        time.sleep(POLL_FILL)
        try:
            o = request(s, "GET", f"orders/{oid}")
        except ApiException:
            break
        filled, vwap, status = float(o.get("quantity_filled") or filled), o.get("vwap") or vwap, o.get("status")
    if status not in ("TRANSACTED", "CANCELLED"):
        try:
            request(s, "DELETE", f"orders/{oid}")
        except ApiException:
            try:
                request(s, "POST", "commands/cancel", params={"ids": str(oid)})
            except ApiException:
                pass
        try:
            o = request(s, "GET", f"orders/{oid}")
            filled, vwap = float(o.get("quantity_filled") or filled), o.get("vwap") or vwap
        except ApiException:
            pass
    return filled, (float(vwap) if vwap else None)


def flatten(s, log, tick, tries=40):
    """Bring the aggregated position to zero in limit slices at the touch, on the venue
    with the better price. Never walks the book. Returns the remaining net."""
    net = 0.0
    for _ in range(tries):
        q = get_quotes(s)
        net = net_position(q)
        if abs(net) < 1:
            return 0.0
        if net > 0:
            t = max((TICKER_M, TICKER_A), key=lambda x: q[x]["bid"])
            action, px, depth = "SELL", q[t]["bid"], q[t]["bid_size"]
        else:
            t = min((TICKER_M, TICKER_A), key=lambda x: q[x]["ask"] or 1e9)
            action, px, depth = "BUY", q[t]["ask"], q[t]["ask_size"]
        if not px:
            time.sleep(0.2)
            continue
        qty = int(min(abs(net), max(depth, MIN_QUANTITY), 10000))
        filled, vwap = settle(s, limit_order(s, t, action, qty, px))
        print(f"  flatten: net {net:+.0f} -> {action} {t} {filled:.0f}/{qty} @{vwap}")
        log.writerow([now(), tick, "FLATTEN", t, action, qty, px, filled, vwap, "", "", "", ""])
        if filled < 1:
            time.sleep(0.2)
    return net


def now():
    return datetime.now().isoformat(timespec="milliseconds")


# ----------------------------- one arbitrage attempt --------------------------
def attempt(s, first, second, tick, log):
    """first/second: dicts with ticker, action, price, qty. Leg 1 must fill or we walk away."""
    f1, v1 = settle(s, limit_order(s, first["ticker"], first["action"], first["qty"], first["price"]))
    if f1 < 1:
        print(f"[tick {tick:3d}] {first['action']} {first['ticker']} 0/{first['qty']} @{first['price']}: gone, abandoned")
        log.writerow([now(), tick, "ABANDON", first["ticker"], first["action"], first["qty"], first["price"],
                      0, "", second["ticker"], second["price"], "", ""])
        return False
    f2, v2 = settle(s, limit_order(s, second["ticker"], second["action"], f1, second["price"]))
    matched = min(f1, f2)
    buy_v, sell_v = (v1, v2) if first["action"] == "BUY" else (v2, v1)
    pnl = (sell_v - buy_v) * matched if (buy_v and sell_v and matched) else 0.0
    log.writerow([now(), tick, "PAIR", first["ticker"], first["action"], first["qty"], first["price"],
                  f1, v1, second["ticker"], second["price"], f2, v2])
    print(f"[tick {tick:3d}] {first['action']} {first['ticker']} {f1:.0f}/{first['qty']} @{v1}"
          f" -> {second['action']} {second['ticker']} {f2:.0f}/{f1:.0f} @{v2}"
          f"  edge {abs(second['price'] - first['price']):.2f}  matched {matched:.0f}  pnl {pnl:.2f}")
    if f2 < f1:                               # second leg short: repair in limit slices now
        flatten(s, log, tick, tries=REPAIR_TRIES)
    return True


def main():
    os.makedirs("logs", exist_ok=True)
    path = os.path.join("logs", f"trades_v3_{datetime.now():%Y%m%d_%H%M%S}.csv")
    f = open(path, "w", newline="")
    log = csv.writer(f)
    log.writerow(["time", "tick", "type", "ticker1", "action1", "qty1", "price1", "filled1", "vwap1",
                  "ticker2", "price2", "filled2", "vwap2"])
    who = f"{USER}@{BASE_URL}" if USER else f"api-key@{BASE_URL}"
    print(f"algo1_v3  {who}  first={LEG_ORDER}  qty<= {QUANTITY_MAX}  min_edge={MIN_EDGE}"
          f"  guard={POSITION_TOLERANCE}  stop={STOP_TICK}  log={path}")
    n_pairs = n_abandon = 0
    flattened_at_end = False

    with make_session() as s:
        case = get_case(s)
        print(f"case '{case.get('name')}' status {case.get('status')} tick {case.get('tick')}")
        tick, status = case.get("tick", 0), case.get("status")
        last_case_poll = 0.0
        last_snapshot = None

        while not shutdown:
            if time.perf_counter() - last_case_poll > 0.25:
                case = get_case(s)
                tick, status = case.get("tick", 0), case.get("status")
                last_case_poll = time.perf_counter()
                if tick < START_TICK:
                    flattened_at_end = False
            if status != "ACTIVE" or tick < START_TICK:
                time.sleep(0.5)
                continue
            if tick >= FLATTEN_TICK:                         # R7: end the run flat
                if not flattened_at_end:
                    cancel_all(s)
                    net = flatten(s, log, tick)
                    print(f"[tick {tick:3d}] end of run: position {net:+.0f}")
                    flattened_at_end = True
                    f.flush()
                time.sleep(0.5)
                continue
            if tick > STOP_TICK:
                time.sleep(0.2)
                continue

            q = get_quotes(s)
            if TICKER_M not in q or TICKER_A not in q:
                raise ApiException(f"tickers not found; have {sorted(q)}")
            m, a = q[TICKER_M], q[TICKER_A]
            if not (m["bid"] and m["ask"] and a["bid"] and a["ask"]):
                continue

            net = net_position(q)                             # R4: the server is the truth
            if abs(net) > POSITION_TOLERANCE:
                print(f"[tick {tick:3d}] position guard: net {net:+.0f} -> cancel all, flatten")
                cancel_all(s)
                flatten(s, log, tick)
                f.flush()
                continue

            snapshot = (m["bid"], m["ask"], m["bid_size"], m["ask_size"],
                        a["bid"], a["ask"], a["bid_size"], a["ask_size"])
            if snapshot == last_snapshot:                     # R5
                continue

            # ---- is there a cross? which way? ----------------------------------
            if m["bid"] - a["ask"] > MIN_EDGE:
                buy_t, buy_side, sell_t, sell_side = TICKER_A, a, TICKER_M, m
            elif a["bid"] - m["ask"] > MIN_EDGE:
                buy_t, buy_side, sell_t, sell_side = TICKER_M, m, TICKER_A, a
            else:
                continue
            qty = int(min(QUANTITY_MAX, buy_side["ask_size"], sell_side["bid_size"],
                          MAX_POSITION - abs(net)))
            if qty < MIN_QUANTITY:
                continue
            buy = {"ticker": buy_t, "action": "BUY", "price": buy_side["ask"], "qty": qty}
            sell = {"ticker": sell_t, "action": "SELL", "price": sell_side["bid"], "qty": qty}

            # ---- R2: the fragile leg goes first --------------------------------
            if LEG_ORDER in ("M", "A"):
                first_ticker = TICKER_M if LEG_ORDER == "M" else TICKER_A
                buy_first = (buy_t == first_ticker)
            elif LEG_ORDER == "buy":
                buy_first = True
            elif LEG_ORDER == "sell":
                buy_first = False
            else:
                buy_first = buy_side["ask_size"] <= sell_side["bid_size"]
            first, second = (buy, sell) if buy_first else (sell, buy)

            last_snapshot = snapshot
            if attempt(s, first, second, tick, log):
                n_pairs += 1
                time.sleep(SLEEP_AFTER_TRADE)
            else:
                n_abandon += 1
            f.flush()

    f.close()
    print(f"stopped. {n_pairs} pairs, {n_abandon} abandoned. Log: {path}")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    try:
        main()
    except ApiException as e:
        print(f"API error: {e}")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        print("Cannot reach RIT: check BASE_URL in rit_config.py and the network.")
        sys.exit(1)
