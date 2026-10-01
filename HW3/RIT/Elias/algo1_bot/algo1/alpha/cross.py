"""cross.evaluate — situation: crossed books (incl. deeper than L1) → PAIR intents.

Edge on the netted BBO (the book minus our own orders):
    e1 = bid_A - ask_M - fee2      buy on M, sell on A
    e2 = bid_M - ask_A - fee2      buy on A, sell on M

If a fresh ladder exists for both venues, `overlap()` merge-walks the rich venue's bids against
the cheap venue's asks and returns the full crossable quantity, the average edge and protective
limit prices at the last crossed level. Otherwise quantity is the L1 min and the limits are the
observed touch. Quantity is capped by the pair headroom and cut into `max_order` clips.

Each PAIR carries two legs `(venue, side, px, kind)`; the dispatcher sends them concurrently on
ord1/ord2. LIMIT legs at the crossed price are protective: if the cross is gone when the order
arrives, the leg rests instead of trading through — the dispatcher then cancels the remainder
(or keeps it inside the band, cfg.keep_remainder).
"""
from __future__ import annotations

from ..core.types import A, BUY, Intent, LIMIT, M, P0, P_CROSS, PAIR, SELL


def overlap(bids, asks, fee2):
    """Merge-walk two ladders. bids: rich venue, desc, (px, qty, own); asks: cheap venue, asc.
    Own quantity is excluded (we never trade against ourselves here).
    Returns (q, avg_edge, px_buy_limit, px_sell_limit); q = 0 if nothing crosses."""
    i = j = 0
    q = 0
    pnl = 0.0
    bi = [b[1] - b[2] for b in bids]
    aj = [a[1] - a[2] for a in asks]
    last_i = last_j = -1
    while i < len(bids) and j < len(asks) and bids[i][0] - asks[j][0] > fee2:
        if bi[i] <= 0:
            i += 1
            continue
        if aj[j] <= 0:
            j += 1
            continue
        n = min(bi[i], aj[j])
        q += n
        pnl += n * (bids[i][0] - asks[j][0] - fee2)
        bi[i] -= n
        aj[j] -= n
        last_i, last_j = i, j
        if bi[i] == 0:
            i += 1
        if aj[j] == 0:
            j += 1
    if q == 0:
        return 0, 0.0, 0, 0
    return q, pnl / q, asks[last_j][0], bids[last_i][0]


def clips(q, max_order, min_order=1):
    out = []
    while q >= min_order:
        c = min(q, max_order)
        out.append(c)
        q -= c
    return out


def evaluate(ms, inv, cfg):
    intents = []
    b = ms.bbo
    if b is None or not (ms.venue_enabled[M] and ms.venue_enabled[A]):
        return intents
    f2 = cfg.fee2
    e1 = b.bid[A] - b.ask[M] - f2       # buy M, sell A
    e2 = b.bid[M] - b.ask[A] - f2       # buy A, sell M
    if e1 <= cfg.min_edge_cross and e2 <= cfg.min_edge_cross:
        return intents
    if e1 > e2:
        cheap, rich, edge = M, A, e1
    else:
        cheap, rich, edge = A, M, e2
    if b.ask[cheap] <= 0 or b.bid[rich] <= 0:
        return intents
    use_ladder = ms.ladder_fresh(cheap) and ms.ladder_fresh(rich)
    if use_ladder:
        q, edge_avg, px_buy, px_sell = overlap(ms.ladders[rich].bids, ms.ladders[cheap].asks, f2)
        if q == 0:
            # ladder is stale relative to the BBO (the BBO is newer): fall back to L1
            use_ladder = False
    if not use_ladder:
        q = min(b.ask_size[cheap], b.bid_size[rich])
        edge_avg = edge
        px_buy, px_sell = b.ask[cheap], b.bid[rich]
    q = min(q, inv.pair_headroom(cheap, rich))
    if q < cfg.min_order:
        return intents
    kind = cfg.leg_kind
    # scarce leg first: the side with less displayed size is the one other bots will take first;
    # with confirm_then_hedge the deep leg is then sized to what the scarce leg actually filled
    buy_leg, sell_leg = (cheap, BUY, px_buy, kind), (rich, SELL, px_sell, kind)
    legs = (sell_leg, buy_leg) if b.bid_size[rich] < b.ask_size[cheap] else (buy_leg, sell_leg)
    for c in clips(q, cfg.max_order, cfg.min_order):
        intents.append(Intent(PAIR, P0, qty=c, purpose=P_CROSS, expected_edge=edge_avg,
                              legs=legs, order_kind=kind))
    return intents
