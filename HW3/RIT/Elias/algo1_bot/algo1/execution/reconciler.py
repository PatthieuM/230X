"""reconciler.plan — desired book → messages, with the token budget and one message per key.

    key = (venue, side, level): at most one NEW per key per loop; a CANCEL followed by a NEW on
          the same key is a replace (cancel first, never two resting on one key).
    sort by priority P0..P4 (stable, so an alpha's CANCEL-then-QUOTE order survives).
    PAIR: `budget.try_acquire(2, P0)` or the whole pair is dropped — never leg in on the budget.
    others: `try_acquire(1, cls)` or the denial is counted per class.

Orders are created here (id_local, t_intent = now, snapshot_id, fair_at_intent) so every
downstream timestamp is measured from the same origin. Output is a list of `Msg`.
"""
from __future__ import annotations

from time import perf_counter_ns

from ..core.types import (CANCEL, CANCEL_SENT, LIMIT, Order, P0, PAIR, QUOTE, RESTING_STATES,
                          TAKE)

NEW, CXL, PAIRMSG = 0, 1, 2
MSG_NAME = ("NEW", "CANCEL", "PAIR")


class Msg:
    __slots__ = ("kind", "orders", "ref", "priority", "intent")

    def __init__(self, kind, orders=(), ref=None, priority=P0, intent=None):
        self.kind = kind
        self.orders = orders
        self.ref = ref
        self.priority = priority
        self.intent = intent

    def __repr__(self):
        return f"Msg({MSG_NAME[self.kind]} P{self.priority} {self.orders or self.ref})"


class Reconciler:
    __slots__ = ("cfg", "n_pair_denied", "n_dup_key", "n_planned")

    def __init__(self, cfg):
        self.cfg = cfg
        self.n_pair_denied = 0
        self.n_dup_key = 0
        self.n_planned = 0

    def plan(self, intents, inv, budget, snapshot_id=0, fair=0.0, t=None):
        t = t or perf_counter_ns()
        cfg = self.cfg
        msgs = []
        claimed = set()          # keys with a NEW this loop
        cancelled = set()        # order id_locals with a CANCEL this loop
        for it in sorted(intents, key=lambda x: x.priority):
            k = it.kind
            if k == CANCEL:
                o = it.ref
                if o is None or o.state not in RESTING_STATES or o.state == CANCEL_SENT \
                        or o.id_local in cancelled or not o.id_server:
                    continue      # a failed DELETE leaves state ACKED, so a retry passes here
                if cfg.cancel_costs_token and not budget.try_acquire(1, it.priority, t, o.venue):
                    continue
                cancelled.add(o.id_local)
                msgs.append(Msg(CXL, ref=o, priority=it.priority, intent=it))
            elif k == PAIR:
                if not budget.try_acquire_pair(P0, t, (it.legs[0][0], it.legs[1][0])):
                    self.n_pair_denied += 1
                    continue
                orders = []
                for v, side, px, kind in it.legs:
                    orders.append(Order(v, side, kind, it.qty, px if kind == LIMIT else None,
                                        it.purpose, 0, snapshot_id, px or 0, it.expected_edge, P0,
                                        t, fair))
                msgs.append(Msg(PAIRMSG, orders, priority=P0, intent=it))
            elif k in (QUOTE, TAKE):
                key = (it.venue, it.side, it.level)
                if key in claimed:
                    self.n_dup_key += 1
                    continue
                if k == QUOTE:
                    # never two resting on one key: something must be resting-and-not-cancelled?
                    live = [o for o in inv.own_resting[key[:2]]
                            if o.level == it.level and o.id_local not in cancelled
                            and o.state != CANCEL_SENT]
                    if live:
                        self.n_dup_key += 1
                        continue
                if not budget.try_acquire(1, it.priority, t, it.venue):
                    continue
                claimed.add(key)
                kind = it.order_kind if k == TAKE else LIMIT
                o = Order(it.venue, it.side, kind, it.qty, it.px if kind == LIMIT else None,
                          it.purpose, it.level, snapshot_id, it.px or 0, it.expected_edge,
                          it.priority, t, fair)
                msgs.append(Msg(NEW, [o], priority=it.priority, intent=it))
        self.n_planned += len(msgs)
        return msgs
