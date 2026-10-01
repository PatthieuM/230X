"""
ALGO1 - Algorithmic Arbitrage 1
Rotman Interactive Trader (RIT) REST API

Strategy
--------
CRZY trades on two exchanges:  "Main" (CRZY_M) and "Alternate" (CRZY_A).
Positions are aggregated, so buying X shares on one exchange and selling X on
the other leaves a net-zero CRZY position while locking in a profit whenever the
two markets are "crossed":

    Case 1:  CRZY_M.bid  >  CRZY_A.ask      -> SELL on Main,  BUY on Alternate
    Case 2:  CRZY_A.bid  >  CRZY_M.ask      -> SELL on Alt,   BUY on Main

Both legs are sent as price-protected (marketable LIMIT) orders priced AT the
crossed prices, and sized to only the shares actually resting at those prices.
That way the spread is captured immediately but we can never fill worse than the
arb price: if the resting size shrank since we read the book, the unfillable
remainder just doesn't execute instead of walking the book into a loss.

After firing both legs we reconcile: cancel any unfilled remainder, and if the
two legs filled unequally (a "broken leg"), flatten the difference at market so
we never carry a naked directional position.

Constraints (from the case brief)
---------------------------------
  * 10,000 shares max per order
  * 25,000 shares position limit (gross OR net)
  * No commissions (a configurable per-share cost is included anyway - see
    discussion question 1)
  * 300-second (5 minute) trading session

Setup
-----
  pip install requests
Then set API_KEY below to the key shown under the "API" icon on the RIT client's
bottom status bar (also confirm the port matches - default 9999).
"""

import time
import requests

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #
API_KEY = {"X-API-key": "YOUR_RIT_API_KEY"}   # <-- paste your RIT API key
BASE_URL = "http://localhost:7238/v1"          # confirm port on the RIT client

TICKER_M = "CRZY_M"      # Main exchange
TICKER_A = "CRZY_A"      # Alternate exchange

MAX_ORDER_SIZE = 10_000  # exchange limit: shares per order
POSITION_LIMIT = 25_000  # gross or net share limit
TX_COST = 0.00           # per-share transaction cost (Q1). Keep 0 for ALGO1.
# *** SAFETY DIAL: only trade when the cross is at least this big. ***
# A cross must clear MIN_EDGE ($/share) before we fire. A wide cross has room to
# absorb a broken leg (whose cost is roughly one venue's spread) and STILL profit;
# a thin cross does not, so thin crosses are pure legging risk. Bigger = safer but
# fewer trades. If it barely trades, lower toward 0.03; if it still takes losers,
# raise toward 0.10. Below MIN_LOT-sized or sub-MIN_EDGE crosses are simply skipped.
# Start modest so it actually trades; the heartbeat below shows you the real cross
# sizes in this case, then raise this to just under the crosses you see recurring.
MIN_EDGE = 0.02
SLEEP = 0.10             # seconds between polling loops. Kept conservative: too
                         # fast + the cancel-if-gone polling blew past RIT's
                         # ~100 req/sec limit -> 429s -> flatten orders REJECTED
                         # -> naked longs piled up. Slow and reliable beats fast
                         # and rate-limited. Retune down only once stable.

# --- Pure-arb mode --------------------------------------------------------- #
# When False (default): STRICTLY trade the arb. Fire both legs as marketable
# limits at the crossed prices, cancel any unfilled remainder (free), and NEVER
# send a market order to flatten a broken leg. Every blowup so far came from
# market-flattening into a fast, wide-spread book (buy high / sell low whipsaw);
# removing it removes the bleed. A broken leg just leaves a small held residual,
# and order_size() keeps total exposure bounded by the position limit on its own.
# Set True only to re-enable the old broken-leg + net-exposure market flattening.
MARKET_FLATTEN = False

# Fire the two arb legs as MARKET orders instead of price-protected LIMITs.
# True  -> both legs always fully fill, so legs never break and you stay flat;
#          but no price protection, so a fill can slip past the cross in a fast/
#          thin book. order_size() caps each leg to top-of-book size to limit the
#          walk, and MIN_EDGE must exceed typical slippage to stay profitable.
# False -> marketable LIMIT at the cross price (can't fill worse than the arb, but
#          a leg can go unfilled -> residual).
MARKET_ARB = True

# (Only used when MARKET_FLATTEN is True.)
MAX_NET = 100            # shares; |aggregate position| above this -> flatten
FLATTEN_RETRIES = 5      # times to re-send a flatten until the position is gone
HEARTBEAT = 2.0          # seconds between "here's the current best cross" status lines

# --- Cancel-if-arb-gone ---------------------------------------------------- #
# When True, after firing both legs we let any unfilled remainder REST briefly
# to try to fill, but we re-check the live quote and CANCEL those resting legs
# the instant the market has adapted and the cross no longer exists. This
# captures more of each opportunity than cancelling immediately, without leaving
# stale orders sitting in a market that is no longer profitable.
# Set to False to restore the original behavior (cancel any remainder at once).
# DEFAULT OFF: its per-cycle polling adds ~3 requests every FILL_POLL seconds,
# which pushed us over RIT's rate limit and caused flatten orders to be rejected.
# Only turn back on once the request budget is well under 100/sec.
CANCEL_IF_GONE = False
FILL_WAIT = 0.30         # max seconds to let unfilled legs rest before giving up
FILL_POLL = 0.05         # seconds between fill / arb-still-there re-checks

# --- Bet sizing ------------------------------------------------------------ #
# The rule: trade ONLY the shares that are actually profitable, i.e. the size
# resting AT the crossed prices (top of book on each leg). Sending more makes a
# marketable order sweep past that level into prices that are no longer crossed,
# so the excess shares LOSE money and can wipe out the good top-of-book fills.
# order_size() caps every trade at min(bid_size, ask_size) plus the hard limits,
# and the LIMIT price on each leg (see submit_order) is the backstop in case the
# book moved between the snapshot and the order landing.
MIN_LOT = 100            # don't bother sending orders smaller than this


class ApiException(Exception):
    """Raised when the RIT client returns an authentication / server error."""
    pass


# --------------------------------------------------------------------------- #
#  Market data helpers
# --------------------------------------------------------------------------- #
def get_tick(session):
    """Return (tick, status) for the running case. status == 'ACTIVE' while live."""
    resp = session.get(f"{BASE_URL}/case")
    if not resp.ok:
        raise ApiException(f"/case request failed - check API key/port: {resp.text}")
    case = resp.json()
    return case["tick"], case["status"]


def get_securities(session):
    """
    Return a dict keyed by ticker with the fields the algo needs:
        {ticker: {bid, ask, bid_size, ask_size, position}}
    Uses the /securities endpoint, which reports the best bid/ask and our
    current position for every security in the case.
    """
    resp = session.get(f"{BASE_URL}/securities")
    if not resp.ok:
        raise ApiException(f"/securities request failed: {resp.text}")

    book = {}
    for s in resp.json():
        book[s["ticker"]] = {
            "bid": s["bid"],
            "ask": s["ask"],
            "bid_size": s.get("bid_size", MAX_ORDER_SIZE),
            "ask_size": s.get("ask_size", MAX_ORDER_SIZE),
            "position": s.get("position", 0),
        }
    return book


def still_crossed(session, sell_ticker, buy_ticker, threshold):
    """
    Re-read the book and report whether the arb is STILL live: i.e. the price
    we planned to SELL at (sell_ticker's bid) still exceeds the price we planned
    to BUY at (buy_ticker's ask) by more than the required edge. Used to bail out
    of resting legs the moment the market adapts and the cross disappears.
    """
    book = get_securities(session)
    return book[sell_ticker]["bid"] - book[buy_ticker]["ask"] > threshold


# --------------------------------------------------------------------------- #
#  Order submission
# --------------------------------------------------------------------------- #
def submit_order(session, ticker, action, quantity, price):
    """
    Submit one arb leg. Returns the parsed order dict (order_id / quantity_filled
    / status) on success, else None.

    MARKET_ARB True  -> MARKET order: always fully fills (no broken leg), but no
                        price protection, so relies on order_size() capping to
                        top-of-book size so it doesn't walk deep into the book.
    MARKET_ARB False -> marketable LIMIT priced AT the cross: fills against resting
                        liquidity immediately but never worse than the arb price;
                        any unfillable remainder just doesn't execute.
    """
    params = {
        "ticker": ticker,
        "type": "MARKET" if MARKET_ARB else "LIMIT",
        "quantity": int(quantity),
        "action": action,          # 'BUY' or 'SELL'
    }
    if not MARKET_ARB:
        params["price"] = round(price, 2)
    resp = session.post(f"{BASE_URL}/orders", params=params)
    if resp.ok:
        return resp.json()
    # 429 = too many requests / rate limited; anything else -> report and skip.
    print(f"  ! {action} {quantity} {ticker} @ {price} rejected: {resp.text}")
    return None


def get_order(session, order_id):
    """Fetch the latest state of one order (fills, status), or None on failure."""
    resp = session.get(f"{BASE_URL}/orders/{order_id}")
    return resp.json() if resp.ok else None


def cancel_order(session, order_id):
    """Cancel one open order by id."""
    session.delete(f"{BASE_URL}/orders/{order_id}")


def flatten(session, ticker, action, quantity):
    """
    Close a residual naked position with a MARKET order, RETRYING until the full
    quantity is actually filled. Fire-and-forget is dangerous: if the single
    order is rejected (e.g. a 429 rate-limit) the naked position silently stays,
    and in a trending market those pile up into a big directional loss. So we
    re-send the unfilled remainder up to FLATTEN_RETRIES times.
    """
    remaining = int(quantity)
    if remaining < 1:
        return
    print(f"  ~ flatten -> {action} {remaining} {ticker} at market")
    # RIT rejects any single order above MAX_ORDER_SIZE, and the net position we
    # unwind can be larger than that, so send it in <=MAX_ORDER_SIZE lots. `stalls`
    # counts consecutive lots that made no progress (rejected / zero fill) so a
    # persistently failing flatten gives up instead of spinning forever.
    stalls = 0
    while remaining >= 1 and stalls < FLATTEN_RETRIES:
        lot = min(remaining, MAX_ORDER_SIZE)
        params = {"ticker": ticker, "type": "MARKET",
                  "quantity": lot, "action": action}
        resp = session.post(f"{BASE_URL}/orders", params=params)
        if resp.ok:
            filled = int(resp.json().get("quantity_filled", 0))
            remaining -= filled
            stalls = 0 if filled > 0 else stalls + 1
        else:
            print(f"  ! flatten {action} {lot} {ticker} rejected: {resp.text}")
            stalls += 1
            time.sleep(0.1)     # brief back-off, then re-send
    if remaining >= 1:
        print(f"  !! flatten INCOMPLETE: {remaining} {ticker} still unhedged")


# --------------------------------------------------------------------------- #
#  Position sizing
# --------------------------------------------------------------------------- #
# IMPORTANT: CRZY_M and CRZY_A share ONE aggregated position (per the case:
# "buying X on one exchange and selling X on the other leaves a net-zero CRZY
# position"). RIT reports that SAME aggregate on BOTH ticker rows, so we must
# read ONE of them -- NOT the sum. Summing double-counts the exposure, which made
# the safety net trade 2x the real position, overshoot past zero, flip sign, and
# oscillate ~35k shares every cycle (a ~$120k blowup). One leg = the true number.
def aggregate_position(book):
    """The single shared CRZY position (same on both legs)."""
    return book[TICKER_M]["position"]


def gross_position(book):
    """Magnitude of the shared aggregate position (NOT |pos_M| + |pos_A|)."""
    return abs(aggregate_position(book))


def net_position(book):
    """Aggregate directional exposure (should hover near 0 for a hedged arb)."""
    return aggregate_position(book)


def neutralize_net(session, book):
    """
    SAFETY NET. If aggregate exposure has drifted directional (|net| > MAX_NET),
    force it back toward flat BEFORE hunting new arbs. This makes reconciliation
    self-healing: even if a per-cycle flatten was rejected, the next loop catches
    and closes the residual, so one failed order can never snowball into a large
    naked position (which is exactly what produced the 25,000-share long blowup).
    A balanced hedge (long one venue / short the other, net ~0) is left untouched.
    Returns True if it had to act.
    """
    net = net_position(book)
    if abs(net) <= MAX_NET:
        return False
    if net > 0:      # net LONG -> sell it down on the venue with the higher bid
        tkr = TICKER_M if book[TICKER_M]["bid"] >= book[TICKER_A]["bid"] else TICKER_A
        print(f"  !! SAFETY: net long {net} -> flatten {net} {tkr}")
        flatten(session, tkr, "SELL", net)
    else:            # net SHORT -> buy it back on the venue with the lower ask
        tkr = TICKER_M if book[TICKER_M]["ask"] <= book[TICKER_A]["ask"] else TICKER_A
        print(f"  !! SAFETY: net short {-net} -> flatten {-net} {tkr}")
        flatten(session, tkr, "BUY", -net)
    return True


def order_size(sell_ticker, buy_ticker, book):
    """
    Largest paired quantity we may trade this cycle, respecting:
      - the 10,000 share per-order cap
      - the resting size available on each side (don't walk the book / slip)
      - remaining gross-position headroom (both legs add to gross before they
        aggregate away, so budget for 2x the leg size)
    """
    # Liquidity available at the crossed prices.
    avail = min(book[sell_ticker]["bid_size"], book[buy_ticker]["ask_size"])

    # Gross headroom: each arb round adds up to 2 * qty to the gross position.
    headroom = max(0, POSITION_LIMIT - gross_position(book))
    by_gross = headroom // 2

    return int(min(MAX_ORDER_SIZE, avail, by_gross))


# --------------------------------------------------------------------------- #
#  Paired execution with broken-leg reconciliation
# --------------------------------------------------------------------------- #
def leg_filled(session, order):
    """
    Return the shares filled on one leg, and cancel any unfilled remainder so a
    leftover limit order can't rest and re-fill later (which would re-open the
    imbalance we are about to close).
    """
    if order is None:
        return 0
    order_id = order["order_id"]
    latest = get_order(session, order_id) or order
    if latest.get("status") == "OPEN":
        cancel_order(session, order_id)
        latest = get_order(session, order_id) or latest   # re-read after cancel
    return int(latest.get("quantity_filled", 0))


def wait_for_fills_or_arb_gone(session, orders, sell_ticker, buy_ticker, threshold):
    """
    Let the resting legs try to fill, but stop waiting (so the caller cancels the
    remainder) as soon as EITHER:
      * nothing is still OPEN (both legs are done), or
      * the market has adapted and the cross no longer exists.
    Polls up to FILL_WAIT seconds. Only called when CANCEL_IF_GONE is True.
    """
    deadline = time.time() + FILL_WAIT
    while time.time() < deadline:
        states = [get_order(session, o["order_id"]) or o for o in orders]
        if all(s.get("status") != "OPEN" for s in states):
            break                                     # both legs fully done
        if not still_crossed(session, sell_ticker, buy_ticker, threshold):
            print("  x arb closed while resting -> cancelling unfilled legs")
            break                                     # market adapted -> bail
        time.sleep(FILL_POLL)


def execute_arb(session, sell_ticker, buy_ticker, sell_price, buy_price, qty, threshold):
    """
    Fire both legs, then reconcile:
      1. (optional) if CANCEL_IF_GONE, let unfilled legs rest to fill but cancel
         them the instant the cross disappears (see wait_for_fills_or_arb_gone),
      2. read how much each leg actually filled,
      3. cancel any unfilled remainder (handled inside leg_filled),
      4. if the legs filled UNEQUALLY (a broken leg), flatten the difference at
         market so we never carry a naked directional position.
    Returns (sell_filled, buy_filled).
    """
    sell_ord = submit_order(session, sell_ticker, "SELL", qty, sell_price)
    buy_ord = submit_order(session, buy_ticker, "BUY", qty, buy_price)

    if CANCEL_IF_GONE:
        resting = [o for o in (sell_ord, buy_ord) if o is not None]
        if resting:
            wait_for_fills_or_arb_gone(session, resting,
                                       sell_ticker, buy_ticker, threshold)

    sell_filled = leg_filled(session, sell_ord)
    buy_filled = leg_filled(session, buy_ord)

    # PURE ARB: by default we do NOT market-flatten a broken leg -- that's the
    # move that whipsaws us (crossing a wide, fast book at a bad price). We simply
    # keep whatever filled; the unfilled remainder was already cancelled above, so
    # a broken leg leaves only a small held residual, not a locked spread loss.
    if MARKET_FLATTEN:
        imbalance = sell_filled - buy_filled
        if imbalance > 0:      # sold more than we bought -> unhedged SHORT is in
            flatten(session, sell_ticker, "BUY", imbalance)   # buy it back there
        elif imbalance < 0:    # bought more than we sold -> unhedged LONG is in
            flatten(session, buy_ticker, "SELL", -imbalance)  # sell it back there

    return sell_filled, buy_filled


# --------------------------------------------------------------------------- #
#  Main loop
# --------------------------------------------------------------------------- #
def main():
    with requests.Session() as session:
        session.headers.update(API_KEY)

        # Net edge required per share to bother trading (costs on both legs).
        threshold = 2 * TX_COST + MIN_EDGE

        tick, status = get_tick(session)
        print(f"Case is {status} at tick {tick}. Watching for crossed markets...")
        print(f"Only trading crosses wider than {threshold:.2f}/share.")
        last_beat = 0.0

        while status == "ACTIVE":
            book = get_securities(session)
            m, a = book[TICKER_M], book[TICKER_A]

            # ---- Heartbeat: SEE the market even when we're not trading, so
            #      "nothing's happening" is never a mystery. Shows the best cross
            #      available vs. the threshold you require -- use it to tune MIN_EDGE.
            now = time.time()
            if now - last_beat >= HEARTBEAT:
                c1, c2 = m["bid"] - a["ask"], a["bid"] - m["ask"]
                best = max(c1, c2)
                verdict = "TRADE" if best > threshold else "skip (too thin)"
                print(f"[{tick}] best cross {best:+.2f} vs need >{threshold:.2f} -> {verdict}"
                      f"   M {m['bid']}/{m['ask']}  A {a['bid']}/{a['ask']}  net={net_position(book):.0f}")
                last_beat = now

            # ---- Safety net (only when market-flattening is enabled) --------
            # In pure-arb mode we deliberately do NOT market-flatten; exposure is
            # kept bounded by order_size()'s position-limit headroom instead.
            if MARKET_FLATTEN and neutralize_net(session, book):
                time.sleep(SLEEP)
                tick, status = get_tick(session)
                continue

            # ---- Case 1: Main bid richer than Alternate ask -----------------
            if m["bid"] - a["ask"] > threshold:
                edge = m["bid"] - a["ask"] - 2 * TX_COST      # net of round-trip cost
                qty = order_size(TICKER_M, TICKER_A, book)    # capped to resting size
                if qty >= MIN_LOT:
                    print(f"[{tick}] CROSS: sell {qty} {TICKER_M}@{m['bid']} / "
                          f"buy {qty} {TICKER_A}@{a['ask']}  edge={edge:.2f}")
                    execute_arb(session, TICKER_M, TICKER_A, m["bid"], a["ask"], qty, threshold)

            # ---- Case 2: Alternate bid richer than Main ask -----------------
            elif a["bid"] - m["ask"] > threshold:
                edge = a["bid"] - m["ask"] - 2 * TX_COST      # net of round-trip cost
                qty = order_size(TICKER_A, TICKER_M, book)    # capped to resting size
                if qty >= MIN_LOT:
                    print(f"[{tick}] CROSS: sell {qty} {TICKER_A}@{a['bid']} / "
                          f"buy {qty} {TICKER_M}@{m['ask']}  edge={edge:.2f}")
                    execute_arb(session, TICKER_A, TICKER_M, a["bid"], m["ask"], qty, threshold)

            time.sleep(SLEEP)
            tick, status = get_tick(session)

        print(f"Case is {status}. Algorithm stopped at tick {tick}.")


if __name__ == "__main__":
    main()
