"""
RIT ALGO2 - Algorithmic Market Making (Version Optimisée Execution)
"""

import time
import requests

# CONFIGURATION RIT CLIENT

API_KEY = {'X-API-key': 'W4QQPY2E'}
BASE_URL = 'http://localhost:8000/v1'

TICKER = 'ALGO'  # Remplacer par 'CRZY_M' si nécessaire

ORDER_LIMIT = 5_000         # max shares per single order
POSITION_LIMIT = 25_000     # max gross/net shares
BASE_QTY = 1_000            # shares quoted per side
SKEW_STRENGTH = 1.0         # influence de l'inventaire sur le skew
TICK_SIZE = 0.01

THROTTLE_THRESHOLD = 15_000  # |position| début du freinage
HARD_STOP_THRESHOLD = 20_000 # |position| arrêt des achats/ventes dans le sens accumulé

POLL_SLEEP = 0.05           # Boucle plus rapide (50ms) pour choper les fills
RATE_LIMIT_BACKOFF = 0.5
PNL_PRINT_INTERVAL = 1.0


def get_case(session):
    resp = session.get(f'{BASE_URL}/case')
    resp.raise_for_status()
    return resp.json()


def get_best_quote(session):
    resp = session.get(f'{BASE_URL}/securities/book', params={'ticker': TICKER, 'limit': 1})
    resp.raise_for_status()
    book = resp.json()
    
    bids = book.get('bids') or book.get('bid') or []
    asks = book.get('asks') or book.get('ask') or []
    
    best_bid = bids[0] if bids else None
    best_ask = asks[0] if asks else None
    return best_bid, best_ask


def get_position_and_pnl(session):
    resp = session.get(f'{BASE_URL}/securities')
    resp.raise_for_status()
    for s in resp.json():
        if s['ticker'] == TICKER:
            realized = s.get('realized') or 0.0
            unrealized = s.get('unrealized') or 0.0
            position = s.get('position') or 0
            return position, realized + unrealized
    return 0, 0.0


def get_open_orders(session):
    resp = session.get(f'{BASE_URL}/orders', params={'status': 'OPEN', 'ticker': TICKER})
    resp.raise_for_status()
    return resp.json()


def cancel_order(session, order_id):
    resp = session.delete(f'{BASE_URL}/orders/{order_id}')
    if resp.ok:
        print(f'   [CANCEL] order {order_id}')
    else:
        print(f'   [CANCEL FAILED] order {order_id} -> {resp.status_code}')


def submit_limit_order(session, action, quantity, price):
    if quantity <= 0:
        return None
    params = {
        'ticker': TICKER,
        'type': 'LIMIT',
        'quantity': quantity,
        'action': action,
        'price': round(price, 2)
    }
    resp = session.post(f'{BASE_URL}/orders', params=params)
    if resp.ok:
        order = resp.json()
        print(f'   [{action}] {quantity} {TICKER} @ {price:.2f} -> ID {order.get("order_id")}')
        return order
    print(f'   [ORDER FAILED] {action} {quantity} {TICKER} @ {price:.2f} -> {resp.status_code} {resp.text}')
    return None


def throttle_factor(position):
    abs_pos = abs(position)
    if abs_pos <= THROTTLE_THRESHOLD:
        return 0.0
    if abs_pos >= HARD_STOP_THRESHOLD:
        return 1.0
    return (abs_pos - THROTTLE_THRESHOLD) / (HARD_STOP_THRESHOLD - THROTTLE_THRESHOLD)


def compute_quote_sizes(position):
    factor = throttle_factor(position)
    accumulating_qty = round(BASE_QTY * (1 - factor))
    room_to_grow = max(0, POSITION_LIMIT - abs(position))

    if position >= 0:
        bid_qty = min(accumulating_qty, room_to_grow, ORDER_LIMIT)
        ask_qty = min(BASE_QTY, POSITION_LIMIT + position, ORDER_LIMIT)
    else:
        ask_qty = min(accumulating_qty, room_to_grow, ORDER_LIMIT)
        bid_qty = min(BASE_QTY, POSITION_LIMIT - position, ORDER_LIMIT)
    return bid_qty, ask_qty


def compute_quote_prices(best_bid, best_ask, position):
    bid_p = best_bid['price'] if isinstance(best_bid, dict) else best_bid[0]
    ask_p = best_ask['price'] if isinstance(best_ask, dict) else best_ask[0]

    # Skew d'inventaire : si long -> baisse le bid pour moins acheter / abaisse l'ask pour vendre plus vite
    inventory_ratio = position / POSITION_LIMIT
    
    bid_price = bid_p
    ask_price = ask_p

    # Si gros inventaire Long (> 30%), on s'éloigne du Bid et on colle l'Ask
    if inventory_ratio > 0.3:
        bid_price = bid_p - TICK_SIZE
    # Si gros inventaire Short (< -30%), on colle le Bid et on s'éloigne de l'Ask
    elif inventory_ratio < -0.3:
        ask_price = ask_p + TICK_SIZE

    # Garantir qu'on reste passif (pas de crossing le spread)
    bid_price = min(bid_price, ask_p - TICK_SIZE)
    ask_price = max(ask_price, bid_p + TICK_SIZE)

    return round(bid_price, 2), round(ask_price, 2)


# Variables d'état globales
bid_order_id = None
ask_order_id = None
last_bid_price = None
last_ask_price = None


def check_and_trade(session):
    global bid_order_id, ask_order_id, last_bid_price, last_ask_price

    # 1. Vérification des ordres ouverts dans le système RIT
    open_orders = {o['order_id']: o for o in get_open_orders(session)}
    
    if bid_order_id not in open_orders:
        bid_order_id = None
    if ask_order_id not in open_orders:
        ask_order_id = None

    # 2. Position et calcul des tailles requises
    position, _ = get_position_and_pnl(session)
    target_bid_qty, target_ask_qty = compute_quote_sizes(position)

    # 3. Annulation si hard stop atteint sur un côté
    if target_bid_qty == 0 and bid_order_id:
        cancel_order(session, bid_order_id)
        bid_order_id = None
    if target_ask_qty == 0 and ask_order_id:
        cancel_order(session, ask_order_id)
        ask_order_id = None

    # 4. Obtention des prix de marché actuels
    best_bid, best_ask = get_best_quote(session)
    if not best_bid or not best_ask:
        return

    target_bid_price, target_ask_price = compute_quote_prices(best_bid, best_ask, position)

    # 5. Si le marché a bougé, annuler les anciens ordres pour repositionner au touch
    if bid_order_id and last_bid_price != target_bid_price:
        cancel_order(session, bid_order_id)
        bid_order_id = None

    if ask_order_id and last_ask_price != target_ask_price:
        cancel_order(session, ask_order_id)
        ask_order_id = None

    # 6. Soumission des nouveaux ordres si nécessaire
    if target_bid_qty > 0 and not bid_order_id:
        order = submit_limit_order(session, 'BUY', target_bid_qty, target_bid_price)
        if order:
            bid_order_id = order.get('order_id')
            last_bid_price = target_bid_price

    if target_ask_qty > 0 and not ask_order_id:
        order = submit_limit_order(session, 'SELL', target_ask_qty, target_ask_price)
        if order:
            ask_order_id = order.get('order_id')
            last_ask_price = target_ask_price


def main():
    with requests.Session() as session:
        session.headers.update(API_KEY)
        last_pnl_print = 0.0

        print(f"Connecté à {BASE_URL}. En attente de la simulation...")

        while True:
            try:
                case = get_case(session)
                if case['status'] == 'STOPPED':
                    print('Case stopped. Lancez la simulation dans RIT Client.')
                    time.sleep(1.0)
                    continue
                
                if case['status'] == 'ACTIVE':
                    check_and_trade(session)

                    now = time.time()
                    if now - last_pnl_print >= PNL_PRINT_INTERVAL:
                        position, pnl = get_position_and_pnl(session)
                        print(f'[tick {case["tick"]}] Position: {position} | PnL: {pnl:.2f}$')
                        last_pnl_print = now

            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    time.sleep(RATE_LIMIT_BACKOFF)
                    continue
                raise
                
            time.sleep(POLL_SLEEP)


if __name__ == '__main__':
    main()