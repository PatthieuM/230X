"""Fair value and the optimal quoting distance.

fair v0 — depth-weighted microprice across venues:
    fair = (micro_M * depth_M + micro_A * depth_A) / (depth_M + depth_A)
The microprice tilts the mid toward the heavier side of the book (the side about to be hit is
the thinner one). Pooling both venues by depth treats the two books as one sample of the same
latent price.

fair v1 (Friday) — centre of the resting-order sample per venue from the Ladder: the book is a
truncated sample of the AI agents' price distribution; the pooled centre is a sharper estimate of
the mean than the mid, and its dispersion is `sigma_hat`.

d_star — for a symmetric quote at distance x·sigma from fair, with fill probability
approximated by 1 - Phi(x) and rebate r (ticks), maximise expected edge per quote
    (x + r/sigma) * (1 - Phi(x))
on a grid x ∈ [0, 3]. With r = 0 the optimum is x* ≈ 0.75; r > 0 pulls x* toward 0 (quote at the
touch, get paid to be the queue); an adverse-selection cost a (sigma units, conditional on fill)
pushes it out. c_min (a fee/spread floor) and the band clamp in `alpha/quotes.py` guard other
things — a losing round trip and cross-venue pick-off — not informed flow; only `a` does.
"""
from __future__ import annotations

import math

_SQRT2 = math.sqrt(2.0)
_GRID = [i * 0.01 for i in range(0, 301)]


def phi_cdf(x):
    return 0.5 * math.erfc(-x / _SQRT2)


def fair_v0(bbo, v_enabled=(True, True)):
    """Depth-weighted microprice across enabled venues. Falls back to the plain mid."""
    num = 0.0
    den = 0.0
    for v in (0, 1):
        if not v_enabled[v] or bbo.ask[v] <= 0 or bbo.bid[v] <= 0:
            continue
        d = bbo.depth(v)
        if d <= 0:
            continue
        num += bbo.micro(v) * d
        den += d
    if den > 0:
        return num / den
    # no depth anywhere (e.g. we are the touch on both sides): average of available mids
    mids = [bbo.mid(v) for v in (0, 1) if v_enabled[v] and bbo.ask[v] > 0 and bbo.bid[v] > 0]
    return sum(mids) / len(mids) if mids else 0.0


def d_star(sigma, lam=0.0, r=0.0, a=0.0):
    """Optimal half-distance in ticks: maximise (x + r/sigma − a)(1 − Φ(x)).

    `sigma` is the std of the mid over the quote's resting horizon (MarketState.sigma_hat, ticks).
    `a` is the expected adverse move conditional on a fill, in sigma units — the Copeland–Galai
    option premium that the fee/spread floor does not price. Rebate and adverse selection enter
    with opposite signs: a = 0.1 moves x* from 0.75 to 0.82, a = 0.25 to 0.93, a = 0.5 to 1.12.
    `a` is measured from live passive fills (report: post-fill drift / sigma) and stays 0 until
    then (`cfg.adverse_a`). `lam` is accepted for the Friday queue model and unused in v0."""
    if sigma <= 0:
        return 0.0
    best_x = 0.0
    best_val = -1.0
    rs = r / sigma - a
    for x in _GRID:
        val = (x + rs) * (1.0 - phi_cdf(x))
        if val > best_val:
            best_val = val
            best_x = x
    return best_x * sigma
