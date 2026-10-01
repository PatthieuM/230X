"""Configuration: static knobs, overrides from the case sheet, and `params.json` from analysis.

Three layers, later wins:
    1. `Config()` defaults — safe values for the mock and for a first live session.
    2. `Config.apply_sheet(sheet)` — fee / rebate / max order / delay / orders-per-second, read
       from GET /v1/securities at tick 0. These are facts, never guessed.
    3. `Config.apply_params(path)` — `params.json` written by `sim/analysis.py` (d, c_min,
       ec_coef, stale_leader): the strategy numbers that come from data.

Every `VERIFY LIVE` flag is a config field with a conservative default. They are listed in
CLAUDE.md and checked at tick 0 of a live session; they are never silently promoted to a default.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields

from .types import LIMIT


@dataclass
class Sheet:
    """Facts from GET /v1/securities (+ /v1/limits) at tick 0."""
    quoted_decimals: int = 2
    fee: tuple = (0.0, 0.0)                  # $ per share, per venue (M, A)
    rebate: tuple = (0.0, 0.0)               # $ per share limit-order rebate, per venue
    max_trade_size: int = 10000
    min_trade_size: int = 1
    api_orders_per_second: int = 10
    execution_delay_ms: tuple = (0, 0)
    gross_limit: int = 25000
    net_limit: int = 25000
    ticks_per_period: int = 300
    tickers: tuple = ("CRZY_M", "CRZY_A")

    @property
    def scale(self):
        return 10 ** self.quoted_decimals


@dataclass
class Config:
    # ---- connection --------------------------------------------------------------------
    host: str = "localhost"
    port: int = 9999
    api_key: str = ""
    timeout_s: float = 2.0

    # ---- ticks and costs (float ticks; filled by apply_sheet) ---------------------------
    scale: int = 100
    fee: tuple = (0.0, 0.0)                  # ticks per share, per venue
    rebate: tuple = (0.0, 0.0)
    fee2: float = 0.0                        # round-trip taker fee of a pair, ticks

    # ---- limits ------------------------------------------------------------------------
    max_order: int = 10000
    min_order: int = 1
    gross_limit: int = 25000
    net_limit: int = 25000
    slack: int = 1000                        # keep this far from the limit
    max_abs_pos: int = 6000                  # inventory cap for the quoting alpha

    # ---- cross alpha -------------------------------------------------------------------
    min_edge_cross: float = 0.0              # ticks; PAIR requires edge > this
    leg_kind: int = LIMIT                    # LIMIT at the observed touch (protective) or MARKET
    ladder_fresh_loops: int = 5              # use ladder depth for overlap if age <= this
    pair_mode: str = "sequential"            # sequential | concurrent; the RIT client serialises
                                             # connections (probe 10 Sep 2026: 4 GETs 0.98x), so
                                             # concurrent legs only add a thread hand-off
    confirm_then_hedge: bool = True          # send the SCARCE leg first (smaller displayed size), size the deep leg to
                                             # its fill. Test session 10 Sep (50 bots): crosses are other students' small
                                             # orders gone in ms; the deep leg filled, the scarce one missed, and the
                                             # residual was hedged at MARKET into a 58-tick-wide book (−$17k).
    second_leg_market: bool = False          # second leg as MARKET: fills at the new touch instead of resting when 50 bots
                                             # ate the level first; bounds a miss at that venue's spread, no residual to hedge.
                                             # Test-session decision: on if the one-leg rate > 20 %
    keep_remainder: bool = False             # marketable-LIMIT remainder: keep if inside band

    # ---- quotes alpha ------------------------------------------------------------------
    enable_quotes: bool = False
    quote_size: int = 500                    # used when quote_size_mode == "fixed"
    quote_size_mode: str = "model"           # model: q* = (d − a·σ)/(c(1+ρ)), c = skew_at_limit/max_abs_pos — the same
                                             # quadratic inventory penalty that the linear skew is the FOC of (review 10 Sep:
                                             # a fixed 500 contradicted the skew by 1.8–3.6×). Bracketed by ρ, capped by room.
    rho_venues: float = 0.5                  # P(the other venue's same-side quote also fills | this one fills); unmeasured,
                                             # 0 → 1800/venue at d=3, 1 → 900; a live quoting run gives the co-fill rate
    d: float = 3.0                           # half-spread in ticks (params.json overrides)
    d_cap: float = 12.0
    c_min: float = 0.0                       # 0 → computed: ceil(s_tight/2 + f - r + 1)
    use_d_star: bool = False                 # d from sigma_hat / lambda_hat instead of fixed d
    k_fill: float = 0.0                      # fill-intensity decay per tick (analyze --fills); 0 = unfitted
    use_k_width: bool = False                # d = 1/k_fill (GLFT width term) instead of fixed d
    skew_at_limit: float = 10.0              # ticks of reservation-price shift at |pos| = max_abs_pos
    skew_curve: float = 1.0                  # 1 = linear (A-S); >1 accelerates at the extremes (GLFT-like)
    skew_through_fair: bool = False          # allow the reducing quote to cross fair (liquidation mode)
    k_unwind: float = 20.0                   # ticks before the end: reduce-only quoting at the touch
    tol: int = 2                             # hysteresis: keep resting if |px - target| <= tol
                                             # (tol=1 let quotes eat the whole 10/s budget on the mock)
    min_edge_quote: float = 0.0
    band_w: int = -1                         # -1 → ceil(fee2)

    # ---- other alphas ------------------------------------------------------------------
    enable_layers: bool = False
    enable_dislocation: bool = False
    ec_coef: float = 0.0                     # > 0 enables stale alpha
    stale_leader: int = 0                    # venue whose move leads (M=0, A=1)
    stale_J: int = 3                         # ticks; leader jump size that triggers stale
    stale_size: int = 500

    # ---- risk --------------------------------------------------------------------------
    hedge_trigger: int = 2000                # |pos| above this for hold_ms → aggressive flatten (quotes on:
                                             # the passive hedge gets a chance first)
    hold_ms: int = 500
    hedge_trigger_no_quotes: int = 0         # quotes off: any one-leg residual is hedged at the touch…
    hold_ms_no_quotes: int = 250             # …after this delay (must exceed the position-field lag)
    hedge_retry_ms: float = 300.0            # one aggressive hedge/flatten in flight at a time, retried after this
    hedge_kind_no_quotes: int = 1            # LIMIT, clamped (never MARKET into a wide book — test session 10 Sep)
    max_hedge_ticks: float = 4.0             # a hedge never executes further than this from fair; beyond it, it rests
    breaker_5xx_hard: bool = False           # p2: the RIT client returns 500 under load (12×); a hard trip cancelled
                                             # the other venue each time. Soft: count toward N, back off 5xx_backoff_ms
    backoff_5xx_ms: float = 200.0            # no new orders to that venue for this long after a 5xx
    pos_lag_window_ms: float = 1000.0        # server position inside the envelope of recent positions = lag, not a fill
    max_aggr_shares_10s: int = 250_000       # runaway guard: FILLED hedge/flatten shares per 10 s beyond this → kill.
                                             # Filled, not sent (a resting clamped hedge is harmless); cross legs excluded.
                                             # The p2 runaway was ~600k filled hedge shares / 10 s. (Heat: 100 orders/s)
    max_unattributed: int = 50_000           # runaway guard: unattributed fill volume beyond this → kill
    breaker_n: int = 3                       # consecutive non-200 / timeouts → venue disabled
    breaker_cooldown_s: float = 5.0          # then probe the venue again
    k_quotes: float = 10.0                   # ticks before the end: cancel all quotes
    k_flat: float = 5.0                      # ticks before the end: flatten residual
    kill_file: str = "runs/KILL"
    kill_check_every: int = 50               # loops (the only file I/O near the loop)

    # ---- polling cadence (loops) -------------------------------------------------------
    poll_case_every: int = 25
    poll_book_every: int = 10
    poll_orders_every: int = 50
    book_limit: int = 10

    # ---- market state ------------------------------------------------------------------
    fair_mode: str = "micro_depth"           # micro_depth (fair_v0) | mean_mids | mean_micro. Review 10 Sep: on the
                                             # demo tapes each centre "forecasts" its own future best (target tautology);
                                             # deciding needs T&S mark-outs. Unchanged until then.
    sigma0: float = 1.0                      # ticks; initial sigma_hat over the horizon
    sigma_horizon_s: float = 1.0             # the quote's expected resting horizon: sigma_hat = std of mid over it
    sigma_mode: str = "rate"                 # rate: sqrt(EWMA(Δmid²/dt)·H) — unbiased for a random walk, overstates under
                                             # bounce; lag: EWMA((mid_t − mid_{t−H})²) measured at the horizon. Review 10 Sep
                                             # found ×2.5 overstatement on MOCK tapes; on the demo tapes the signature is flat
                                             # (×1.0–1.3), so rate stays. Both are computed and logged; a live run decides.
    sigma_lag_slots: int = 32                # ring slots for the lag estimator (slot = H / slots)
    adverse_a: float = 0.0                   # expected adverse move after a fill, in sigma units (report: post-fill drift/σ)
    tilt_k: float = 0.0                      # widen the exposed side by tilt_k·(micro − mid): the side the book leans against
    ewma_alpha: float = 0.05                 # per-loop EWMA for lambda_hat (fills/s)
    sigma_tau_s: float = 10.0                # memory of the sigma smoothers in seconds (time-based: a = dt/τ), so a
                                             # 1,200-poll/s loop does not reduce the window to a few milliseconds
    jump_J: int = 3                          # ticks; dislocation flag threshold

    # ---- budget ------------------------------------------------------------------------
    orders_per_second: float = 10.0
    budget_per_ticker: bool = False           # VERIFY LIVE (probe §7b): one bucket per ticker, pair = 1 token each
    p0_reserve: int = 4                      # two pairs always possible; quotes never take these
    cancel_costs_token: bool = False         # VERIFY LIVE: are DELETEs rate-limited?
    get_costs_token: bool = False            # verified 10 Sep 2026 on the ALGO1 demo: 5,762 GETs in 3 s, no 429

    # ---- VERIFY LIVE assumptions -------------------------------------------------------
    pos_per_ticker: bool = False             # verified 10 Sep 2026 (demo): both tickers carry the aggregated case position
    gross_both_legs: bool = False            # verified 10 Sep 2026 (demo): a 100-share pair leaves gross = net = 0
    self_trade_prevented: bool = False       # verified 10 Sep 2026 (demo): our MARKET buy hit our own resting ask
    cancel_retry_ms: float = 15.0            # the server registers an order ~10 ms after the ack: DELETE before that → 404;
                                             # resting remainders are cancelled by risk.gate after this delay, retried each loop
    immediate_remainder_cancel: bool = False # dispatcher cancels an aggressive LIMIT remainder right after the ack (404 on RIT)

    # ---- monitoring --------------------------------------------------------------------
    params_path: str = "params.json"
    run_name: str = "run"
    seed: int = 0                            # provenance only: the mock seed this run was paired on
    ring_loops: int = 1 << 19                # 524k loop records ≈ 52 MB: a 300-tick heat at ~1,200 loops/s is ~360k
    ring_orders: int = 1 << 14
    ring_fills: int = 1 << 14

    # ---- derived -----------------------------------------------------------------------
    delay_ms: tuple = (0, 0)
    tickers: tuple = ("CRZY_M", "CRZY_A")

    # -------------------------------------------------------------------------------------
    def apply_sheet(self, sh: Sheet):
        """Facts from the server override defaults."""
        self.scale = sh.scale
        self.fee = (sh.fee[0] * sh.scale, sh.fee[1] * sh.scale)
        self.rebate = (sh.rebate[0] * sh.scale, sh.rebate[1] * sh.scale)
        self.fee2 = self.fee[0] + self.fee[1]
        self.max_order = int(sh.max_trade_size)
        self.min_order = max(1, int(sh.min_trade_size))
        self.gross_limit = int(sh.gross_limit)
        self.net_limit = int(sh.net_limit)
        self.orders_per_second = float(sh.api_orders_per_second or self.orders_per_second)
        self.delay_ms = tuple(sh.execution_delay_ms)
        self.tickers = tuple(sh.tickers)
        return self

    def apply_params(self, path):
        """`params.json` from analysis: only known fields, ignore the rest."""
        try:
            with open(path) as f:
                d = json.load(f)
        except FileNotFoundError:
            return self
        names = {f.name for f in fields(self)}
        for k, v in d.items():
            if k in names:
                setattr(self, k, v)
        return self

    def apply_overrides(self, **kw):
        for k, v in kw.items():
            if v is not None and hasattr(self, k):
                setattr(self, k, v)
        return self

    @property
    def band_width(self):
        return self.band_w if self.band_w >= 0 else int(math.ceil(self.fee2))

    def edge_fee(self, v):
        """Taker fee minus maker rebate, ticks, on venue v."""
        return self.fee[v] - self.rebate[v]
