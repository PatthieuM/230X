"""
RIT ALGO2 - Momentum taker on ALGO (market orders only)

In the practice cases the price of ALGO was pushed hard in one direction
for long stretches, which runs over a passive market maker: the quote that
fills is always the one on the wrong side of the push. This algo follows
the push instead:

  - Signal: fast EMA - slow EMA of the mid, sampled once per case tick, in cents.
  - Target position grows with the strength of the push, capped at MAX_POSITION.
  - Every trade is a MARKET order of at most MAX_STEP shares, so the position
    follows the signal immediately.
  - Exit when the push fades or flips, per-position stop-loss, drawdown breaker.
  - Flatten before the close.
  - Ctrl+C: cancel everything and flatten the position before exiting.

Costs: a market order pays the 1 cent commission plus half the spread, about
1.5 cents per share, so a round trip needs a move of about 3 cents to pay.

Run (Anaconda Prompt):
    python algo2_momentum.py
"""

import signal
import time
import requests

API_KEY = {'X-API-key': 'W4QQPY2E'}
BASE_URL = 'http://localhost:8000/v1'

TICKER = 'ALGO'

# --- signal ---
EMA_FAST = 4                 # ticks (seconds)
EMA_SLOW = 15
ENTRY_GATE = 1.0             # |trend| in cents to open / add
EXIT_GATE = 0.3              # |trend| below this -> close
WARMUP_TICKS = 5             # case ticks of data before the first trade

# --- sizing ---
SHARES_PER_TREND_CENT = 2_500
MAX_POSITION = 10_000        # well under the 25,000 limit
MAX_STEP = 2_500             # shares per market order
MIN_STEP = 1_000             # skip adjustments smaller than this (saves commissions)
TRADE_GAP = 0.3              # seconds between orders

# --- risk ---
STOP_CENTS = 5               # unrealized loss per share (cents) -> exit
STOP_COOLDOWN = 5.0          # seconds with no entries after a stop
MAX_DRAWDOWN = 2_000         # $ drop of P&L from peak -> flatten and pause
DD_PAUSE = 20.0
END_TICKS = 6                # flatten in the last N ticks of the case

ORDER_LIMIT = 5_000
CENT = 0.01
LOOP_SLEEP = 0.05
FLATTEN_TIMEOUT = 10.0       # seconds Ctrl+C keeps trying to get flat

stop_requested = {'flag': False}


class SignalState:
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
        self.trend = (self.ema_fast - self.ema_slow) / CENT


# ---------------------------------------------------------------- API

def get_case(session):
    resp = session.get(f'{BASE_URL}/case')
    resp.raise_for_status()
    return resp.json()


def get_security(session):
    resp = session.get(f'{BASE_URL}/securities', params={'ticker': TICKER})
    resp.raise_for_status()
    for s in resp.json():
        if s['ticker'] == TICKER:
            return s
    return None


def cancel_all(session):
    try:
        session.post(f'{BASE_URL}/commands/cancel', params={'all': 1})
    except requests.exceptions.RequestException:
        pass


def market(session, quantity):
    """Market order: buy (quantity > 0) or sell (quantity < 0), at most MAX_STEP shares."""
    qty = int(min(abs(quantity), MAX_STEP, ORDER_LIMIT))
    if qty <= 0:
        return False
    action = 'BUY' if quantity > 0 else 'SELL'
    resp = session.post(f'{BASE_URL}/orders', params={'ticker': TICKER, 'type': 'MARKET',
                                                        'quantity': qty, 'action': action})
    if resp.ok:
        return True
    print(f'  ORDER FAILED: {action} {qty} {TICKER} -> {resp.status_code} {resp.text}')
    return False


# ---------------------------------------------------------------- strategy

def mid_price(sec):
    bid, ask = sec.get('bid') or 0.0, sec.get('ask') or 0.0
    return (bid + ask) / 2 if bid and ask and ask > bid else None


def target_position(st, position, now):
    if now < st.pause_until or st.samples < WARMUP_TICKS:
        return 0
    trend = st.trend
    if abs(trend) >= ENTRY_GATE:
        size = min(MAX_POSITION, abs(trend) * SHARES_PER_TREND_CENT)
        return int(size // 100 * 100) * (1 if trend > 0 else -1)
    if abs(trend) <= EXIT_GATE or position * trend < 0:
        return 0                                   # faded or flipped
    return position                                # weak but same direction: hold


def handle(session, sec, st, tick, now):
    position = int(sec.get('position') or 0)
    mid = mid_price(sec)
    if mid is None:
        return
    st.update(tick, mid)

    # Stop-loss: the push reversed on us
    unrealized = sec.get('unrealized') or 0.0
    if abs(position) >= 500 and unrealized <= -STOP_CENTS * CENT * abs(position):
        print(f'  >> STOP: pos {position:+d}, unrealized {unrealized:.0f}')
        st.pause_until = now + STOP_COOLDOWN

    target = target_position(st, position, now)
    diff = target - position
    if diff == 0 or now - st.last_trade < TRADE_GAP:
        return
    if abs(diff) < MIN_STEP and target != 0:
        return
    if market(session, diff):
        st.last_trade = now


def flatten(session, sec, st, now):
    """One flatten pass toward zero (respects TRADE_GAP)."""
    position = int(sec.get('position') or 0)
    if position and now - st.last_trade >= TRADE_GAP:
        if market(session, -position):
            st.last_trade = now


def flatten_on_exit(session):
    """Ctrl+C: cancel everything and keep flattening until flat or timeout."""
    print('\nCtrl+C -> cancelling orders and flattening the position...')
    deadline = time.time() + FLATTEN_TIMEOUT
    while time.time() < deadline:
        cancel_all(session)
        try:
            sec = get_security(session)
        except requests.exceptions.RequestException:
            time.sleep(0.3)
            continue
        position = int(sec.get('position') or 0) if sec else 0
        if not position:
            print('Flat. Bye.')
            return
        market(session, -position)
        print(f'  flattening {position:+d}')
        time.sleep(0.4)
    print('!! Could not get fully flat - check the RIT client and close the position manually.')


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
        st = SignalState()
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
                    st = SignalState()
                    peak, halt_until = None, 0.0
                last_tick = tick

                now = time.time()
                sec = get_security(session)
                if sec is None:
                    time.sleep(LOOP_SLEEP)
                    continue
                pnl = (sec.get('realized') or 0.0) + (sec.get('unrealized') or 0.0)
                peak = pnl if peak is None else max(peak, pnl)

                if tick != last_print_tick:
                    last_print_tick = tick
                    tag = ' PAUSED' if now < halt_until else ''
                    print(f'[{tick:3d}] P&L {pnl:+.0f}$ (peak {peak:+.0f}){tag} | '
                          f'{TICKER} {int(sec.get("position") or 0):+d} tr {st.trend:+.1f}')

                ending = tick >= end_tick - END_TICKS
                if not ending and now >= halt_until and peak - pnl >= MAX_DRAWDOWN:
                    halt_until = now + DD_PAUSE
                    print(f'  >> DRAWDOWN BREAKER: P&L {pnl:.0f} vs peak {peak:.0f} -> flatten, pause {DD_PAUSE:.0f}s')

                if ending or now < halt_until:
                    flatten(session, sec, st, now)
                    mid = mid_price(sec)                # keep EMAs warm during the pause
                    if mid is not None:
                        st.update(tick, mid)
                    if not ending:
                        peak = pnl
                else:
                    handle(session, sec, st, tick, now)

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
