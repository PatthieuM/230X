from algo1.alpha.quotes import evaluate
from algo1.core.types import A, BUY, CANCEL, Inventory, M, P1, P3, QUOTE, SELL
from conftest import make_bbo, make_cfg, make_ms, resting


def _cfg(**kw):
    base = dict(enable_quotes=True, d=3.0, tol=1, quote_size=500, quote_size_mode="fixed", max_abs_pos=6000, k_skew=0.5)
    base.update(kw)
    return make_cfg(**base)


def _keys(its, kind):
    return {(i.venue, i.side) for i in its if i.kind == kind}


def test_quotes_both_venues_when_nothing_resting():
    cfg = _cfg()
    ms = make_ms(cfg)                 # fair 999, spreads 2 → c_min 2, d 3 → 996 / 1002
    its = evaluate(ms, Inventory(), cfg)
    assert _keys(its, QUOTE) == {(M, BUY), (M, SELL), (A, BUY), (A, SELL)}
    q = {(i.venue, i.side): i.px for i in its}
    assert q[(M, BUY)] == 996 and q[(M, SELL)] == 1002 and all(i.priority == P3 for i in its)


def test_keep_inside_tol():
    cfg = _cfg()
    inv = Inventory()
    resting(inv, M, BUY, 995)         # target 996, tol 1 → keep
    its = evaluate(make_ms(cfg, inv), inv, cfg)
    assert (M, BUY) not in _keys(its, QUOTE) and (M, BUY) not in _keys(its, CANCEL)


def test_replace_when_target_moves_beyond_tol():
    cfg = _cfg()
    inv = Inventory()
    o = resting(inv, M, BUY, 990)
    its = evaluate(make_ms(cfg, inv), inv, cfg)
    c = [i for i in its if i.kind == CANCEL and i.ref is o]
    assert len(c) == 1 and c[0].priority == P3 and (M, BUY) in _keys(its, QUOTE)


def test_band_violation_forces_p1_cancel():
    cfg = _cfg()
    inv = Inventory()
    o = resting(inv, M, BUY, 1000)    # band bid_max = ask_A - 0 - 1 = 999 → outside
    its = evaluate(make_ms(cfg, inv), inv, cfg)
    c = [i for i in its if i.kind == CANCEL and i.ref is o]
    assert len(c) == 1 and c[0].priority == P1


def test_targets_never_cross_the_band():
    cfg = _cfg(d=0.5, c_min=0.5)
    b = make_bbo(bm=990, am=992, ba=1004, aa=1006)   # venues far apart: fair ≈ 998
    its = evaluate(make_ms(cfg, bbo=b), Inventory(), cfg)
    q = {(i.venue, i.side): i.px for i in its}
    assert q[(M, BUY)] <= 1006 - 1 and q[(M, BUY)] <= 992 - 1
    assert q[(A, SELL)] >= 990 + 1 and q[(A, SELL)] >= 1004 + 1
    assert q[(M, SELL)] >= 1004 + 1                    # ask on M must sit above A's bid


def test_side_blocked_at_inventory_cap():
    cfg = _cfg()
    inv = Inventory()
    inv.pos = 6000
    o = resting(inv, A, BUY, 996)
    its = evaluate(make_ms(cfg, inv), inv, cfg)
    assert BUY not in {s for v, s in _keys(its, QUOTE)}
    assert any(i.kind == CANCEL and i.ref is o and i.priority == P1 for i in its)
    assert (M, SELL) in _keys(its, QUOTE)


def test_skew_sign_and_magnitude():
    from algo1.alpha.quotes import skew_ticks
    cfg = _cfg(skew_at_limit=10.0, max_abs_pos=6000)
    assert skew_ticks(0, cfg) == 0 and skew_ticks(6000, cfg) == -10.0 and skew_ticks(-3000, cfg) == 5.0
    assert skew_ticks(12000, cfg) == -10.0                     # capped beyond the limit
    cfg.skew_curve = 2.0
    assert abs(skew_ticks(3000, cfg)) == 2.5                    # convex: under-reacts mid-range
    cfg.skew_curve = 1.0
    inv = Inventory()
    flat = {(i.venue, i.side): i.px for i in evaluate(make_ms(cfg, inv), inv, cfg)}
    inv.pos = 3000
    longq = {(i.venue, i.side): i.px for i in evaluate(make_ms(cfg, inv), inv, cfg)}
    assert flat[(M, BUY)] - longq[(M, BUY)] == 5               # 5 ticks lower bid at half the cap
    assert longq[(M, SELL)] == 999                            # ask clamped at fair (999): not through fair
    # through-fair needs skew > d and room above the venue's own bid: wide book, big skew
    cfg = _cfg(skew_at_limit=20.0, max_abs_pos=6000, d=11.0)
    wide = make_bbo(bm=990, am=1010, ba=990, aa=1010)        # fair 1000, s_tight 20 → c_min 11
    inv.pos = 6000                                            # skew -20 → r = 980, ask target 991
    clamped = {(i.venue, i.side): i.px for i in evaluate(make_ms(cfg, inv, wide), inv, cfg)}
    assert clamped[(M, SELL)] == 1000                         # never through fair by default
    cfg.skew_through_fair = True
    liq = {(i.venue, i.side): i.px for i in evaluate(make_ms(cfg, inv, wide), inv, cfg)}
    assert liq[(M, SELL)] == 991                              # liquidation mode: ask below fair


def test_unwind_mode_reduce_only_at_touch():
    cfg = _cfg(k_unwind=20)
    inv = Inventory()
    inv.pos = 1200
    ob = resting(inv, M, BUY, 996)
    ms = make_ms(cfg, inv)
    ms.ticks_left = 15
    its = evaluate(ms, inv, cfg)
    assert any(i.kind == CANCEL and i.ref is ob and i.priority == P1 for i in its)   # adding side off
    sells = [i for i in its if i.kind == QUOTE and i.side == SELL]
    assert len(sells) == 2 and all(i.px == 1000 and i.qty == 500 for i in sells)     # join the touch
    assert not any(i.kind == QUOTE and i.side == BUY for i in its)
    ms.ticks_left = 25
    assert any(i.kind == QUOTE and i.side == BUY for i in evaluate(make_ms(cfg, Inventory()), Inventory(), cfg))


def test_side_tilt_widens_exposed_side_and_diagnostics_logged():
    cfg = _cfg(tilt_k=2.0)
    # heavy bid queue on M → micro > mid → the ask is exposed → wider ask, unchanged bid
    b = make_bbo(bm=998, am=1000, bsm=9000, asm=1000, ba=998, aa=1000)
    ms = make_ms(cfg, bbo=b)
    its = {(i.venue, i.side): i.px for i in evaluate(ms, Inventory(), cfg)}
    assert ms.d_ask[M] > ms.d_bid[M] and ms.d_bind[M] == 0 and ms.d_raw[M] == 3.0
    sym = _cfg(tilt_k=0.0)
    ref = {(i.venue, i.side): i.px for i in evaluate(make_ms(sym, bbo=b), Inventory(), sym)}
    assert its[(M, SELL)] >= ref[(M, SELL)] and its[(M, BUY)] == ref[(M, BUY)]


def test_model_size_matches_skew_calibration_and_room_cap():
    from algo1.alpha.quotes import quote_qty
    cfg = _cfg(quote_size_mode="model", skew_at_limit=10.0, max_abs_pos=6000, rho_venues=0.5, adverse_a=0.0)
    inv = Inventory()
    ms = make_ms(cfg, inv)
    # c = 10/6000; q* = 3 / (c · 1.5) = 1200 per venue at d = 3
    assert quote_qty(BUY, 3.0, ms, inv, cfg, M, 2) == 1200
    cfg.rho_venues = 0.0
    assert quote_qty(BUY, 3.0, ms, inv, cfg, M, 2) == 1800
    cfg.rho_venues = 1.0
    assert quote_qty(BUY, 3.0, ms, inv, cfg, M, 2) == 900
    # room cap: pos 5800, two venues quoting → each adding bid may take at most 100
    inv.pos = 5800
    assert quote_qty(BUY, 3.0, ms, inv, cfg, M, 2) == 100
    assert quote_qty(SELL, 3.0, ms, inv, cfg, M, 2) == 900          # reducing side keeps its size
    inv.pos = 6000
    assert quote_qty(BUY, 3.0, ms, inv, cfg, M, 2) == 0            # at the cap: blocked
    cfg.adverse_a = 1.0                                             # a·σ = 1 tick eats a third of d
    inv.pos = 0
    assert quote_qty(BUY, 3.0, ms, inv, cfg, M, 2) == 600


def test_size_shrinks_instead_of_cancelling_near_the_cap():
    cfg = _cfg(quote_size_mode="fixed", quote_size=500, max_abs_pos=6000)
    inv = Inventory()
    inv.pos = 5800
    its = evaluate(make_ms(cfg, inv), inv, cfg)
    bids = [i for i in its if i.kind == QUOTE and i.side == BUY]
    assert bids and all(i.qty == 100 for i in bids)                 # 200 of room split over 2 venues
