"""Trading objects. Every object carries its own timestamps (perf_counter ns; 0 = not reached).

Latency analysis is a join over these objects, never a log. Nothing here allocates beyond the
fixed-size record being created; no I/O, no formatting.
"""
from __future__ import annotations

import itertools
from collections import deque

# ---- venues / sides / kinds --------------------------------------------------------------
M, A = 0, 1
VENUES = (M, A)
VENUE_NAME = ("M", "A")
TICKER = ("CRZY_M", "CRZY_A")
OTHER = (A, M)

BUY, SELL = 0, 1
SIDE_NAME = ("BUY", "SELL")
SIGN = (1, -1)          # position sign of a fill on that side
OPP = (SELL, BUY)

MARKET, LIMIT = 0, 1
KIND_NAME = ("MARKET", "LIMIT")

# ---- purposes ----------------------------------------------------------------------------
PURPOSES = ("cross", "quote", "layer", "stale", "hedge", "flatten", "unknown")
P_CROSS, P_QUOTE, P_LAYER, P_STALE, P_HEDGE, P_FLATTEN, P_UNKNOWN = range(7)
AGGRESSIVE_PURPOSES = frozenset((P_CROSS, P_STALE, P_HEDGE, P_FLATTEN))

# ---- priority classes (lower = more urgent) ---------------------------------------------
P0, P1, P2, P3, P4 = range(5)   # P0 pair legs, P1 band-forced cancel, P2 hedge/flatten, P3 quotes, P4 layers

# ---- order states ------------------------------------------------------------------------
(INTENT, SENT, ACKED, PARTIAL, FILLED,
 CANCEL_SENT, CANCELLED, REJECTED, RATE_LIMITED) = range(9)
STATE_NAME = ("INTENT", "SENT", "ACKED", "PARTIAL", "FILLED",
              "CANCEL_SENT", "CANCELLED", "REJECTED", "RATE_LIMITED")
RESTING_STATES = frozenset((ACKED, PARTIAL, CANCEL_SENT))
DONE_STATES = frozenset((FILLED, CANCELLED, REJECTED, RATE_LIMITED))

# ---- intent kinds ------------------------------------------------------------------------
PAIR, QUOTE, CANCEL, TAKE = range(4)
INTENT_KIND_NAME = ("PAIR", "QUOTE", "CANCEL", "TAKE")


class BBO:
    """Top of book on both venues, integer ticks. Index by venue (M=0, A=1)."""
    __slots__ = ("bid", "ask", "bid_size", "ask_size")

    def __init__(self, bid=(0, 0), ask=(0, 0), bid_size=(0, 0), ask_size=(0, 0)):
        self.bid = [bid[0], bid[1]]
        self.ask = [ask[0], ask[1]]
        self.bid_size = [bid_size[0], bid_size[1]]
        self.ask_size = [ask_size[0], ask_size[1]]

    def mid(self, v):
        return (self.bid[v] + self.ask[v]) * 0.5

    def micro(self, v):
        bs = self.bid_size[v]
        az = self.ask_size[v]
        tot = bs + az
        if tot <= 0:
            return self.mid(v)
        return (self.ask[v] * bs + self.bid[v] * az) / tot

    def spread(self, v):
        return self.ask[v] - self.bid[v]

    def depth(self, v):
        return self.bid_size[v] + self.ask_size[v]

    def copy(self):
        return BBO(self.bid, self.ask, self.bid_size, self.ask_size)

    def __repr__(self):
        return (f"BBO(M {self.bid[0]}x{self.bid_size[0]} / {self.ask[0]}x{self.ask_size[0]}, "
                f"A {self.bid[1]}x{self.bid_size[1]} / {self.ask[1]}x{self.ask_size[1]})")


class Snapshot:
    """One /v1/securities poll. id = loop counter."""
    __slots__ = ("id", "t_send", "t_recv", "bbo", "pos_server", "pos_venue")

    def __init__(self, id, t_send, t_recv, bbo, pos_server, pos_venue):
        self.id = id
        self.t_send = t_send
        self.t_recv = t_recv
        self.bbo = bbo
        self.pos_server = pos_server
        self.pos_venue = pos_venue      # [pos_M, pos_A]


class Ladder:
    """Aggregated depth on one venue: bids desc / asks asc, levels are (px, qty, own_qty)."""
    __slots__ = ("venue", "bids", "asks", "t_recv", "snapshot_id")

    def __init__(self, venue, bids, asks, t_recv=0, snapshot_id=0):
        self.venue = venue
        self.bids = bids
        self.asks = asks
        self.t_recv = t_recv
        self.snapshot_id = snapshot_id


_local_ids = itertools.count(1)


class Order:
    __slots__ = ("id_local", "id_server", "venue", "side", "kind", "qty", "px",
                 "purpose", "level", "snapshot_id", "expected_px", "expected_edge",
                 "state", "filled", "vwap_ticks", "priority",
                 "t_intent", "t_send", "t_ack", "t_first_fill", "t_cancel_send",
                 "t_cancel_ack", "t_done", "fair_at_intent", "http_status", "pos_applied")

    def __init__(self, venue, side, kind, qty, px=None, purpose=P_UNKNOWN, level=0,
                 snapshot_id=0, expected_px=0, expected_edge=0.0, priority=P3,
                 t_intent=0, fair_at_intent=0.0):
        self.id_local = next(_local_ids)
        self.id_server = 0
        self.venue = venue
        self.side = side
        self.kind = kind
        self.qty = qty
        self.px = px
        self.purpose = purpose
        self.level = level
        self.snapshot_id = snapshot_id
        self.expected_px = expected_px if expected_px else (px or 0)
        self.expected_edge = expected_edge
        self.state = INTENT
        self.filled = 0
        self.vwap_ticks = 0.0
        self.priority = priority
        self.t_intent = t_intent
        self.t_send = 0
        self.t_ack = 0
        self.t_first_fill = 0
        self.t_cancel_send = 0
        self.t_cancel_ack = 0
        self.t_done = 0
        self.fair_at_intent = fair_at_intent
        self.http_status = 0
        self.pos_applied = 0      # filled qty already folded into Inventory.pos

    @property
    def remaining(self):
        return self.qty - self.filled

    @property
    def is_resting(self):
        return self.state in RESTING_STATES and self.kind == LIMIT

    @property
    def is_aggressive(self):
        return self.kind == MARKET or self.purpose in AGGRESSIVE_PURPOSES

    def __repr__(self):
        return (f"Order(#{self.id_local}/{self.id_server} {VENUE_NAME[self.venue]} "
                f"{SIDE_NAME[self.side]} {KIND_NAME[self.kind]} {self.qty}@{self.px} "
                f"{PURPOSES[self.purpose]} {STATE_NAME[self.state]} filled={self.filled})")


class Fill:
    __slots__ = ("order_local", "venue", "side", "qty", "px", "t_seen", "passive",
                 "purpose", "fair_at_fill", "edge")

    def __init__(self, order_local, venue, side, qty, px, t_seen, passive, purpose,
                 fair_at_fill, edge=0.0):
        self.order_local = order_local
        self.venue = venue
        self.side = side
        self.qty = qty
        self.px = px
        self.t_seen = t_seen
        self.passive = passive
        self.purpose = purpose
        self.fair_at_fill = fair_at_fill
        self.edge = edge

    def __repr__(self):
        return (f"Fill({VENUE_NAME[self.venue]} {SIDE_NAME[self.side]} {self.qty}@{self.px} "
                f"{'passive' if self.passive else 'aggr'} {PURPOSES[self.purpose]} edge={self.edge:.2f})")


class Intent:
    """What an alpha wants. PAIR carries two legs: ((venue, side, px, kind), (venue, side, px, kind))."""
    __slots__ = ("kind", "priority", "venue", "side", "px", "qty", "purpose", "level",
                 "ttl_loops", "expected_edge", "ref", "legs", "order_kind")

    def __init__(self, kind, priority, venue=M, side=BUY, px=None, qty=0, purpose=P_UNKNOWN,
                 level=0, ttl_loops=1, expected_edge=0.0, ref=None, legs=None, order_kind=LIMIT):
        self.kind = kind
        self.priority = priority
        self.venue = venue
        self.side = side
        self.px = px
        self.qty = qty
        self.purpose = purpose
        self.level = level
        self.ttl_loops = ttl_loops
        self.expected_edge = expected_edge
        self.ref = ref              # Order for CANCEL
        self.legs = legs            # for PAIR
        self.order_kind = order_kind

    def __repr__(self):
        if self.kind == PAIR:
            return f"Intent(PAIR P{self.priority} {self.qty} legs={self.legs} edge={self.expected_edge:.2f})"
        if self.kind == CANCEL:
            return f"Intent(CANCEL P{self.priority} {self.ref!r})"
        return (f"Intent({INTENT_KIND_NAME[self.kind]} P{self.priority} {VENUE_NAME[self.venue]} "
                f"{SIDE_NAME[self.side]} {self.qty}@{self.px} {PURPOSES[self.purpose]})")


class Inventory:
    """Position, own resting orders, fill detection from the position delta.

    `pos` is: server position as of the last poll + acked aggressive fills since that poll.
    So `pos_server - pos` at the next poll is exactly the unaccounted (passive / late) fills.
    """
    __slots__ = ("pos", "pos_venue", "own_resting", "pending_aggr", "orders", "gross_limit",
                 "net_limit", "slack", "pos_per_ticker", "gross_both_legs", "n_unattributed",
                 "by_server", "pos_hist", "lag_window_ns", "n_lag_ignored")

    def __init__(self, gross_limit=25000, net_limit=25000, slack=1000,
                 pos_per_ticker=True, gross_both_legs=True, pos_lag_window_ms=1000.0):
        self.pos = 0
        self.pos_venue = [0, 0]
        self.own_resting = {(v, s): [] for v in VENUES for s in (BUY, SELL)}
        self.pending_aggr = []
        self.orders = {}            # id_local -> Order (everything we ever sent)
        self.by_server = {}         # id_server -> Order
        self.gross_limit = gross_limit
        self.net_limit = net_limit
        self.slack = slack
        self.pos_per_ticker = pos_per_ticker
        self.gross_both_legs = gross_both_legs
        self.n_unattributed = 0
        # positions the bot has held recently (t_ns, pos): the server's lagging field is believed
        # only when it leaves this envelope (RIT: 30–200 ms lag, probe 10 Sep; the p2 runaway)
        self.pos_hist = deque()
        self.lag_window_ns = int(pos_lag_window_ms * 1e6)
        self.n_lag_ignored = 0

    # ---- limits ------------------------------------------------------------------------
    def gross(self):
        return abs(self.pos_venue[0]) + abs(self.pos_venue[1])

    def headroom(self):
        """Spec formula: gross_limit - slack - |pos|  (net headroom)."""
        return max(0, min(self.gross_limit, self.net_limit) - self.slack - abs(self.pos))

    def headroom_gross(self):
        """Gross headroom. When RIT nets the two tickers (`gross_both_legs=False`, verified on the
        demo) the exchange's gross is |net|, not the bot's per-venue sum — otherwise a filled pair
        would wrongly consume 2q of headroom and block every later pair, including the reverse
        cross that unwinds."""
        gross = self.gross() if self.gross_both_legs else abs(self.pos)
        return max(0, self.gross_limit - self.slack - gross)

    def pair_headroom(self, cheap=None, rich=None):
        """Max pair size q (buy q on `cheap`, sell q on `rich`) that keeps gross exposure within
        gross_limit - slack. With `gross_both_legs` (VERIFY LIVE: RIT counts per-ticker
        positions toward gross), gross after the pair is |pos_cheap + q| + |pos_rich - q|; a pair
        in the direction that unwinds existing exposure is allowed even when the naive
        (gross_limit - gross)/2 is zero. Without venues, the conservative symmetric bound."""
        if not self.gross_both_legs:
            return self.headroom_gross()
        G = self.gross_limit - self.slack
        if cheap is None or rich is None:
            return max(0, (G - self.gross()) // 2)
        a = self.pos_venue[cheap]
        b = self.pos_venue[rich]
        if abs(a) + abs(b) > G:
            # already over: only a pair that unwinds both legs is allowed, and only up to flat
            return min(-a, b) if (a < 0 and b > 0) else 0
        lo, hi = 0, max(self.gross_limit, 1)
        # f(q) = |a+q| + |b-q| is convex and f(0) <= G, so {q >= 0 : f(q) <= G} is [0, q_max]
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if abs(a + mid) + abs(b - mid) <= G:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def resting_qty(self, v, side):
        n = 0
        for o in self.own_resting[(v, side)]:
            n += o.qty - o.filled
        return n

    def resting_at(self, v, side, px):
        n = 0
        for o in self.own_resting[(v, side)]:
            if o.px == px:
                n += o.qty - o.filled
        return n

    # ---- netting -----------------------------------------------------------------------
    def net_out(self, bbo):
        """The book minus us. If our order IS the touch and nothing sits behind it, that side
        shows size 0 (px kept) until a ladder poll supplies the next level (MarketState)."""
        out = bbo.copy()
        for v in VENUES:
            ob = self.resting_at(v, BUY, bbo.bid[v])
            if ob:
                out.bid_size[v] = max(0, bbo.bid_size[v] - ob)
            oa = self.resting_at(v, SELL, bbo.ask[v])
            if oa:
                out.ask_size[v] = max(0, bbo.ask_size[v] - oa)
        return out

    # ---- registration ------------------------------------------------------------------
    def register(self, o):
        self.orders[o.id_local] = o
        if o.id_server:
            self.by_server[o.id_server] = o

    def add_resting(self, o):
        lst = self.own_resting[(o.venue, o.side)]
        if o not in lst:
            lst.append(o)
            # best price first: bids desc, asks asc
            lst.sort(key=(lambda x: -x.px) if o.side == BUY else (lambda x: x.px))

    def remove_resting(self, o):
        lst = self.own_resting[(o.venue, o.side)]
        if o in lst:
            lst.remove(o)

    def all_resting(self):
        out = []
        for lst in self.own_resting.values():
            out.extend(lst)
        return out

    # ---- fills from the position delta -------------------------------------------------
    def _envelope(self, t_seen):
        """(lo, hi) of positions held within the lag window, including the current one."""
        h = self.pos_hist
        cut = t_seen - self.lag_window_ns
        while h and h[0][0] < cut:
            h.popleft()
        lo = hi = self.pos
        for _, p_ in h:
            if p_ < lo:
                lo = p_
            elif p_ > hi:
                hi = p_
        return lo, hi

    def detect_fills(self, pos_server, pos_venue_server, t_seen, fair):
        """Fills the server knows about and we do not.

        The server's position field lags our acks (RIT: 30–200 ms). So a reading inside the
        envelope of positions we held in the last `lag_window` is lag, not a fill, and is ignored
        (`pos` stays ours). Only the part of the reading that leaves the envelope is attributed:
        above it → a buy of (pos_server − hi); below → a sell of (lo − pos_server). With no acks
        for a window the envelope collapses to `pos` and the server is authoritative again.
        Attribution: pending aggressive orders first, then own resting best-price-first, then
        "unknown". Provisional; the orders sweep corrects it."""
        fills = []
        if not (self.pos_per_ticker and pos_venue_server is not None):
            lo, hi = self._envelope(t_seen)
            if lo <= pos_server <= hi:
                if pos_server != self.pos:
                    self.n_lag_ignored += 1
                return fills
            delta = (pos_server - hi) if pos_server > hi else (pos_server - lo)
            side = BUY if delta > 0 else SELL
            order = sorted(VENUES, key=lambda v: -len(self.own_resting[(v, side)]))
            rem = abs(delta)
            for v in order:
                if rem == 0:
                    break
                rem = self._attribute(v, rem * SIGN[side], t_seen, fair, fills, residual=False)
            if rem:
                self._residual(order[0], side, rem, t_seen, fair, fills)
            self.pos += delta
            self.pos_hist.append((t_seen, self.pos))
            return fills
        if self.pos_per_ticker and pos_venue_server is not None:
            for v in VENUES:
                delta = pos_venue_server[v] - self.pos_venue[v]
                if delta:
                    self._attribute(v, delta, t_seen, fair, fills)
                self.pos_venue[v] = pos_venue_server[v]
            self.pos = pos_server
        return fills

    def _attribute(self, v, delta, t_seen, fair, fills, residual=True):
        side = BUY if delta > 0 else SELL
        rem = abs(delta)
        # 1. pending aggressive orders on (v, side)
        for o in list(self.pending_aggr):
            if rem == 0:
                break
            if o.venue != v or o.side != side:
                continue
            take = min(rem, o.qty - o.filled)
            if take <= 0:
                self.pending_aggr.remove(o)
                continue
            self._apply_fill(o, take, o.expected_px, t_seen, False, fair, fills)
            rem -= take
            if o.filled >= o.qty:
                self.pending_aggr.remove(o)
        # 2. own resting on (v, side), best px first
        for o in list(self.own_resting[(v, side)]):
            if rem == 0:
                break
            take = min(rem, o.qty - o.filled)
            if take <= 0:
                continue
            self._apply_fill(o, take, o.px, t_seen, True, fair, fills)
            rem -= take
            if o.filled >= o.qty:
                self.remove_resting(o)
        # 3. residual → unknown
        if rem and residual:
            self._residual(v, side, rem, t_seen, fair, fills)
            rem = 0
        if not self.pos_per_ticker:
            self.pos_venue[v] += (abs(delta) - rem) * SIGN[side]
        return rem

    def _residual(self, v, side, qty, t_seen, fair, fills):
        self.n_unattributed += qty
        fills.append(Fill(-1, v, side, qty, int(round(fair)), t_seen, True, P_UNKNOWN, fair, 0.0))

    def _apply_fill(self, o, qty, px, t_seen, passive, fair, fills):
        if o.filled == 0:
            o.t_first_fill = t_seen
        o.vwap_ticks = (o.vwap_ticks * o.filled + px * qty) / (o.filled + qty)
        o.filled += qty
        o.pos_applied = o.filled     # the position delta already contains this fill
        if o.filled >= o.qty:
            o.state = FILLED
            o.t_done = t_seen
        elif o.state == ACKED:
            o.state = PARTIAL
        fills.append(Fill(o.id_local, o.venue, o.side, qty, px, t_seen, passive, o.purpose, fair))

    # ---- after dispatch ----------------------------------------------------------------
    def apply_orders(self, orders, keep_remainder=False, fair=0.0, t_seen=0):
        """Fold dispatcher results in. Returns the Fills implied by the acks.

        - Fills reported in an ack (aggressive legs, or a quote that turned out marketable)
          move `pos` immediately; `pos_applied` remembers how much so the next poll's delta
          does not double count.
        - Partially filled LIMIT orders that are allowed to rest are registered in
          `own_resting`; MARKET remainders (rare, delayed venues) go to `pending_aggr`.
        - CANCELLED / REJECTED / RATE_LIMITED de-register.
        """
        fills = []
        for o in orders:
            self.register(o)
            new_qty = o.filled - o.pos_applied
            if new_qty > 0:
                if o.t_first_fill == 0:
                    o.t_first_fill = o.t_ack
                self.pos_hist.append((t_seen or o.t_ack, self.pos))     # the value the server may still show
                self.pos += SIGN[o.side] * new_qty
                self.pos_venue[o.venue] += SIGN[o.side] * new_qty
                o.pos_applied = o.filled
                px = o.vwap_ticks if o.vwap_ticks else (o.px if o.px is not None else o.expected_px)
                fills.append(Fill(o.id_local, o.venue, o.side, new_qty, px, t_seen or o.t_ack,
                                  not o.is_aggressive, o.purpose, fair or o.fair_at_intent))
            st = o.state
            if st in DONE_STATES:
                self.remove_resting(o)
                if o in self.pending_aggr:
                    self.pending_aggr.remove(o)
                continue
            if st in (SENT, INTENT, CANCEL_SENT):
                continue
            if o.remaining > 0 and o.kind == LIMIT:
                if o.is_aggressive and not keep_remainder:
                    # the dispatcher tried to cancel the remainder and the server said no
                    # (it may be filling): keep it as pending so fills are attributed to it
                    if o not in self.pending_aggr:
                        self.pending_aggr.append(o)
                    continue
                self.add_resting(o)
            elif o.remaining > 0 and o.kind == MARKET and o not in self.pending_aggr:
                self.pending_aggr.append(o)
        return fills

    def apply_sweep(self, rows, t_seen):
        """rows: [(id_server, filled, vwap_ticks, status_str)] from GET /v1/orders.
        Attribution only: correct filled / vwap / state on known orders."""
        changed = []
        for id_server, filled, vwap, status in rows:
            o = self.by_server.get(id_server)
            if o is None:
                continue
            if filled > o.filled:
                o.filled = filled
                if o.t_first_fill == 0:
                    o.t_first_fill = t_seen
                changed.append(o)
            if vwap:
                o.vwap_ticks = vwap
            if status == "TRANSACTED" and o.state != FILLED:
                o.state = FILLED
                o.t_done = o.t_done or t_seen
                self.remove_resting(o)
                if o in self.pending_aggr:
                    self.pending_aggr.remove(o)
                changed.append(o)
            elif status == "CANCELLED" and o.state not in DONE_STATES:
                o.state = CANCELLED
                o.t_done = o.t_done or t_seen
                self.remove_resting(o)
                changed.append(o)
        return changed
