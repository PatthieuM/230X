"""dispatcher.send — the only place messages touch the wire.

    PAIR    → `PairSender` on ord1/ord2 (concurrent / slow-first / confirm-then-hedge)
    NEW     → ord lanes round-robin
    CANCEL  → cxl lane (DELETE /v1/orders/{id}); success → CANCELLED, else the state stays and
              the orders sweep decides (the order may have filled first)

An aggressive LIMIT leg acked with a remainder is left to `risk.gate`, which emits CANCEL(P1)
once `cancel_retry_ms` has passed and again every loop until the server confirms: the RIT client
acks before the server has registered the order, so an immediate DELETE returns 404
(probe 10 Sep 2026). `cfg.immediate_remainder_cancel` restores the old behaviour for the mock.
Every result lands in `Order.state` and its timestamps; a 429 penalises the shared budget.
Returns the list of Orders touched this loop (the engine writes them to the ring).
"""
from __future__ import annotations

from ..api.messages import parse_cancel
from ..core.types import (ACKED, BUY, CANCEL_SENT, CANCELLED, LIMIT, PARTIAL, RESTING_STATES)
from .pair import PairSender, send_leg
from .reconciler import CXL, NEW, PAIRMSG


class Dispatcher:
    __slots__ = ("cfg", "lanes", "pb", "budget", "breaker", "pairs", "n_sent", "n_cancel",
                 "n_pairs", "n_remainder_cancel")

    def __init__(self, cfg, lanes, pb, budget, breaker):
        self.cfg = cfg
        self.lanes = lanes
        self.pb = pb
        self.budget = budget
        self.breaker = breaker
        self.pairs = PairSender()
        self.n_sent = 0
        self.n_cancel = 0
        self.n_pairs = 0
        self.n_remainder_cancel = 0

    def close(self):
        self.pairs.close()

    def send(self, msgs, ms=None):
        touched = []
        cfg = self.cfg
        for m in msgs:
            if m.kind == PAIRMSG:
                o1, o2 = m.orders
                self.pairs.send(self.lanes, self.pb, o1, o2, cfg, self.budget, self.breaker)
                self.n_pairs += 1
                self.n_sent += 2
                touched.append(o1)
                touched.append(o2)
                for o in (o1, o2):
                    self._maybe_cancel_remainder(o, ms, touched)
            elif m.kind == NEW:
                o = m.orders[0]
                send_leg(self.lanes.ord(), self.pb, o, cfg.scale, self.budget, self.breaker)
                self.n_sent += 1
                touched.append(o)
                if o.is_aggressive:
                    self._maybe_cancel_remainder(o, ms, touched)
            elif m.kind == CXL:
                self.cancel(m.ref)
                touched.append(m.ref)
        return touched

    def cancel(self, o):
        """DELETE on the cxl lane. Keeps the order's state if the server says no."""
        if not o.id_server or o.state not in RESTING_STATES:
            return o
        status, body, t_send, t_recv = self.lanes.cxl.delete(self.pb.cancel_path(o.id_server))
        o.t_cancel_send = t_send
        o.t_cancel_ack = t_recv
        self.n_cancel += 1
        self.breaker.record(o.venue, status != 0 and status < 500, t_recv, hard=status >= 500)
        if parse_cancel(status, body):
            o.state = CANCELLED
            o.t_done = t_recv
        return o

    def cancel_all(self):
        status, body, t_send, t_recv = self.lanes.cxl.post(self.pb.cancel_all_path())
        return status == 200

    def _maybe_cancel_remainder(self, o, ms, touched):
        if not self.cfg.immediate_remainder_cancel:
            return              # risk.gate cancels it after cancel_retry_ms (RIT registers orders late)
        if o.kind != LIMIT or o.state not in (ACKED, PARTIAL) or o.remaining <= 0:
            return
        if self.cfg.keep_remainder and ms is not None and ms.bbo is not None:
            bid_max, ask_min = ms.band(o.venue)
            inside = (o.side == BUY and o.px <= bid_max) or (o.side != BUY and o.px >= ask_min)
            if inside:
                return
        self.n_remainder_cancel += 1
        self.cancel(o)
