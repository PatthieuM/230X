from algo1.market.fair import d_star, fair_v0
from conftest import make_bbo


def test_d_star_zero_rebate_is_0_75_sigma():
    assert abs(d_star(1.0) - 0.75) < 0.02
    assert abs(d_star(2.0) - 1.5) < 0.04


def test_d_star_rebate_pulls_toward_touch():
    assert d_star(1.0, 0.0, 0.5) < d_star(1.0)


def test_fair_v0_depth_weighted():
    b = make_bbo(bm=998, am=1000, ba=1000, aa=1002, bsm=1000, asm=1000, bsa=3000, asa=3000)
    f = fair_v0(b)
    assert abs(f - (999 * 2000 + 1001 * 6000) / 8000) < 1e-9
    assert fair_v0(b, (True, False)) == 999.0


def test_d_star_adverse_term_widens():
    assert abs(d_star(1.0, 0.0, 0.0, 0.25) - 0.93) < 0.02
    assert abs(d_star(1.0, 0.0, 0.0, 0.5) - 1.12) < 0.02
    assert abs(d_star(1.0, 0.0, 0.1, 0.1) - 0.75) < 0.02     # rebate and adverse selection offset exactly
