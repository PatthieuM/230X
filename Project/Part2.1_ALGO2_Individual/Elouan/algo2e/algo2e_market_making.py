"""
RIT ALGO2e - Algorithmic Market Making Capstone

    Ticker   Active fee   Passive rebate
    CNR       0.0027        0.0023     -> maker-taker, rebates help
    RY       -0.0014       -0.0020     -> inverted: makers PAY, takers are PAID
    AC        0.0015        0.0011     -> maker-taker, smaller rebate

Why the first versions lost money: they quoted big size at every spread and
kept getting filled on the wrong side while prices trended (adverse
selection), then dumped inventory with market orders into thin books.

This version only provides liquidity when it is paid enough to:
  - Edge filter: spread + 2*rebate must beat MIN_EDGE AND a fraction of the
    recent price range (no quoting 1-cent spreads on a stock moving 10 cents).
  - Direction filter: no new buying while the price is falling or the book is
    ask-heavy, and vice versa (trend over TREND_SECONDS + order imbalance).
  - Own orders are removed from the book before computing spread/trend/size,
    so we never chase our own quotes.
  - Small size, inventory skew, and tight per-ticker limits.
  - If the price moves against our inventory, we exit with a capped
    marketable limit order (never a market order walking the book).
  - Circuit breakers: a ticker that loses TICKER_DRAWDOWN from its peak is
    benched for COOLDOWN_SECONDS; total P&L below GLOBAL_STOP -> unwind only.
  - Flatten before the close; Ctrl+C cancels all resting orders.

Before running: load ALGO2e in the RIT Client, click the API icon in the
status bar for the port and key, paste the key below. Start the script
BEFORE the case starts and leave it running.
"""

import time
from collections import deque
import requests
from concurrent.futures import ThreadPoolExecutor

API_KEY = {'X-API-key': 'W4QQPY2E'}
BASE_URL = 'http://localhost:8000/v1'

# rebate = passive rebate (negative = maker fee); base_qty = shares per quote
TICKERS = {
    'CNR': {'rebate': 0.0023, 'base_qty': 500},
    'RY':  {'rebate': -0.0020, 'base_qty': 300},
    'AC':  {'rebate': 0.0011, 'base_qty': 500},
}

ORDER_LIMIT = 5_000
POSITION_LIMIT = 25_000
TICK = 0.01

# --- when to quote ---
MIN_EDGE = 0.015             # min (spread + 2*rebate) per share
VOL_SECONDS = 5              # window for recent high-low range of the mid
VOL_MULT = 0.3               # spread must be >= VOL_MULT * recent range
MAX_SPREAD_TICKS = 20        # wider = broken book, don't add inventory
TREND_SECONDS = 1.5          # window for the trend filter
TREND_TICKS = 3              # mid move over the window that counts as a trend
IMBALANCE_LEVELS = 3         # book levels used for order imbalance
IMBALANCE_BLOCK = 0.6        # |imbalance| above this blocks the side being run over

# --- inventory ---
SOFT_LIMIT = 800             # start shrinking the accumulating side
HARD_LIMIT = 2_000           # stop accumulating
MAX_SKEW_TICKS = 3           # quote skew at HARD_LIMIT
EXIT_MIN_POS = 100           # min |position| before an adverse-move exit
EXIT_MAX_QTY = 1_000         # max shares per exit order
EXIT_COOLDOWN = 0.5          # seconds between exit orders on one ticker

# --- momentum (ride strong trends instead of sitting out) ---
MOMENTUM = True              # False = pure market making
MOM_SECONDS = 3              # trend window for entries
MOM_ENTRY_TICKS = 6          # mid move over MOM_SECONDS needed to enter
MOM_IMB = 0.5                # book imbalance must agree at least this much
MOM_MAX_POS = 3_000          # target position per ticker while riding a trend
MOM_STEP = 1_000             # max shares per take order
MOM_TRADE_COOLDOWN = 0.3     # seconds between take orders on one ticker
MOM_TRAIL_TICKS = 8          # exit when mid retraces this far from its best
MOM_EXIT_IMB = 0.5           # exit when imbalance flips this far against us
MOM_REENTRY_COOLDOWN = 3     # seconds after an exit before re-entering
MOM_NO_ENTRY_TICKS = 30      # no new trend trades in the last N ticks of the case

# --- circuit breakers ---
TICKER_DRAWDOWN = 2_500      # $ below a ticker's peak P&L -> bench it
COOLDOWN_SECONDS = 20        # how long a benched ticker sits out
GLOBAL_STOP = -15_000        # total P&L below this -> unwind only for the rest

FLATTEN_TICKS_BEFORE_END = 12
FLATTEN_SLIPPAGE_TICKS = 3
REQUOTE_QTY_RATIO = 0.5
POLL_SLEEP = 0.05
RATE_LIMIT_BACKOFF = 0.5
STATUS_INTERVAL = 1.0

executor = ThreadPoolExecutor(max_workers=8)
mid_history = {t: deque() for t in TICKERS}     # (time, mid)
peak_pnl = {t: 0.0 for t in TICKERS}
benched_until = {t: 0.0 for t in TICKERS}
last_exit = {t: 0.0 for t in TICKERS}
signals = {t: {} for t in TICKERS}               # last computed signals, for the status line
state = {'trader_id': None, 'stopped': False, 'late': False}
# dir: +1 long trend, -1 short trend, 0 none; exiting: unwinding to flat
mom = {t: {'dir': 0, 'best': 0.0, 'exiting': False, 'last_trade': 0.0, 'last_exit': 0.0} for t in TICKERS}


# ---------------------------------------------------------------- API

def get_case(session):
    resp = session.get(f'{BASE_URL}/case')
    resp.raise_for_status()
    return resp.json()


def get_trader_id(session):
    resp = session.get(f'{BASE_URL}/trader')
    resp.raise_for_status()
    return resp.json().get('trader_id')


def get_book(session, ticker):
    """Top of book with our own orders removed: (bid, ask, bid_size, ask_size, imbalance)."""
    resp = session.get(f'{BASE_URL}/securities/book', params={'ticker': ticker, 'limit': 20})
    resp.raise_for_status()
    book = resp.json()

    def levels(side):
        agg = {}
        for o in book.get(side) or []:
            if o.get('trader_id') == state['trader_id']:
                continue
            qty = o['quantity'] - o.get('quantity_filled', 0)
            if qty > 0:
                agg[o['price']] = agg.get(o['price'], 0) + qty
        return agg

    bids, asks = levels('bids') or levels('bid'), levels('asks') or levels('ask')
    if not bids or not asks:
        return None
    bid_prices = sorted(bids, reverse=True)
    ask_prices = sorted(asks)
    bid_vol = sum(bids[p] for p in bid_prices[:IMBALANCE_LEVELS])
    ask_vol = sum(asks[p] for p in ask_prices[:IMBALANCE_LEVELS])
    imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    return bid_prices[0], ask_prices[0], bids[bid_prices[0]], asks[ask_prices[0]], imbalance


def get_books(session):
    futures = {t: executor.submit(get_book, session, t) for t in TICKERS}
    return {t: f.result() for t, f in futures.items()}


def get_positions_and_pnl(session):
    resp = session.get(f'{BASE_URL}/securities')
    resp.raise_for_status()
    positions, pnls = {t: 0 for t in TICKERS}, {t: 0.0 for t in TICKERS}
    for s in resp.json():
        if s['ticker'] in TICKERS:
            positions[s['ticker']] = int(s.get('position') or 0)
            pnls[s['ticker']] = (s.get('realized') or 0.0) + (s.get('unrealized') or 0.0)
    return positions, pnls


def get_open_orders(session):
    resp = session.get(f'{BASE_URL}/orders', params={'status': 'OPEN'})
    resp.raise_for_status()
    grouped = {t: {'BUY': [], 'SELL': []} for t in TICKERS}
    for o in resp.json():
        if o['ticker'] in grouped and o['action'] in ('BUY', 'SELL'):
            grouped[o['ticker']][o['action']].append(o)
    return grouped


def cancel_order(session, order_id):
    session.delete(f'{BASE_URL}/orders/{order_id}')  # 404 = already filled, fine


def cancel_all(session):
    resp = session.post(f'{BASE_URL}/commands/cancel', params={'all': 1})
    if not resp.ok:
        print(f'  CANCEL ALL FAILED: {resp.status_code} {resp.text}')


def submit_limit_order(session, ticker, action, quantity, price):
    params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': int(quantity),
              'action': action, 'price': round(price, 2)}
    resp = session.post(f'{BASE_URL}/orders', params=params)
    if resp.ok:
        return resp.json()
    print(f'  ORDER FAILED: {action} {quantity} {ticker} @ {price:.2f} -> {resp.status_code} {resp.text}')
    return None


# ---------------------------------------------------------------- signals

def update_signals(ticker, bid, ask, now):
    """Record the mid; return (short trend over TREND_SECONDS, long trend over
    MOM_SECONDS, both in ticks, and the high-low range over VOL_SECONDS)."""
    hist = mid_history[ticker]
    hist.append((now, (bid + ask) / 2))
    while hist and now - hist[0][0] > max(VOL_SECONDS, MOM_SECONDS):
        hist.popleft()
    mid = hist[-1][1]
    trend = (mid - next(m for t, m in hist if now - t <= TREND_SECONDS)) / TICK
    mom_trend = (mid - next(m for t, m in hist if now - t <= MOM_SECONDS)) / TICK
    recent = [m for t, m in hist if now - t <= VOL_SECONDS]
    return trend, mom_trend, max(recent) - min(recent)


def momentum_step(session, ticker, position, book, mom_trend, open_orders, gross, now, blocked):
    """Ride a strong trend with take orders. Returns True when momentum owns this
    ticker this loop (market making is skipped)."""
    bid, ask, bid_size, ask_size, imbalance = book
    mid = (bid + ask) / 2
    m = mom[ticker]

    if m['dir'] == 0:
        if (not MOMENTUM or blocked or state['late']
                or now - m['last_exit'] < MOM_REENTRY_COOLDOWN):
            return False
        if mom_trend >= MOM_ENTRY_TICKS and imbalance >= MOM_IMB:
            m['dir'] = 1
        elif mom_trend <= -MOM_ENTRY_TICKS and imbalance <= -MOM_IMB:
            m['dir'] = -1
        else:
            return False
        m['best'], m['exiting'] = mid, False
        print(f'  >> {ticker} TREND {"UP" if m["dir"] > 0 else "DOWN"} '
              f'(move {mom_trend:+.0f}t, imb {imbalance:+.2f})')

    d = m['dir']
    if not m['exiting']:
        m['best'] = max(m['best'], mid) if d > 0 else min(m['best'], mid)
        retrace = (m['best'] - mid) / TICK * d
        if retrace >= MOM_TRAIL_TICKS or imbalance * d <= -MOM_EXIT_IMB or blocked or state['late']:
            m['exiting'] = True
            print(f'  << {ticker} exit trend (retrace {retrace:.0f}t, imb {imbalance:+.2f}) pos={position}')

    target = 0 if m['exiting'] else d * MOM_MAX_POS
    if m['exiting'] and position == 0:
        m['dir'], m['exiting'], m['last_exit'] = 0, False, now
        return False

    # Momentum trades are takes only: clear any resting quotes on this ticker
    for side in ('BUY', 'SELL'):
        for o in open_orders[side]:
            cancel_order(session, o['order_id'])

    diff = target - position
    if diff == 0 or now - m['last_trade'] < MOM_TRADE_COOLDOWN:
        return True
    adding = abs(target) > abs(position)
    room = max(0, POSITION_LIMIT - gross) if adding else ORDER_LIMIT
    if diff > 0:
        qty = min(diff, MOM_STEP, ask_size, room, ORDER_LIMIT)
        if qty > 0:
            submit_limit_order(session, ticker, 'BUY', qty, ask)
    else:
        qty = min(-diff, MOM_STEP, bid_size, room, ORDER_LIMIT)
        if qty > 0:
            submit_limit_order(session, ticker, 'SELL', qty, bid)
    m['last_trade'] = now
    return True


def is_benched(ticker, pnl, now):
    """Per-ticker drawdown breaker; resets the peak when the bench ends."""
    if now < benched_until[ticker]:
        return True
    if benched_until[ticker]:
        benched_until[ticker] = 0.0
        peak_pnl[ticker] = pnl
    peak_pnl[ticker] = max(peak_pnl[ticker], pnl)
    if pnl < peak_pnl[ticker] - TICKER_DRAWDOWN:
        benched_until[ticker] = now + COOLDOWN_SECONDS
        print(f'  !! {ticker} benched for {COOLDOWN_SECONDS}s (P&L {pnl:.0f}, peak {peak_pnl[ticker]:.0f})')
        return True
    return False


# ---------------------------------------------------------------- quoting

def quote_sizes(ticker, position, gross, net):
    base = TICKERS[ticker]['base_qty']
    abs_pos = abs(position)
    if abs_pos <= SOFT_LIMIT:
        factor = 1.0
    else:
        factor = max(0.0, 1.0 - (abs_pos - SOFT_LIMIT) / (HARD_LIMIT - SOFT_LIMIT))
    shrunk = int(base * factor / 100) * 100

    bid_qty = shrunk if position >= 0 else base
    ask_qty = shrunk if position <= 0 else base

    # Unwinding side is allowed to take off the whole position
    if position < 0:
        bid_qty = max(bid_qty, min(abs_pos, 2 * base))
    elif position > 0:
        ask_qty = max(ask_qty, min(abs_pos, 2 * base))

    gross_room = max(0, POSITION_LIMIT - gross)
    bid_cap = (POSITION_LIMIT - net) if position < 0 else min(gross_room, POSITION_LIMIT - net)
    ask_cap = (POSITION_LIMIT + net) if position > 0 else min(gross_room, POSITION_LIMIT + net)
    return max(0, min(bid_qty, bid_cap, ORDER_LIMIT)), max(0, min(ask_qty, ask_cap, ORDER_LIMIT))


def quote_prices(bid, ask, position):
    spread_ticks = round((ask - bid) / TICK)
    skew = round(MAX_SKEW_TICKS * max(-1.0, min(1.0, position / HARD_LIMIT)))
    bid_px, ask_px = bid - skew * TICK, ask - skew * TICK

    # Holding inventory and there's room: step the unwind side 1 tick inside
    if spread_ticks >= 2:
        if position >= SOFT_LIMIT:
            ask_px = min(ask_px, ask - TICK)
        elif position <= -SOFT_LIMIT:
            bid_px = max(bid_px, bid + TICK)

    bid_px = min(bid_px, ask - TICK)
    ask_px = max(ask_px, bid + TICK)
    return round(bid_px, 2), round(ask_px, 2)


def sync_side(session, ticker, action, open_orders, target_qty, target_price):
    if target_qty > 0 and len(open_orders) == 1:
        o = open_orders[0]
        remaining = o['quantity'] - o.get('quantity_filled', 0)
        if round(o['price'], 2) == target_price and remaining >= REQUOTE_QTY_RATIO * target_qty:
            return
    for o in open_orders:
        cancel_order(session, o['order_id'])
    if target_qty > 0:
        submit_limit_order(session, ticker, action, target_qty, target_price)


def adverse_exit(session, ticker, position, trend, bid, ask, bid_size, ask_size, now):
    """Price running against our inventory: take liquidity, capped at top-of-book size."""
    if abs(position) < EXIT_MIN_POS or now - last_exit[ticker] < EXIT_COOLDOWN:
        return
    if position > 0 and trend <= -TREND_TICKS:
        qty = min(position, bid_size, EXIT_MAX_QTY)
        submit_limit_order(session, ticker, 'SELL', qty, bid)
    elif position < 0 and trend >= TREND_TICKS:
        qty = min(-position, ask_size, EXIT_MAX_QTY)
        submit_limit_order(session, ticker, 'BUY', qty, ask)
    else:
        return
    last_exit[ticker] = now
    print(f'  EXIT {ticker} pos={position} trend={trend:+.0f}t')


def trade_once(session):
    now = time.time()
    books_future = executor.submit(get_books, session)
    orders_future = executor.submit(get_open_orders, session)
    positions, pnls = get_positions_and_pnl(session)
    books, open_orders = books_future.result(), orders_future.result()

    gross = sum(abs(p) for p in positions.values())
    net = sum(positions.values())
    if not state['stopped'] and sum(pnls.values()) < GLOBAL_STOP:
        state['stopped'] = True
        print(f'  !! GLOBAL STOP hit (P&L {sum(pnls.values()):.0f}) -> unwind only')

    for ticker in TICKERS:
        book = books[ticker]
        if book is None:
            continue
        bid, ask, bid_size, ask_size, imbalance = book
        position = positions[ticker]

        trend, mom_trend, vol_range = update_signals(ticker, bid, ask, now)
        spread = ask - bid
        edge = spread + 2 * TICKERS[ticker]['rebate']
        benched = is_benched(ticker, pnls[ticker], now)

        if momentum_step(session, ticker, position, book, mom_trend, open_orders[ticker],
                         gross, now, blocked=state['stopped'] or benched):
            signals[ticker] = {'spread': spread, 'range': vol_range, 'trend': mom_trend,
                               'imb': imbalance, 'on': False, 'benched': benched,
                               'mom': mom[ticker]['dir']}
            continue

        can_add = (not state['stopped'] and not benched
                   and TICKERS[ticker]['base_qty'] > 0
                   and edge >= MIN_EDGE
                   and spread >= VOL_MULT * vol_range
                   and spread <= MAX_SPREAD_TICKS * TICK
                   and abs(position) < HARD_LIMIT)
        can_buy = can_add and trend > -TREND_TICKS and imbalance > -IMBALANCE_BLOCK
        can_sell = can_add and trend < TREND_TICKS and imbalance < IMBALANCE_BLOCK
        signals[ticker] = {'spread': spread, 'range': vol_range, 'trend': trend,
                           'imb': imbalance, 'on': can_add, 'benched': benched}

        adverse_exit(session, ticker, position, trend, bid, ask, bid_size, ask_size, now)

        bid_px, ask_px = quote_prices(bid, ask, position)
        bid_qty, ask_qty = quote_sizes(ticker, position, gross, net)
        if not can_buy:
            bid_qty = min(bid_qty, max(0, -position))   # only buys that cover a short
        if not can_sell:
            ask_qty = min(ask_qty, max(0, position))    # only sells that reduce a long

        sync_side(session, ticker, 'BUY', open_orders[ticker]['BUY'], bid_qty, bid_px)
        sync_side(session, ticker, 'SELL', open_orders[ticker]['SELL'], ask_qty, ask_px)


def flatten_all(session, positions, books):
    cancel_all(session)
    for ticker, pos in positions.items():
        if pos == 0 or books[ticker] is None:
            continue
        bid, ask = books[ticker][0], books[ticker][1]
        qty = min(ORDER_LIMIT, abs(pos))
        if pos > 0:
            submit_limit_order(session, ticker, 'SELL', qty, bid - FLATTEN_SLIPPAGE_TICKS * TICK)
        else:
            submit_limit_order(session, ticker, 'BUY', qty, ask + FLATTEN_SLIPPAGE_TICKS * TICK)


def print_status(session, tick):
    positions, pnls = get_positions_and_pnl(session)
    parts = []
    for t in TICKERS:
        s = signals[t]
        if s.get('mom'):
            flag = 'TREND-UP' if s['mom'] > 0 else 'TREND-DN'
        else:
            flag = 'BENCH' if s.get('benched') else ('MM' if s.get('on') else 'off')
        parts.append(f"{t} {positions[t]:+d} {pnls[t]:+.0f}$ "
                     f"[sp {s.get('spread', 0) / TICK:.0f}t rg {s.get('range', 0) / TICK:.0f}t "
                     f"tr {s.get('trend', 0):+.0f} im {s.get('imb', 0):+.2f} {flag}]")
    print(f"[{tick:3d}] P&L {sum(pnls.values()):+.0f}$ | " + ' | '.join(parts))


# ---------------------------------------------------------------- main

def main():
    with requests.Session() as session:
        session.headers.update(API_KEY)
        state['trader_id'] = get_trader_id(session)
        print(f'Connected as {state["trader_id"]}')
        last_status, last_tick = 0.0, None

        try:
            while True:
                try:
                    case = get_case(session)
                    tick = case['tick']
                    if case['status'] != 'ACTIVE':
                        print(f'Case {case["status"]}. Waiting for the case to start...')
                        time.sleep(1.0)
                        continue

                    # New run of the case: reset all per-run state
                    if last_tick is not None and tick < last_tick:
                        for t in TICKERS:
                            mid_history[t].clear()
                            peak_pnl[t], benched_until[t], last_exit[t] = 0.0, 0.0, 0.0
                            mom[t].update(dir=0, best=0.0, exiting=False, last_trade=0.0, last_exit=0.0)
                        state['stopped'] = False
                    last_tick = tick

                    end_tick = case.get('ticks_per_period') or 300
                    state['late'] = tick >= end_tick - MOM_NO_ENTRY_TICKS
                    if tick >= end_tick - FLATTEN_TICKS_BEFORE_END:
                        positions, _ = get_positions_and_pnl(session)
                        if any(positions.values()):
                            print(f'[{tick}] Flattening {positions}')
                            flatten_all(session, positions, get_books(session))
                        time.sleep(0.5)
                        continue

                    trade_once(session)

                    now = time.time()
                    if now - last_status >= STATUS_INTERVAL:
                        print_status(session, tick)
                        last_status = now

                except requests.exceptions.HTTPError as e:
                    if e.response is not None and e.response.status_code == 429:
                        time.sleep(RATE_LIMIT_BACKOFF)
                        continue
                    print(f'  HTTP error: {e} -> retrying')
                    time.sleep(0.2)
                except requests.exceptions.ConnectionError:
                    print('  Cannot reach RIT Client -> retrying')
                    time.sleep(1.0)

                time.sleep(POLL_SLEEP)
        except KeyboardInterrupt:
            print('\nStopping: cancelling all open orders...')
            cancel_all(session)


if __name__ == '__main__':
    main()
