"""quotes.evaluate — situations 1–3: quote both venues, passive hedge, queue hysteresis.

Inventory is a *pricing* input, not only a permission: quotes sit around a reservation price
    r    = fair + skew,   skew = -c · pos,   c = skew_at_limit / max_abs_pos   (ticks per share)
so a long book shades both quotes down, the reducing side is priced to be lifted and the adding
side is priced out gradually, well before the hard cap in `risk.gate` binds. This is the linear
Avellaneda–Stoikov skew (the O(1) approximation of the Guéant–Lehalle–Fernandez-Tapia optimum,
which is convex in inventory); `skew_curve > 1` adds curvature at the extremes. Calibrate by the
skew wanted at the cap (`skew_at_limit`, ticks), not by a risk-aversion γ chosen by feel.

Half-distance (per side)
    d    = max(c_min, min(d_fixed | d_star(sigma_hat, lambda_hat, r, adverse_a) | 1/k_fill, d_cap))
           + tilt_k·|micro − mid| on the exposed side
           c_min = ceil(s_tight/2 + f - r + 1): never quote inside what the tighter venue's
           spread plus costs would make a losing round trip.
           With a fitted fill-intensity decay k (per tick, `analyze --fills`), the GLFT width term
           (1/γ)·ln(1+γ/k) ≈ 1/k dominates in this regime; `use_k_width` selects it.
    tb   = min(floor(r - d), band(v).bid_max, ask_v - 1)
    ta   = max(ceil (r + d), band(v).ask_min, bid_v + 1)
`skew_through_fair=False` (default) clamps the reducing quote at fair: an ask below fair is
liquidation pressure paid for with adverse selection, outside the model's domain; it is a
deliberate mode, not the optimiser's answer.

Time to close enters only through a mode switch (GLFT: quotes are almost independent of t away
from T). `ms.ticks_left <= k_unwind` and pos != 0 → **unwind mode**: quote the reducing side only,
joining the touch, and cancel the adding side. Then `risk.gate` takes over: quotes off at k_quotes,
aggressive flatten at k_flat. VERIFY LIVE: how RIT marks residual inventory at the close.

Per (venue, side), at most one resting order at level 0. Decision table:
    side blocked (no room, unwind)        → CANCEL(P1) if resting; otherwise the size shrinks to the room
    nothing resting                       → QUOTE(P3, target)
    resting outside band(v)               → CANCEL(P1) + QUOTE(P3, target)   (band-forced)
    |resting.px - target| <= tol and
        edge(resting.px) >= min_edge      → keep (queue priority is worth more than a tick)
    else                                  → CANCEL(P3) + QUOTE(P3, target)
"""
from __future__ import annotations

import math

from ..core.types import BUY, CANCEL, Intent, P1, P3, P_QUOTE, QUOTE, SELL, VENUES
from ..market.fair import d_star


def edge_of(side, px, fair, f, r):
    return (fair - px if side == BUY else px - fair) - f + r


def half_distance(ms, cfg, v):
    """(d_bid, d_ask) in ticks for venue v, and diagnostics into ms (logged per loop).

    d_raw is the model width (d_star with the adverse term, or 1/k, or the fixed d); the final
    scalar is max(c_min, min(d_raw, d_cap)); `d_bind` records which term bound. The exposed side —
    the one the book leans against (micro > mid: the ask is about to be lifted) — is widened by
    tilt_k·|micro − mid| (Cartea–Jaimungal–Ricci: post deeper on the side more likely to be picked
    off). tilt_k = 0 keeps the width symmetric."""
    f = cfg.fee[v]
    r = cfg.rebate[v]
    c_min = cfg.c_min if cfg.c_min > 0 else math.ceil(ms.s_tight / 2 + f - r + 1)
    if cfg.use_k_width and cfg.k_fill > 0:
        d_raw = 1.0 / cfg.k_fill
        model = True
    elif cfg.use_d_star:
        d_raw = d_star(ms.sigma_hat[v], ms.lambda_hat[v], r, cfg.adverse_a)
        model = True
    else:
        d_raw = cfg.d
        model = False
    if d_raw < c_min:
        d, bind = c_min, 1
    elif d_raw > cfg.d_cap:
        d, bind = cfg.d_cap, 2
    else:
        d, bind = d_raw, (3 if model else 0)
    tilt = cfg.tilt_k * (ms.micro[v] - ms.mid[v]) if cfg.tilt_k else 0.0
    d_bid = d + max(0.0, -tilt)          # book leans on the bid side (micro < mid): our bid is exposed
    d_ask = d + max(0.0, tilt)
    ms.d_bid[v], ms.d_ask[v], ms.d_raw[v], ms.d_bind[v] = d_bid, d_ask, d_raw, bind
    return d_bid, d_ask


def skew_ticks(pos, cfg):
    """Reservation-price shift in ticks: -c·pos, optional curvature, capped at ±skew_at_limit."""
    if cfg.max_abs_pos <= 0 or cfg.skew_at_limit <= 0:
        return 0.0
    x = pos / cfg.max_abs_pos
    x = max(-1.0, min(1.0, x))
    mag = abs(x) ** cfg.skew_curve if cfg.skew_curve != 1.0 else abs(x)
    return -math.copysign(mag * cfg.skew_at_limit, x)


def quote_qty(side, d_side, ms, inv, cfg, v, n_venues):
    """Size of one quote. Model: the certainty equivalent of acquiring X at distance d under the
    quadratic penalty whose FOC is the linear skew is dX − cX²/2, so the same-side aggregate the
    book will bear is d/c; split across venues that may fill together, q* = d/(c(1+ρ)); adverse
    selection reduces the effective edge, q* = (d − a·σ)/(c(1+ρ)). Then the side-aware room cap:
    an adding order may not take the position past max_abs_pos even if every quoting venue fills
    (room / n_venues); a reducing order keeps its size; and never past headroom or max_order.
    Shrinks instead of cancelling (the old rule blocked a side with 400 shares of room)."""
    if cfg.quote_size_mode == "model" and cfg.skew_at_limit > 0 and cfg.max_abs_pos > 0:
        c = cfg.skew_at_limit / cfg.max_abs_pos
        eff = d_side - cfg.adverse_a * ms.sigma_hat[v]
        q = int(round(eff / (c * (1.0 + cfg.rho_venues)))) if eff > 0 else 0
    else:
        q = cfg.quote_size
    pos = inv.pos
    room = (cfg.max_abs_pos - pos) if side == BUY else (cfg.max_abs_pos + pos)
    reducing = (side == SELL and pos > 0) or (side == BUY and pos < 0)
    if not reducing:
        q = min(q, room // max(1, n_venues))
    q = min(q, inv.headroom(), cfg.max_order)
    return q if q >= cfg.min_order else 0


def evaluate(ms, inv, cfg):
    intents = []
    b = ms.bbo
    if b is None or ms.fair <= 0:
        return intents
    fair = ms.fair
    pos = inv.pos
    n_venues = sum(1 for v in VENUES if ms.venue_enabled[v] and b.bid[v] > 0 and b.ask[v] > 0)
    unwind = (ms.ticks_left is not None and ms.ticks_left <= cfg.k_unwind and pos != 0)
    reducing = SELL if pos > 0 else BUY
    skew = skew_ticks(pos, cfg)
    r_px = fair + skew
    for v in VENUES:
        if not ms.venue_enabled[v] or b.bid[v] <= 0 or b.ask[v] <= 0:
            continue
        d_bid, d_ask = half_distance(ms, cfg, v)
        bid_max, ask_min = ms.band(v)
        tb = min(math.floor(r_px - d_bid), bid_max, b.ask[v] - 1)
        ta = max(math.ceil(r_px + d_ask), ask_min, b.bid[v] + 1)
        if not cfg.skew_through_fair:
            # the reducing quote never crosses fair
            if pos > 0:
                ta = max(ta, math.ceil(fair))
            elif pos < 0:
                tb = min(tb, math.floor(fair))
        if unwind:
            # reducing side joins the touch (band-clamped); the adding side is blocked below
            if reducing == SELL:
                ta = max(b.ask[v], ask_min, b.bid[v] + 1)
            else:
                tb = min(b.bid[v], bid_max, b.ask[v] - 1)
        f = cfg.fee[v]
        r = cfg.rebate[v]
        for side, target, d_side in ((BUY, tb, d_bid), (SELL, ta, d_ask)):
            resting = inv.own_resting[(v, side)]
            resting0 = resting[0] if resting else None
            qty = quote_qty(side, d_side, ms, inv, cfg, v, n_venues)
            blocked = qty == 0 or (unwind and side != reducing)
            if blocked:
                for o in resting:
                    intents.append(Intent(CANCEL, P1, v, side, ref=o, purpose=P_QUOTE))
                continue
            if target <= 0:
                continue
            if unwind:
                qty = max(cfg.min_order, min(qty, abs(pos)))
            if resting0 is None:
                intents.append(Intent(QUOTE, P3, v, side, target, qty, P_QUOTE,
                                      expected_edge=edge_of(side, target, fair, f, r)))
                continue
            px = resting0.px
            outside = (side == BUY and px > bid_max) or (side == SELL and px < ask_min)
            if outside:
                intents.append(Intent(CANCEL, P1, v, side, ref=resting0, purpose=P_QUOTE))
                intents.append(Intent(QUOTE, P3, v, side, target, qty, P_QUOTE,
                                      expected_edge=edge_of(side, target, fair, f, r)))
            elif abs(px - target) <= cfg.tol and edge_of(side, px, fair, f, r) >= cfg.min_edge_quote \
                    and not unwind:
                pass                                   # keep: queue priority
            elif unwind and px == target:
                pass
            else:
                intents.append(Intent(CANCEL, P3, v, side, ref=resting0, purpose=P_QUOTE))
                intents.append(Intent(QUOTE, P3, v, side, target, qty, P_QUOTE,
                                      expected_edge=edge_of(side, target, fair, f, r)))
            for o in resting[1:]:
                intents.append(Intent(CANCEL, P3, v, side, ref=o, purpose=P_QUOTE))
    return intents
