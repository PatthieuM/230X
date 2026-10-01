"""Clock: monotonic nanoseconds for latency, plus the case tick estimated between /v1/case polls.

Why two clocks: the server only exposes tick-resolution time (1 tick = 1 s by default) and only
when polled. Everything latency-related is client-observed `perf_counter_ns()`; the tick is needed
only for end-of-case logic, so it is polled every N loops and extrapolated in between.
"""
from __future__ import annotations

from time import perf_counter_ns

now_ns = perf_counter_ns


class Clock:
    __slots__ = ("t_start", "tick", "ticks_total", "status", "t_tick_seen", "tick_ns",
                 "period", "total_periods")

    def __init__(self, ticks_total=300, tick_ns=1_000_000_000):
        self.t_start = now_ns()
        self.tick = 0
        self.ticks_total = ticks_total
        self.status = "ACTIVE"
        self.t_tick_seen = self.t_start
        self.tick_ns = tick_ns          # wall ns per tick (1 s real time by default; mock may run faster)
        self.period = 1
        self.total_periods = 1

    def set_case(self, tick, ticks_total, status, t_seen=None, period=1, total_periods=1):
        """Record a /v1/case observation."""
        t_seen = t_seen or now_ns()
        if tick > self.tick and self.t_tick_seen:
            dt = t_seen - self.t_tick_seen
            dtick = tick - self.tick
            if dt > 0 and dtick > 0:
                # EWMA of the observed ns-per-tick so the mock's speed-up is learned
                self.tick_ns = 0.5 * self.tick_ns + 0.5 * (dt / dtick)
        self.tick = tick
        self.ticks_total = ticks_total
        self.status = status
        self.t_tick_seen = t_seen
        self.period = period
        self.total_periods = total_periods

    def tick_est(self, t=None):
        """Extrapolated tick between polls (never past ticks_total)."""
        t = t or now_ns()
        est = self.tick + (t - self.t_tick_seen) / self.tick_ns
        return min(est, self.ticks_total)

    def ticks_left(self, t=None):
        return self.ticks_total - self.tick_est(t)

    def elapsed_s(self, t=None):
        return ((t or now_ns()) - self.t_start) * 1e-9

    @property
    def active(self):
        return self.status == "ACTIVE"
