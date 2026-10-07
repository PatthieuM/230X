#!/usr/bin/env python3
"""
@file algo2_market_maker.py
@brief RIT ALGO2 - single-security market maker (ticker ALGO) using the RIT Client REST API.

@details
Strategy
--------
The algorithm continuously keeps resting limit orders on both sides of the book and earns
the bid/ask spread plus the passive rebate (0.5 cent per share in ALGO2). Design choices:

 1. **Layered quotes.** Each side holds several limit orders (see ::LAYERS): three
    5,000-share orders at our best price and smaller orders one and two ticks behind,
    which are already queued if the price moves.
 2. **Queue priority.** Orders are indexed by price. An order that is still at a wanted
    price is never cancelled, so it keeps its time priority. Orders are always cancelled
    *before* new ones are placed, so position + open orders never exceeds the cap.
 3. **Pricing.** We join the best bid/ask of the *other* participants (our own orders are
    excluded from the book) and improve by one tick only when their spread is >= 3 ticks.
    Quotes are never allowed to cross the book (we want the rebate, not the commission).
 4. **Inventory control.** Quotes are shifted against the position once it exceeds a
    dead band (::DEADBAND), sizes are asymmetric (smaller on the side that adds
    inventory), and position + open orders is capped at ::MAX_POSITION (the case limit
    is 25,000 shares with a 10 cent/share fine). Above ::FLATTEN_TRIGGER the position is
    reduced with a market order.
 5. **End of case.** The position cap is lowered in the last ::END_TICKS seconds and any
    remaining position is closed at market in the last ::FLATTEN_TICKS seconds.

Known weakness (observed in the graded run): the inventory skew moves the quotes but does
not close the position, and there is no price-based stop-loss. In a strongly trending
market the algorithm is filled on one side only and accumulates inventory against the
move.

Usage
-----
 1. Open the RIT client, log in, and enable the API (the port and API key are shown in
    the client's API panel).
 2. Run, before the case starts:
        python algo2_market_maker.py --key YOUR_API_KEY --port 9999
 3. The script waits for the case to become ACTIVE, trades automatically, prints one
    status line per second, and cancels all its orders when it stops (Ctrl+C or end of
    case). Only one instance should run at a time. All parameters below can be
    overridden on the command line (see --help).

Requires Python 3.6+ and the standard library only.
"""

import argparse
import json
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

## Default port of the RIT Client REST API (shown in the RIT client).
API_PORT = 9999
## Default API key (shown in the RIT client). Override with --key.
API_KEY = "YOUR_API_KEY"

## Ticker traded in the ALGO2 case.
TICKER = "ALGO"
## Minimum price increment, in dollars.
TICK = 0.01
## Maximum order size allowed by the case.
MAX_ORDER = 5000

## Quote ladder per side: (distance from our best price in ticks, size in shares),
## ordered from most to least aggressive. Total = 22,000 shares = ::MAX_POSITION.
LAYERS = [(0, 5000), (0, 5000), (0, 5000), (1, 4000), (2, 3000)]
## Internal cap on position + open orders per side (case limit is 25,000).
MAX_POSITION = 22000
## Position above which inventory is reduced with a market order.
FLATTEN_TRIGGER = 20000
## Position targeted after a forced reduction.
FLATTEN_TARGET = 12000
## Quotes shift by one tick per this many shares of inventory beyond the dead band.
SKEW_SHARES = 2500
## No inventory skew while |position| is below this level.
DEADBAND = 4000
## Quote shift in cents per cent of trend (fast EMA minus slow EMA of the mid price).
TREND_COEF = 0.5
## EMA spans, in seconds, used for the trend estimate.
EMA_FAST, EMA_SLOW = 4, 20

## In the last END_TICKS seconds the position cap is reduced to ::END_MAX_POSITION.
END_TICKS = 15
## Position cap applied near the end of the case.
END_MAX_POSITION = 2500
## In the last FLATTEN_TICKS seconds the position is closed with market orders.
FLATTEN_TICKS = 3
## Pause between two iterations of the main loop, in seconds.
LOOP_SLEEP = 0.02


class ApiError(Exception):
    """@brief Raised when the RIT API returns an unrecoverable error."""


class RIT:
    """
    @brief Minimal wrapper around the RIT Client REST API (v1).

    @details Handles the API key header, HTTP 429 rate-limit responses (waits the delay
    announced by RIT and retries), transient connection errors, and a client-side
    minimum gap between order submissions.
    """

    def __init__(self, key, host):
        """
        @param key  API key shown in the RIT client.
        @param host Base URL of the API, e.g. "http://localhost:9999".
        """
        self.base = host.rstrip("/") + "/v1"
        self.key = key
        ## Minimum time between two order submissions (set from the case rate limit).
        self.gap = 0.0
        ## Time of the last order submission.
        self.last = 0.0

    def req(self, method, path, **params):
        """
        @brief Send one HTTP request and return the decoded JSON body.
        @param method HTTP verb ("GET", "POST", "DELETE").
        @param path   Endpoint path, e.g. "/orders".
        @param params Query-string parameters.
        @return Decoded JSON (dict or list).
        @throws ApiError on authentication failure, repeated connection errors or any
                non-429 HTTP error.
        """
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        r = urllib.request.Request(url, method=method, headers={"X-API-Key": self.key})
        for attempt in range(20):
            try:
                with urllib.request.urlopen(r, timeout=2) as resp:
                    return json.loads(resp.read() or b"null")
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code == 429:  # rate limited: RIT tells us how long to wait
                    try:
                        wait = float(json.loads(body).get("wait", 0.1))
                    except (ValueError, AttributeError):
                        wait = float(e.headers.get("Retry-After", 0.1))
                    time.sleep(max(wait, 0.01))
                    continue
                if e.code == 401:
                    raise ApiError("API key rejected (401)")
                raise ApiError(f"{method} {path} {params} -> {e.code} {body[:200]}")
            except (urllib.error.URLError, OSError) as e:
                if attempt >= 4:
                    raise ApiError(f"cannot connect ({self.base}): {e}")
                time.sleep(0.2)
        raise ApiError("too many 429 responses")

    def cancel(self, oid):
        """@brief Cancel one order by id; errors (already filled/cancelled) are ignored."""
        try:
            self.req("DELETE", f"/orders/{oid}")
        except ApiError:
            pass

    def cancel_all(self):
        """@brief Cancel all our open orders on ::TICKER."""
        try:
            self.req("POST", "/commands/cancel", ticker=TICKER)
        except ApiError:
            pass

    def order(self, action, qty, price=None):
        """
        @brief Submit one order on ::TICKER.
        @param action "BUY" or "SELL".
        @param qty    Number of shares (orders with qty <= 0 are skipped).
        @param price  Limit price; None submits a MARKET order.
        """
        qty = int(qty)
        if qty <= 0:
            return
        wait = self.gap - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        p = dict(ticker=TICKER, action=action, quantity=qty)
        if price is None:
            p["type"] = "MARKET"
        else:
            p.update(type="LIMIT", price=round(price, 2))
        try:
            self.req("POST", "/orders", **p)
        except ApiError as e:
            print(f"  ! order rejected: {e}")
        finally:
            self.last = time.time()


def rnd(x):
    """@brief Round a price to the tick grid. @param x Price. @return Rounded price."""
    return round(round(x / TICK) * TICK, 2)


def rem(o):
    """@brief Remaining (unfilled) quantity of an order dict returned by the API."""
    return o["quantity"] - (o.get("quantity_filled") or 0)


def num(x):
    """@brief Return x if it is a number, else 0.0 (RIT sends null before the first trade)."""
    return x if isinstance(x, (int, float)) else 0.0


def others_top(book, own):
    """
    @brief Best bid and ask of the other participants.
    @param book Order book returned by GET /securities/book.
    @param own  Set of our own open order ids, excluded so we never outbid ourselves.
    @return (best_bid, best_ask); either can be None if that side is empty.
    """
    bids = [o["price"] for o in book.get("bids", []) if o["order_id"] not in own and rem(o) > 0]
    asks = [o["price"] for o in book.get("asks", []) if o["order_id"] not in own and rem(o) > 0]
    return (max(bids) if bids else None), (min(asks) if asks else None)


def sync_side(api, side, orders, levels, cap):
    """
    @brief Bring one side of our quotes in line with the target ladder.

    @details Existing orders are matched to target levels by price, oldest first (best
    queue position). An order is kept when its remaining size is close enough to the
    target; everything else is cancelled BEFORE the missing orders are placed, so the
    total exposure never exceeds @p cap.

    @param api    RIT API wrapper.
    @param side   "BUY" or "SELL".
    @param orders Our currently open orders on that side.
    @param levels Target ladder as a list of (price, quantity), most aggressive first;
                  the same price may appear several times.
    @param cap    Maximum total quantity allowed on that side (position limit headroom).
    """
    pool = {}
    for o in sorted(orders, key=lambda o: o["order_id"]):
        pool.setdefault(rnd(o["price"]), []).append(o)
    left = cap
    to_cancel, to_place = [], []
    for p, q in levels:
        q = int(max(0, min(q, left, MAX_ORDER)) // 100 * 100)
        cands = pool.get(p, [])
        if cands:
            o = cands.pop(0)
            r = rem(o)
            if q > 0 and 0.3 * q <= r <= left and r <= max(1.5 * q, q + 100):
                left -= r           # keep this order and its queue position
                continue
            to_cancel.append(o)
        if q >= 100:
            to_place.append((p, q))
            left -= q
    for os_ in pool.values():       # orders at prices that are no longer wanted
        to_cancel.extend(os_)
    for o in to_cancel:
        api.cancel(o["order_id"])
    for p, q in to_place:
        api.order(side, q, p)


def run(cfg):
    """
    @brief Main trading loop.
    @param cfg Parsed command-line arguments (see main()).

    @details Each iteration reads the case clock, our position, our open orders and the
    order book, then (in order of priority): flattens at the end of the case, cuts
    inventory above the trigger, or recomputes the target quotes and synchronises both
    sides of the book.
    """
    api = RIT(cfg.key, cfg.host)
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    ema_f = ema_s = None
    last_tick = -1

    print("Waiting for the case to start...")
    while not stop["flag"]:
        c = api.req("GET", "/case")
        if c["status"] == "ACTIVE":
            break
        time.sleep(0.5)
    sec = api.req("GET", "/securities", ticker=TICKER)[0]
    if sec.get("api_orders_per_second"):
        api.gap = 1.0 / float(sec["api_orders_per_second"])
    print(f"Market maker active ({sec.get('api_orders_per_second', '?')} orders/s). Ctrl+C to stop.")

    try:
        while not stop["flag"]:
            c = api.req("GET", "/case")
            if c["status"] != "ACTIVE":
                if c["status"] == "STOPPED":
                    print("Case finished.")
                    break
                time.sleep(0.2)
                continue
            tick, left = c["tick"], c["ticks_per_period"] - c["tick"]
            sec = api.req("GET", "/securities", ticker=TICKER)[0]
            pos = int(num(sec.get("position")))
            orders = [o for o in api.req("GET", "/orders", status="OPEN") if o["ticker"] == TICKER]
            bid, ask = others_top(api.req("GET", "/securities/book", ticker=TICKER, limit=50),
                                  {o["order_id"] for o in orders})
            bid = bid or num(sec.get("bid")) or None
            ask = ask or num(sec.get("ask")) or None
            if not (bid and ask):
                time.sleep(LOOP_SLEEP)
                continue
            mid = (bid + ask) / 2

            # Trend estimate, updated once per second (one case tick).
            if tick != last_tick:
                last_tick = tick
                if ema_f is None:
                    ema_f = ema_s = mid
                ema_f += 2 / (EMA_FAST + 1) * (mid - ema_f)
                ema_s += 2 / (EMA_SLOW + 1) * (mid - ema_s)
                print(f"t={tick:3d} pos={pos:+7d} {bid:.2f}/{ask:.2f} "
                      f"trend={(ema_f - ema_s) * 100:+5.1f}c "
                      f"realized={num(sec.get('realized')):9.2f} "
                      f"unrealized={num(sec.get('unrealized')):9.2f}")
            trend = ema_f - ema_s

            # 1) End of case: cancel everything and close the position at market.
            if left <= cfg.flatten_ticks:
                if orders:
                    api.cancel_all()
                if pos:
                    api.order("SELL" if pos > 0 else "BUY", min(abs(pos), MAX_ORDER))
                time.sleep(0.05)
                continue

            # 2) Inventory circuit breaker: reduce the position with a market order.
            if abs(pos) >= cfg.flatten_trigger:
                api.cancel_all()
                q = min(abs(pos) - cfg.flatten_target, MAX_ORDER)
                print(f"  >> position {pos:+d}: market reduction of {q}")
                api.order("SELL" if pos > 0 else "BUY", q)
                continue

            # 3) Target quotes. Skew (in cents): against inventory beyond the dead band,
            #    and with the short-term trend.
            mag = max(0, abs(pos) - cfg.deadband)
            skew = (-mag if pos > 0 else mag) / cfg.skew_shares + cfg.trend_coef * trend * 100
            spread = ask - bid
            if spread >= 0.03 - 1e-9:        # room to improve both sides by one tick
                qb, qa = bid + TICK, ask - TICK
            elif spread >= 0.02 - 1e-9:      # room for one side only: the favoured one
                qb, qa = (bid + TICK, ask) if skew > 0 else (bid, ask - TICK)
            else:                            # one-tick market: join the best prices
                qb, qa = bid, ask
            shift = round(skew) * TICK
            qb = min(qb + shift, ask - TICK)   # never cross the book
            qa = max(qa + shift, bid + TICK)
            if qa <= qb:
                qa = qb + TICK

            # Asymmetric sizes: smaller on the side that increases inventory.
            cap = END_MAX_POSITION if left <= END_TICKS else cfg.max_position
            r = max(-1.0, min(1.0, pos / cfg.max_position))
            buy_levels = [(rnd(qb - d * TICK), s * (1 - r)) for d, s in LAYERS]
            sell_levels = [(rnd(qa + d * TICK), s * (1 + r)) for d, s in LAYERS]
            buys = [o for o in orders if o["action"] == "BUY"]
            sells = [o for o in orders if o["action"] == "SELL"]
            sync_side(api, "BUY", buys, buy_levels, cap - pos)
            sync_side(api, "SELL", sells, sell_levels, cap + pos)
            time.sleep(LOOP_SLEEP)
    except ApiError as e:
        print(f"API error: {e}")
    finally:
        api.cancel_all()
        print("Open orders cancelled.")


def main():
    """@brief Parse the command line and start the trading loop."""
    p = argparse.ArgumentParser(description="RIT ALGO2 - single-security market maker")
    p.add_argument("--key", default=API_KEY, help="RIT API key")
    p.add_argument("--port", type=int, default=API_PORT, help="RIT API port")
    p.add_argument("--host", default=None, help="full API URL (overrides --port)")
    p.add_argument("--max-position", type=int, default=MAX_POSITION)
    p.add_argument("--flatten-trigger", type=int, default=FLATTEN_TRIGGER)
    p.add_argument("--flatten-target", type=int, default=FLATTEN_TARGET)
    p.add_argument("--skew-shares", type=int, default=SKEW_SHARES)
    p.add_argument("--deadband", type=int, default=DEADBAND)
    p.add_argument("--trend-coef", type=float, default=TREND_COEF)
    p.add_argument("--flatten-ticks", type=int, default=FLATTEN_TICKS)
    cfg = p.parse_args()
    cfg.host = cfg.host or f"http://localhost:{cfg.port}"
    if cfg.max_position >= 25000:
        sys.exit("--max-position must stay below 25000 (case position limit)")
    if not cfg.flatten_trigger < cfg.max_position:
        sys.exit("--flatten-trigger must be below --max-position")
    run(cfg)


if __name__ == "__main__":
    main()
