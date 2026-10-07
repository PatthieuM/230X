"""
RIT ALGO2e - Momentum taker on CNR, RY, AC

In this case the ANON order flow pushes prices into long, persistent trends,
so passive market making gets run over. This algo trades WITH the trend:

  - Signal: fast EMA - slow EMA of the mid, sampled once per case tick, in ticks.
  - Target position grows with trend strength, capped per ticker.
  - Orders are marketable LIMIT orders capped at MAX_SLIPPAGE_TICKS through the
    touch and MAX_STEP shares, so we never walk deep into a thin book. Any
    unfilled remainder is cancelled on the next loop.
  - Exit when the trend fades or flips, per-position stop-loss, drawdown breaker.
  - Flatten before the close.
  - Ctrl+C: cancel everything and flatten all positions before exiting.

Run (Anaconda Prompt):
    python algo2e_momentum.py
"""

import signal
import time
import requests

API_KEY = {'X-API-key': 'W4QQPY2E'}
BASE_URL = 'http://localhost:8000/v1'

TICKERS = ['CNR', 'RY', 'AC']

# --- signal ---
EMA_FAST = 4                 # ticks (seconds)
EMA_SLOW = 15
ENTRY_GATE = 1.0             # |trend| in price ticks to open / add
EXIT_GATE = 0.3              # |trend| below this -> close
WARMUP_TICKS = 5             # case ticks of data before the first trade

# --- sizing ---
SHARES_PER_TREND_TICK = 1_500
MAX_POSITION = 6_000         # per ticker (3 x 6,000 = 18,000 < 25,000 limit)
MAX_STEP = 2_000             # shares per order (keeps us near the top of the book)
MIN_STEP = 500               # skip adjustments smaller than this (saves fees)
MAX_SLIPPAGE_TICKS = 3       # marketable limit: worst price vs. the touch
TRADE_GAP = 0.3              # seconds between orders on one ticker

# --- risk ---
STOP_TICKS = 5               # unrealized loss per share (ticks) -> exit
STOP_COOLDOWN = 5.0          # seconds with no entries on that ticker after a stop
MAX_DRAWDOWN = 2_000         # $ drop of total P&L from peak -> flatten and pause
DD_PAUSE = 20.0
END_TICKS = 6                # flatten in the last N ticks of the case

ORDER_LIMIT = 5_000
POSITION_LIMIT = 25_000
TICK = 0.01
LOOP_SLEEP = 0.05
FLATTEN_TIMEOUT = 10.0       # seconds Ctrl+C keeps trying to get flat

stop_requested = {'flag': False}


class TickerState:
    def __init__(self):
        self.ema_fast = self.ema_slow = None
        self.ema_tick = -1
        self.samples = 0
        self.trend = 0.0
        self.pause_until = 0.0
        self.last_trade = 0.0

    def update(self, tick, mid):
        if tick == self.ema_tick:
            return
        self.ema_tick = tick
        if self.ema_fast is None:
            self.ema_fast = self.ema_slow = mid
        self.ema_fast += 2 / (EMA_FAST + 1) * (mid - self.ema_fast)
        self.ema_slow += 2 / (EMA_SLOW + 1) * (mid - self.ema_slow)
        self.samples += 1
        self.trend = (self.ema_fast - self.ema_slow) / TICK


# ---------------------------------------------------------------- API

def get_case(session):
    resp = session.get(f'{BASE_URL}/case')
    resp.raise_for_status()
    return resp.json()


def get_securities(session):
    resp = session.get(f'{BASE_URL}/securities')
    resp.raise_for_status()
    return {s['ticker']: s for s in resp.json() if s['ticker'] in TICKERS}


def cancel_all(session):
    try:
        session.post(f'{BASE_URL}/commands/cancel', params={'all': 1})
    except requests.exceptions.RequestException:
        pass


def take(session, ticker, quantity, bid, ask):
    """Marketable limit order: buy (qty > 0) or sell (qty < 0) up to
    MAX_SLIPPAGE_TICKS through the touch."""
    qty = int(min(abs(quantity), MAX_STEP, ORDER_LIMIT))
    if qty <= 0:
        return False
    if quantity > 0:
        action, price = 'BUY', ask + MAX_SLIPPAGE_TICKS * TICK
    else:
        action, price = 'SELL', bid - MAX_SLIPPAGE_TICKS * TICK
    params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': qty,
              'action': action, 'price': round(price, 2)}
    resp = session.post(f'{BASE_URL}/orders', params=params)
    if resp.ok:
        return True
    print(f'  ORDER FAILED: {action} {qty} {ticker} @ {price:.2f} -> {resp.status_code} {resp.text}')
    return False


def market(session, ticker, quantity):
    """Plain market order, used only as a last resort when flattening."""
    qty = int(min(abs(quantity), ORDER_LIMIT))
    if qty <= 0:
        return
    action = 'BUY' if quantity > 0 else 'SELL'
    session.post(f'{BASE_URL}/orders', params={'ticker': ticker, 'type': 'MARKET',
                                                 'quantity': qty, 'action': action})


# ---------------------------------------------------------------- strategy

def book_prices(sec):
    bid, ask = sec.get('bid') or 0.0, sec.get('ask') or 0.0
    return (bid, ask) if bid and ask and ask > bid else (None, None)


def target_position(st, position, now):
    if now < st.pause_until or st.samples < WARMUP_TICKS:
        return 0
    trend = st.trend
    if abs(trend) >= ENTRY_GATE:
        size = min(MAX_POSITION, abs(trend) * SHARES_PER_TREND_TICK)
        return int(size // 100 * 100) * (1 if trend > 0 else -1)
    if abs(trend) <= EXIT_GATE or position * trend < 0:
        return 0                                   # faded or flipped
    return position                                # weak but same direction: hold


def handle(session, ticker, sec, st, tick, now):
    position = int(sec.get('position') or 0)
    bid, ask = book_prices(sec)
    if bid is None:
        return
    st.update(tick, (bid + ask) / 2)

    # Stop-loss: the trend fooled us
    unrealized = sec.get('unrealized') or 0.0
    if abs(position) >= 300 and unrealized <= -STOP_TICKS * TICK * abs(position):
        print(f'  >> STOP {ticker}: pos {position:+d}, unrealized {unrealized:.0f}')
        st.pause_until = now + STOP_COOLDOWN

    target = target_position(st, position, now)
    diff = target - position
    if diff == 0 or now - st.last_trade < TRADE_GAP:
        return
    if abs(diff) < MIN_STEP and target != 0:
        return
    if take(session, ticker, diff, bid, ask):
        st.last_trade = now


def flatten(session, secs, states, now):
    """One flatten pass: take toward zero on every ticker (respects TRADE_GAP)."""
    for ticker, sec in secs.items():
        position = int(sec.get('position') or 0)
        bid, ask = book_prices(sec)
        st = states[ticker]
        if position and bid is not None and now - st.last_trade >= TRADE_GAP:
            if take(session, ticker, -position, bid, ask):
                st.last_trade = now


def flatten_on_exit(session):
    """Ctrl+C: cancel everything and keep flattening until flat or timeout,
    switching to market orders for the last few seconds."""
    print('\nCtrl+C -> cancelling orders and flattening all positions...')
    deadline = time.time() + FLATTEN_TIMEOUT
    while time.time() < deadline:
        cancel_all(session)
        try:
            secs = get_securities(session)
        except requests.exceptions.RequestException:
            time.sleep(0.3)
            continue
        positions = {t: int(s.get('position') or 0) for t, s in secs.items()}
        if not any(positions.values()):
            print('All flat. Bye.')
            return
        use_market = deadline - time.time() < FLATTEN_TIMEOUT / 3
        for ticker, pos in positions.items():
            if not pos:
                continue
            if use_market:
                market(session, ticker, -pos)
            else:
                bid, ask = book_prices(secs[ticker])
                if bid is not None:
                    take(session, ticker, -pos, bid, ask)
        print(f'  flattening {positions}')
        time.sleep(0.4)
    print('!! Could not get fully flat - check the RIT client and close positions manually.')


def disable_quickedit():
    """Windows: stop a click in the console window from freezing the program."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle_in = kernel32.GetStdHandle(-10)
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle_in, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle_in, (mode.value & ~0x0040) | 0x0080)
    except Exception:
        pass


# ---------------------------------------------------------------- main

def main():
    disable_quickedit()
    signal.signal(signal.SIGINT, lambda *_: stop_requested.update(flag=True))

    with requests.Session() as session:
        session.headers.update(API_KEY)
        states = {t: TickerState() for t in TICKERS}
        peak, halt_until, last_tick, last_print_tick = None, 0.0, None, None

        while not stop_requested['flag']:
            try:
                case = get_case(session)
                if case['status'] != 'ACTIVE':
                    print(f'Case {case["status"]}. Waiting for the case to start...')
                    time.sleep(1.0)
                    continue

                tick = case['tick']
                end_tick = case.get('ticks_per_period') or 300
                if last_tick is not None and tick < last_tick:      # new run: reset
                    states = {t: TickerState() for t in TICKERS}
                    peak, halt_until = None, 0.0
                last_tick = tick

                now = time.time()
                secs = get_securities(session)
                pnl = sum((s.get('realized') or 0.0) + (s.get('unrealized') or 0.0) for s in secs.values())
                peak = pnl if peak is None else max(peak, pnl)

                if tick != last_print_tick:
                    last_print_tick = tick
                    parts = [f"{t} {int(secs[t].get('position') or 0):+d} tr {states[t].trend:+.1f}"
                             for t in TICKERS if t in secs]
                    tag = ' PAUSED' if now < halt_until else ''
                    print(f'[{tick:3d}] P&L {pnl:+.0f}$ (peak {peak:+.0f}){tag} | ' + ' | '.join(parts))

                ending = tick >= end_tick - END_TICKS
                if not ending and now >= halt_until and peak - pnl >= MAX_DRAWDOWN:
                    halt_until = now + DD_PAUSE
                    print(f'  >> DRAWDOWN BREAKER: P&L {pnl:.0f} vs peak {peak:.0f} -> flatten, pause {DD_PAUSE:.0f}s')

                if ending or now < halt_until:
                    cancel_all(session)
                    flatten(session, secs, states, now)
                    for t in TICKERS:                   # keep EMAs warm during the pause
                        if t in secs:
                            bid, ask = book_prices(secs[t])
                            if bid is not None:
                                states[t].update(tick, (bid + ask) / 2)
                    if not ending:
                        peak = pnl
                else:
                    cancel_all(session)                 # drop unfilled remainders
                    for t in TICKERS:
                        if t in secs:
                            handle(session, t, secs[t], states[t], tick, now)

            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    time.sleep(0.2)
                    continue
                print(f'  HTTP error: {e} -> retrying')
                time.sleep(0.2)
            except requests.exceptions.ConnectionError:
                print('  Cannot reach RIT Client -> retrying')
                time.sleep(1.0)

            time.sleep(LOOP_SLEEP)

        flatten_on_exit(session)


if __name__ == '__main__':
    main()
