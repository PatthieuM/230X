"""Pair execution: two legs, three modes.

    sequential          slow leg then fast leg, no threads (default: the RIT client serialises its
                        connections — probe 10 Sep 2026 — so concurrency buys nothing).
                        `second_leg_market`: the second leg goes as MARKET sized to the first leg's
                        fill — in a 50-bot heat the level is often gone by the time it lands; a
                        MARKET leg bounds the miss at the spread instead of resting and being hedged
    concurrent          both legs POSTed at once on ord1/ord2 (threads; the GIL is released in
                        socket I/O). Leg gap = |t_send_1 - t_send_2| is measured, not assumed.
    sequential slow-first
                        when the venues' execution delays are asymmetric: send the slow leg,
                        then the fast one, so both acks land close together.
    confirm-then-hedge  (default since the 10 Sep test session) send the SCARCE leg first — the
                        side with less displayed size, which other bots take first — read
                        `ack.filled`, size the deep leg to it. A residual then needs the deep side
                        to vanish within ~2 ms. Cost: one RTT of cross lifetime.

`send_leg` is the single place an order POST is timed and parsed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from time import perf_counter_ns

from ..api.messages import Ack, RateLimited, Reject, parse_ack
from ..core.types import ACKED, FILLED, MARKET, PARTIAL, RATE_LIMITED, REJECTED, SENT


def send_leg(lane, pb, o, scale, budget=None, breaker=None):
    """POST one order on `lane`; fill in the Order's timestamps / state. Returns the Order."""
    path = pb.order_path(o.venue, o.side, o.kind, o.qty, o.px)
    o.state = SENT
    status, body, t_send, t_recv = lane.post(path)
    o.t_send = t_send
    o.t_ack = t_recv
    o.http_status = status
    if breaker is not None:
        breaker.record(o.venue, status != 0 and status < 500, t_recv, hard=status >= 500)
    res = parse_ack(status, body, scale) if status else Reject("TRANSPORT", "", 0)
    if isinstance(res, Ack):
        o.id_server = res.server_id
        o.filled = res.filled
        o.vwap_ticks = res.vwap_ticks
        if res.status_str == "TRANSACTED" or o.filled >= o.qty:
            o.state = FILLED
            o.t_done = t_recv
        elif o.filled > 0:
            o.state = PARTIAL
        else:
            o.state = ACKED
        if o.filled:
            o.t_first_fill = t_recv
    elif isinstance(res, RateLimited):
        o.state = RATE_LIMITED
        o.t_done = t_recv
        if budget is not None:
            budget.penalize(res.wait_s, t_recv, o.venue)
    else:
        o.state = REJECTED
        o.t_done = t_recv
    return o


class PairSender:
    __slots__ = ("pool",)

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="leg")

    def close(self):
        self.pool.shutdown(wait=False)

    def send(self, lanes, pb, o1, o2, cfg, budget=None, breaker=None):
        """Send a pair. Mode from cfg: confirm_then_hedge > asymmetric delay > concurrent."""
        scale = cfg.scale
        d1, d2 = cfg.delay_ms[o1.venue], cfg.delay_ms[o2.venue]
        if cfg.confirm_then_hedge:
            # o1 is the scarce leg (cross.evaluate orders the legs); the deep leg follows, sized to the fill
            first, second = o1, o2
            send_leg(lanes.ord1, pb, first, scale, budget, breaker)
            if first.filled <= 0:
                second.state = REJECTED        # never sent: the scarce leg got nothing → no residual
                second.t_done = perf_counter_ns()
                return o1, o2
            second.qty = first.filled
            send_leg(lanes.ord2, pb, second, scale, budget, breaker)
            return o1, o2
        if getattr(cfg, "pair_mode", "sequential") == "sequential":
            # the client serialises connections anyway: slow leg first, then the fast one, no threads
            slow, fast = (o1, o2) if d1 >= d2 else (o2, o1)
            send_leg(lanes.ord1, pb, slow, scale, budget, breaker)
            if getattr(cfg, "second_leg_market", False) and slow.filled > 0:
                fast.kind = MARKET          # take whatever is there now; size to the first leg's fill
                fast.px = None
                fast.qty = slow.filled
            elif getattr(cfg, "second_leg_market", False):
                fast.state = REJECTED       # first leg got nothing: no second leg
                fast.t_done = perf_counter_ns()
                return o1, o2
            send_leg(lanes.ord2, pb, fast, scale, budget, breaker)
            return o1, o2
        if d1 != d2 and getattr(cfg, "slow_first", True):
            slow, fast = (o1, o2) if d1 > d2 else (o2, o1)
            f = self.pool.submit(send_leg, lanes.ord1, pb, slow, scale, budget, breaker)
            # small head start for the slow leg, then the fast one on the other lane
            send_leg(lanes.ord2, pb, fast, scale, budget, breaker)
            f.result()
            return o1, o2
        f1 = self.pool.submit(send_leg, lanes.ord1, pb, o1, scale, budget, breaker)
        f2 = self.pool.submit(send_leg, lanes.ord2, pb, o2, scale, budget, breaker)
        f1.result()
        f2.result()
        return o1, o2
