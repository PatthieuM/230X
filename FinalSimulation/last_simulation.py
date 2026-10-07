#!/usr/bin/env python3
"""
RIT ALGO2e - INVERSE of the market maker: momentum liquidity TAKER on CNR, RY, AC.

The market maker posted limit orders on both sides and leaned AGAINST moves,
so trends ran it over. This version does the opposite:
  * never posts limit orders - only MARKET orders (takes liquidity);
  * trades WITH the trend: EMA fast > EMA slow -> long, EMA fast < EMA slow -> short;
  * holds while the trend lasts, exits when it fades or reverses;
  * keeps risk controls: per-position stop, drawdown breaker, end-of-case flatten.

Run:  & "C:\\ProgramData\\Anaconda3\\python.exe" last_simulation.py
"""

import argparse
import json
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_PORT = 7238
API_KEY = ""

# ticker : (market order fee, limit rebate) - negative fee = you get PAID to take
FEES = {"CNR": (0.0027, 0.0023), "RY": (-0.0014, -0.0020), "AC": (0.0015, 0.0011)}
MAX_ORDER = 5000

SHARES_PER_TICK = 2000   # target position per tick of trend strength
MAX_POSITION = 7000      # per ticker (3 x 7000 = 21,000 < 25,000 limit)
ENTRY_GATE = 1.0         # |trend| in ticks needed to open / add
EXIT_GATE = 0.3          # |trend| below this -> close the position
MIN_STEP = 500           # ignore adjustments smaller than this (saves fees)
TRADE_GAP = 0.30         # seconds between orders on one ticker (RIT position lags)
STOP_TICKS = 4.0         # unrealized loss per share (ticks) -> exit at market
COOLDOWN = 5.0           # no new entries on that ticker after a stop
MAX_DRAWDOWN = 1500.0    # total P&L drop from peak -> flatten everything
DD_PAUSE = 30.0
EMA_FAST, EMA_SLOW = 3, 12   # in ticks (seconds)
END_TICKS = 5            # last ticks: flatten and stop trading
LOOP_SLEEP = 0.05
LOG_FILE = "momentum_log.txt"


class ApiError(Exception):
    pass


def log(msg):
    print(msg, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def num(x):
    return x if isinstance(x, (int, float)) else 0.0


def disable_quickedit():
    """Windows: stop a click in the console from freezing the program."""
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
    def __init__(self, key, host):
        self.base = host.rstrip("/") + "/v1"
        self.key = key

    def req(self, method, path, **params):
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
        raise ApiError("too many 429s")

    def cancel_ticker(self, tk):
        try:
            self.req("POST", "/commands/cancel", ticker=tk)
        except ApiError:
            pass

    def market(self, tk, action, qty):
        qty = int(min(abs(qty), MAX_ORDER))
        if qty <= 0:
            return False
        try:
            self.req("POST", "/orders", ticker=tk, action=action, quantity=qty, type="MARKET")
            return True
        except ApiError as e:
            log(f"  ! order rejected {tk} {action} {qty}: {e}")
            return False


class TickerState:
    def __init__(self, sec):
        self.dec = int(num(sec.get("quoted_decimals")) or 2)
        self.tick = 10 ** -self.dec
        self.ema_f = self.ema_s = None
        self.ema_tick = -1
        self.trend = 0.0
        self.pause_until = 0.0
        self.last_trade = 0.0
        self.info = ""


def trade_toward(api, tk, st, pos, target, now, cfg, force=False):
    """Send one MARKET order moving pos toward target (respects TRADE_GAP)."""
    diff = target - pos
    if diff == 0:
        return
    if not force and abs(diff) < cfg.min_step and target != 0:
        return
    if now - st.last_trade < cfg.trade_gap:
        return
    if api.market(tk, "BUY" if diff > 0 else "SELL", diff):
        st.last_trade = now


def handle(api, tk, sec, st, tick, now, cfg):
    pos = int(num(sec.get("position")))
    bid, ask = num(sec.get("bid")), num(sec.get("ask"))
    if not (bid and ask) or ask <= bid:
        st.info = f"{tk} {pos:+6d} (no prices)"
        return
    t = st.tick
    mid = (bid + ask) / 2
    if tick != st.ema_tick:
        st.ema_tick = tick
        if st.ema_f is None:
            st.ema_f = st.ema_s = mid
        st.ema_f += 2 / (cfg.ema_fast + 1) * (mid - st.ema_f)
        st.ema_s += 2 / (cfg.ema_slow + 1) * (mid - st.ema_s)
        st.trend = (st.ema_f - st.ema_s) / t          # in ticks
    trend = st.trend
    unreal = num(sec.get("unrealized"))
    st.info = f"{tk} {pos:+6d} {bid:.{st.dec}f}/{ask:.{st.dec}f} t{trend:+.1f}"

    # --- stop-loss: trend fooled us -> get out, cool down -------------------
    if abs(pos) >= 300 and unreal <= -cfg.stop_ticks * t * abs(pos):
        log(f"  >> STOP {tk}: position {pos:+d}, unrealized {unreal:.0f} -> exit at market")
        st.pause_until = now + cfg.cooldown
        trade_toward(api, tk, st, pos, 0, now, cfg, force=True)
        return

    # --- target position: ride the trend -------------------------------------
    sign_trend = (trend > 0) - (trend < 0)
    sign_pos = (pos > 0) - (pos < 0)
    if now < st.pause_until:
        target = 0
    elif abs(trend) >= cfg.entry_gate:
        size = min(cfg.max_position, abs(trend) * cfg.shares_per_tick)
        target = sign_trend * int(size // 100 * 100)
    elif abs(trend) <= cfg.exit_gate or (sign_pos and sign_trend != sign_pos):
        target = 0                                     # trend faded or reversed
    else:
        target = pos                                   # weak but same-direction trend: hold

    trade_toward(api, tk, st, pos, target, now, cfg)


def flatten_all(api, tickers, secs, state, now, cfg):
    for tk in tickers:
        pos = int(num(secs[tk].get("position")))
        trade_toward(api, tk, state[tk], pos, 0, now, cfg, force=True)
        state[tk].info = f"{tk} {pos:+6d} (flatten)"


def run(cfg):
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
    tickers = [tk for tk in tickers if tk in secs]
    if not tickers:
        log(f"No matching tickers. Case tickers: {sorted(secs)}")
        return
    state = {tk: TickerState(secs[tk]) for tk in tickers}
    for tk in tickers:
        api.cancel_ticker(tk)                       # clear any leftover limit orders
    log(f"Momentum taker active on {tickers}. Ctrl+C to stop.")

    peak, halt_until, last_tick = None, 0.0, -1
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
            pnl = sum(num(secs[tk].get("realized")) + num(secs[tk].get("unrealized")) for tk in tickers)
            peak = pnl if peak is None else max(peak, pnl)

            if tick != last_tick:
                last_tick = tick
                tag = " PAUSE" if now < halt_until else ""
                log(f"t={tick:3d} PnL={pnl:9.2f} (max {peak:9.2f}){tag} | "
                    + " | ".join(state[tk].info or tk for tk in tickers))

            ending = left <= cfg.end_ticks
            if not ending and now >= halt_until and peak - pnl >= cfg.max_drawdown:
                halt_until = now + cfg.dd_pause
                log(f"  >> DRAWDOWN BREAKER: P&L {pnl:.0f} vs peak {peak:.0f} -> flatten, "
                    f"pause {cfg.dd_pause:.0f} s")
            if ending or now < halt_until:
                flatten_all(api, tickers, secs, state, now, cfg)
                if not ending:
                    peak = pnl
                time.sleep(LOOP_SLEEP)
                continue

            for tk in tickers:
                handle(api, tk, secs[tk], state[tk], tick, now, cfg)
            time.sleep(LOOP_SLEEP)
    except ApiError as e:
        log(f"API error: {e}")
    finally:
        log("Stopped. Check the RIT client: positions should be flat.")


def main():
    p = argparse.ArgumentParser(description="RIT ALGO2e - momentum taker (inverse of the market maker)")
    p.add_argument("--key", default=API_KEY)
    p.add_argument("--host", default=f"http://localhost:{API_PORT}")
    p.add_argument("--tickers", default="CNR,RY,AC")
    p.add_argument("--shares-per-tick", type=int, default=SHARES_PER_TICK)
    p.add_argument("--max-position", type=int, default=MAX_POSITION)
    p.add_argument("--entry-gate", type=float, default=ENTRY_GATE)
    p.add_argument("--exit-gate", type=float, default=EXIT_GATE)
    p.add_argument("--min-step", type=int, default=MIN_STEP)
    p.add_argument("--trade-gap", type=float, default=TRADE_GAP)
    p.add_argument("--stop-ticks", type=float, default=STOP_TICKS)
    p.add_argument("--cooldown", type=float, default=COOLDOWN)
    p.add_argument("--max-drawdown", type=float, default=MAX_DRAWDOWN)
    p.add_argument("--dd-pause", type=float, default=DD_PAUSE)
    p.add_argument("--ema-fast", type=float, default=EMA_FAST)
    p.add_argument("--ema-slow", type=float, default=EMA_SLOW)
    p.add_argument("--end-ticks", type=int, default=END_TICKS)
    cfg = p.parse_args()
    n = len([x for x in cfg.tickers.split(",") if x.strip()])
    if cfg.max_position * n > 24000:
        sys.exit("--max-position x number of tickers must stay <= 24000 (25,000 limit)")
    if not cfg.exit_gate < cfg.entry_gate:
        sys.exit("--exit-gate must be < --entry-gate")
    run(cfg)


if __name__ == "__main__":
    main()
