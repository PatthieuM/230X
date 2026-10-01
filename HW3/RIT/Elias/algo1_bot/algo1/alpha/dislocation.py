"""dislocation.evaluate — situation 7: fade the impact of a one-venue jump (Friday).

Plan: if `ms.jump[v]` and the shift is not confirmed by subsequent arrivals on v (T&S / ladder
refresh clustering at the new level), quote both sides inside v's new spread at fair ± c,
band-clamped. Once confirmed, cancel and let `stale.evaluate` treat the other venue as the
laggard. Not implemented tonight — returns no intents; gated by `cfg.enable_dislocation`.
"""
from __future__ import annotations


def evaluate(ms, inv, cfg):
    return []
