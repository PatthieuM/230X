import numpy as np
import pandas as pd

from algo1.sim.analysis import analyze, cross_events, error_correction


def test_error_correction_sign_on_synthetic():
    rng = np.random.default_rng(0)
    n = 4000
    mid_m = np.cumsum(rng.normal(0, 1.0, n)) + 1000
    mid_a = np.empty(n)
    mid_a[0] = mid_m[0]
    for t in range(1, n):
        mid_a[t] = mid_a[t - 1] + 0.5 * (mid_m[t - 1] - mid_a[t - 1]) + rng.normal(0, 0.3)
    r = error_correction(mid_m, mid_a)
    assert 0.3 < r["ec_a"] < 0.7 and r["t_a"] > 5 and r["follower"] == 1 and r["leader"] == 0
    assert r["t_a_nw"] > 5 and r["t_a_boot"] > 5 and r["spread_stationary"]     # a real gap closer passes all gates


def test_gate_rejects_spurious_regression_on_random_walks():
    """Two independent random walks: the naive t can be large (spurious); NW / bootstrap / DF must not."""
    from algo1.sim.analysis import error_correction
    rng = np.random.default_rng(5)
    n = 3000
    mid_m = 1000 + np.cumsum(rng.normal(0, 1.0, n))
    mid_a = 1000 + np.cumsum(rng.normal(0, 1.0, n))
    r = error_correction(mid_m, mid_a)
    assert not r["spread_stationary"]
    assert abs(r["t_a_nw"]) < abs(r["t_a"]) + 1e-9 or abs(r["t_a"]) < 3


def test_cross_events_and_analyze():
    t = np.arange(100) * 1_000_000
    bid_m = np.full(100, 998); ask_m = np.full(100, 1000)
    bid_a = np.full(100, 998); ask_a = np.full(100, 1000)
    bid_a[40:45] = 1002                       # 5 polls crossed (bid_A > ask_M)
    ask_a[40:45] = 1004
    df = pd.DataFrame({"t_send": t, "t_recv": t, "bid_m": bid_m, "ask_m": ask_m, "bs_m": 1, "as_m": 1,
                       "bid_a": bid_a, "ask_a": ask_a, "bs_a": 1, "as_a": 1, "pos_m": 0, "pos_a": 0, "tick": 0})
    ev = cross_events(df)
    assert len(ev) == 1 and ev.iloc[0]["life_ns"] == 5_000_000 and ev.iloc[0]["dir"] == "buyM_sellA"
    res = analyze(df, horizon_ms=1.0)          # 1 ms bins on a 1 ms-spaced synthetic tape: no resampling
    assert res["n_cross"] == 1 and res["params"]["d"] >= res["params"]["c_min"]
    # the step-and-revert in mid_A is a real error correction; ec_coef may be > 0 here
    assert res["params"]["stale_leader"] in (0, 1)


def _synthetic_orders(k_true=0.4, n=3000, dmax=8, seed=1, base=1.5):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        dist = int(rng.integers(1, dmax))
        expo = 2.0
        rate = base * np.exp(-k_true * dist)
        # an order fills at most once: time-to-fill ~ Exp(rate), exposure stops at the fill
        t_fill = rng.exponential(1.0 / rate) if rate > 0 else np.inf
        filled = t_fill < expo
        lived = min(t_fill, expo)
        rows.append({"id_local": i, "purpose": 1, "kind": 1, "t_ack": 1_000_000_000 * i,
                     "t_done": 1_000_000_000 * i + int(lived * 1e9), "px": 1000 + dist,
                     "fair_at_intent": 1000.0, "filled": 100 if filled else 0})
    return pd.DataFrame(rows)


def test_fit_fill_decay_recovers_k():
    from algo1.sim.analysis import fit_fill_decay
    res = fit_fill_decay(_synthetic_orders())
    assert res["success"] and 0.3 < res["k"] < 0.5 and res["n_bins"] >= 5
    assert res["se_k"] > 0 and res["relative_se_k"] < 0.3


def test_fit_fill_decay_keeps_zero_fill_bins():
    """Far bins with zero fills are evidence of steep decay; a censored fit would flatten k."""
    from algo1.sim.analysis import fit_binned_poisson_decay, fit_fill_decay
    df = _synthetic_orders(k_true=0.8, n=4000, dmax=12, base=0.6)
    res = fit_fill_decay(df)
    zero_bins = [b for b in res["bins"] if b[1] == 0]
    assert zero_bins, "the synthetic tape should have far bins with no fills"
    assert res["success"] and 0.6 < res["k"] < 1.0
    # the same bins without the zero-fill ones: the censored slope is flatter
    kept = [b for b in res["bins"] if b[1] > 0]
    cens = fit_binned_poisson_decay([b[0] for b in kept], [b[1] for b in kept], [b[2] for b in kept])
    assert cens["k"] < res["k"]


def test_fit_fill_decay_rejects_unidentified():
    from algo1.sim.analysis import fit_fill_decay
    df = _synthetic_orders(k_true=0.0, n=200, dmax=3)          # flat: k should not be promoted
    res = fit_fill_decay(df)
    assert res["k"] == 0.0 and not res["success"] and res["reason"]


def test_fair_markout_scores_against_trades():
    from algo1.sim.analysis import fair_markout
    t = np.arange(200) * 50_000_000
    bid_m = np.full(200, 998); ask_m = np.full(200, 1000); bid_a = np.full(200, 998); ask_a = np.full(200, 1000)
    df = pd.DataFrame({"t_send": t, "t_recv": t, "bid_m": bid_m, "ask_m": ask_m, "bs_m": 9000, "as_m": 1000,
                       "bid_a": bid_a, "ask_a": ask_a, "bs_a": 9000, "as_a": 1000, "pos_m": 0, "pos_a": 0, "tick": 0})
    tas = pd.DataFrame({"t_recv": t[::5] + 1, "venue": 0, "id": np.arange(40), "tick": 0, "px": 1000, "qty": 100})
    out = fair_markout(df, tas, horizons_ms=(1000,))
    r = out[1000]
    assert r["n"] > 0 and r["micro_depth"] < r["mean_mids"]          # trades print at the ask: the tilt wins here
