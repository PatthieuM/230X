"""MarketState — everything the alphas read, refreshed once per loop from the poll.

    bbo        the netted top of book (`Inventory.net_out`): the market minus us
    mid/micro  per venue
    fair       fair_v0 (depth-weighted microprice), the reference for quotes and P&L marks
    s          mid_A - mid_M: the venue spread, the tradable object
    sigma_hat  std of the mid over `sigma_horizon_s` (ticks): sqrt(var_rate · horizon), where
               var_rate is an EWMA of Δmid²/dt in ticks²/s — a standard deviation on a fixed
               horizon, which is what d_star's 1 − Φ(x) expects. (v0 was an EWMA of |Δmid| per
               poll: a mean absolute deviation, 0.8σ, whose scale tracked the poll rate.)
    lambda_hat EWMA of passive fills per second per venue, from Fills
    jump[v]    |Δbbo_v| >= J and |Δbbo_other| <= 1  → dislocation flag on v
    band(v)    (bid_max, ask_min): the no-arb band imposed by the OTHER venue's touch:
                   bid_max = ask_other - band_w - 1,  ask_min = bid_other + band_w + 1
               A resting bid above ask_other would be picked off by anyone who buys there.
    ticks_left case ticks to the close, from the engine's Clock (quotes' unwind mode)
    ladders    last Ladder per venue with age in loops; when our order is the only thing at the
               touch (netted size 0) a fresh ladder supplies the next level.

No allocation in `update` beyond scalars and the netted BBO record.
"""
from __future__ import annotations

from ..core.types import A, M, OTHER, VENUES


class MarketState:
    __slots__ = ("cfg", "raw_bbo", "bbo", "mid", "micro", "fair", "s", "sigma_hat", "lambda_hat",
                 "jump", "band_bid_max", "band_ask_min", "ladders", "ladder_age", "prev_mid",
                 "loop_i", "t_last", "n_passive", "venue_enabled", "band_w", "dmid", "t_first",
                 "s_tight", "ready", "ticks_left", "var_rate", "d_bid", "d_ask", "d_raw", "d_bind",
                 "sigma_rate", "sigma_lag", "var_lag", "_ring_t", "_ring_m", "_ring_i", "_ring_n")

    def __init__(self, cfg):
        self.cfg = cfg
        self.raw_bbo = None
        self.bbo = None
        self.mid = [0.0, 0.0]
        self.micro = [0.0, 0.0]
        self.fair = 0.0
        self.s = 0.0
        self.s_tight = 0
        self.sigma_hat = [cfg.sigma0, cfg.sigma0]
        self.var_rate = [cfg.sigma0 ** 2 / max(1e-6, cfg.sigma_horizon_s)] * 2   # ticks²/s
        self.sigma_rate = [cfg.sigma0, cfg.sigma0]     # rate estimator, ticks over the horizon
        self.sigma_lag = [cfg.sigma0, cfg.sigma0]      # horizon-lag estimator, ticks over the horizon
        self.var_lag = [cfg.sigma0 ** 2] * 2
        K = max(4, cfg.sigma_lag_slots)
        self._ring_t = [[0] * K, [0] * K]              # grid-aligned ring of (t, mid) per venue, slot = H/K
        self._ring_m = [[0.0] * K, [0.0] * K]
        self._ring_i = [0, 0]                          # slot index of the newest sample
        self._ring_n = [0, 0]                          # samples stored
        self.d_bid = [0.0, 0.0]     # diagnostics written by quotes.half_distance, logged per loop
        self.d_ask = [0.0, 0.0]
        self.d_raw = [0.0, 0.0]
        self.d_bind = [0, 0]        # 0 fixed d, 1 c_min floor, 2 d_cap, 3 model (d_star / 1/k)
        self.lambda_hat = [0.0, 0.0]
        self.jump = [False, False]
        self.dmid = [0.0, 0.0]
        self.band_bid_max = [0, 0]
        self.band_ask_min = [0, 0]
        self.ladders = [None, None]
        self.ladder_age = [1 << 30, 1 << 30]
        self.prev_mid = [None, None]
        self.loop_i = 0
        self.t_last = 0
        self.t_first = 0
        self.n_passive = [0, 0]
        self.venue_enabled = [True, True]
        self.band_w = cfg.band_width
        self.ready = False
        self.ticks_left = None      # set by the engine from Clock each loop (unwind mode)

    # ---------------------------------------------------------------------------------------
    def update(self, snap, fills, inv):
        cfg = self.cfg
        self.loop_i += 1
        self.raw_bbo = snap.bbo
        bbo = inv.net_out(snap.bbo)
        # our order is the touch and nothing behind it: use the ladder's next level if fresh
        for v in VENUES:
            lad = self.ladders[v]
            if lad is None or self.ladder_age[v] > cfg.ladder_fresh_loops:
                continue
            if bbo.bid_size[v] == 0:
                for px, q, oq in lad.bids:
                    if q - oq > 0 and px <= bbo.bid[v]:
                        bbo.bid[v] = px
                        bbo.bid_size[v] = q - oq
                        break
            if bbo.ask_size[v] == 0:
                for px, q, oq in lad.asks:
                    if q - oq > 0 and px >= bbo.ask[v]:
                        bbo.ask[v] = px
                        bbo.ask_size[v] = q - oq
                        break
        self.bbo = bbo
        alpha = cfg.ewma_alpha
        t = snap.t_recv
        dt_s = (t - self.t_last) * 1e-9 if self.t_last else 0.0
        H = cfg.sigma_horizon_s
        for v in VENUES:
            m = bbo.mid(v)
            self.mid[v] = m
            self.micro[v] = bbo.micro(v)
            pm = self.prev_mid[v]
            if pm is None:
                self.dmid[v] = 0.0
            else:
                d = m - pm
                self.dmid[v] = d
                if dt_s > 0:
                    a = min(1.0, dt_s / cfg.sigma_tau_s)          # time-based smoothing
                    self.var_rate[v] = (1 - a) * self.var_rate[v] + a * (d * d / dt_s)
                    self.sigma_rate[v] = (self.var_rate[v] * H) ** 0.5
            self.prev_mid[v] = m
            self._lag_update(v, m, t, H, cfg.sigma_tau_s)
            self.sigma_hat[v] = self.sigma_lag[v] if cfg.sigma_mode == "lag" else self.sigma_rate[v]
        # dislocation flags
        J = cfg.jump_J
        for v in VENUES:
            self.jump[v] = abs(self.dmid[v]) >= J and abs(self.dmid[OTHER[v]]) <= 1
        self.fair = self._fair(bbo)
        self.s = self.mid[A] - self.mid[M]
        sm, sa = bbo.spread(M), bbo.spread(A)
        self.s_tight = min(sm, sa) if (sm > 0 and sa > 0) else max(sm, sa)
        # passive fill intensity per venue (fills / s), EWMA on the loop's dt
        if self.t_first == 0:
            self.t_first = t
        if self.t_last:
            dt = (t - self.t_last) * 1e-9
            if dt > 0:
                for v in VENUES:
                    n = 0
                    for f in fills:
                        if f.passive and f.venue == v:
                            n += 1
                    self.lambda_hat[v] = (1 - alpha) * self.lambda_hat[v] + alpha * (n / dt)
        self.t_last = t
        # bands
        bw = self.band_w
        for v in VENUES:
            o = OTHER[v]
            self.band_bid_max[v] = bbo.ask[o] - bw - 1
            self.band_ask_min[v] = bbo.bid[o] + bw + 1
        for v in VENUES:
            self.ladder_age[v] += 1
        self.ready = True
        return self

    def _fair(self, bbo):
        from .fair import fair_v0
        mode = self.cfg.fair_mode
        if mode == "micro_depth":
            return fair_v0(bbo, self.venue_enabled)
        vs = [v for v in VENUES if self.venue_enabled[v] and bbo.bid[v] > 0 and bbo.ask[v] > 0]
        if not vs:
            return 0.0
        if mode == "mean_mids":
            return sum(bbo.mid(v) for v in vs) / len(vs)
        return sum(bbo.micro(v) for v in vs) / len(vs)          # mean_micro

    def _lag_update(self, v, m, t, H, tau):
        """Horizon-lag variance: EWMA of (mid_t − mid_{t−H})², updated once per slot advance with
        a = slot/τ (time-based). A K-slot ring spaced H/K apart with a grid-aligned catch-up
        advance (the slot clock never falls behind the poll clock), so the oldest sample is
        H ± H/K old regardless of poll cadence. O(1), no allocation."""
        rt, rm = self._ring_t[v], self._ring_m[v]
        K = len(rt)
        slot_ns = int(H * 1e9 / K)
        i = self._ring_i[v]
        if self._ring_n[v] == 0:
            rt[i], rm[i] = t, m
            self._ring_n[v] = 1
            return
        steps = int((t - rt[i]) // slot_ns) if slot_ns else 0
        if steps <= 0:
            return                                     # same slot: keep the first sample of the slot
        for _ in range(min(steps, K)):                 # catch up on the fixed grid
            i = (i + 1) % K
            rt[i], rm[i] = t, m
        self._ring_i[v] = i
        self._ring_n[v] = min(K, self._ring_n[v] + steps)
        if self._ring_n[v] >= K:
            oldest = (i + 1) % K                       # the slot ~H ago
            if t - rt[oldest] >= 0.8 * H * 1e9:
                d = m - rm[oldest]
                a = min(1.0, (H / K) * min(steps, K) / tau)
                self.var_lag[v] = (1 - a) * self.var_lag[v] + a * d * d
                self.sigma_lag[v] = self.var_lag[v] ** 0.5

    # ---------------------------------------------------------------------------------------
    def set_ladder(self, ladder):
        self.ladders[ladder.venue] = ladder
        self.ladder_age[ladder.venue] = 0

    def ladder_fresh(self, v):
        return self.ladders[v] is not None and self.ladder_age[v] <= self.cfg.ladder_fresh_loops

    def band(self, v):
        return self.band_bid_max[v], self.band_ask_min[v]

    def crossed(self):
        """(edge_buyM_sellA, edge_buyA_sellM) in ticks after fee2, on the netted book."""
        b = self.bbo
        f2 = self.cfg.fee2
        return b.bid[A] - b.ask[M] - f2, b.bid[M] - b.ask[A] - f2
