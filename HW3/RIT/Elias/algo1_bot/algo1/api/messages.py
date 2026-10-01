"""Message builders and parsers — the parse boundary.

Everything past this module is integer ticks. Prices arrive as floats from RIT (`10.05`); we
convert with `int(round(px * scale))` where `scale = 10 ** quoted_decimals`, and convert back to
a decimal string only when building an order path. Fees / rebates stay float ticks.

Order paths are built as `prefix + qty (+ "&price=" + px_str)` from prebuilt per-(venue, side,
kind) prefixes; price strings are memoised so the hot path does no formatting for repeat prices.

Shapes follow the RIT Client REST API 1.0.3 (https://rit.306w.ca/RIT-REST-API/1.0.3/).
"""
from __future__ import annotations

import json

from ..core.params import Sheet
from ..core.types import (A, BBO, BUY, KIND_NAME, LIMIT, M, MARKET, SIDE_NAME, Ladder, SELL,
                          TICKER, VENUES)

_loads = json.loads


# ---- results of an order POST --------------------------------------------------------------
class Ack:
    __slots__ = ("server_id", "filled", "vwap_ticks", "status_str", "qty")

    def __init__(self, server_id, filled, vwap_ticks, status_str, qty):
        self.server_id = server_id
        self.filled = filled
        self.vwap_ticks = vwap_ticks
        self.status_str = status_str
        self.qty = qty


class Reject:
    __slots__ = ("code", "msg", "status")

    def __init__(self, code, msg, status):
        self.code = code
        self.msg = msg
        self.status = status


class RateLimited:
    __slots__ = ("wait_s",)

    def __init__(self, wait_s):
        self.wait_s = wait_s


def as_dict(obj):
    """Slot classes have no __dict__; this gives a printable view (Ack / Reject / RateLimited)."""
    return {k: getattr(obj, k) for k in getattr(obj, "__slots__", ())}


class PathBuilder:
    """Prebuilt order paths. One instance per Config (scale + tickers)."""

    def __init__(self, scale, tickers=TICKER, decimals=None):
        self.scale = scale
        self.decimals = decimals if decimals is not None else len(str(scale)) - 1
        self.tickers = tuple(tickers)
        self._px_cache = {}
        # prefix[(venue, side, kind)]
        self.prefix = {}
        for v in VENUES:
            for s in (BUY, SELL):
                for k in (MARKET, LIMIT):
                    self.prefix[(v, s, k)] = (f"/v1/orders?ticker={self.tickers[v]}&type={KIND_NAME[k]}"
                                              f"&action={SIDE_NAME[s]}&quantity=")

    def px_str(self, px):
        s = self._px_cache.get(px)
        if s is None:
            whole, frac = divmod(px, self.scale)
            s = f"{whole}.{frac:0{self.decimals}d}" if self.decimals else str(whole)
            self._px_cache[px] = s
        return s

    def order_path(self, venue, side, kind, qty, px=None):
        p = self.prefix[(venue, side, kind)] + str(qty)
        if kind == LIMIT:
            p += "&price=" + self.px_str(px)
        return p

    @staticmethod
    def cancel_path(server_id):
        return f"/v1/orders/{server_id}"

    @staticmethod
    def cancel_all_path():
        return "/v1/commands/cancel?all=1"

    def book_path(self, venue, limit=10):
        return f"/v1/securities/book?ticker={self.tickers[venue]}&limit={limit}"

    SECURITIES = "/v1/securities"
    CASE = "/v1/case"
    ORDERS_OPEN = "/v1/orders?status=OPEN"
    ORDERS_DONE = "/v1/orders?status=TRANSACTED"
    LIMITS = "/v1/limits"
    TRADER = "/v1/trader"


def to_ticks(px, scale):
    return int(round(px * scale)) if px is not None else 0


def parse_ack(status, body, scale):
    """POST /v1/orders → Ack | Reject | RateLimited."""
    if status == 200:
        d = _loads(body)
        filled = int(d.get("quantity_filled") or 0)
        vwap = d.get("vwap")
        return Ack(int(d["order_id"]), filled, to_ticks(vwap, scale) if vwap else 0.0,
                   d.get("status", "OPEN"), int(d.get("quantity") or 0))
    if status == 429:
        try:
            d = _loads(body)
            return RateLimited(float(d.get("wait", 0.1)))
        except Exception:
            return RateLimited(0.1)
    try:
        d = _loads(body)
        return Reject(d.get("code", "HTTP"), d.get("message", ""), status)
    except Exception:
        return Reject("HTTP", "", status)


def parse_cancel(status, body):
    """DELETE /v1/orders/{id} → bool success."""
    if status != 200:
        return False
    try:
        return bool(_loads(body).get("success", False))
    except Exception:
        return False


def parse_securities(body, scale, tickers=TICKER):
    """GET /v1/securities → (BBO, pos_total, pos_venue). Positions are per ticker; the case
    aggregates them (VERIFY LIVE: `pos_per_ticker`)."""
    rows = _loads(body)
    bbo = BBO()
    pos_venue = [0, 0]
    for r in rows:
        t = r.get("ticker")
        if t == tickers[M]:
            v = M
        elif t == tickers[A]:
            v = A
        else:
            continue
        bbo.bid[v] = to_ticks(r.get("bid"), scale)
        bbo.ask[v] = to_ticks(r.get("ask"), scale)
        bbo.bid_size[v] = int(r.get("bid_size") or 0)
        bbo.ask_size[v] = int(r.get("ask_size") or 0)
        pos_venue[v] = int(round(r.get("position") or 0))
    return bbo, pos_venue[0] + pos_venue[1], pos_venue


def parse_sheet(sec_body, limits_body=None, tickers=TICKER):
    """Case sheet from GET /v1/securities (+ GET /v1/limits)."""
    rows = _loads(sec_body)
    sh = Sheet()
    fee = [0.0, 0.0]
    reb = [0.0, 0.0]
    delay = [0, 0]
    for r in rows:
        t = r.get("ticker")
        if t == tickers[M]:
            v = M
        elif t == tickers[A]:
            v = A
        else:
            continue
        fee[v] = float(r.get("trading_fee") or 0.0)
        reb[v] = float(r.get("limit_order_rebate") or 0.0)
        delay[v] = int(r.get("execution_delay_ms") or 0)
        sh.quoted_decimals = int(r.get("quoted_decimals") or sh.quoted_decimals)
        sh.max_trade_size = int(r.get("max_trade_size") or sh.max_trade_size)
        sh.min_trade_size = int(r.get("min_trade_size") or sh.min_trade_size)
        sh.api_orders_per_second = int(r.get("api_orders_per_second") or sh.api_orders_per_second)
    sh.fee = tuple(fee)
    sh.rebate = tuple(reb)
    sh.execution_delay_ms = tuple(delay)
    sh.tickers = tuple(tickers)
    if limits_body:
        try:
            lim = _loads(limits_body)
            if lim:
                sh.gross_limit = int(lim[0].get("gross_limit") or sh.gross_limit)
                sh.net_limit = int(lim[0].get("net_limit") or sh.net_limit)
        except Exception:
            pass
    return sh


def parse_trader(body):
    """GET /v1/trader → net liquid value (the server's own P&L in dollars). This is truth; the
    bot's fill-based cash is corrupted by the position-lag phantom fills (10 Sep post-mortem)."""
    try:
        d = _loads(body)
        return float(d.get("nlv", 0.0))
    except Exception:
        return None


def parse_case(body):
    """GET /v1/case → (tick, ticks_per_period, status, period, total_periods)."""
    d = _loads(body)
    return (int(d.get("tick", 0)), int(d.get("ticks_per_period", 300)), d.get("status", "ACTIVE"),
            int(d.get("period", 1)), int(d.get("total_periods", 1)))


def parse_book(body, venue, scale, own_server_ids=(), trader_id=None, t_recv=0, snapshot_id=0):
    """GET /v1/securities/book → Ladder with aggregated levels (px, qty, own_qty).

    RIT returns individual orders (with `trader_id`), so own orders are flagged either by
    server id or by trader id. Quantities are remaining (`quantity - quantity_filled`)."""
    d = _loads(body)
    out = []
    for key, desc in (("bid", True), ("ask", False)):
        agg = {}
        for o in d.get(key, ()):
            px = to_ticks(o.get("price"), scale)
            rem = int((o.get("quantity") or 0) - (o.get("quantity_filled") or 0))
            if rem <= 0:
                continue
            own = (o.get("order_id") in own_server_ids) or (trader_id is not None and o.get("trader_id") == trader_id)
            q, oq = agg.get(px, (0, 0))
            agg[px] = (q + rem, oq + (rem if own else 0))
        levels = [(px, q, oq) for px, (q, oq) in agg.items()]
        levels.sort(key=lambda x: -x[0] if desc else x[0])
        out.append(levels)
    return Ladder(venue, out[0], out[1], t_recv, snapshot_id)


def parse_orders(body, scale):
    """GET /v1/orders → [(server_id, filled, vwap_ticks, status_str)]."""
    rows = _loads(body)
    return [(int(r["order_id"]), int(r.get("quantity_filled") or 0),
             to_ticks(r.get("vwap"), scale) if r.get("vwap") else 0.0, r.get("status", "OPEN"))
            for r in rows]


def parse_tas(body, scale):
    """GET /v1/securities/tas → [(id, tick, px_ticks, qty)] (Friday: lambda and sweep detection)."""
    rows = _loads(body)
    return [(int(r["id"]), int(r.get("tick", 0)), to_ticks(r.get("price"), scale), int(r.get("quantity") or 0))
            for r in rows]
