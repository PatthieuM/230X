"""layers.evaluate — situation 6: sweep harvesting (Friday).

Plan: for each venue and side, k levels at fair ∓ (d + j·step), sizes base·decay^j, all inside
band(v); sticky repricing (reprice level j only if |fair - fair_at_placement_j| > step or the
band is violated); total layer size <= layer_cap <= headroom. Emits QUOTE(P4) / CANCEL(P1 if
band-forced else P4). Not implemented tonight — returns no intents and is gated by
`cfg.enable_layers`.
"""
from __future__ import annotations


def evaluate(ms, inv, cfg):
    return []
