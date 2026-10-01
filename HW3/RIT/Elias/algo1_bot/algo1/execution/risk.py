"""risk.gate — the policy layer between alphas and the wire. Every intent passes through here.

Checks, in order:
    kill switch   `runs/KILL` exists (the engine checks the file every `kill_check_every` loops
                  and sets `risk.killed`) → cancel everything, flatten, then the engine exits.
    runaway guard the p2 incident (10 Sep 2026): the client's lagging position field was read as
                  phantom fills and the flatten re-fired every loop, 3.3M shares crossed the
                  spread. Now: aggressive volume per 10 s or unattributed volume above a ceiling
                  trips the kill switch; hedges/flattens are one-in-flight with a retry delay.
    breaker       `Breaker` counts consecutive transport failures / 5xx per venue. At N the venue
                  is disabled: intents on it are dropped, no PAIR is allowed, and every resting
                  order on the OTHER venue is cancelled once (an unhedgeable quote is a risk).
                  After `breaker_cooldown_s` the venue is probed again.
    end-of-case   ticks_left <= k_quotes → drop QUOTE intents and cancel all resting quotes;
                  ticks_left <= k_flat   → flatten the residual with a MARKET TAKE(P2).
                  PAIRs stay allowed to the last tick (they are net-flat; late crosses go
                  untaken by others). VERIFY LIVE: how positions are marked at the close.
    unhedged      |pos| > hedge_trigger for longer than hold_ms → TAKE(P2) hedge on the venue
                  with the better touch (LIMIT at the touch, purpose hedge). With quotes off the
                  trigger is `hedge_trigger_no_quotes` (0) after `hold_ms_no_quotes`: a one-leg
                  pair residual has no passive hedge coming and is taken at once (review 10 Sep:
                  13.5% of mock pairs filled one leg; live rate from the report's hit ratio).
    self-trade    a PAIR / TAKE leg that would execute against our own resting order on that
                  (venue, opposite side) → prepend CANCEL(P1) for that order and drop the intent
                  this loop. VERIFY LIVE: does RIT prevent self-execution? (mock: flag).
    headroom      cap TAKE qty at headroom; cap PAIR qty at pair_headroom (both legs must fit as
                  unhedged gross); drop below min_order.
    remainders    an aggressive LIMIT leg that rested (the cross was gone when it landed) is
                  cancelled with CANCEL(P1) once `cancel_retry_ms` has passed since the ack or the
                  last attempt — RIT acks before the server registers the order, so an immediate
                  DELETE returns 404 (probe 10 Sep 2026).
"""
from __future__ import annotations

import math
from time import perf_counter_ns

from ..core.types import (ACKED, BUY, CANCEL, CANCEL_SENT, Intent, LIMIT, M, MARKET, OTHER, P1, P2, PARTIAL,
                          P_FLATTEN, P_HEDGE, P_QUOTE, P_LAYER, PAIR, QUOTE, SELL, TAKE, VENUES)


class Breaker:
    """Per-venue failure counter.
        transport failure / timeout (status 0): counts 1; N consecutive → disabled
        HTTP 5xx: on the RIT client this means "overloaded" (p2, 10 Sep: 12 × 500 under 8 orders/s +
                  1,200 polls/s), not "venue down" — counts 1 and backs the venue off for
                  `backoff_5xx_ms`; `hard_5xx` restores the immediate trip (the mock's 503 = halted)
    A dead venue we stop talking to would otherwise never be detected (the first failed pair leg
    closes the cross and no message goes there again)."""
    __slots__ = ("n", "fails", "disabled", "t_disabled", "cooldown_ns", "n_trips", "hard_5xx",
                 "backoff_ns", "t_backoff_until", "n_5xx")

    def __init__(self, n=3, cooldown_s=5.0, hard_5xx=False, backoff_5xx_ms=200.0):
        self.n = n
        self.fails = [0, 0]
        self.disabled = [False, False]
        self.t_disabled = [0, 0]
        self.cooldown_ns = int(cooldown_s * 1e9)
        self.n_trips = 0
        self.hard_5xx = hard_5xx
        self.backoff_ns = int(backoff_5xx_ms * 1e6)
        self.t_backoff_until = [0, 0]
        self.n_5xx = 0

    def record(self, v, ok, t=None, hard=False):
        if ok:
            self.fails[v] = 0
            if self.disabled[v]:
                self.disabled[v] = False
            return
        if hard:
            # a 5xx: on the RIT client this is "overloaded", not "venue down" (p2, 10 Sep): back off
            self.n_5xx += 1
            self.t_backoff_until[v] = (t or perf_counter_ns()) + self.backoff_ns
        self.fails[v] += self.n if (hard and self.hard_5xx) else 1
        if self.fails[v] >= self.n and not self.disabled[v]:
            self.disabled[v] = True
            self.t_disabled[v] = t or perf_counter_ns()
            self.n_trips += 1

    def maybe_probe(self, t=None):
        """After the cooldown, allow one attempt (fails stays at N-1 so one more failure re-trips)."""
        t = t or perf_counter_ns()
        for v in VENUES:
            if self.disabled[v] and t - self.t_disabled[v] >= self.cooldown_ns:
                self.disabled[v] = False
                self.fails[v] = self.n - 1

    def any_disabled(self):
        return self.disabled[0] or self.disabled[1]

    def backing_off(self, v, t):
        return t < self.t_backoff_until[v]


class Risk:
    __slots__ = ("cfg", "breaker", "killed", "kill_reason", "t_unhedged_since", "breaker_cancelled",
                 "quotes_off", "flatten_phase", "n_self_trade", "n_dropped_venue", "n_capped",
                 "last_hedge_t", "dry", "aggr_log", "aggr_10s", "last_flatten_t")

    def __init__(self, cfg, breaker=None):
        self.cfg = cfg
        self.breaker = breaker or Breaker(cfg.breaker_n, getattr(cfg, "breaker_cooldown_s", 5.0),
                                          getattr(cfg, "breaker_5xx_hard", False), getattr(cfg, "backoff_5xx_ms", 200.0))
        self.killed = False
        self.t_unhedged_since = 0
        self.breaker_cancelled = [False, False]
        self.quotes_off = False
        self.flatten_phase = False
        self.n_self_trade = 0
        self.n_dropped_venue = 0
        self.n_capped = 0
        self.last_hedge_t = 0
        self.last_flatten_t = 0
        self.dry = False            # run --dry: gate returns nothing, ever
        self.kill_reason = ""
        self.aggr_log = []          # (t_ns, qty) of aggressive orders sent, last 10 s
        self.aggr_10s = 0

    # ---------------------------------------------------------------------------------------
    def gate(self, intents, ms, inv, clock, t=None):
        t = t or perf_counter_ns()
        cfg = self.cfg
        out = []
        br = self.breaker
        if self.dry:
            return out
        br.maybe_probe(t)
        for v in VENUES:
            ms.venue_enabled[v] = not br.disabled[v]

        # ---- runaway guard: the p2 incident (10 Sep) — phantom fills + a flatten every loop ---------
        if inv.n_unattributed > cfg.max_unattributed and not self.killed:
            self.killed, self.kill_reason = True, f"unattributed volume {inv.n_unattributed} > {cfg.max_unattributed}"
        if self.aggr_10s > cfg.max_aggr_shares_10s and not self.killed:
            self.killed, self.kill_reason = True, f"hedge/flatten volume {self.aggr_10s} in 10 s > {cfg.max_aggr_shares_10s}"

        # ---- kill switch -----------------------------------------------------------------
        if self.killed:
            out.extend(self._cancel_all(inv))
            if self._flatten_allowed(inv, t):
                out.extend(self._flatten(ms, inv, MARKET, P_FLATTEN, clamp=False))
                self.last_flatten_t = t
            return out

        # ---- breaker: cancel the other venue once, drop the dead venue, no pairs ------------
        for v in VENUES:
            o = OTHER[v]
            if br.disabled[v]:
                if not self.breaker_cancelled[o]:
                    out.extend(self._cancel_venue(inv, o))
                    self.breaker_cancelled[o] = True
            else:
                self.breaker_cancelled[o] = False

        # ---- end of case -----------------------------------------------------------------
        left = clock.ticks_left(t)
        self.quotes_off = left <= cfg.k_quotes
        self.flatten_phase = left <= cfg.k_flat
        if self.quotes_off:
            out.extend(self._cancel_quotes(inv))
        if self.flatten_phase and inv.pos != 0 and self._flatten_allowed(inv, t):
            out.extend(self._flatten(ms, inv, MARKET, P_FLATTEN, clamp=False))
            self.last_flatten_t = t

        # ---- aggressive LIMIT remainders resting on the book (cross legs that missed) -------------
        if not cfg.keep_remainder:
            delay_ns = cfg.cancel_retry_ms * 1_000_000
            for o in inv.pending_aggr:
                if o.kind == LIMIT and o.state in (ACKED, PARTIAL) and o.remaining > 0 \
                        and t - o.t_ack >= delay_ns and t - o.t_cancel_send >= delay_ns:
                    out.append(Intent(CANCEL, P1, o.venue, o.side, ref=o, purpose=o.purpose))

        # ---- unhedged inventory ------------------------------------------------------------
        # pairs-only: a one-leg residual has no passive hedge coming, take it at the touch at once
        trigger = cfg.hedge_trigger if cfg.enable_quotes else cfg.hedge_trigger_no_quotes
        hold_ns = (cfg.hold_ms if cfg.enable_quotes else cfg.hold_ms_no_quotes) * 1_000_000
        if abs(inv.pos) > trigger and abs(inv.pos) >= cfg.min_order:
            if self.t_unhedged_since == 0:
                self.t_unhedged_since = t
            elif (t - self.t_unhedged_since) > hold_ns and not self.flatten_phase:
                if self._flatten_allowed(inv, t):
                    kind = LIMIT if cfg.enable_quotes else cfg.hedge_kind_no_quotes
                    out.extend(self._flatten(ms, inv, kind, P_HEDGE))
                    self.last_hedge_t = t
        else:
            self.t_unhedged_since = 0

        # ---- per-intent checks -------------------------------------------------------------
        any_dead = br.any_disabled()
        room = inv.headroom()
        for it in intents:
            k = it.kind
            if k == PAIR:
                if any_dead or br.backing_off(it.legs[0][0], t) or br.backing_off(it.legs[1][0], t):
                    self.n_dropped_venue += 1
                    continue
                if self._self_trade(it.legs, inv, out):
                    continue
                buy_leg = it.legs[0] if it.legs[0][1] == BUY else it.legs[1]
                sell_leg = it.legs[1] if buy_leg is it.legs[0] else it.legs[0]
                pair_room = inv.pair_headroom(buy_leg[0], sell_leg[0])
                if it.qty > pair_room:
                    self.n_capped += 1
                    it.qty = pair_room
                if it.qty < cfg.min_order:
                    continue
                out.append(it)
            elif k == QUOTE:
                if self.quotes_off or any_dead or br.backing_off(it.venue, t):
                    self.n_dropped_venue += br.disabled[it.venue]
                    continue
                if it.qty > room:
                    it.qty = room
                if it.qty < cfg.min_order:
                    continue
                out.append(it)
            elif k == TAKE:
                if br.disabled[it.venue] or br.backing_off(it.venue, t):
                    self.n_dropped_venue += 1
                    continue
                if it.px is not None and self._self_trade(((it.venue, it.side, it.px, it.order_kind),), inv, out):
                    continue
                if it.purpose not in (P_FLATTEN, P_HEDGE) and it.qty > room:
                    self.n_capped += 1
                    it.qty = room
                if it.qty < cfg.min_order:
                    continue
                out.append(it)
            elif k == CANCEL:
                out.append(it)
        return out

    # ---------------------------------------------------------------------------------------
    def _flatten_allowed(self, inv, t):
        """One aggressive hedge/flatten in flight at a time, and not within hedge_retry_ms of the last."""
        retry = self.cfg.hedge_retry_ms * 1_000_000
        if t - max(self.last_hedge_t, self.last_flatten_t) < retry:
            return False
        for o in inv.pending_aggr:
            if o.purpose in (P_HEDGE, P_FLATTEN) and o.remaining > 0 and o.state in (ACKED, PARTIAL):
                return False
        return True

    def note_fills(self, fills, t):
        """Engine hook: rolling FILLED hedge/flatten volume for the runaway guard — the real
        spread-crossing impact. A clamped hedge that rests and never fills adds nothing; only
        volume that actually executed against the book counts (test session 10 Sep: a resting
        hedge retried on a wide book inflated sent-volume and false-tripped the guard)."""
        cut = t - 10_000_000_000
        for f in fills:
            if f.purpose in (P_HEDGE, P_FLATTEN):
                self.aggr_log.append((f.t_seen or t, f.qty))
        while self.aggr_log and self.aggr_log[0][0] < cut:
            self.aggr_log.pop(0)
        self.aggr_10s = sum(q for _, q in self.aggr_log)

    def _self_trade(self, legs, inv, out):
        """True (and CANCEL prepended) if any leg would hit our own resting order."""
        hit = False
        for v, side, px, kind in legs:
            for o in inv.own_resting[(v, 1 - side)]:
                if o.state == CANCEL_SENT:
                    continue
                crosses = kind == MARKET or px is None or \
                    (side == BUY and o.px <= px) or (side == SELL and o.px >= px)
                if crosses:
                    out.append(Intent(CANCEL, P1, v, 1 - side, ref=o, purpose=o.purpose))
                    hit = True
        if hit:
            self.n_self_trade += 1
        return hit

    def _cancel_all(self, inv):
        return [Intent(CANCEL, P1, o.venue, o.side, ref=o, purpose=o.purpose)
                for o in inv.all_resting() if o.state != CANCEL_SENT]

    def _cancel_venue(self, inv, v):
        return [Intent(CANCEL, P1, o.venue, o.side, ref=o, purpose=o.purpose)
                for o in inv.all_resting() if o.venue == v and o.state != CANCEL_SENT]

    def _cancel_quotes(self, inv):
        return [Intent(CANCEL, P1, o.venue, o.side, ref=o, purpose=o.purpose)
                for o in inv.all_resting()
                if o.purpose in (P_QUOTE, P_LAYER) and o.state != CANCEL_SENT]

    def _flatten(self, ms, inv, kind, purpose, clamp=True):
        """TAKE(P2) the residual on the venue with the better touch. A LIMIT hedge is clamped to
        fair ∓ max_hedge_ticks: it takes the touch when the touch is within that distance and
        rests there otherwise — never a MARKET order into a 58-tick-wide book (test session 10 Sep)."""
        pos = inv.pos
        if pos == 0 or ms.bbo is None:
            return []
        b = ms.bbo
        cfg = self.cfg
        if pos > 0:
            side = SELL
            cands = [(b.bid[v], v) for v in VENUES if ms.venue_enabled[v] and b.bid[v] > 0]
            if not cands:
                return []
            px, v = max(cands)
            if kind == LIMIT and clamp and ms.fair > 0:
                px = max(px, int(math.floor(ms.fair - cfg.max_hedge_ticks)))
        else:
            side = BUY
            cands = [(b.ask[v], v) for v in VENUES if ms.venue_enabled[v] and b.ask[v] > 0]
            if not cands:
                return []
            px, v = min(cands)
            if kind == LIMIT and clamp and ms.fair > 0:
                px = min(px, int(math.ceil(ms.fair + cfg.max_hedge_ticks)))
        qty = min(abs(pos), cfg.max_order)
        return [Intent(TAKE, P2, v, side, px if kind == LIMIT else None, qty, purpose,
                       order_kind=kind)]
