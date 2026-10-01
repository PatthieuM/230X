from time import perf_counter_ns

from algo1.core.types import A, BUY, Inventory, Ladder, M, SELL, Snapshot
from algo1.market.state import MarketState
from conftest import make_bbo, make_cfg, make_ms, resting


def test_bands_from_other_venue():
    cfg = make_cfg(band_w=1)
    ms = make_ms(cfg, bbo=make_bbo(bm=998, am=1000, ba=1001, aa=1003))
    assert ms.band(M) == (1003 - 1 - 1, 1001 + 1 + 1)
    assert ms.band(A) == (1000 - 2, 998 + 2)


def test_own_touch_replaced_by_ladder_next_level():
    cfg = make_cfg()
    inv = Inventory()
    resting(inv, M, BUY, 998, 1000)
    ms = MarketState(cfg)
    ms.set_ladder(Ladder(M, [(998, 1000, 1000), (997, 600, 0)], [(1000, 500, 0)]))
    ms.update(Snapshot(1, 0, perf_counter_ns(), make_bbo(bsm=1000), 0, [0, 0]), [], inv)
    assert ms.bbo.bid[M] == 997 and ms.bbo.bid_size[M] == 600


def test_jump_flag_and_sigma():
    cfg = make_cfg(jump_J=3)
    inv = Inventory()
    ms = MarketState(cfg)
    t = perf_counter_ns()
    ms.update(Snapshot(1, 0, t, make_bbo(), 0, [0, 0]), [], inv)
    ms.update(Snapshot(2, 0, t + 1000, make_bbo(bm=1002, am=1004), 0, [0, 0]), [], inv)
    assert ms.jump[M] and not ms.jump[A] and ms.sigma_hat[M] > ms.sigma_hat[A]
    assert ms.s == ms.mid[A] - ms.mid[M]


def test_sigma_hat_is_a_horizon_std_not_a_per_poll_mad():
    """Same Gaussian mid path polled at 1 ms and at 10 ms must give the same sigma over 1 s."""
    import numpy as np
    from algo1.core.types import Snapshot
    rng = np.random.default_rng(0)
    steps = rng.normal(0, 0.5, 20000)                  # ticks per ms → sigma_1s = 0.5·sqrt(1000) ≈ 15.8
    path = 1000 + np.cumsum(steps)
    out = []
    for stride in (1, 10):
        cfg = make_cfg(sigma_tau_s=5.0, sigma_horizon_s=1.0)
        ms = MarketState(cfg)
        inv = Inventory()
        for k in range(0, len(path), stride):
            m = int(round(path[k]))
            ms.update(Snapshot(k, 0, k * 1_000_000, make_bbo(bm=m - 1, am=m + 1, ba=m - 1, aa=m + 1), 0, [0, 0]), [], inv)
        out.append(ms.sigma_hat[M])
    assert abs(out[0] - out[1]) / out[1] < 0.35 and 8 < out[1] < 25


def test_fair_mode_switch():
    b = make_bbo(bm=998, am=1000, ba=1000, aa=1002, bsm=1000, asm=1000, bsa=3000, asa=1000)
    v0 = make_ms(make_cfg(fair_mode="micro_depth"), bbo=b).fair
    mm = make_ms(make_cfg(fair_mode="mean_mids"), bbo=b).fair
    mmic = make_ms(make_cfg(fair_mode="mean_micro"), bbo=b).fair
    assert mm == 1000.0 and mmic > mm and v0 != mm


def test_lag_sigma_is_robust_to_bounce_and_agrees_on_a_random_walk():
    """A tick-discrete random walk (jumps of ±1 tick at random times, as RIT's mid moves): both
    estimators agree with the realised 1 s sigma. Add iid bounce (transient ±1 tick on 30% of polls):
    the rate estimator overstates by an order of magnitude, the lag estimator stays close."""
    import numpy as np
    from algo1.core.types import Snapshot
    rng = np.random.default_rng(2)
    n = 40000                                                    # 40 s at 1 ms polls
    jumps = rng.choice([-1, 0, 1], size=n, p=[0.0005, 0.999, 0.0005])   # ~1 jump/s → sigma_1s ≈ 1 tick
    base = 1000 + np.cumsum(jumps)
    for bounce, expect_rate_over in ((False, False), (True, True)):
        noise = np.where(rng.random(n) < 0.3, rng.choice([-1, 1], size=n), 0) if bounce else np.zeros(n, int)
        path = base + noise
        cfg = make_cfg(sigma_tau_s=10.0, sigma_horizon_s=1.0, sigma_lag_slots=32)
        ms = MarketState(cfg)
        inv = Inventory()
        for k in range(n):
            m = int(path[k])
            ms.update(Snapshot(k, 0, k * 1_000_000, make_bbo(bm=m - 1, am=m + 1, ba=m - 1, aa=m + 1), 0, [0, 0]), [], inv)
        realised = float(np.std(path[1000:] - path[:-1000]))
        rate, lag = ms.sigma_rate[M], ms.sigma_lag[M]
        assert abs(lag - realised) / realised < 0.5, (lag, realised)
        if expect_rate_over:
            assert rate > 3 * realised, (rate, realised)
        else:
            assert abs(rate - realised) / realised < 0.5, (rate, realised)
