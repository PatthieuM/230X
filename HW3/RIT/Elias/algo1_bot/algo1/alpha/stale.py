"""stale.evaluate — situation 4: one venue moves, the other lags → TAKE on the laggard.

Enabled only when `cfg.ec_coef > 0`, i.e. the error-correction regression in `sim/analysis.py`
    Δmid_follower ~ ec_coef * (mid_leader - mid_follower)
found that the follower actually closes the gap (otherwise "stale" is just noise and taking it
is paying the spread for nothing).

v0 rule: if the leader's mid jumped by >= stale_J ticks this loop and the follower's did not
move, buy the follower at its ask (leader went up) or sell at its bid (leader went down), size
`stale_size`, expected edge = ec_coef * gap - fee. The position is then a directional bet that
is hedged by the quotes alpha or by the risk flatten; this is deliberately the least-trusted
alpha and stays off by default.
"""
from __future__ import annotations

from ..core.types import BUY, Intent, LIMIT, OTHER, P2, P_STALE, SELL, TAKE


def evaluate(ms, inv, cfg):
    intents = []
    if cfg.ec_coef <= 0 or ms.bbo is None:
        return intents
    lead = cfg.stale_leader
    fol = OTHER[lead]
    if not (ms.venue_enabled[lead] and ms.venue_enabled[fol]):
        return intents
    d_lead = ms.dmid[lead]
    if abs(d_lead) < cfg.stale_J or abs(ms.dmid[fol]) >= 1:
        return intents
    gap = ms.mid[lead] - ms.mid[fol]
    b = ms.bbo
    q = min(cfg.stale_size, inv.headroom(), b.ask_size[fol] if d_lead > 0 else b.bid_size[fol])
    if q < cfg.min_order:
        return intents
    if d_lead > 0:
        px = b.ask[fol]
        edge = cfg.ec_coef * gap - (px - ms.mid[fol]) - cfg.fee[fol]
        side = BUY
    else:
        px = b.bid[fol]
        edge = cfg.ec_coef * (-gap) - (ms.mid[fol] - px) - cfg.fee[fol]
        side = SELL
    if edge <= 0:
        return intents
    intents.append(Intent(TAKE, P2, fol, side, px, q, P_STALE, expected_edge=edge, order_kind=LIMIT))
    return intents
