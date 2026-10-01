"""Token budget with priority classes, a P0 reserve, and optionally one bucket per ticker.

RIT rate-limits POST /v1/orders at `api_orders_per_second` per security (the sheet field), and
the API doc adds that a global limit may exist on top. Two models:
    global      one bucket for all lanes (conservative; the default until `probe` §7b shows that
                9 POSTs on M followed by 9 on A all pass)
    per ticker  one bucket per venue (`budget_per_ticker`); a pair takes one token from EACH
                venue's bucket, or none — never leg in because of the budget.
In either model `p0_reserve` tokens are invisible to P3/P4: a quote reprice is refused rather than
let a bucket fall below what one pair leg needs, so a cross can always be taken the instant it
appears. External review (10 Sep 2026) measured that with a global bucket the reserve floor, not
exhaustion, refuses ~59% of desired reprices; the per-ticker split gives the quoting lane room
without touching the reserve. Both are A/B candidates (`bench --crn`), not blind changes.

A 429 (`wait` seconds) empties the bucket that produced it and freezes its refill until `wait`
has passed.
"""
from __future__ import annotations

from time import perf_counter_ns

from ..core.types import P0, P4


class Budget:
    __slots__ = ("rate", "capacity", "tokens", "t_last", "reserve", "deny", "n429", "t_last_429",
                 "t_frozen_until", "n_acquired", "n_buckets")

    def __init__(self, rate_per_s=10.0, capacity=None, p0_reserve=2, per_ticker=False):
        self.rate = float(rate_per_s)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate_per_s))
        self.n_buckets = 2 if per_ticker else 1
        self.tokens = [self.capacity] * self.n_buckets
        t = perf_counter_ns()
        self.t_last = [t] * self.n_buckets
        self.t_frozen_until = [0] * self.n_buckets
        # reserve seen by class: P0–P2 may use everything, P3/P4 must leave p0_reserve
        self.reserve = {P0: 0, 1: 0, 2: 0, 3: p0_reserve, 4: p0_reserve}
        self.deny = [0] * (P4 + 1)
        self.n429 = 0
        self.t_last_429 = 0
        self.n_acquired = 0

    def _b(self, venue):
        return 0 if self.n_buckets == 1 or venue is None else venue

    def refill(self, t=None):
        t = t or perf_counter_ns()
        for b in range(self.n_buckets):
            if t < self.t_frozen_until[b]:
                self.t_last[b] = t
                continue
            dt = (t - self.t_last[b]) * 1e-9
            if dt > 0:
                self.tokens[b] = min(self.capacity, self.tokens[b] + dt * self.rate)
                self.t_last[b] = t

    def try_acquire(self, n, cls, t=None, venue=None):
        """n tokens from the venue's bucket (or the global one)."""
        self.refill(t)
        b = self._b(venue)
        if self.tokens[b] - n < self.reserve.get(cls, 0) or self.tokens[b] < n:
            self.deny[cls] += 1
            return False
        self.tokens[b] -= n
        self.n_acquired += n
        return True

    def try_acquire_pair(self, cls, t=None, venues=(0, 1)):
        """One token per leg from each leg's bucket, atomically: both or neither."""
        self.refill(t)
        if self.n_buckets == 1:
            return self.try_acquire(2, cls, t)
        res = self.reserve.get(cls, 0)
        b0, b1 = self._b(venues[0]), self._b(venues[1])
        if b0 == b1:
            return self.try_acquire(2, cls, t, venues[0])
        if self.tokens[b0] - 1 < res or self.tokens[b1] - 1 < res or self.tokens[b0] < 1 or self.tokens[b1] < 1:
            self.deny[cls] += 1
            return False
        self.tokens[b0] -= 1
        self.tokens[b1] -= 1
        self.n_acquired += 2
        return True

    def penalize(self, wait_s, t=None, venue=None):
        """On a 429: empty the offending bucket and freeze its refill for `wait_s`."""
        t = t or perf_counter_ns()
        self.n429 += 1
        self.t_last_429 = t
        b = self._b(venue)
        self.tokens[b] = 0.0
        self.t_frozen_until[b] = max(self.t_frozen_until[b], t + int(wait_s * 1e9))
        self.t_last[b] = t

    @property
    def tokens_now(self):
        self.refill()
        return min(self.tokens)

    def snapshot(self):
        return (self.tokens_now, tuple(self.deny), self.n429)
