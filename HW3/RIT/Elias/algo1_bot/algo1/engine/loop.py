"""The engine loop with timing stages.

    t0 ─ poll GET /v1/securities ──(ts, tr)── parse ─t1─ fills + state ─t2─ alphas ─t3─
    risk ─t4─ plan ─t5─ dispatch + inventory ─t6─ ring write

Every iteration writes one loop record into the shared-memory ring (one memcpy), plus one order
record per order touched and one fill record per fill. Nothing else is logged in the loop.

Low-cadence side polls (each every N loops, on the poll lane):
    /v1/case                    tick + status → Clock; stop when not ACTIVE
    /v1/securities/book ×2      ladders → MarketState (cross depth, fair v1, sigma)
    /v1/orders?status=…         correct provisional fill attribution (attribution only)
    runs/KILL                   kill switch (the only file I/O)

Stages, all client-observed (the server only exposes tick-resolution time):
    poll RTT = tr - ts (the floor) · parse = t1 - tr · state = t2 - t1 · alphas = t3 - t2
    risk = t4 - t3 · plan = t5 - t4 · send→ack per leg = o.t_ack - o.t_send
    leg gap = |t_send_1 - t_send_2| · decision→ack for a pair = max(t_ack) - t0 of the
    motivating snapshot  ← the number to compare against the cross lifetime.
"""
from __future__ import annotations

import json
import os
import time
from time import perf_counter_ns

from ..alpha import cross, dislocation, layers, quotes, stale
from ..api.budget import Budget
from ..api.client import Lanes
from ..api.messages import (PathBuilder, parse_book, parse_case, parse_orders, parse_securities,
                            parse_sheet, parse_trader)
from ..core.clock import Clock
from ..core.params import Config
from ..core.types import (A, CANCELLED, FILLED, M, MARKET, P_FLATTEN, SIGN, Inventory, Snapshot, VENUES)
from ..execution.dispatcher import Dispatcher
from ..execution.reconciler import Reconciler
from ..execution.risk import Breaker, Risk
from ..market.state import MarketState
from ..monitor.ringbuf import RingSet

_loads = json.loads


class Engine:
    def __init__(self, cfg: Config, run_dir=None, shared=True, max_loops=0, max_seconds=0.0):
        self.cfg = cfg
        self.run_dir = run_dir or os.path.join("runs", cfg.run_name)
        self.max_loops = max_loops
        self.max_seconds = max_seconds
        self.lanes = Lanes(cfg.host, cfg.port, cfg.api_key, cfg.timeout_s)
        self.pb = None
        self.sheet = None
        self.trader_id = None
        self.clock = Clock()
        self.inv = None
        self.ms = None
        self.budget = None
        self.breaker = None
        self.risk = None
        self.reconciler = None
        self.dispatcher = None
        self.ring = None
        self.shared = shared
        self.alphas = []
        self.i = 0
        self.n429_poll = 0
        self.stopped_reason = ""
        self.stats = {"pairs": 0, "pairs_both_filled": 0, "fills": 0, "pnl_edge": 0.0}
        self.nlv = 0.0          # server truth: net liquid value ($), polled from /v1/trader
        self.nlv_peak = 0.0
        self.nlv_min = 0.0
        self.own_ids = set()

    # ---------------------------------------------------------------------------------------
    def init(self):
        cfg = self.cfg
        self.lanes.connect_all()
        st, body, _, _ = self.lanes.poll.get("/v1/securities")
        if st != 200:
            raise RuntimeError(f"GET /v1/securities failed: {st} {body[:200]!r}")
        stl, lim, _, _ = self.lanes.poll.get("/v1/limits")
        self.sheet = parse_sheet(body, lim if stl == 200 else None, cfg.tickers)
        cfg.apply_sheet(self.sheet)
        cfg.apply_params(cfg.params_path)
        stt, tb, _, _ = self.lanes.poll.get("/v1/trader")
        if stt == 200:
            try:
                self.trader_id = _loads(tb).get("trader_id")
            except Exception:
                pass
        self.pb = PathBuilder(cfg.scale, cfg.tickers, self.sheet.quoted_decimals)
        self.inv = Inventory(cfg.gross_limit, cfg.net_limit, cfg.slack, cfg.pos_per_ticker,
                             cfg.gross_both_legs, cfg.pos_lag_window_ms)
        self.ms = MarketState(cfg)
        self.budget = Budget(cfg.orders_per_second, None, cfg.p0_reserve, cfg.budget_per_ticker)
        self.breaker = Breaker(cfg.breaker_n, cfg.breaker_cooldown_s, cfg.breaker_5xx_hard, cfg.backoff_5xx_ms)
        self.risk = Risk(cfg, self.breaker)
        self.reconciler = Reconciler(cfg)
        self.dispatcher = Dispatcher(cfg, self.lanes, self.pb, self.budget, self.breaker)
        os.makedirs(self.run_dir, exist_ok=True)
        self.ring = RingSet(cfg.run_name, cfg.ring_loops, cfg.ring_orders, cfg.ring_fills,
                            shared=self.shared, create=True)
        self.alphas = [cross.evaluate]
        if cfg.enable_quotes:
            self.alphas.append(quotes.evaluate)
        if cfg.enable_layers:
            self.alphas.append(layers.evaluate)
        if cfg.enable_dislocation:
            self.alphas.append(dislocation.evaluate)
        if cfg.ec_coef > 0:
            self.alphas.append(stale.evaluate)
        self._poll_case()
        # seed the position from the server (a restarted bot must not think it is flat)
        bbo, pos, pv = parse_securities(body, cfg.scale, cfg.tickers)
        if not cfg.pos_per_ticker:
            pos, pv = pv[0], [pv[0], 0]          # per-venue split unknown at start: assume all on M
        self.inv.pos = pos
        self.inv.pos_venue = list(pv)
        return self

    # ---------------------------------------------------------------------------------------
    def _poll_case(self):
        st, body, ts, tr = self.lanes.poll.get("/v1/case")
        if st == 200:
            tick, tpp, status, period, tot = parse_case(body)
            self.clock.set_case(tick, tpp, status, tr, period, tot)
        return st

    def _poll_books(self):
        for v in VENUES:
            if not self.ms.venue_enabled[v]:
                continue
            st, body, ts, tr = self.lanes.poll.get(self.pb.book_path(v, self.cfg.book_limit))
            if st == 200:
                self.ms.set_ladder(parse_book(body, v, self.cfg.scale, self.inv.by_server.keys(),
                                              self.trader_id, tr, self.i))

    def _poll_orders(self):
        changed = []
        for path in (PathBuilder.ORDERS_DONE, PathBuilder.ORDERS_OPEN):
            st, body, ts, tr = self.lanes.poll.get(path)
            if st == 200:
                changed.extend(self.inv.apply_sweep(parse_orders(body, self.cfg.scale), tr))
        return changed

    def _check_kill(self):
        if os.path.exists(self.cfg.kill_file):
            self.risk.killed = True

    # ---------------------------------------------------------------------------------------
    def step(self):
        """One loop iteration. Returns False when the engine should stop."""
        cfg = self.cfg
        inv, ms, ring = self.inv, self.ms, self.ring
        i = self.i
        t0 = perf_counter_ns()
        status, body, ts, tr = self.lanes.poll.get(PathBuilder.SECURITIES)
        if status != 200:
            if status == 429:
                self.n429_poll += 1
                try:
                    self.budget.penalize(float(_loads(body).get("wait", 0.1)), tr)
                except Exception:
                    self.budget.penalize(0.1, tr)
            else:
                # transport failure on the poll lane: both venues look dead; count on both
                self.breaker.record(M, False, tr)
                self.breaker.record(A, False, tr)
                time.sleep(0.005)          # not the hot path: the server is unreachable
            self.i += 1
            if self.max_loops and self.i >= self.max_loops:
                self.stopped_reason = 'max_loops'
                return False
            return True
        bbo, pos_server, pos_venue = parse_securities(body, cfg.scale, cfg.tickers)
        if not cfg.pos_per_ticker:
            # both tickers carry the same aggregated case position: use one, not the sum
            pos_server, pos_venue = pos_venue[0], None
        snap = Snapshot(i, ts, tr, bbo, pos_server, pos_venue)
        t1 = perf_counter_ns()
        fills = inv.detect_fills(pos_server, pos_venue, tr, ms.fair)
        ms.update(snap, fills, inv)
        ms.ticks_left = self.clock.ticks_left(tr)
        t2 = perf_counter_ns()
        intents = []
        for a in self.alphas:
            intents += a(ms, inv, cfg)
        t3 = perf_counter_ns()
        intents = self.risk.gate(intents, ms, inv, self.clock, t3)
        t4 = perf_counter_ns()
        msgs = self.reconciler.plan(intents, inv, self.budget, i, ms.fair, t4)
        t5 = perf_counter_ns()
        orders = self.dispatcher.send(msgs, ms) if msgs else ()
        if orders:
            fills = fills + inv.apply_orders(orders, cfg.keep_remainder, ms.fair, perf_counter_ns())
        self.risk.note_fills(fills, perf_counter_ns())
        t6 = perf_counter_ns()
        fired = 0
        for it in intents:
            fired |= 1 << it.kind
        ring.write_loop((i, t0, ts, tr, t1, t2, t3, t4, t5, t6, len(intents), len(msgs),
                         sum(self.budget.deny), self.budget.n429, inv.pos, ms.fair, ms.s, fired,
                         int(self.clock.tick_est(t6)), min(self.budget.tokens),
                         bbo.bid[M], bbo.ask[M], bbo.bid[A], bbo.ask[A],
                         ms.sigma_hat[M], ms.sigma_hat[A], ms.d_raw[M], max(ms.d_bid[M], ms.d_ask[M]), ms.d_bind[M],
                         ms.sigma_lag[M], ms.sigma_lag[A]))
        for o in orders:
            ring.write_order(o)
        for f in fills:
            f.edge = SIGN[f.side] * (f.fair_at_fill - f.px) - cfg.fee[f.venue] + (cfg.rebate[f.venue] if f.passive else 0.0)
            ring.write_fill(f)
            self.stats["fills"] += 1
            self.stats["pnl_edge"] += f.edge * f.qty
        # pair bookkeeping for the summary
        for m in msgs:
            if m.kind == 2:
                self.stats["pairs"] += 1
                if all(o.state == FILLED for o in m.orders):
                    self.stats["pairs_both_filled"] += 1
        # ---- low-cadence side polls -------------------------------------------------------
        if i % cfg.poll_case_every == 0:
            self._poll_case()
            stt, tb, _, _ = self.lanes.poll.get(PathBuilder.TRADER)
            if stt == 200:
                v = parse_trader(tb)
                if v is not None:
                    self.nlv = v
                    self.nlv_peak = max(self.nlv_peak, v)
                    self.nlv_min = min(self.nlv_min, v)
            if not self.clock.active:
                self.stopped_reason = "case " + self.clock.status
                return False
        if i % cfg.poll_book_every == 0:
            self._poll_books()
        if i % cfg.poll_orders_every == 0:
            for o in self._poll_orders():
                ring.write_order(o)
        if i % cfg.kill_check_every == 0:
            self._check_kill()
            if self.risk.killed and inv.pos == 0 and not inv.all_resting():
                self.stopped_reason = "kill switch: " + (self.risk.kill_reason or "runs/KILL")
                return False
        self.i += 1
        if self.max_loops and self.i >= self.max_loops:
            self.stopped_reason = "max_loops"
            return False
        if self.max_seconds and (t6 - self.clock.t_start) * 1e-9 >= self.max_seconds:
            self.stopped_reason = "max_seconds"
            return False
        return True

    # ---------------------------------------------------------------------------------------
    def run(self):
        try:
            while self.step():
                pass
        except KeyboardInterrupt:
            self.stopped_reason = "interrupt"
        finally:
            self.shutdown()
        return self

    def shutdown(self):
        """Cancel everything, flatten if the case is still live, dump the rings."""
        try:
            if not self.risk.dry:
                self.dispatcher.cancel_all()
            for o in self.inv.all_resting():
                o.state = CANCELLED
            self.inv.own_resting = {k: [] for k in self.inv.own_resting}
            self._poll_case()
            if self.clock.active and self.inv.pos != 0 and self.ms.bbo is not None and not self.risk.dry:
                for it in self.risk._flatten(self.ms, self.inv, MARKET, P_FLATTEN):
                    msgs = self.reconciler.plan([it], self.inv, self.budget, self.i, self.ms.fair)
                    self.inv.apply_orders(self.dispatcher.send(msgs, self.ms), False, self.ms.fair)
        except Exception:
            pass
        try:
            self.dump()
        finally:
            self.dispatcher.close()
            self.lanes.close_all()
            self.ring.close(unlink=True)

    def dump(self):
        os.makedirs(self.run_dir, exist_ok=True)
        self.ring.dump(self.run_dir)
        summ = self.summary()
        with open(os.path.join(self.run_dir, "summary.json"), "w") as f:
            json.dump(summ, f, indent=2, default=str)
        return summ

    def summary(self):
        b = self.budget
        return {
            "run": self.cfg.run_name, "seed": self.cfg.seed, "loops": self.i, "stopped": self.stopped_reason,
            "pos": self.inv.pos, "pos_venue": list(self.inv.pos_venue),
            "pairs_sent": self.stats["pairs"], "pairs_both_filled": self.stats["pairs_both_filled"],
            "fills": self.stats["fills"], "nlv": round(self.nlv, 2), "nlv_peak": round(self.nlv_peak, 2),
            "nlv_min": round(self.nlv_min, 2), "pnl_edge_ticks_UNRELIABLE": round(self.stats["pnl_edge"], 1),
            "orders_sent": self.dispatcher.n_sent, "cancels": self.dispatcher.n_cancel,
            "n429": b.n429, "deny": list(b.deny), "poll_429": self.n429_poll,
            "breaker_trips": self.breaker.n_trips, "n_5xx": self.breaker.n_5xx, "self_trade_blocks": self.risk.n_self_trade,
            "unattributed": self.inv.n_unattributed, "lag_ignored": self.inv.n_lag_ignored,
            "kill_reason": self.risk.kill_reason, "pair_denied_budget": self.reconciler.n_pair_denied,
            "sheet": self.sheet.__dict__ if self.sheet else None,
            "lanes": {l.name: {"req": l.n_req, "fail": l.n_fail, "reconnect": l.n_reconnect} for l in self.lanes.all()},
        }
