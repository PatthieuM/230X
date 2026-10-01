"""
RIT ALGO1 - Algorithmic Arbitrage
CRZY trades on two exchanges (CRZY_M, CRZY_A). When the two books cross
(best bid on one side > best ask on the other), buy the cheap side and
sell the expensive side to capture the spread.

Case rules:
  - Max 10,000 shares per order
  - Max 25,000 shares position (gross AND net)
  - Session length: 300 seconds
  - No commissions (TRADING_COST below lets you test question (1) from the
    case brief: how trading costs change the arbitrage threshold)

Before running: open the RIT Client, load the ALGO1 case, click the API
icon in the bottom status bar to confirm the port and get your API key,
and paste the key below.
"""

import time
import requests
from concurrent.futures import ThreadPoolExecutor

API_KEY = {'X-API-key': 'YOUR_API_KEY'}
BASE_URL = 'http://localhost:8000/v1'

TICKER_M = 'CRZY_M'
TICKER_A = 'CRZY_A'

ORDER_LIMIT = 10_000      # max shares per single order
POSITION_LIMIT = 25_000   # max gross/net shares across both tickers
TRADING_COST = 0.00       # per-share cost assumed on each leg (see Q1)
POLL_SLEEP = 0.05         # seconds between ticks
RATE_LIMIT_BACKOFF = 0.5  # seconds to wait after a 429 before retrying
PNL_PRINT_INTERVAL = 1.0  # seconds between PnL status prints

executor = ThreadPoolExecutor(max_workers=4)


def get_case(session):
    resp = session.get(f'{BASE_URL}/case')
    resp.raise_for_status()
    return resp.json()


def get_best_quote(session, ticker):
    resp = session.get(f'{BASE_URL}/securities/book', params={'ticker': ticker, 'limit': 1})
    resp.raise_for_status()
    book = resp.json()
    best_bid = book['bid'][0] if book['bid'] else None
    best_ask = book['ask'][0] if book['ask'] else None
    return best_bid, best_ask


def get_positions(session):
    resp = session.get(f'{BASE_URL}/securities')
    resp.raise_for_status()
    positions = {s['ticker']: s['position'] for s in resp.json()}
    return positions.get(TICKER_M, 0), positions.get(TICKER_A, 0)


def get_pnl(session):
    """Realized + unrealized P&L, summed across both CRZY tickers."""
    resp = session.get(f'{BASE_URL}/securities')
    resp.raise_for_status()
    return sum(s['realized'] + s['unrealized'] for s in resp.json() if s['ticker'] in (TICKER_M, TICKER_A))


def submit_market_order(session, ticker, action, quantity):
    params = {'ticker': ticker, 'type': 'MARKET', 'quantity': quantity, 'action': action}
    resp = session.post(f'{BASE_URL}/orders', params=params)
    if resp.ok:
        order = resp.json()
        print(f'  {action} {quantity} {ticker} -> order_id {order.get("order_id")}')
        return order
    print(f'  ORDER FAILED: {action} {quantity} {ticker} -> {resp.status_code} {resp.text}')
    return None


def max_tradable_quantity(pos_m, pos_a, buy_ticker, sell_ticker, requested):
    """Cap requested quantity so neither gross nor net position exceeds POSITION_LIMIT."""
    positions = {TICKER_M: pos_m, TICKER_A: pos_a}
    qty = min(requested, ORDER_LIMIT)

    for step in range(qty, 0, -1):
        trial = dict(positions)
        trial[buy_ticker] += step
        trial[sell_ticker] -= step
        gross = abs(trial[TICKER_M]) + abs(trial[TICKER_A])
        net = trial[TICKER_M] + trial[TICKER_A]
        if gross <= POSITION_LIMIT and abs(net) <= POSITION_LIMIT:
            return step
    return 0


def submit_both_legs(session, buy_ticker, sell_ticker, size):
    """Fire both legs at once instead of waiting for the first response before sending the second."""
    buy_future = executor.submit(submit_market_order, session, buy_ticker, 'BUY', size)
    sell_future = executor.submit(submit_market_order, session, sell_ticker, 'SELL', size)
    buy_future.result()
    sell_future.result()


def check_and_trade(session):
    # Fetch both books at once instead of one after the other.
    quote_m_future = executor.submit(get_best_quote, session, TICKER_M)
    quote_a_future = executor.submit(get_best_quote, session, TICKER_A)
    bid_m, ask_m = quote_m_future.result()
    bid_a, ask_a = quote_a_future.result()
    if not (bid_m and ask_m and bid_a and ask_a):
        return

    # Opportunity 1: bid on Main > ask on Alternate -> buy A, sell M
    edge_1 = bid_m['price'] - ask_a['price'] - 2 * TRADING_COST
    # Opportunity 2: bid on Alternate > ask on Main -> buy M, sell A
    edge_2 = bid_a['price'] - ask_m['price'] - 2 * TRADING_COST

    if edge_1 <= 0 and edge_2 <= 0:
        return  # no crossing -> skip the position check entirely, saves an API call per tick

    pos_m, pos_a = get_positions(session)

    if edge_1 > 0:
        size = min(bid_m['quantity'], ask_a['quantity'])
        size = max_tradable_quantity(pos_m, pos_a, buy_ticker=TICKER_A, sell_ticker=TICKER_M, requested=size)
        if size > 0:
            print(f'Crossed market: bid {TICKER_M}={bid_m["price"]} > ask {TICKER_A}={ask_a["price"]} '
                  f'(edge {edge_1:.4f}/share, size {size})')
            submit_both_legs(session, buy_ticker=TICKER_A, sell_ticker=TICKER_M, size=size)

    elif edge_2 > 0:
        size = min(bid_a['quantity'], ask_m['quantity'])
        size = max_tradable_quantity(pos_m, pos_a, buy_ticker=TICKER_M, sell_ticker=TICKER_A, requested=size)
        if size > 0:
            print(f'Crossed market: bid {TICKER_A}={bid_a["price"]} > ask {TICKER_M}={ask_m["price"]} '
                  f'(edge {edge_2:.4f}/share, size {size})')
            submit_both_legs(session, buy_ticker=TICKER_M, sell_ticker=TICKER_A, size=size)


def main():
    with requests.Session() as session:
        session.headers.update(API_KEY)
        last_pnl_print = 0.0

        while True:
            try:
                case = get_case(session)
                if case['status'] == 'STOPPED':
                    print('Case stopped.')
                    break
                if case['status'] == 'ACTIVE':
                    check_and_trade(session)

                    now = time.time()
                    if now - last_pnl_print >= PNL_PRINT_INTERVAL:
                        pnl = get_pnl(session)
                        print(f'[tick {case["tick"]}] PnL: {pnl:.2f}')
                        last_pnl_print = now
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    time.sleep(RATE_LIMIT_BACKOFF)
                    continue
                raise
            time.sleep(POLL_SLEEP)


if __name__ == '__main__':
    main()
