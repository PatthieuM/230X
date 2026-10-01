"""Mock RIT client: the subset of the Client REST API 1.0.3 that ALGO1 uses, plus control hooks.

Endpoints (all need `X-API-Key` or `?key=`): GET /v1/case, /v1/trader, /v1/limits,
/v1/securities, /v1/securities/book, /v1/orders, /v1/orders/{id}; POST /v1/orders;
DELETE /v1/orders/{id}; POST /v1/commands/cancel. Control: GET /mock/state,
POST /mock/cross, /mock/kill, /mock/reset.

Market model (v0, "synthetic ladders"):
    one latent efficient price (random walk, ticks) + a small mean-reverting offset per venue;
    each venue shows a fixed-width spread around its own mid and `n_levels` synthetic levels per
    side with regenerated quantities whenever the touch moves. Consumed quantity persists until
    the level moves. Every `cross_every_s` (jittered) one venue's ladder is shifted by
    `cross_ticks` for `cross_life_ms` — a crossed market with `cross_qty` at the touch. Hunters
    (competing arbitrageurs) consume the crossed quantity after their latency.
Execution:
    MARKET / marketable LIMIT walk the ladder (synthetic levels, and our own resting orders on
    the other side if `self_trade == "allow"`), price-time priority; the unfilled LIMIT remainder
    rests. Resting orders fill when the venue's synthetic touch trades through their price
    (crude; AI order flow is the Friday upgrade). Own resting orders show in the BBO
    (`own_in_bbo`) as a real exchange would.
Latency and limits:
    `delay_ms[v]` is slept before matching (the ack returns after execution, as RIT does with
    execution_delay_ms — VERIFY LIVE). POST /v1/orders is rate-limited per ticker at
    `orders_per_sec` → 429 with `wait`. A killed venue answers 503 to order/cancel requests
    while /v1/securities keeps showing its (frozen) quotes — case-brief question 5.
Time: tick = elapsed_s × speed, capped at `ticks`; status ACTIVE then STOPPED.

Determinism (common random numbers): the latent price, venue offsets, cross schedule / direction and
hunter timing are indexed by *market step* (step k happens when elapsed >= k·step_ms, however late
the thread wakes), and drawn from `rng_price`; level quantities come from a separate `rng_book`.
So two runs with the same seed see the same latent path and the same crosses at the same steps
regardless of what the bot does — fills only consume displayed liquidity (endogenous), they never
move the latent price. This is what makes paired (CRN) evaluation of quoting changes valid.
"""
from __future__ import annotations

import json
import math
import random
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TICKERS = ("CRZY_M", "CRZY_A")


@dataclass
class MockConfig:
    port: int = 9999
    api_key: str = "mock"
    seed: int = 1
    ticks: int = 300
    speed: float = 1.0                 # ticks per real second
    decimals: int = 2
    mid0: int = 1000                   # ticks ($10.00)
    spread: tuple = (2, 2)             # ticks per venue
    n_levels: int = 5
    level_qty: tuple = (800, 2500)     # uniform range per level
    sigma_step: float = 0.15           # latent price std per step, ticks
    offset_phi: float = 0.9            # per-venue offset AR(1)
    offset_sigma: float = 0.3
    step_ms: int = 10
    delay_ms: tuple = (0, 0)
    orders_per_sec: int = 10
    cross_every_s: float = 4.0
    cross_life_ms: int = 600
    cross_ticks: int = 3
    cross_qty: int = 6000
    hunters: int = 0
    hunter_latency_ms: int = 250
    hunter_size: int = 2000
    self_trade: str = "allow"          # allow | prevent
    own_in_bbo: bool = True
    fee: tuple = (0.0, 0.0)            # $/share
    rebate: tuple = (0.0, 0.0)
    max_trade_size: int = 10000
    gross_limit: int = 25000
    net_limit: int = 25000
    limit_gets: bool = False
    passive_fill: bool = True
    aggregate_positions: bool = False   # report the case-level position on both tickers (VERIFY LIVE shape)
    position_lag_ms: float = 0.0        # /securities shows the position as it was this long ago (RIT: 30–200 ms)
    quiet: bool = True


class _Bucket:
    def __init__(self, rate):
        self.rate = float(rate)
        self.tokens = float(rate)
        self.t = time.monotonic()

    def take(self):
        now = time.monotonic()
        self.tokens = min(self.rate, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        return round((1.0 - self.tokens) / self.rate, 3)


class Venue:
    def __init__(self, idx, cfg, rng):
        self.idx = idx
        self.cfg = cfg
        self.rng = rng              # rng_book: level quantities only
        self.offset = 0.0
        self.shift = 0
        self.bid = 0
        self.ask = 0
        self.taken = {}             # (side, px) -> consumed qty
        self.base_qty = {}          # (side, px) -> level qty
        self.resting = []           # order dicts (our own)
        self.killed = False
        self.cross_qty_at = None    # px at which cross_qty applies (touch during a cross)
        self.bucket = _Bucket(cfg.orders_per_sec)

    def set_touch(self, mid):
        half = self.cfg.spread[self.idx] / 2.0
        m = mid + self.offset + self.shift
        bid = int(math.floor(m - half))
        ask = int(math.ceil(m + half))
        if ask <= bid:
            ask = bid + 1
        if bid != self.bid or ask != self.ask:
            self.bid, self.ask = bid, ask
        # prune stale level bookkeeping outside the window
        n = self.cfg.n_levels
        for k in list(self.taken):
            side, px = k
            if (side == "bid" and (px > self.bid or px < self.bid - n)) or (side == "ask" and (px < self.ask or px > self.ask + n)):
                self.taken.pop(k, None)
                self.base_qty.pop(k, None)

    def level_qty(self, side, px):
        k = (side, px)
        q = self.base_qty.get(k)
        if q is None:
            lo, hi = self.cfg.level_qty
            q = self.rng.randint(lo, hi)
            if self.shift and ((side == "bid" and px == self.bid) or (side == "ask" and px == self.ask)):
                q = self.cfg.cross_qty
            self.base_qty[k] = q
        return max(0, q - self.taken.get(k, 0))

    def synthetic(self, side):
        """[(px, qty)] best first."""
        out = []
        for i in range(self.cfg.n_levels):
            px = self.bid - i if side == "bid" else self.ask + i
            q = self.level_qty(side, px)
            if q > 0:
                out.append((px, q))
        return out

    def consume(self, side, px, qty):
        self.taken[(side, px)] = self.taken.get((side, px), 0) + qty

    def own_levels(self, action):
        agg = {}
        for o in self.resting:
            if o["action"] == action and o["status"] == "OPEN":
                agg[o["price"]] = agg.get(o["price"], 0) + o["quantity"] - o["quantity_filled"]
        return agg

    def display_touch(self):
        """(bid, bid_size, ask, ask_size) including own resting orders if own_in_bbo."""
        sb = self.synthetic("bid")
        sa = self.synthetic("ask")
        bid, bs = (sb[0] if sb else (self.bid, 0))
        ask, az = (sa[0] if sa else (self.ask, 0))
        if self.cfg.own_in_bbo:
            ob = self.own_levels("BUY")
            oa = self.own_levels("SELL")
            if ob:
                best = max(ob)
                if best > bid:
                    bid, bs = best, ob[best]
                elif best == bid:
                    bs += ob[best]
            if oa:
                best = min(oa)
                if best < ask:
                    ask, az = best, oa[best]
                elif best == ask:
                    az += oa[best]
        return bid, bs, ask, az


class MockMarket:
    def __init__(self, cfg: MockConfig):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)                 # rng_price: latent path, offsets, crosses, hunters
        self.rng_book = random.Random(cfg.seed * 1_000_003 + 7)
        self.lock = threading.RLock()
        self.venues = [Venue(0, cfg, self.rng_book), Venue(1, cfg, self.rng_book)]
        self.eff = float(cfg.mid0)
        self.pos = [0, 0]
        self.cash = 0.0
        self.orders = {}            # id -> order dict
        self.next_id = 1
        self.t_start = time.monotonic()
        self.step_i = 0             # market steps taken; all dynamics are indexed by this
        self.step_dt = cfg.step_ms / 1000.0
        self.next_cross_step = self._steps(cfg.cross_every_s * (0.5 + self.rng.random()))
        self.cross = None           # dict(venue, step0, step_end, hunters_done:set)
        self.n_cross = 0
        self.stop = False
        self.thread = None
        self.trader_id = "mock_trader"
        self.pos_log = []           # (t, pos_M, pos_A) after each fill, for position_lag_ms
        self.tas = {0: [], 1: []}   # time & sales per venue: (id, tick, px, qty)
        self.tas_id = 0
        for v in self.venues:
            v.set_touch(self.eff)

    def _steps(self, seconds):
        return max(1, int(round(seconds / self.step_dt)))

    # ---- time ----------------------------------------------------------------------------
    def tick(self):
        return min(self.cfg.ticks, int((time.monotonic() - self.t_start) * self.cfg.speed))

    def status(self):
        return "ACTIVE" if self.tick() < self.cfg.ticks else "STOPPED"

    def reset_clock(self):
        with self.lock:
            self.t_start = time.monotonic()

    # ---- dynamics ------------------------------------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="mock-market")
        self.thread.start()

    def _run(self):
        """Catch-up stepping: step k runs once elapsed >= k·step_dt, so the number of steps by
        wall time t is the same in every run (up to one step), whatever the thread's jitter."""
        dt = self.step_dt
        t0 = time.monotonic()
        while not self.stop:
            time.sleep(dt / 2)
            target = int((time.monotonic() - t0) / dt)
            with self.lock:
                while self.step_i < target:
                    self.step()

    def step(self):
        cfg = self.cfg
        self.step_i += 1
        k = self.step_i
        self.eff += self.rng.gauss(0.0, cfg.sigma_step)
        for v in self.venues:
            v.offset = cfg.offset_phi * v.offset + self.rng.gauss(0.0, cfg.offset_sigma)
        # forced cross lifecycle, indexed by step
        if self.cross is None and k >= self.next_cross_step and self.status() == "ACTIVE":
            self.force_cross()
        if self.cross is not None:
            c = self.cross
            if k >= c["step_end"]:
                self.end_cross()
            else:
                for h in range(cfg.hunters):
                    if h in c["hunters_done"]:
                        continue
                    if k >= c["step0"] + self._steps((cfg.hunter_latency_ms * (1 + 0.5 * h)) / 1000.0):
                        c["hunters_done"].add(h)
                        self._hunter_take(cfg.hunter_size)
        for v in self.venues:
            v.set_touch(self.eff)
        if cfg.passive_fill:
            self._passive_fills()

    def force_cross(self, venue=None, ticks=None, life_ms=None, qty=None, direction=None):
        cfg = self.cfg
        vi = self.rng.randrange(2) if venue is None else venue
        up = (self.rng.random() < 0.5) if direction is None else (direction == "up")
        v = self.venues[vi]
        k = ticks if ticks is not None else cfg.cross_ticks
        v.shift = k if up else -k
        v.base_qty.clear()
        v.taken.clear()
        if qty is not None:
            old = cfg.cross_qty
            cfg.cross_qty = qty
        v.set_touch(self.eff)
        v.synthetic("bid")
        v.synthetic("ask")
        if qty is not None:
            cfg.cross_qty = old
        k = self.step_i
        life = (life_ms if life_ms is not None else cfg.cross_life_ms) / 1000.0
        self.cross = {"venue": vi, "step0": k, "step_end": k + self._steps(life),
                      "hunters_done": set(), "up": up}
        self.n_cross += 1
        self.next_cross_step = k + self._steps(cfg.cross_every_s * (0.5 + self.rng.random()))

    def end_cross(self):
        if self.cross is None:
            return
        v = self.venues[self.cross["venue"]]
        v.shift = 0
        v.base_qty.clear()
        v.taken.clear()
        v.set_touch(self.eff)
        self.cross = None

    def _hunter_take(self, qty):
        """A competitor takes `qty` from both crossed touches."""
        vm, va = self.venues
        for cheap, rich in ((vm, va), (va, vm)):
            sa = cheap.synthetic("ask")
            sb = rich.synthetic("bid")
            if sa and sb and sb[0][0] > sa[0][0]:
                n = min(qty, sa[0][1], sb[0][1])
                cheap.consume("ask", sa[0][0], n)
                rich.consume("bid", sb[0][0], n)

    def _passive_fills(self):
        for v in self.venues:
            sb = v.synthetic("bid")
            sa = v.synthetic("ask")
            for o in v.resting:
                if o["status"] != "OPEN":
                    continue
                rem = o["quantity"] - o["quantity_filled"]
                if o["action"] == "BUY" and sa and sa[0][0] <= o["price"]:
                    self._fill(o, rem, o["price"])
                elif o["action"] == "SELL" and sb and sb[0][0] >= o["price"]:
                    self._fill(o, rem, o["price"])
            v.resting = [o for o in v.resting if o["status"] == "OPEN"]

    # ---- orders --------------------------------------------------------------------------
    def _fill(self, o, qty, px):
        if qty <= 0:
            return
        f = o["quantity_filled"]
        o["vwap"] = (o["vwap"] * f + px * qty) / (f + qty) if f else float(px)
        o["quantity_filled"] = f + qty
        vi = TICKERS.index(o["ticker"])
        sgn = 1 if o["action"] == "BUY" else -1
        self.pos[vi] += sgn * qty
        self.cash -= sgn * qty * px / (10 ** self.cfg.decimals)
        if self.cfg.position_lag_ms:
            self.pos_log.append((time.monotonic(), self.pos[0], self.pos[1]))
        self.tas_id += 1
        self.tas[vi].append((self.tas_id, self.tick(), px, qty))
        if o["quantity_filled"] >= o["quantity"]:
            o["status"] = "TRANSACTED"

    def place(self, ticker, otype, qty, action, price):
        """Match a new order. `price` in ticks (None for MARKET). Returns the order dict."""
        vi = TICKERS.index(ticker)
        v = self.venues[vi]
        with self.lock:
            oid = self.next_id
            self.next_id += 1
            o = {"order_id": oid, "period": 1, "tick": self.tick(), "trader_id": self.trader_id,
                 "ticker": ticker, "type": otype, "quantity": int(qty), "action": action,
                 "price": price, "quantity_filled": 0, "vwap": None, "status": "OPEN"}
            self.orders[oid] = o
            side_book = "ask" if action == "BUY" else "bid"
            levels = [(px, q, None) for px, q in v.synthetic(side_book)]
            if self.cfg.self_trade == "allow":
                opp = "SELL" if action == "BUY" else "BUY"
                for r in v.resting:
                    if r["action"] == opp and r["status"] == "OPEN":
                        levels.append((r["price"], r["quantity"] - r["quantity_filled"], r))
            levels.sort(key=lambda x: (x[0] if action == "BUY" else -x[0], x[2] is not None))
            rem = int(qty)
            for px, q, r in levels:
                if rem <= 0:
                    break
                if otype == "LIMIT" and ((action == "BUY" and px > price) or (action == "SELL" and px < price)):
                    break
                n = min(rem, q)
                if r is None:
                    v.consume(side_book, px, n)
                else:
                    self._fill(r, n, px)
                self._fill(o, n, px)
                rem -= n
            if otype == "LIMIT" and o["status"] == "OPEN":
                v.resting.append(o)
            elif otype == "MARKET" and o["status"] == "OPEN":
                # MARKET remainder with no liquidity: RIT would fill against deeper book; here cancel
                if o["quantity_filled"] == 0:
                    o["status"] = "CANCELLED"
                else:
                    o["status"] = "TRANSACTED"
                    o["quantity"] = o["quantity_filled"]
            v.resting = [x for x in v.resting if x["status"] == "OPEN"]
            return dict(o, price=self._px_out(o["price"]), vwap=self._px_out(o["vwap"]))

    def cancel(self, oid):
        with self.lock:
            o = self.orders.get(oid)
            if o is None or o["status"] != "OPEN":
                return False
            o["status"] = "CANCELLED"
            for v in self.venues:
                v.resting = [x for x in v.resting if x["status"] == "OPEN"]
            return True

    def cancel_all(self, ticker=None, ids=None):
        with self.lock:
            out = []
            for o in list(self.orders.values()):
                if o["status"] != "OPEN":
                    continue
                if ticker and o["ticker"] != ticker:
                    continue
                if ids is not None and o["order_id"] not in ids:
                    continue
                o["status"] = "CANCELLED"
                out.append(o["order_id"])
            for v in self.venues:
                v.resting = [x for x in v.resting if x["status"] == "OPEN"]
            return out

    # ---- views ---------------------------------------------------------------------------
    def _px_out(self, px):
        return None if px is None else round(px / (10 ** self.cfg.decimals), self.cfg.decimals)

    def order_view(self, o):
        return dict(o, price=self._px_out(o["price"]), vwap=self._px_out(o["vwap"]))

    def _shown_pos(self):
        """Positions as the client would show them: lagged by position_lag_ms if configured."""
        if not self.cfg.position_lag_ms:
            return list(self.pos)
        cut = time.monotonic() - self.cfg.position_lag_ms / 1000.0
        shown = [0, 0]
        for t, pm, pa in self.pos_log:
            if t <= cut:
                shown = [pm, pa]
            else:
                break
        return shown

    def securities(self):
        with self.lock:
            out = []
            sp = self._shown_pos()
            for vi, v in enumerate(self.venues):
                bid, bs, ask, az = v.display_touch()
                shown = (sp[0] + sp[1]) if self.cfg.aggregate_positions else sp[vi]
                out.append({
                    "ticker": TICKERS[vi], "type": "STOCK", "size": 1, "position": shown,
                    "vwap": 0, "nlv": 0, "last": self._px_out(bid), "bid": self._px_out(bid),
                    "bid_size": bs, "ask": self._px_out(ask), "ask_size": az, "volume": 0,
                    "unrealized": 0, "realized": 0, "currency": "CAD", "total_volume": 0,
                    "limits": [{"name": "EQUITY", "units": 1}], "interest_rate": 0,
                    "is_tradeable": True, "is_shortable": True, "start_period": 1, "stop_period": 1,
                    "description": "CRZY", "unit_multiplier": 1, "display_unit": "", "start_price": self._px_out(self.cfg.mid0),
                    "min_price": 0, "max_price": 100, "quoted_decimals": self.cfg.decimals,
                    "trading_fee": self.cfg.fee[vi], "limit_order_rebate": self.cfg.rebate[vi],
                    "min_trade_size": 1, "max_trade_size": self.cfg.max_trade_size,
                    "required_tickers": "", "bond_coupon": 0, "interest_payments_per_period": 0,
                    "base_security": "", "fixing_ticker": "", "api_orders_per_second": self.cfg.orders_per_sec,
                    "execution_delay_ms": self.cfg.delay_ms[vi], "interest_rate_ticker": "", "otc_price_range": 0,
                })
            return out

    def book(self, ticker, limit=20):
        vi = TICKERS.index(ticker)
        v = self.venues[vi]
        with self.lock:
            out = {"bid": [], "ask": []}
            for side, key, action in (("bid", "bid", "BUY"), ("ask", "ask", "SELL")):
                rows = []
                for j, (px, q) in enumerate(v.synthetic(side)):
                    rows.append({"order_id": -(vi * 1000 + j + (0 if side == "bid" else 500)), "period": 1, "tick": self.tick(),
                                 "trader_id": "ANON", "ticker": ticker, "type": "LIMIT", "quantity": q,
                                 "action": action, "price": self._px_out(px), "quantity_filled": 0, "vwap": None, "status": "OPEN"})
                for o in v.resting:
                    if o["action"] == action and o["status"] == "OPEN":
                        rows.append(self.order_view(o))
                rows.sort(key=lambda r: -r["price"] if side == "bid" else r["price"])
                out[key] = rows[:limit]
            return out

    def state(self):
        with self.lock:
            return {"tick": self.tick(), "status": self.status(), "eff": self.eff, "step": self.step_i,
                    "seed": self.cfg.seed, "pos": self.pos,
                    "cash": self.cash, "n_cross": self.n_cross, "cross": None if self.cross is None else
                    {"venue": self.cross["venue"], "up": self.cross["up"]},
                    "venues": [{"bid": v.bid, "ask": v.ask, "shift": v.shift, "killed": v.killed,
                                "resting": len(v.resting)} for v in self.venues],
                    "n_orders": len(self.orders)}


# ---- HTTP ----------------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    market: MockMarket = None
    cfg: MockConfig = None

    def log_message(self, fmt, *args):
        if not self.cfg.quiet:
            super().log_message(fmt, *args)

    def _send(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, qs):
        key = self.headers.get("X-API-Key") or (qs.get("key") or [None])[0]
        if key != self.cfg.api_key:
            self._send(401, {"code": "UNAUTHORIZED", "message": "bad key"})
            return False
        return True

    def _route(self, method):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        p = u.path
        m = self.market
        if p.startswith("/mock/"):
            return self._control(p, qs)
        if not self._auth(qs):
            return
        if self.cfg.limit_gets and method == "GET":
            w = m.venues[0].bucket.take()
            if w:
                return self._send(429, {"code": "RATE_LIMIT_EXCEEDED", "wait": w}, {"Retry-After": str(w)})
        try:
            if method == "GET":
                if p == "/v1/case":
                    return self._send(200, {"name": "ALGO1", "period": 1, "tick": m.tick(), "ticks_per_period": self.cfg.ticks,
                                            "total_periods": 1, "status": m.status(), "is_enforce_trading_limits": True})
                if p == "/v1/trader":
                    return self._send(200, {"trader_id": m.trader_id, "first_name": "Mock", "last_name": "Trader", "nlv": m.cash})
                if p == "/v1/limits":
                    g = abs(m.pos[0] + m.pos[1]) if self.cfg.aggregate_positions else abs(m.pos[0]) + abs(m.pos[1])
                    return self._send(200, [{"name": "EQUITY", "gross": g, "net": m.pos[0] + m.pos[1],
                                             "gross_limit": self.cfg.gross_limit, "net_limit": self.cfg.net_limit,
                                             "gross_fine": 0.1, "net_fine": 0.1}])
                if p == "/v1/securities":
                    rows = m.securities()
                    t = (qs.get("ticker") or [None])[0]
                    return self._send(200, [r for r in rows if not t or r["ticker"] == t])
                if p == "/v1/securities/book":
                    t = (qs.get("ticker") or [None])[0]
                    if t not in TICKERS:
                        return self._send(400, {"code": "BAD_TICKER"})
                    return self._send(200, m.book(t, int((qs.get("limit") or ["20"])[0])))
                if p == "/v1/securities/tas":
                    t = (qs.get("ticker") or [None])[0]
                    if t not in TICKERS:
                        return self._send(400, {"code": "BAD_TICKER"})
                    after = int(float((qs.get("after") or ["0"])[0]))
                    with m.lock:
                        rows = [{"id": i, "period": 1, "tick": tk, "price": m._px_out(px), "quantity": q}
                                for i, tk, px, q in m.tas[TICKERS.index(t)] if i > after]
                    return self._send(200, rows)
                if p == "/v1/orders":
                    st = (qs.get("status") or ["OPEN"])[0]
                    with m.lock:
                        return self._send(200, [m.order_view(o) for o in m.orders.values() if o["status"] == st])
                if p.startswith("/v1/orders/"):
                    oid = int(p.rsplit("/", 1)[1])
                    with m.lock:
                        o = m.orders.get(oid)
                    return self._send(200, m.order_view(o)) if o else self._send(404, {"code": "NOT_FOUND"})
            elif method == "POST":
                if p == "/v1/orders":
                    t = (qs.get("ticker") or [None])[0]
                    if t not in TICKERS:
                        return self._send(400, {"code": "BAD_TICKER", "message": "unknown ticker"})
                    vi = TICKERS.index(t)
                    v = m.venues[vi]
                    if v.killed:
                        return self._send(503, {"code": "VENUE_DOWN", "message": "exchange halted"})
                    if m.status() != "ACTIVE":
                        return self._send(400, {"code": "CASE_NOT_ACTIVE", "message": "case stopped"})
                    w = v.bucket.take()
                    if w:
                        return self._send(429, {"code": "RATE_LIMIT_EXCEEDED", "wait": w}, {"Retry-After": str(w)})
                    otype = (qs.get("type") or ["MARKET"])[0]
                    qty = int(float((qs.get("quantity") or ["0"])[0]))
                    action = (qs.get("action") or ["BUY"])[0]
                    if qty <= 0 or qty > self.cfg.max_trade_size or action not in ("BUY", "SELL"):
                        return self._send(400, {"code": "BAD_ORDER", "message": "qty / action"})
                    price = None
                    if otype == "LIMIT":
                        if "price" not in qs:
                            return self._send(400, {"code": "BAD_ORDER", "message": "price required"})
                        price = int(round(float(qs["price"][0]) * 10 ** self.cfg.decimals))
                    if self.cfg.delay_ms[vi]:
                        time.sleep(self.cfg.delay_ms[vi] / 1000.0)
                    o = m.place(t, otype, qty, action, price)
                    return self._send(200, o, {"X-Wait-Until": "0"})
                if p == "/v1/commands/cancel":
                    if (qs.get("all") or ["0"])[0] == "1":
                        return self._send(200, {"cancelled_order_ids": m.cancel_all()})
                    if "ticker" in qs:
                        return self._send(200, {"cancelled_order_ids": m.cancel_all(ticker=qs["ticker"][0])})
                    if "ids" in qs:
                        ids = {int(x) for x in qs["ids"][0].split(",") if x}
                        return self._send(200, {"cancelled_order_ids": m.cancel_all(ids=ids)})
                    return self._send(400, {"code": "BAD_REQUEST"})
            elif method == "DELETE":
                if p.startswith("/v1/orders/"):
                    oid = int(p.rsplit("/", 1)[1])
                    with m.lock:
                        o = m.orders.get(oid)
                    if o is not None and m.venues[TICKERS.index(o["ticker"])].killed:
                        return self._send(503, {"code": "VENUE_DOWN"})
                    return self._send(200, {"success": m.cancel(oid)})
            return self._send(404, {"code": "NOT_FOUND", "message": p})
        except Exception as e:  # pragma: no cover
            return self._send(500, {"code": "MOCK_ERROR", "message": repr(e)})

    def _control(self, p, qs):
        m = self.market
        if p == "/mock/state":
            return self._send(200, m.state())
        if p == "/mock/cross":
            with m.lock:
                m.end_cross()
                m.force_cross(venue=int(qs["venue"][0]) if "venue" in qs else None,
                              ticks=int(qs["ticks"][0]) if "ticks" in qs else None,
                              life_ms=int(qs["life_ms"][0]) if "life_ms" in qs else None,
                              qty=int(qs["qty"][0]) if "qty" in qs else None,
                              direction=(qs.get("dir") or [None])[0])
            return self._send(200, m.state())
        if p == "/mock/kill":
            vi = int(qs.get("venue", ["1"])[0])
            m.venues[vi].killed = (qs.get("on") or ["1"])[0] == "1"
            return self._send(200, m.state())
        if p == "/mock/reset":
            with m.lock:
                m.cancel_all()
                m.pos = [0, 0]
                m.cash = 0.0
                m.reset_clock()
            return self._send(200, m.state())
        return self._send(404, {"code": "NOT_FOUND"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


class MockServer:
    """Threaded server; `start()` returns immediately (tests), `serve_forever()` blocks (CLI)."""

    def __init__(self, cfg: MockConfig):
        self.cfg = cfg
        self.market = MockMarket(cfg)
        handler = type("Handler", (_Handler,), {"market": self.market, "cfg": cfg})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", cfg.port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = None

    def start(self):
        self.market.start()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="mock-http")
        self.thread.start()
        return self

    def serve_forever(self):
        self.market.start()
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        self.market.stop = True
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
