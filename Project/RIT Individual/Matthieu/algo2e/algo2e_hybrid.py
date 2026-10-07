#!/usr/bin/env python3
"""
@file algo2e_hybrid.py
@brief RIT ALGO2e - regime-switching trend follower on CNR, RY and AC (RIT Client REST API).

@details
Background
----------
ALGO2e differs from ALGO2 in ways that change the economics of market making:
three securities, much smaller rebates (RY even charges a fee for passive orders and
pays a rebate for active ones), a 25,000-share position limit shared across securities,
500 orders per second, and prices that trend strongly for long stretches.
Our first versions were pure market makers and lost money in this environment (they were
filled on the wrong side of every trend). Taking liquidity is cheap here (0.27 cent on
CNR, 0.15 cent on AC, and RY pays 0.14 cent), so the final algorithm follows trends with
marketable orders and stays flat when there is no clear direction.

Strategy
--------
For each security, once per second:

 1. **Regime detection.** Kaufman's efficiency ratio over the last ::ER_WINDOW seconds,
        ER = |net price change| / sum of |one-second price changes|.
    ER close to 1 means a clean trend, ER close to 0 means a choppy market.
      - ER >= ::ER_TREND  -> regime "M" (momentum): trade with the trend.
      - ER <= ::ER_RANGE  -> regime "R" (range): close any position and stay flat.
      - in between        -> keep the previous regime ("N" = undecided at start).
 2. **Momentum position.** Trend strength = fast EMA minus slow EMA of the mid price, in
    ticks. Target position = sign(trend) x ::SHARES_PER_TICK x |trend|, capped at
    ::MOM_MAX. A position is opened or increased when |trend| >= ::ENTRY_GATE, held
    while the trend keeps its sign, and closed when |trend| <= ::EXIT_GATE or the trend
    reverses.
 3. **Execution.** The position is moved towards its target with marketable limit
    orders priced ::EXIT_SLIP ticks through the best quote, in blocks of at most
    ::EXIT_CHUNK shares, so a single order cannot sweep a thin book.

Safeguards
----------
 - **Execution-delay lock.** After each order a security is locked for ::THROTTLE
   seconds, and the next order is only sent once the reported position has changed.
   (An earlier version re-sent orders before RIT had processed them, which doubled
   every exit and flipped the position.)
 - **Stop-loss** in dollars per security (::STOP_USD), followed by a cool-down.
 - **Drawdown breaker.** If total P&L falls ::MAX_DRAWDOWN below its peak, everything
   is closed and trading pauses for ::DD_PAUSE seconds.
 - **Position limits.** Per-security cap ::MOM_MAX (3 x 6,000 = 18,000 < 25,000).
 - **Clean start and end.** Any existing position is closed at start-up; the cap is
   reduced in the last ::END_TICKS seconds and everything is closed in the last
   ::FLATTEN_TICKS seconds.

Usage
-----
 1. Open the RIT client, log in, and enable the API (port and key are in the API panel).
 2. Run once, before the case starts:
        python algo2e_hybrid.py --key YOUR_API_KEY --port 9999
 3. One status line is printed per second and appended to ::LOG_FILE, e.g.
        t=180 PnL=754.50 (max 754.50) | CNR -6000 154.97/154.98 t-13.0 M er1.00 | ...
    (position, best bid/ask, trend in ticks, regime, efficiency ratio).
 4. Do not restart the script during a case: a restart clears the price history used
    for the regime detection. Run only one instance at a time.

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
from collections import deque

## Default port of the RIT Client REST API (shown in the RIT client).
API_PORT = 9999
## Default API key (shown in the RIT client). Override with --key.
API_KEY = "YOUR_API_KEY"

## Securities traded in the ALGO2e case.
TICKERS = "CNR,RY,AC"
## Maximum order size allowed by the case.
MAX_ORDER = 5000

## Window, in seconds, of the efficiency ratio.
ER_WINDOW = 20
## Minimum number of one-second price samples before a regime is assigned.
ER_MIN_SAMPLES = 10
## Efficiency ratio at or above which a security is treated as trending.
ER_TREND = 0.40
## Efficiency ratio at or below which a security is treated as range-bound.
ER_RANGE = 0.25
## EMA spans, in seconds, of the trend estimate (fast minus slow).
EMA_FAST, EMA_SLOW = 3, 12

## Momentum: target shares per tick of trend strength.
SHARES_PER_TICK = 1000
## Momentum: maximum position per security.
MOM_MAX = 6000
## Momentum: |trend| (ticks) required to open or increase a position.
ENTRY_GATE = 1.5
## Momentum: |trend| (ticks) below which the position is closed.
EXIT_GATE = 0.5
## Momentum: position adjustments smaller than this are skipped.
MIN_STEP = 500

## Unrealized loss per security, in dollars, that triggers an exit.
STOP_USD = 80.0
## Seconds without new entries on a security after a stop-loss.
COOLDOWN = 10.0
## Drop of total P&L from its peak, in dollars, that closes everything.
MAX_DRAWDOWN = 800.0
## Trading pause, in seconds, after the drawdown breaker.
DD_PAUSE = 30.0
## Maximum size of one execution order.
EXIT_CHUNK = 2000
## Execution orders are priced this many ticks through the best quote.
EXIT_SLIP = 2
## Minimum lock, in seconds, on a security after an order (raised by RIT's delay).
THROTTLE = 0.4

## In the last END_TICKS seconds the per-security cap drops to ::END_MAX_POSITION.
END_TICKS = 15
## Per-security cap near the end of the case.
END_MAX_POSITION = 500
## In the last FLATTEN_TICKS seconds all positions are closed.
FLATTEN_TICKS = 5
## Pause between two iterations of the main loop, in seconds.
LOOP_SLEEP = 0.02
## File to which every console line is appended.
LOG_FILE = "algo2e_log.txt"


class ApiError(Exception):
    """@brief Raised when the RIT API returns an unrecoverable error."""


def log(msg):
    """@brief Print a line and append it to ::LOG_FILE. @param msg Text to log."""
    print(msg, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def num(x):
    """@brief Return x if it is a number, else 0.0 (RIT sends null before the first trade)."""
    return x if isinstance(x, (int, float)) else 0.0


def disable_quickedit():
    """
    @brief Disable QuickEdit mode of the Windows console.
    @details With QuickEdit enabled, clicking in the console window blocks the next
    print() call and therefore freezes the trading loop. No effect on other systems.
    """
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)
        mode = ctypes.c_uint()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)
    except Exception:
        pass


class RIT:
    """
    @brief Minimal wrapper around the RIT Client REST API (v1).
    @details Adds the API key header, retries on HTTP 429 (rate limit) and on transient
    connection errors, and raises ApiError otherwise.
    """

    def __init__(self, key, host):
        """
        @param key  API key shown in the RIT client.
        @param host Base URL of the API, e.g. "http://localhost:9999".
        """
        self.base = host.rstrip("/") + "/v1"
        self.key = key

    def req(self, method, path, **params):
        """
        @brief Send one HTTP request and return the decoded JSON body.
        @param method HTTP verb ("GET", "POST", "DELETE").
        @param path   Endpoint path, e.g. "/securities".
        @param params Query-string parameters.
        @return Decoded JSON (dict or list).
        @throws ApiError on authentication failure, repeated connection errors or any
                non-429 HTTP error.
        """
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        r = urllib.request.Request(url, method=method, headers={"X-API-Key": self.key})
        for attempt in range(8):
            try:
                with urllib.request.urlopen(r, timeout=2) as resp:
                    return json.loads(resp.read() or b"null")
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code == 429:
                    try:
                        wait = float(json.loads(body).get("wait", 0.05))
                    except (ValueError, AttributeError):
                        wait = 0.05
                    time.sleep(min(max(wait, 0.005), 0.5))
                    continue
                if e.code == 401:
                    raise ApiError("API key rejected (401)")
                raise ApiError(f"{method} {path} {params} -> {e.code} {body[:160]}")
            except (urllib.error.URLError, OSError) as e:
                if attempt >= 4:
                    raise ApiError(f"cannot connect ({self.base}): {e}")
                time.sleep(0.2)
        raise ApiError("too many 429 responses")

    def cancel_ticker(self, tk):
        """@brief Cancel all our open orders on one security. @param tk Ticker."""
        try:
            self.req("POST", "/commands/cancel", ticker=tk)
        except ApiError:
            pass

    def order(self, tk, action, qty, price=None):
        """
        @brief Submit one order; a rejection is logged, not raised.
        @param tk     Ticker.
        @param action "BUY" or "SELL".
        @param qty    Number of shares.
        @param price  Limit price; None submits a MARKET order.
        """
        qty = int(qty)
        if qty <= 0:
            return
        p = dict(ticker=tk, action=action, quantity=qty)
        if price is None:
            p["type"] = "MARKET"
        else:
            p.update(type="LIMIT", price=price)
        try:
            self.req("POST", "/orders", **p)
        except ApiError as e:
            log(f"  ! order rejected {tk} {action} {qty}: {e}")


class TickerState:
    """@brief Per-security state kept between iterations of the main loop."""

    def __init__(self, sec):
        """@param sec Security description returned by GET /securities."""
        ## Number of quoted decimals and corresponding tick size.
        self.dec = int(num(sec.get("quoted_decimals")) or 2)
        self.tick = 10 ** -self.dec
        ## Fast and slow EMAs of the mid price.
        self.ema_f = self.ema_s = None
        ## Last case tick at which the EMAs / price history were updated.
        self.ema_tick = -1
        self.er_tick = -1
        ## One-second mid-price history for the efficiency ratio.
        self.mids = deque(maxlen=ER_WINDOW + 1)
        ## Latest efficiency ratio (None until enough samples).
        self.er = None
        ## Current regime: "N" undecided, "M" momentum, "R" range.
        self.regime = "N"
        ## No new entries before this time (set after a stop-loss).
        self.pause_until = 0.0
        ## Security locked (no new order) until this time.
        self.busy_until = 0.0
        ## Position reported when the last order was sent, and when it was sent.
        self.sent_pos = None
        self.sent_time = 0.0
        ## Text shown for this security in the status line.
        self.info = ""


def step_toward(api, tk, pos, goal, has_orders, st, bid, ask, now, cfg):
    """
    @brief Send at most one order moving the position towards a target.

    @details Uses a marketable limit order priced EXIT_SLIP ticks through the best quote
    and capped at EXIT_CHUNK shares. Nothing is sent while the security is locked, or
    while the previous order is not yet visible in the reported position (RIT execution
    delay); this prevents duplicate orders.

    @param api        RIT API wrapper.
    @param tk         Ticker.
    @param pos        Current reported position.
    @param goal       Target position.
    @param has_orders True if we still have open orders on this security.
    @param st         TickerState of the security.
    @param bid        Best bid (may be None).
    @param ask        Best ask (may be None).
    @param now        Current time (time.time()).
    @param cfg        Parsed command-line arguments.
    """
    if now < st.busy_until:
        return
    if st.sent_pos is not None and pos == st.sent_pos and now - st.sent_time < 3 * cfg.throttle + 1.0:
        return                                   # previous order not processed yet
    if has_orders:
        api.cancel_ticker(tk)                    # remove any unfilled remainder
    diff = goal - pos
    if diff == 0:
        st.sent_pos = None
        return
    qty = min(abs(diff), cfg.exit_chunk, MAX_ORDER)
    if diff < 0:
        price = None if cfg.exit_slip < 0 or not bid else round(bid - cfg.exit_slip * st.tick, st.dec)
        api.order(tk, "SELL", qty, price)
    else:
        price = None if cfg.exit_slip < 0 or not ask else round(ask + cfg.exit_slip * st.tick, st.dec)
        api.order(tk, "BUY", qty, price)
    st.sent_pos, st.sent_time = pos, now
    st.busy_until = now + cfg.throttle


def momentum_target(pos, trend, cap, paused, cfg):
    """
    @brief Target position of the momentum regime.
    @param pos    Current position.
    @param trend  Trend strength in ticks (fast EMA minus slow EMA).
    @param cap    Maximum absolute position allowed right now.
    @param paused True during the cool-down after a stop-loss.
    @param cfg    Parsed command-line arguments.
    @return Target position in shares (signed).
    """
    sign_t = (trend > 0) - (trend < 0)
    sign_p = (pos > 0) - (pos < 0)
    if paused:
        goal = 0
    elif abs(trend) >= cfg.entry_gate:
        goal = sign_t * int(min(cap, abs(trend) * cfg.shares_per_tick) // 100 * 100)
        if sign_p == sign_t and abs(goal) < abs(pos):
            goal = pos                           # slightly weaker trend: keep the position
    elif abs(trend) <= cfg.exit_gate or (sign_p and sign_t != sign_p):
        goal = 0                                 # trend faded or reversed
    else:
        goal = pos                               # weak trend in our direction: hold
    if abs(goal) > cap:
        goal = (1 if goal > 0 else -1) * cap
    return goal


def handle(api, tk, sec, has_orders, st, tick, left, now, cfg):
    """
    @brief Process one security for one iteration: update signals, pick the regime, trade.
    @param api        RIT API wrapper.
    @param tk         Ticker.
    @param sec        Security data returned by GET /securities (position, bid, ask, P&L).
    @param has_orders True if we have open orders on this security.
    @param st         TickerState of the security.
    @param tick       Current case tick (seconds since the start).
    @param left       Ticks remaining in the case.
    @param now        Current time (time.time()).
    @param cfg        Parsed command-line arguments.
    """
    pos = int(num(sec.get("position")))
    bid, ask = num(sec.get("bid")) or None, num(sec.get("ask")) or None
    if not (bid and ask) or ask <= bid:
        st.info = f"{tk} {pos:+6d} (no prices)"
        return
    mid = (bid + ask) / 2
    unreal = num(sec.get("unrealized"))

    # Signals, updated once per second: trend (EMAs) and efficiency ratio.
    if tick != st.ema_tick:
        st.ema_tick = tick
        if st.ema_f is None:
            st.ema_f = st.ema_s = mid
        st.ema_f += 2 / (EMA_FAST + 1) * (mid - st.ema_f)
        st.ema_s += 2 / (EMA_SLOW + 1) * (mid - st.ema_s)
        st.mids.append(mid)
        if len(st.mids) >= ER_MIN_SAMPLES:
            m = list(st.mids)
            moves = sum(abs(b - a) for a, b in zip(m, m[1:]))
            st.er = abs(m[-1] - m[0]) / moves if moves > 0 else 0.0
    trend = (st.ema_f - st.ema_s) / st.tick

    # Regime with hysteresis: unchanged while ER is between the two thresholds.
    if st.er is not None:
        if st.er >= cfg.er_trend:
            st.regime = "M"
        elif st.er <= cfg.er_range:
            st.regime = "R"
    er_txt = "--" if st.er is None else f"{st.er:.2f}"
    st.info = (f"{tk} {pos:+6d} {bid:.{st.dec}f}/{ask:.{st.dec}f} "
               f"t{trend:+.1f} {st.regime} er{er_txt}")
    if st.sent_pos is not None and pos != st.sent_pos:
        st.sent_pos = None                       # previous order has been processed

    # Range or undecided: no position.
    if st.regime != "M":
        if pos or has_orders:
            step_toward(api, tk, pos, 0, has_orders, st, bid, ask, now, cfg)
        return

    # Momentum regime: stop-loss, then target position.
    if pos and unreal <= -cfg.stop_usd and st.sent_pos is None and now >= st.busy_until:
        log(f"  >> STOP {tk}: position {pos:+d}, unrealized {unreal:.0f} -> exit")
        st.pause_until = now + cfg.cooldown
    cap = cfg.end_max_position if left <= cfg.end_ticks else cfg.mom_max
    goal = momentum_target(pos, trend, cap, now < st.pause_until, cfg)
    if goal != 0 and goal != pos and abs(goal - pos) < cfg.min_step and st.sent_pos is None:
        goal = pos                               # adjustment too small to be worth the fee
    if goal != pos or has_orders:
        step_toward(api, tk, pos, goal, has_orders, st, bid, ask, now, cfg)


def run(cfg):
    """
    @brief Main loop: wait for the case, close any initial position, then trade until
           the case stops or Ctrl+C is pressed.
    @param cfg Parsed command-line arguments (see main()).
    """
    disable_quickedit()
    api = RIT(cfg.key, cfg.host)
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    tickers = [x.strip().upper() for x in cfg.tickers.split(",") if x.strip()]

    log("Waiting for the case to start...")
    while not stop["flag"]:
        if api.req("GET", "/case")["status"] == "ACTIVE":
            break
        time.sleep(0.3)

    secs = {s["ticker"]: s for s in api.req("GET", "/securities")}
    missing = [tk for tk in tickers if tk not in secs]
    if missing:
        log(f"Tickers not found: {missing}. Case tickers: {sorted(secs)}")
        tickers = [tk for tk in tickers if tk in secs]
        if not tickers:
            return
    # The lock after each order must cover RIT's own execution delay.
    delays = [num(secs[tk].get("execution_delay_ms")) for tk in tickers]
    cfg.throttle = max(cfg.throttle, 1.5 * max(delays) / 1000.0 + 0.1)
    state = {tk: TickerState(secs[tk]) for tk in tickers}
    log(f"Hybrid active on {tickers}. RIT delay {max(delays):.0f} ms -> lock "
        f"{cfg.throttle:.2f} s. Ctrl+C to stop.")

    peak, halt_until, last_tick = None, 0.0, -1
    started = cfg.keep           # False until any pre-existing position has been closed
    try:
        while not stop["flag"]:
            c = api.req("GET", "/case")
            if c["status"] != "ACTIVE":
                if c["status"] == "STOPPED":
                    log("Case finished.")
                    break
                time.sleep(0.2)
                continue
            now = time.time()
            tick, left = c["tick"], c["ticks_per_period"] - c["tick"]
            secs = {s["ticker"]: s for s in api.req("GET", "/securities")}
            open_orders = api.req("GET", "/orders", status="OPEN") or []
            has = {tk: any(o["ticker"] == tk for o in open_orders) for tk in tickers}
            pos = {tk: int(num(secs[tk].get("position"))) for tk in tickers}
            pnl = sum(num(secs[tk].get("realized")) + num(secs[tk].get("unrealized"))
                      for tk in tickers)
            peak = pnl if peak is None else max(peak, pnl)

            if tick != last_tick:
                last_tick = tick
                tag = " PAUSE" if now < halt_until else ""
                log(f"t={tick:3d} PnL={pnl:9.2f} (max {peak:9.2f}){tag} | "
                    + " | ".join(state[tk].info or tk for tk in tickers))

            # Close everything: at start-up, at the end of the case, or after the
            # drawdown breaker has fired.
            ending = left <= cfg.flatten_ticks
            if not ending and started and now >= halt_until and peak - pnl >= cfg.max_drawdown:
                halt_until = now + cfg.dd_pause
                log(f"  >> DRAWDOWN BREAKER: P&L {pnl:.0f} vs peak {peak:.0f} -> close all, "
                    f"pause {cfg.dd_pause:.0f} s")
            if not started or ending or now < halt_until:
                if not started and not any(pos.values()):
                    started = True
                    continue
                for tk in tickers:
                    st, s = state[tk], secs[tk]
                    if st.sent_pos is not None and pos[tk] != st.sent_pos:
                        st.sent_pos = None
                    if pos[tk] or has[tk]:
                        step_toward(api, tk, pos[tk], 0, has[tk], st, num(s.get("bid")) or None,
                                    num(s.get("ask")) or None, now, cfg)
                    st.info = f"{tk} {pos[tk]:+6d} (closing)"
                if not ending:
                    peak = pnl               # measure the next drawdown from here
                time.sleep(LOOP_SLEEP)
                continue

            for tk in tickers:
                handle(api, tk, secs[tk], has[tk], state[tk], tick, left, now, cfg)
            time.sleep(LOOP_SLEEP)
    except ApiError as e:
        log(f"API error: {e}")
    finally:
        for tk in tickers:
            api.cancel_ticker(tk)
        log("Open orders cancelled.")


def main():
    """@brief Parse the command line and start the trading loop."""
    p = argparse.ArgumentParser(description="RIT ALGO2e - regime-switching trend follower")
    p.add_argument("--key", default=API_KEY, help="RIT API key")
    p.add_argument("--port", type=int, default=API_PORT, help="RIT API port")
    p.add_argument("--host", default=None, help="full API URL (overrides --port)")
    p.add_argument("--tickers", default=TICKERS)
    p.add_argument("--keep", action="store_true", help="do not close positions at start-up")
    p.add_argument("--er-trend", type=float, default=ER_TREND)
    p.add_argument("--er-range", type=float, default=ER_RANGE)
    p.add_argument("--shares-per-tick", type=int, default=SHARES_PER_TICK)
    p.add_argument("--mom-max", type=int, default=MOM_MAX)
    p.add_argument("--entry-gate", type=float, default=ENTRY_GATE)
    p.add_argument("--exit-gate", type=float, default=EXIT_GATE)
    p.add_argument("--min-step", type=int, default=MIN_STEP)
    p.add_argument("--stop-usd", type=float, default=STOP_USD)
    p.add_argument("--cooldown", type=float, default=COOLDOWN)
    p.add_argument("--max-drawdown", type=float, default=MAX_DRAWDOWN)
    p.add_argument("--dd-pause", type=float, default=DD_PAUSE)
    p.add_argument("--exit-chunk", type=int, default=EXIT_CHUNK)
    p.add_argument("--exit-slip", type=int, default=EXIT_SLIP, help="-1 = pure market orders")
    p.add_argument("--throttle", type=float, default=THROTTLE)
    p.add_argument("--end-ticks", type=int, default=END_TICKS)
    p.add_argument("--end-max-position", type=int, default=END_MAX_POSITION)
    p.add_argument("--flatten-ticks", type=int, default=FLATTEN_TICKS)
    cfg = p.parse_args()
    cfg.host = cfg.host or f"http://localhost:{cfg.port}"
    n = len([x for x in cfg.tickers.split(",") if x.strip()])
    if cfg.mom_max * n > 24000:
        sys.exit("--mom-max x number of tickers must stay <= 24000 (25,000-share limit)")
    if not cfg.exit_gate < cfg.entry_gate:
        sys.exit("--exit-gate must be below --entry-gate")
    run(cfg)


if __name__ == "__main__":
    main()
