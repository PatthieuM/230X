"""
ALGO2 market maker - one script, four versions, and an automatic mode for the test session.

    py algo2_mm.py auto          splits the case into equal segments, one per version
                                 (v3, v4, v5, v6 by default). Each version ends flat, writes
                                 its own log and prints a summary with its P&L.
    py algo2_mm.py auto v5 v6    same, with only the versions listed (300 ticks -> 150 each)
    py algo2_mm.py v5     runs a single version for the whole case (for the final simulation)

Versions:
    v3  passive quotes at the top of the book, no inventory management
    v4  v3 + inventory management (brief rule 2): both quotes shift 1c per 1,000 shares against
        the position (the reducing quote is kept at its skewed price), soft limit at 10,000,
        passive unwind 20 ticks before the end
    v5  v4 + more volume: 2,500 shares per leg, quote deeper inside wide spreads
    v6  v5 + book-imbalance guard: back the quote off the side about to be run over

Common core (v3):
  - no open orders              -> read the book WITHOUT our own orders and post a bid and an ask:
                                     others' spread < 3c : join  (bid = best bid, ask = best ask)
                                     others' spread >= 3c: improve (best bid + 1c, best ask - 1c)
                                   then wait until the book lists them (no double posting)
  - exactly the pair we posted  -> check each leg against the market: outbid, or ahead of where
                                   the rule would place it -> replace that leg only
                                   (REQUOTE BID / ASK); both off -> cancel both (REQUOTE BOTH)
  - anything else               -> cancel everything (RESET), e.g. one leg filled
  - quotes are always passive and at least 1c apart
Every version also logs the touch of the other traders' book: best bid/ask, the size at
each, and the imbalance (qb - qa) / (qb + qa). Observation only in v3-v5.
"""
import csv
import math
import os
import sys
import time
from datetime import datetime
import requests

# ---------------- versions ----------------
# Timing is relative to the END of the version's segment (the end of the case when run alone).
VERSIONS = {
    #       shares/leg  skew per 1,000 sh  soft limit        unwind: ticks     market flatten:    deep quotes  imbalance guard
    #                                                        before the end    ticks before end
    "v3": dict(QTY=1000, SKEW=0.00, SOFT_LIMIT=None,   UNWIND_BEFORE=None, FLATTEN_BEFORE=10, DEEP=False, IMB_TH=None),
    "v4": dict(QTY=1000, SKEW=0.01, SOFT_LIMIT=10_000, UNWIND_BEFORE=20,   FLATTEN_BEFORE=5,  DEEP=False, IMB_TH=None),
    "v5": dict(QTY=2500, SKEW=0.01, SOFT_LIMIT=10_000, UNWIND_BEFORE=20,   FLATTEN_BEFORE=5,  DEEP=True,  IMB_TH=None),
    "v6": dict(QTY=2500, SKEW=0.01, SOFT_LIMIT=10_000, UNWIND_BEFORE=20,   FLATTEN_BEFORE=5,  DEEP=True,  IMB_TH=0.60),
}
AUTO_SCHEDULE = ["v3", "v4", "v5", "v6"]
BUILD = "algo2_mm build 5 (build 4 + volatile-market mode)"
MODE = sys.argv[1].lower() if len(sys.argv) > 1 else "v3"
FORCE_VOL = True if "vol" in [a.lower() for a in sys.argv[2:]] else \
            False if "novol" in [a.lower() for a in sys.argv[2:]] else None   # None = automatic
if MODE != "auto" and MODE not in VERSIONS:
    sys.exit(f"Unknown mode '{MODE}'. Use 'auto' or one of: {', '.join(VERSIONS)}")

# active version settings (set by use_version)
VERSION = None
QTY = SKEW = SOFT_LIMIT = UNWIND_BEFORE = FLATTEN_BEFORE = DEEP = IMB_TH = None
SEG_END = 300           # last tick of the current segment (or of the case)


def use_version(v, seg_end):
    global VERSION, QTY, SKEW, SOFT_LIMIT, UNWIND_BEFORE, FLATTEN_BEFORE, DEEP, IMB_TH, SEG_END
    c = VERSIONS[v]
    VERSION, SEG_END = v, seg_end
    QTY, SKEW, SOFT_LIMIT = c["QTY"], c["SKEW"], c["SOFT_LIMIT"]
    UNWIND_BEFORE, FLATTEN_BEFORE = c["UNWIND_BEFORE"], c["FLATTEN_BEFORE"]
    DEEP, IMB_TH = c["DEEP"], c["IMB_TH"]


def unwinding(tick):
    return UNWIND_BEFORE is not None and tick >= SEG_END - UNWIND_BEFORE


# ---------------- config ----------------
BASE = "http://localhost:9927/v1"      # Client REST API port shown in RIT
API_KEY = {"X-API-Key": "JJ"}
TICKER = "ALGO"
TICK_SIZE = 0.01
EPS = 1e-9
IMPROVE_MIN_SPREAD = 0.03   # improve by 1c on each side when others' spread is at least 3c
DEEP_MIN_SPREAD = 0.04      # v5+: from 4c, quote just around the mid instead
HARD_LIMIT = 25_000         # case position limit (10c/share fine beyond it)
MAX_ORDER = 5000            # case limit per order
SLEEP = 0.1
HOLD = 0.6                  # a leg behind the touch moves only if that lasts this long (s)
HOLD_AHEAD = 0.2            # same for a leg sitting ahead of its target (exposed)
LIST_WAIT = 1.5             # how long a new order may take to show in the book (slow server)
REJECT_BACKOFF = 0.5        # pause before re-posting after the server rejected an order
LAST_ERR = {"t": 0.0}
# volatile-market mode: switches on when the others' mid travels a lot
VOL_WINDOW = 10.0           # seconds of mid history used to measure how much the price moves
VOL_ON = 0.006              # mode ON above 0.6c of mid travel per second...
VOL_OFF = 0.0035            # ...and OFF again below 0.35c per second
VOL_HALF = 0.01             # in the mode, quotes at least 1c from the mid: never inside the market,
                            # 1c behind the touch when the spread is 1c
VOL_BAND = 0.02             # in the mode, a quote BEHIND its target is left alone unless 2c away
VOL = {"on": False, "speed": 0.0, "hist": []}

LOGDIR = r"C:\Users\jjacob\Documents\Algo1\logs"
os.makedirs(LOGDIR, exist_ok=True)
SUMMARY_FILE = os.path.join(LOGDIR, f"algo2_summary_{datetime.now():%Y%m%d_%H%M%S}.csv")
RATE_LIMITED = 0        # number of 429 answers in the current segment

s = requests.Session()
s.headers.update(API_KEY)
ME = None               # our trader id, read at start (used to remove our orders from the book)


# ---------------- API helpers ----------------
def get_case():
    r = s.get(f"{BASE}/case").json()
    return r["tick"], r["status"]


def get_case_info():
    """Tick, status, period and period length (ticks_per_period)."""
    r = s.get(f"{BASE}/case").json()
    return r["tick"], r["status"], r.get("period", 1), r.get("ticks_per_period", 300)


def get_nlv(sec=None):
    """Net liquidation value, used to measure each version's P&L."""
    try:
        v = s.get(f"{BASE}/trader").json().get("nlv")
        if v is not None:
            return float(v)
    except Exception:
        pass
    sec = sec or get_security()
    return float(sec["realized"]) + float(sec["unrealized"])


def get_trader_id():
    try:
        return s.get(f"{BASE}/trader").json().get("trader_id")
    except Exception:
        return None


def get_security():
    return s.get(f"{BASE}/securities", params={"ticker": TICKER}).json()[0]


def open_orders():
    return [o for o in s.get(f"{BASE}/orders", params={"status": "OPEN"}).json()
            if o["ticker"] == TICKER]


def filled_orders():
    return [o for o in s.get(f"{BASE}/orders", params={"status": "TRANSACTED"}).json()
            if o["ticker"] == TICKER]


def others_touch(my_ids):
    """Other traders' best bid and ask (our orders removed), the size resting at each,
    and the imbalance (qb - qa) / (qb + qa), between -1 (sell pressure) and +1 (buy pressure)."""
    book = s.get(f"{BASE}/securities/book", params={"ticker": TICKER, "limit": 20}).json()

    def side(levels, pick):
        lv = [(l["price"], l.get("quantity", 0) - l.get("quantity_filled", 0)) for l in levels
              if l.get("order_id") not in my_ids and (ME is None or l.get("trader_id") != ME)]
        lv = [(p, q) for p, q in lv if q > 0]
        if not lv:
            return None, 0
        best = pick(p for p, _ in lv)
        return best, sum(q for p, q in lv if abs(p - best) < EPS)

    ob, qb = side(book.get("bids", []), max)
    oa, qa = side(book.get("asks", []), min)
    imb = (qb - qa) / (qb + qa) if qb + qa > 0 else 0.0
    return ob, oa, qb, qa, imb


def send_order(action, qty, price=None):
    """LIMIT order if a price is given, MARKET otherwise. Retries when rate-limited."""
    params = {"ticker": TICKER, "type": "LIMIT" if price is not None else "MARKET",
              "quantity": int(qty), "action": action}
    if price is not None:
        params["price"] = round(price, 2)
    for _ in range(3):
        r = s.post(f"{BASE}/orders", params=params)
        if r.status_code == 429:
            global RATE_LIMITED
            RATE_LIMITED += 1
            print("  rate limited (429), waiting")
            time.sleep(r.json().get("wait", 0.1))
            continue
        if not r.ok:
            if time.time() - LAST_ERR["t"] > 1.0:          # at most one message per second
                LAST_ERR["t"] = time.time()
                try:
                    msg = r.json()
                except Exception:
                    msg = r.text
                print(f"  ORDER REFUSED by RIT ({r.status_code}) {action} {qty} @ {price}: {msg}")
            return None
        return r.json()
    return None


def _delete(order_id):
    for _ in range(3):
        r = s.delete(f"{BASE}/orders/{order_id}")
        if r.status_code != 429:
            return
        time.sleep(r.json().get("wait", 0.1))


def cancel_all(orders):
    """Cancel the given orders one by one, then wait until the book shows none."""
    for o in orders:
        _delete(o["order_id"])
    t0 = time.time()
    while open_orders() and time.time() - t0 < 0.5:
        time.sleep(0.05)


def cancel_one(order):
    """Cancel one order and wait until it is gone from the book."""
    _delete(order["order_id"])
    t0 = time.time()
    while (any(o["order_id"] == order["order_id"] for o in open_orders())
           and time.time() - t0 < 0.5):
        time.sleep(0.05)


def wait_listed(ids):
    """Wait (max 1 s) until the book lists the new orders, so the next loop
    never sees a half-posted pair. An order that fills instantly never shows up."""
    t0 = time.time()
    while ids and time.time() - t0 < 1.0:
        live = {o["order_id"] for o in open_orders()}
        if all(i in live for i in ids):
            break
        time.sleep(0.05)


# ---------------- strategy ----------------
def to_cent(px, side):
    """Bids round down, asks round up: rounding never makes a quote more aggressive."""
    c = px * 100
    return (math.floor(c + 1e-6) if side == "BUY" else math.ceil(c - 1e-6)) / 100


def target_quotes(ob, oa, pos, imb):
    """Where the rule puts our bid and ask, from the other traders' touch."""
    spread = oa - ob
    if VOL["on"]:                                             # volatile market: stay out of the way
        mid = (ob + oa) / 2
        bid, ask, how = min(ob, mid - VOL_HALF), max(oa, mid + VOL_HALF), "volatile: wide"
    elif DEEP and spread >= DEEP_MIN_SPREAD - EPS:            # v5+: wide spread, sit around the mid
        mid = (ob + oa) / 2
        bid, ask, how = mid - 0.005, mid + 0.005, "deep"
    elif spread >= IMPROVE_MIN_SPREAD - EPS:
        bid, ask, how = ob + TICK_SIZE, oa - TICK_SIZE, "improve"
    else:
        bid, ask, how = ob, oa, "join"
    if SKEW:                                                  # v4+: long -> both quotes move down
        shift = SKEW * round(pos / 1000)          # whole cents: 1c per 1,000 shares
        bid, ask = bid - shift, ask - shift
        if abs(shift) > EPS:
            how += f", skew {-shift * 100:+.1f}c"
    if IMB_TH is not None:                                    # v6: back off the side at risk
        if imb > IMB_TH:
            ask += TICK_SIZE
            how += ", ask backed off"
        elif imb < -IMB_TH:
            bid -= TICK_SIZE
            how += ", bid backed off"
    bid, ask = to_cent(bid, "BUY"), to_cent(ask, "SELL")
    bid = min(bid, round(oa - TICK_SIZE, 2))                  # never cross: stay passive
    ask = max(ask, round(ob + TICK_SIZE, 2))
    if ask - bid < TICK_SIZE - EPS:                           # at least 1c between our quotes
        ask = round(bid + TICK_SIZE, 2)
    return bid, ask, how


def update_vol(ob, oa):
    """Track how far the others' mid travels (sum of |moves| per second over VOL_WINDOW s).
    Returns an event string when the volatile mode switches on or off."""
    if ob is None or oa is None:
        return None
    now = time.time()
    h = VOL["hist"]
    h.append((now, (ob + oa) / 2))
    while h and now - h[0][0] > VOL_WINDOW:
        h.pop(0)
    span = h[-1][0] - h[0][0] if len(h) > 1 else 0.0
    travel = sum(abs(b[1] - a[1]) for a, b in zip(h, h[1:]))
    VOL["speed"] = travel / span if span >= 3.0 else 0.0     # need a few seconds of history
    was = VOL["on"]
    if FORCE_VOL is not None:
        VOL["on"] = FORCE_VOL
    elif not was and VOL["speed"] > VOL_ON:
        VOL["on"] = True
    elif was and VOL["speed"] < VOL_OFF:
        VOL["on"] = False
    if VOL["on"] != was:
        return (f"VOLATILE MODE {'ON' if VOL['on'] else 'OFF'} "
                f"(mid travels {VOL['speed'] * 100:.2f}c per second)")
    return None


def allowed_sides(pos, tick):
    """Which sides we may quote now."""
    if unwinding(tick):                                       # v4+: end of segment, only reduce
        return {"SELL"} if pos > 0 else {"BUY"} if pos < 0 else set()
    lim = min(SOFT_LIMIT, HARD_LIMIT) if SOFT_LIMIT is not None else HARD_LIMIT
    sides = set()
    if pos + QTY <= lim:
        sides.add("BUY")
    if pos - QTY >= -lim:
        sides.add("SELL")
    return sides


def leg_qty(pos, tick):
    if unwinding(tick):
        return min(abs(pos), MAX_ORDER)
    return QTY


def post_quotes(bid_px, ask_px, sides, qty):
    """Post the allowed legs. Returns {side: price} for what was posted."""
    wanted = {}
    if "BUY" in sides:
        wanted["BUY"] = bid_px
    if "SELL" in sides:
        wanted["SELL"] = ask_px
    ids, accepted = [], {}
    for side, px in wanted.items():
        o = send_order(side, qty, px)
        if o and "order_id" in o:
            ids.append(o["order_id"])
            accepted[side] = px
    wait_listed(ids)        # no double posting
    return accepted, len(accepted) < len(wanted)


# ---------------- one segment: one version until its end ----------------
LOG_HEADER = ["time", "tick", "last", "bid", "ask", "position", "n_open",
              "my_bid", "my_ask", "realized", "unrealized",
              "others_bid", "others_ask", "size_bid", "size_ask", "imbalance", "event", "vol_speed", "vol_mode"]


def run_segment(version, seg_end, period):
    """Trade one version until tick seg_end (or until the case / period ends).
    Returns a summary dict. Leaves no open orders behind."""
    global RATE_LIMITED
    use_version(version, seg_end)
    RATE_LIMITED = 0
    logfile = os.path.join(LOGDIR, f"algo2{version}_{datetime.now():%Y%m%d_%H%M%S}.csv")
    f = open(logfile, "w", newline="")
    log = csv.writer(f)
    log.writerow(LOG_HEADER)
    counts = {"FILL": 0, "REQUOTE BID": 0, "REQUOTE ASK": 0, "REQUOTE BOTH": 0, "RESET": 0,
              "FLATTEN": 0}

    def write(tick, sec, orders, touch, event=""):
        ob, oa, qb, qa, imb = touch
        bids = [o["price"] for o in orders if o["action"] == "BUY"]
        asks = [o["price"] for o in orders if o["action"] == "SELL"]
        log.writerow([f"{time.time():.3f}", tick, sec["last"], sec["bid"], sec["ask"],
                      sec["position"], len(orders),
                      bids[0] if bids else "", asks[0] if asks else "",
                      sec["realized"], sec["unrealized"],
                      "" if ob is None else ob, "" if oa is None else oa, qb, qa, f"{imb:.3f}", event,
                      f"{VOL['speed']:.4f}", int(VOL["on"])])
        f.flush()
        if event:
            for k in counts:
                if event.startswith(k):
                    counts[k] += 1
            print(f"[{VERSION}] t={tick:4d} pos={sec['position']:7.0f} "
                  f"book={sec['bid']:.2f}/{sec['ask']:.2f} imb={imb:+.2f} vol={VOL['speed'] * 100:.2f}c/s{' [V]' if VOL['on'] else ''} | {event}")

    quote = None
    pending = {}            # side -> (target price, time the move was first wanted)
    last_tick = None
    last_pos = None
    flat_tick = None
    seen = {o["order_id"] for o in filled_orders()}   # only count this segment's fills
    sec0 = get_security()
    nlv0 = get_nlv(sec0)
    tick, status, per, _ = get_case_info()
    t_start = tick
    max_abs = abs(sec0["position"])
    shares = 0
    print(f"\n===== {VERSION} starts at tick {tick} (segment ends at tick {seg_end}) =====")

    try:
        while status == "ACTIVE" and per == period and tick <= seg_end:
            sec = get_security()
            orders = open_orders()
            pos = sec["position"]
            touch = others_touch({o["order_id"] for o in orders})
            vol_event = update_vol(touch[0], touch[1])
            ob, oa, qb, qa, imb = touch
            events = [vol_event] if vol_event else []

            if tick != last_tick:                      # once per tick: snapshot and new fills
                write(tick, sec, orders, touch)
                for o in filled_orders():
                    if o["order_id"] not in seen:
                        seen.add(o["order_id"])
                        px = o.get("vwap") or o.get("price") or 0
                        events.append(f"FILL {o['action']} {o['quantity_filled']:.0f} @ {px:.2f}")
                last_tick = tick

            if last_pos is not None and pos != last_pos:
                events.append(f"POSITION {last_pos:.0f} -> {pos:.0f}")
                shares += abs(pos - last_pos)
            last_pos = pos
            max_abs = max(max_abs, abs(pos))

            buys = [o for o in orders if o["action"] == "BUY"]
            sells = [o for o in orders if o["action"] == "SELL"]
            sides_ok = allowed_sides(pos, tick)

            if tick >= SEG_END - FLATTEN_BEFORE:       # end of segment: market orders
                if orders:
                    cancel_all(orders)
                    events.append("STOP QUOTING")
                elif pos != 0 and tick != flat_tick:   # one market order per tick
                    side = "BUY" if pos < 0 else "SELL"
                    qty = min(abs(pos), MAX_ORDER)
                    send_order(side, qty)
                    flat_tick = tick
                    events.append(f"FLATTEN {side} {qty:.0f} at market")

            elif not orders:
                in_flight = (quote is not None and quote["sides"] and pos == quote["pos"]
                             and time.time() - quote["t"] < LIST_WAIT)      # posted, not listed yet
                backoff = (quote is not None and quote["rejected"]
                           and time.time() - quote["t"] < REJECT_BACKOFF)
                if in_flight or backoff:
                    pass
                elif ob is not None and oa is not None and sides_ok:
                    bid_px, ask_px, how = target_quotes(ob, oa, pos, imb)
                    posted, rejected = post_quotes(bid_px, ask_px, sides_ok, leg_qty(pos, tick))
                    quote = {"sides": set(posted), "tick": tick, "t": time.time(), "pos": pos,
                             "rejected": rejected}
                    pending = {}
                    if len(posted) == 2:
                        events.append(f"NEW PAIR {bid_px:.2f}/{ask_px:.2f} "
                                      f"({how}, others {ob:.2f}/{oa:.2f})")
                    elif posted:
                        side, px = next(iter(posted.items()))
                        why = "unwind" if unwinding(tick) else "limit"
                        events.append(f"NEW {'BID' if side == 'BUY' else 'ASK'} only {px:.2f} "
                                      f"({why}, position {pos:.0f}; {how})")

            elif (quote is not None and len(buys) <= 1 and len(sells) <= 1
                  and {o["action"] for o in orders} == quote["sides"]):
                # our own legs, as posted
                extra = quote["sides"] - sides_ok          # a side we may no longer quote
                gained = sides_ok - quote["sides"]         # a side we may quote but do not
                if extra or (gained and not quote["rejected"]):
                    cancel_all(orders)                 # soft limit crossed or unwind started
                    quote, pending = None, {}
                    events.append(f"RESET (allowed sides now {sorted(sides_ok) or 'none'})")
                elif gained:
                    # that leg was refused by RIT: retry it alone after a pause, keep the other
                    if time.time() - quote["t"] >= REJECT_BACKOFF and ob is not None and oa is not None:
                        tb, ta, _ = target_quotes(ob, oa, pos, imb)
                        quote["t"] = time.time()
                        for sd in gained:
                            px = tb if sd == "BUY" else ta
                            o = send_order(sd, leg_qty(pos, tick), px)
                            if o and "order_id" in o:
                                wait_listed([o["order_id"]])
                                quote["sides"].add(sd)
                                events.append(f"REPOST {'BID' if sd == 'BUY' else 'ASK'} {px:.2f} "
                                              f"(after a refused order)")
                        quote["rejected"] = bool(sides_ok - quote["sides"])
                elif ob is not None and oa is not None:
                    tb, ta, _ = target_quotes(ob, oa, pos, imb)
                    bid_o = buys[0] if buys else None
                    ask_o = sells[0] if sells else None
                    why = {}
                    # a leg moves only if its target differs from where it sits:
                    #   outbid AND target above it -> move up; above its target -> move down
                    # (a skewed leg held back behind the touch on purpose is left alone)
                    if bid_o:
                        if ob > bid_o["price"] + EPS and tb > bid_o["price"] + EPS:
                            why["BUY"] = f"bid {bid_o['price']:.2f} outbid by {ob:.2f}"
                        elif bid_o["price"] > tb + EPS:
                            why["BUY"] = f"bid {bid_o['price']:.2f} ahead of target {tb:.2f}"
                    if ask_o:
                        if oa < ask_o["price"] - EPS and ta < ask_o["price"] - EPS:
                            why["SELL"] = f"ask {ask_o['price']:.2f} undercut by {oa:.2f}"
                        elif ask_o["price"] < ta - EPS:
                            why["SELL"] = f"ask {ask_o['price']:.2f} ahead of target {ta:.2f}"
                    if SKEW and pos != 0:
                        # v4+: the leg that REDUCES the position must also sit where the skew
                        # wants it; otherwise a partial fill would leave it behind its target
                        if pos > 0 and ask_o and "SELL" not in why and ask_o["price"] > ta + EPS:
                            why["SELL"] = f"ask {ask_o['price']:.2f} behind skewed target {ta:.2f}"
                        if pos < 0 and bid_o and "BUY" not in why and bid_o["price"] < tb - EPS:
                            why["BUY"] = f"bid {bid_o['price']:.2f} behind skewed target {tb:.2f}"

                    if VOL["on"]:
                        # volatile market: a quote stays put while the market swings around it.
                        # It moves only if it sits alone ahead of everyone (truly exposed) or
                        # if its target is at least VOL_BAND away.
                        for sd in list(why):
                            o = bid_o if sd == "BUY" else ask_o
                            tgt = tb if sd == "BUY" else ta
                            alone = (o["price"] > ob + EPS) if sd == "BUY" else (o["price"] < oa - EPS)
                            if not alone and abs(tgt - o["price"]) < VOL_BAND - EPS:
                                del why[sd]

                    # debounce: the touch flickers when other algos post and cancel; moving on
                    # every flicker cancels our orders before they can trade. Act only when the
                    # same move has been wanted for HOLD seconds.
                    now = time.time()
                    for sd in ("BUY", "SELL"):
                        tgt = tb if sd == "BUY" else ta
                        # a leg sitting AHEAD of its target is exposed (a better price than the
                        # market): it gets the short hold; a leg merely behind gets the full one
                        hold = HOLD_AHEAD if sd in why and "ahead of target" in why[sd] else HOLD
                        if sd not in why:
                            pending.pop(sd, None)
                        elif sd in pending and abs(pending[sd][0] - tgt) < EPS:
                            if now - pending[sd][1] < hold:
                                del why[sd]                    # wanted, but not for long enough
                        elif hold <= 0:
                            pending[sd] = (tgt, now)           # act now
                        else:
                            pending[sd] = (tgt, now)           # new reason: start the clock
                            del why[sd]

                    if len(why) == 1:
                        side = next(iter(why))
                        old = bid_o if side == "BUY" else ask_o
                        kept = ask_o if side == "BUY" else bid_o
                        new_px = tb if side == "BUY" else ta
                        # safety net: the new leg must stay at least 1c from the kept one
                        thin = kept is not None and (
                            (side == "BUY" and new_px > kept["price"] - TICK_SIZE + EPS) or
                            (side == "SELL" and new_px < kept["price"] + TICK_SIZE - EPS))
                        if thin:
                            why["BOTH"] = "new leg too close to the kept one"
                        else:
                            cancel_one(old)
                            pending.pop(side, None)
                            if get_security()["position"] != pos:
                                # a leg filled during the cancel: fall back to the brief's reset
                                cancel_all(open_orders())
                                quote, pending = None, {}
                                events.append("RESET (a leg filled during the requote)")
                            else:
                                o = send_order(side, leg_qty(pos, tick), new_px)
                                ok = bool(o and "order_id" in o)
                                wait_listed([o["order_id"]] if ok else [])
                                quote["t"] = time.time()
                                if not ok:
                                    quote["sides"].discard(side)
                                    quote["rejected"] = True
                                label = "BID" if side == "BUY" else "ASK"
                                events.append(f"REQUOTE {label} {old['price']:.2f} -> {new_px:.2f} "
                                              f"({why[side]})")
                    if len(why) >= 2:
                        cancel_all(orders)
                        quote, pending = None, {}
                        events.append(f"REQUOTE BOTH ({'; '.join(why.values())})")

            elif (quote is not None and pos == quote["pos"] and len(buys) <= 1 and len(sells) <= 1
                  and {o["action"] for o in orders} < quote["sides"]):
                # one of our legs is missing but the position has not changed: it did not
                # trade, it is late (slow server) or was refused. Keep the other leg.
                if time.time() - quote["t"] >= LIST_WAIT:
                    live = {o["action"] for o in orders}
                    miss = [sd for sd in quote["sides"] - live]
                    quote["sides"] = set(live)
                    for sd in miss:
                        if sd in sides_ok and ob is not None and oa is not None:
                            tb, ta, _ = target_quotes(ob, oa, pos, imb)
                            px = tb if sd == "BUY" else ta
                            o = send_order(sd, leg_qty(pos, tick), px)
                            if o and "order_id" in o:
                                wait_listed([o["order_id"]])
                                quote["sides"].add(sd)
                                events.append(f"REPOST {'BID' if sd == 'BUY' else 'ASK'} {px:.2f} "
                                              f"(leg missing, no trade)")
                    quote["t"] = time.time()

            else:
                cancel_all(orders)
                quote, pending = None, {}
                events.append(f"RESET ({len(buys)} buy, {len(sells)} sell open)")

            for e in events:
                write(tick, sec, orders, touch, e)

            time.sleep(SLEEP)
            tick, status, per, _ = get_case_info()
    finally:
        try:
            left = open_orders()
            if left:
                cancel_all(left)
        except Exception as err:
            print("Could not cancel open orders, check the blotter:", err)
        f.close()

    sec1 = get_security()
    summary = {
        "version": version, "period": period, "tick_start": t_start, "tick_end": min(tick, seg_end),
        "pnl": round(get_nlv(sec1) - nlv0, 2), "fills": counts["FILL"], "shares_traded": int(shares),
        "max_abs_position": int(max_abs), "end_position": int(sec1["position"]),
        "requote_bid": counts["REQUOTE BID"], "requote_ask": counts["REQUOTE ASK"],
        "requote_both": counts["REQUOTE BOTH"], "resets": counts["RESET"],
        "market_flatten_orders": counts["FLATTEN"], "rate_limited": RATE_LIMITED, "log": logfile,
    }
    print(f"===== {version} summary: ticks {summary['tick_start']}-{summary['tick_end']} | "
          f"P&L {summary['pnl']:+,.2f} | fills {summary['fills']} ({summary['shares_traded']:,} sh) | "
          f"max |pos| {summary['max_abs_position']:,} | end pos {summary['end_position']:,} | "
          f"requotes bid/ask/both {summary['requote_bid']}/{summary['requote_ask']}/{summary['requote_both']} | "
          f"resets {summary['resets']} | 429s {summary['rate_limited']} =====")
    new = not os.path.exists(SUMMARY_FILE)
    with open(SUMMARY_FILE, "a", newline="") as sf:
        w = csv.DictWriter(sf, fieldnames=list(summary))
        if new:
            w.writeheader()
        w.writerow(summary)
    return summary


# ---------------- controller ----------------
def wait_active():
    tick, status, per, T = get_case_info()
    if status != "ACTIVE":
        print("Waiting for the case to start...")
    while status != "ACTIVE":
        time.sleep(0.5)
        tick, status, per, T = get_case_info()
    return tick, per, T


def main():
    global ME
    ME = get_trader_id()
    schedule = AUTO_SCHEDULE if MODE == "auto" else [MODE]
    if MODE == "auto":
        picked = [a.lower() for a in sys.argv[2:] if a.lower() in VERSIONS]
        if picked:
            schedule = picked                  # e.g. "auto v5 v6": only these, in this order
    print(f"{BUILD} | Mode: {MODE}. Versions: {', '.join(schedule)}. Trader id: {ME}.")
    results = []
    try:
        tick, per, T = wait_active()
        if MODE != "auto":                                 # one version, whole case
            results.append(run_segment(MODE, T, per))
        else:
            # split the case into equal segments, one per version (300 ticks, 2 versions -> 150 each)
            seg, total = T // len(schedule), T
            plan = ", ".join(f"{v} {k * seg + 1}-{(k + 1) * seg if k < len(schedule) - 1 else total}"
                             for k, v in enumerate(schedule))
            print(f"Case of {T} ticks reported. Segments: {plan}.")
            while True:
                tick, status, p, _ = get_case_info()
                if status != "ACTIVE" or p != per or tick >= total:
                    break
                idx = min(max(tick - 1, 0) // seg, len(schedule) - 1)
                end = total if idx == len(schedule) - 1 else (idx + 1) * seg
                results.append(run_segment(schedule[idx], end, per))
                if idx == len(schedule) - 1:
                    break
    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        try:
            left = open_orders()
            if left:
                cancel_all(left)
                print(f"Cancelled {len(left)} open order(s).")
        except Exception as err:
            print("Could not cancel open orders, check the blotter:", err)
        if results:
            print("\n===== RESULTS =====")
            print(f"{'version':<8}{'ticks':>11}{'P&L':>12}{'fills':>7}{'shares':>9}{'max|pos|':>10}{'end pos':>9}{'429s':>6}")
            for r in results:
                print(f"{r['version']:<8}{str(r['tick_start']) + '-' + str(r['tick_end']):>11}{r['pnl']:>12,.2f}"
                      f"{r['fills']:>7}{r['shares_traded']:>9,}{r['max_abs_position']:>10,}"
                      f"{r['end_position']:>9,}{r['rate_limited']:>6}")
            print(f"Summary file: {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
