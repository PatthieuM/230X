from algo1.alpha.cross import clips, evaluate, overlap
from algo1.core.types import A, BUY, Inventory, Ladder, M, P0, PAIR, SELL
from conftest import make_bbo, make_cfg, make_ms


def test_overlap_l1_only():
    q, e, pb, ps = overlap([(1002, 500, 0)], [(1000, 800, 0)], 0.0)
    assert (q, e, pb, ps) == (500, 2.0, 1000, 1002)


def test_overlap_multi_level_and_protective_limits():
    bids = [(1003, 300, 0), (1002, 300, 0), (1001, 300, 0)]
    asks = [(1000, 200, 0), (1001, 500, 0)]
    q, e, pb, ps = overlap(bids, asks, 0.0)
    # 200 @ (1003-1000), 100 @ (1003-1001), 300 @ (1002-1001); 1001 vs 1001 is not > fee2
    assert q == 600 and abs(e - (200 * 3 + 100 * 2 + 300 * 1) / 600) < 1e-9
    assert pb == 1001 and ps == 1002


def test_overlap_fee_kills_last_level():
    bids = [(1003, 300, 0), (1001, 300, 0)]
    asks = [(1000, 300, 0)]
    assert overlap(bids, asks, 1.5)[0] == 300
    assert overlap(bids, asks, 3.0)[0] == 0


def test_overlap_excludes_own_and_exhausted_side():
    bids = [(1002, 500, 500), (1001, 200, 0)]
    asks = [(999, 100, 0), (1000, 1000, 0)]
    q, e, pb, ps = overlap(bids, asks, 0.0)
    assert q == 200 and ps == 1001 and pb == 1000


def test_evaluate_l1_pair():
    cfg = make_cfg()
    b = make_bbo(bm=998, am=1000, bsm=1000, asm=700, ba=1003, aa=1005, bsa=400, asa=1000)   # bid_A 1003 > ask_M 1000
    ms = make_ms(cfg, bbo=b)
    its = evaluate(ms, Inventory(), cfg)
    assert len(its) == 1 and its[0].kind == PAIR and its[0].priority == P0
    assert its[0].qty == 400 and its[0].expected_edge == 3.0
    assert set(its[0].legs) == {(M, BUY, 1000, cfg.leg_kind), (A, SELL, 1003, cfg.leg_kind)}


def test_evaluate_no_cross():
    cfg = make_cfg()
    assert evaluate(make_ms(cfg), Inventory(), cfg) == []


def test_evaluate_uses_fresh_ladders_and_clips():
    cfg = make_cfg(max_order=1000)
    b = make_bbo(bm=998, am=1000, bsm=1000, asm=700, ba=1003, aa=1005, bsa=400, asa=1000)
    inv = Inventory()
    ms = make_ms(cfg, inv, b)
    ms.set_ladder(Ladder(M, [(998, 1000, 0)], [(1000, 700, 0), (1001, 900, 0), (1002, 5000, 0)]))
    ms.set_ladder(Ladder(A, [(1003, 400, 0), (1002, 1200, 0), (1001, 800, 0)], [(1005, 1000, 0)]))
    its = evaluate(ms, inv, cfg)
    total = sum(i.qty for i in its)
    # walk: 400 @ (1003-1000), 300 @ (1002-1000), 900 @ (1002-1001); 1001-1001 = 0 is not > fee2
    assert total == 1600 and [i.qty for i in its] == [1000, 600]
    px = {l[1]: l[2] for l in its[0].legs}
    assert px[BUY] == 1001 and px[SELL] == 1002


def test_evaluate_capped_by_pair_headroom():
    cfg = make_cfg()
    b = make_bbo(bm=998, am=1000, bsm=1000, asm=9000, ba=1003, aa=1005, bsa=9000, asa=1000)
    inv = Inventory(gross_limit=25000, slack=1000)
    inv.pos_venue = [20000, 0]
    its = evaluate(make_ms(cfg, inv, b), inv, cfg)
    assert its[0].qty == 2000    # (25000-1000-20000)//2


def test_clips():
    assert clips(2500, 1000) == [1000, 1000, 500] and clips(0, 1000) == []


def test_scarce_leg_first():
    cfg = make_cfg()
    b = make_bbo(bm=998, am=1000, bsm=1000, asm=100000, ba=1003, aa=1005, bsa=1100, asa=1000)   # A's bid is the small student order
    its = evaluate(make_ms(cfg, bbo=b), Inventory(), cfg)
    assert its[0].legs[0][:2] == (A, SELL) and its[0].legs[1][:2] == (M, BUY) and its[0].qty == 1100
    b2 = make_bbo(bm=998, am=1000, bsm=1000, asm=900, ba=1003, aa=1005, bsa=100000, asa=1000)     # M's ask is scarce
    its = evaluate(make_ms(cfg, bbo=b2), Inventory(), cfg)
    assert its[0].legs[0][:2] == (M, BUY)
