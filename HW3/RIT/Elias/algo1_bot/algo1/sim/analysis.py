"""Analysis of a recording → `params.json` and the numbers that decide which alphas run.

    cross_events(df)      crossed intervals: start, end, lifetime (ns), direction, max edge
    survival(life_ns)     empirical survival function of cross lifetimes
    resample(df, horizon_ms)
                          one row per horizon (default 50 ms): the tape is ~2,000 polls/s and a
                          per-poll coefficient is meaningless; the strategy acts per loop+order.
    error_correction(mid_m, mid_a)
                          Δmid_A(t+1) = a + ec·(mid_M − mid_A)(t): if the follower closes the gap,
                          ec > 0 and `stale.evaluate` has something to trade. Regress both ways; the
                          venue whose OWN change loads on the gap is the follower. Inference:
                          classical t is reported but the gate uses the Newey–West t (Bartlett,
                          NW-1994 lag rule) AND a moving-block bootstrap t (block n^(1/3)) AND a
                          Dickey–Fuller stationarity test on the spread — HF residuals are
                          heteroskedastic and autocorrelated, so an iid t overstates.
    sigma(df)             std of Δmid per venue (ticks) → d = max(c_min, 0.75·sigma) (v0 rule
                          from `d_star` at r = 0)
    write_params(...)     params.json: d, c_min, ec_coef (0 unless t > 3 and ec > 0),
                          stale_leader
    fit_fill_decay(orders_df)
                          k from a run's resting quotes: fills per bin ~ Poisson(exposure ·
                          exp(a − k·δ)), δ = ticks from fair at placement, zero-fill bins kept,
                          se(k) from the Fisher information, gated on identification. The GLFT
                          width term is then ≈ 1/k. (Replaced a censored log-rate OLS, 10 Sep.)
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd


def cross_events(df, fee2=0.0):
    """Crossed intervals from a recording (netted or raw BBO, both in ticks)."""
    e1 = df["bid_a"].values - df["ask_m"].values - fee2      # buy M sell A
    e2 = df["bid_m"].values - df["ask_a"].values - fee2      # buy A sell M
    crossed = (e1 > 0) | (e2 > 0)
    t = df["t_recv"].values
    out = []
    i = 0
    n = len(df)
    while i < n:
        if not crossed[i]:
            i += 1
            continue
        j = i
        while j < n and crossed[j]:
            j += 1
        seg1 = e1[i:j]
        seg2 = e2[i:j]
        direction = "buyM_sellA" if seg1.max() >= seg2.max() else "buyA_sellM"
        end_t = t[j] if j < n else t[j - 1]
        out.append({"i0": i, "i1": j, "t0": int(t[i]), "t1": int(end_t), "life_ns": int(end_t - t[i]),
                    "dir": direction, "max_edge": float(max(seg1.max(), seg2.max())),
                    "n_polls": int(j - i)})
        i = j
    return pd.DataFrame(out)


def survival(life_ns):
    x = np.sort(np.asarray(life_ns, dtype=float))
    if x.size == 0:
        return x, x
    s = 1.0 - np.arange(1, x.size + 1) / x.size
    return x, s


def _ols(y, x, hac_lags=None):
    """OLS of y on [1, x]. Returns (beta, se_classical, se_newey_west). High-frequency residuals
    are heteroskedastic and autocorrelated, so the classical se understates; the Newey–West se
    with Bartlett weights and lag L = floor(4·(n/100)^(2/9)) (Newey–West 1994 rule) is what the
    gate uses."""
    X = np.column_stack([np.ones_like(x), x])
    beta, res, rank, sv = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    n, k = X.shape
    s2 = (resid @ resid) / max(1, n - k)
    XtX_inv = np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.diag(s2 * XtX_inv))
    L = int(np.floor(4 * (n / 100.0) ** (2.0 / 9.0))) if hac_lags is None else int(hac_lags)
    u = X * resid[:, None]
    S = u.T @ u
    for l in range(1, L + 1):
        w = 1.0 - l / (L + 1.0)
        G = u[l:].T @ u[:-l]
        S += w * (G + G.T)
    cov_nw = XtX_inv @ S @ XtX_inv
    se_nw = np.sqrt(np.maximum(np.diag(cov_nw), 0))
    return beta, se, se_nw


def _block_bootstrap_t(y, x, n_boot=500, block=None, seed=0):
    """Moving-block bootstrap of the slope: t = beta / sd(beta*), block length ~ n^(1/3)."""
    n = len(y)
    b = int(max(2, round(n ** (1.0 / 3.0)))) if block is None else int(block)
    rng = np.random.default_rng(seed)
    beta0 = _ols(y, x)[0][1]
    nb = int(np.ceil(n / b))
    bs = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n - b + 1, nb)
        idx = (starts[:, None] + np.arange(b)[None, :]).ravel()[:n]
        bs[i] = _ols(y[idx], x[idx])[0][1]
    sd = bs.std(ddof=1)
    return float(beta0 / sd) if sd > 0 else 0.0, b


def spread_stationary(s, crit=-2.86):
    """Dickey–Fuller test (constant, no trend) on the venue spread s = mid_M − mid_A:
    Δs_t = a + ρ·s_{t−1} + e. Stationary if t(ρ) < crit (5% critical value ≈ −2.86). If the
    spread is a random walk there is no gap to close and the error-correction fit is spurious."""
    s = np.asarray(s, dtype=float)
    if len(s) < 20:
        return False, 0.0
    ds = np.diff(s)
    lag = s[:-1]
    beta, se, se_nw = _ols(ds, lag)
    t = beta[1] / se_nw[1] if se_nw[1] > 0 else 0.0
    return bool(t < crit), float(t)


def error_correction(mid_m, mid_a):
    """Returns dict(ec_a, t_a, ec_m, t_m, follower, leader).
    ec_a: coefficient of Δmid_A on (mid_M − mid_A); ec_m: Δmid_M on (mid_A − mid_M)."""
    mid_m = np.asarray(mid_m, dtype=float)
    mid_a = np.asarray(mid_a, dtype=float)
    gap = mid_m[:-1] - mid_a[:-1]
    d_a = np.diff(mid_a)
    d_m = np.diff(mid_m)
    if gap.size < 10 or np.allclose(gap.std(), 0):
        return {"ec_a": 0.0, "t_a": 0.0, "ec_m": 0.0, "t_m": 0.0, "follower": 1, "leader": 0,
                "t_a_nw": 0.0, "t_m_nw": 0.0, "t_a_boot": 0.0, "t_m_boot": 0.0, "spread_stationary": False}
    ba, sa, sa_nw = _ols(d_a, gap)
    bm, sm, sm_nw = _ols(d_m, -gap)
    t_a = ba[1] / sa[1] if sa[1] > 0 else 0.0
    t_m = bm[1] / sm[1] if sm[1] > 0 else 0.0
    t_a_nw = ba[1] / sa_nw[1] if sa_nw[1] > 0 else 0.0
    t_m_nw = bm[1] / sm_nw[1] if sm_nw[1] > 0 else 0.0
    t_a_boot, blk = _block_bootstrap_t(d_a, gap)
    t_m_boot, _ = _block_bootstrap_t(d_m, -gap)
    stat, t_df = spread_stationary(gap)
    follower = 1 if ba[1] >= bm[1] else 0
    return {"ec_a": float(ba[1]), "t_a": float(t_a), "ec_m": float(bm[1]), "t_m": float(t_m),
            "t_a_nw": float(t_a_nw), "t_m_nw": float(t_m_nw), "t_a_boot": float(t_a_boot),
            "t_m_boot": float(t_m_boot), "boot_block": int(blk), "spread_stationary": stat, "df_t": t_df,
            "follower": follower, "leader": 1 - follower}


def sigma(df):
    mm = (df["bid_m"].values + df["ask_m"].values) / 2.0
    ma = (df["bid_a"].values + df["ask_a"].values) / 2.0
    return float(np.std(np.diff(mm))), float(np.std(np.diff(ma)))


def resample(df, horizon_ms=50.0):
    """One row per `horizon_ms` of client time (last poll in each bin). The per-poll tape runs at
    ~2,000 polls/s on the RIT client, where any regression coefficient is meaninglessly small;
    the strategy acts at the horizon of a loop-plus-order (tens of ms), so regress there."""
    t = df["t_recv"].values
    b = ((t - t[0]) // int(horizon_ms * 1e6)).astype(int)
    keep = np.r_[b[1:] != b[:-1], True]
    return df[keep]


def analyze(df, fee=(0.0, 0.0), rebate=(0.0, 0.0), t_min=3.0, horizon_ms=50.0, ec_min=0.05):
    ev = cross_events(df, fee[0] + fee[1])
    dh = resample(df, horizon_ms)
    mm = (dh["bid_m"].values + dh["ask_m"].values) / 2.0
    ma = (dh["bid_a"].values + dh["ask_a"].values) / 2.0
    ec = error_correction(mm, ma)
    sg = sigma(dh)
    s_tight = float(np.median(np.minimum(df["ask_m"].values - df["bid_m"].values, df["ask_a"].values - df["bid_a"].values)))
    f = max(fee)
    r = min(rebate)
    c_min = float(np.ceil(s_tight / 2 + f - r + 1))
    d = float(max(c_min, round(0.75 * max(sg), 1)))
    ec_use = ec["ec_a"] if ec["follower"] == 1 else ec["ec_m"]
    # the gate: meaningful coefficient AND significant under Newey–West AND under the block
    # bootstrap AND a stationary venue spread (an iid t on HF residuals overstates; review 10 Sep)
    t_nw = ec["t_a_nw"] if ec["follower"] == 1 else ec["t_m_nw"]
    t_bt = ec["t_a_boot"] if ec["follower"] == 1 else ec["t_m_boot"]
    ec_coef = float(ec_use) if (ec_use > ec_min and t_nw > t_min and t_bt > t_min and ec["spread_stationary"]) else 0.0
    return {
        "n_polls": int(len(df)), "n_rows_horizon": int(len(dh)), "horizon_ms": horizon_ms, "n_cross": int(len(ev)),
        "cross_life_p50_us": float(np.percentile(ev["life_ns"], 50) / 1e3) if len(ev) else None,
        "cross_life_p90_us": float(np.percentile(ev["life_ns"], 90) / 1e3) if len(ev) else None,
        "cross_max_edge_p50": float(ev["max_edge"].median()) if len(ev) else None,
        "sigma_m_per_horizon": sg[0], "sigma_a_per_horizon": sg[1], "s_tight": s_tight, "ec": ec,
        "params": {"d": d, "c_min": c_min, "ec_coef": ec_coef, "stale_leader": int(ec["leader"])},
    }


def write_params(result, path="params.json"):
    with open(path, "w") as f:
        json.dump(result["params"], f, indent=2)
    return path


def fit_binned_poisson_decay(distance, fills, exposure, max_iter=50, tol=1e-9):
    """fills_i ~ Poisson(exposure_i · exp(a − k·distance_i)), fitted by Newton–Raphson with a
    step-halving line search that keeps k >= 0. Zero-fill bins stay in the likelihood (they are
    the evidence that far quotes do not fill); exposure enters as an offset with coefficient one;
    se(k) comes from the inverse Fisher information. Returns success=False rather than a clipped
    number when the fit is singular, non-positive or has no admissible step."""
    d = np.asarray(distance, dtype=float)
    y = np.asarray(fills, dtype=float)
    e = np.asarray(exposure, dtype=float)
    keep = np.isfinite(d) & np.isfinite(y) & (e > 0) & (y >= 0)
    d, y, e = d[keep], y[keep], e[keep]
    if len(d) < 2 or y.sum() == 0 or np.allclose(d, d[0]):
        return {"success": False, "reason": "insufficient information", "k": 0.0, "n_bins": int(len(d))}
    X = np.column_stack((np.ones(len(d)), -d))
    theta = np.array([np.log(y.sum() / e.sum()), 0.1])

    def loglik(t):
        eta = np.clip(np.log(e) + X @ t, -30, 30)
        return float(np.sum(y * eta - np.exp(eta)))

    for _ in range(max_iter):
        eta = np.clip(np.log(e) + X @ theta, -30, 30)
        mu = np.exp(eta)
        score = X.T @ (y - mu)
        fisher = X.T @ (mu[:, None] * X)
        try:
            step = np.linalg.solve(fisher, score)
        except np.linalg.LinAlgError:
            return {"success": False, "reason": "singular information", "k": 0.0, "n_bins": int(len(d))}
        old_ll = loglik(theta)
        scale = 1.0
        while scale > 1e-8:
            candidate = theta + scale * step
            if candidate[1] >= 0 and loglik(candidate) >= old_ll:
                break
            scale *= 0.5
        else:
            return {"success": False, "reason": "no admissible step", "k": 0.0, "n_bins": int(len(d))}
        theta = candidate
        if np.max(np.abs(scale * step)) < tol:
            break
    eta = np.clip(np.log(e) + X @ theta, -30, 30)
    mu = np.exp(eta)
    fisher = X.T @ (mu[:, None] * X)
    try:
        cov = np.linalg.inv(fisher)
    except np.linalg.LinAlgError:
        return {"success": False, "reason": "singular information at optimum", "k": 0.0, "n_bins": int(len(d))}
    k = float(theta[1])
    se_k = float(np.sqrt(max(0.0, cov[1, 1])))
    success = k > 0 and np.isfinite(se_k)
    return {"success": success, "reason": "" if success else "k <= 0", "a": float(theta[0]), "k": k if success else 0.0,
            "k_raw": k, "se_k": se_k, "relative_se_k": (se_k / k) if k > 0 else float("inf"),
            "n_bins": int(len(d)), "total_fills": float(y.sum())}


def fit_fill_decay(orders, purpose=1, min_orders=1, max_rel_se=0.5):
    """k per tick from a run's resting quotes (`runs/<name>/orders.parquet`, last record per order).

    Bins by integer distance (ticks from fair at placement); each bin keeps its fill count and its
    total resting exposure in seconds (t_done − t_ack, or the run end for still-open orders).
    Zero-fill bins are kept — dropping them censors the far-distance sample on lucky counts and
    flattens the decay. Fitted with `fit_binned_poisson_decay`. Gate: k > 0 and se(k)/k <=
    `max_rel_se`, otherwise k = 0 (unfitted) with the reason; `analyze --fills` writes `k_fill`
    only on success, and `use_k_width` is a separate switch."""
    O = orders.drop_duplicates("id_local", keep="last")
    O = O[(O["purpose"] == purpose) & (O["kind"] == 1) & (O["t_ack"] > 0)]
    if not len(O):
        return {"k": 0.0, "success": False, "reason": "no resting quotes", "n_bins": 0, "bins": []}
    t_end = int(O["t_ack"].max())
    dist = np.abs(O["px"].values - O["fair_at_intent"].values)
    done = np.where(O["t_done"].values > 0, O["t_done"].values, t_end)
    expo = np.maximum(1e-9, (done - O["t_ack"].values) / 1e9)
    filled = (O["filled"].values > 0).astype(float)
    bins = {}
    for d, e, f in zip(np.round(dist).astype(int), expo, filled):
        a = bins.setdefault(d, [0.0, 0.0, 0])
        a[0] += f
        a[1] += e
        a[2] += 1
    rows = [(d, v[0], v[1], v[2]) for d, v in sorted(bins.items()) if v[2] >= min_orders]
    res = fit_binned_poisson_decay([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])
    res["bins"] = [(d, f, e, n, f / e) for d, f, e, n in rows]        # distance, fills, exposure s, orders, rate
    if res.get("success") and res.get("relative_se_k", np.inf) > max_rel_se:
        res["success"] = False
        res["reason"] = f"weakly identified: se(k)/k = {res['relative_se_k']:.2f} > {max_rel_se}"
        res["k"] = 0.0
    return res


# ---- paired (common-random-number) evaluation -------------------------------------------------------
def paired_stats(base, cand, base_ind=None, cand_ind=None, n_boot=2000, seed=0):
    """Candidate − baseline under CRN pairing vs an independent design of equal budget.

    base, cand:           outcomes on the same seeds (paired, length n)
    base_ind, cand_ind:   outcomes on disjoint seeds (independent design); if omitted, the
                          independent SE is computed from the paired samples treated as unpaired
                          (a conservative what-if, flagged in the result).
    Returns mean diff, paired SE, independent SE, their ratio with a bootstrap 90% CI, the
    outcome correlation, and a paired t-test p-value (two-sided, normal approx for n >= 30,
    t otherwise)."""
    b = np.asarray(base, dtype=float)
    c = np.asarray(cand, dtype=float)
    n = len(b)
    d = c - b
    mean = float(d.mean())
    se_p = float(d.std(ddof=1) / np.sqrt(n)) if n > 1 else float("nan")
    corr = float(np.corrcoef(b, c)[0, 1]) if n > 2 and b.std() > 0 and c.std() > 0 else float("nan")
    if base_ind is None or cand_ind is None:
        bi, ci, flagged = b, c, True
    else:
        bi, ci, flagged = np.asarray(base_ind, float), np.asarray(cand_ind, float), False
    se_i = float(np.sqrt(bi.var(ddof=1) / len(bi) + ci.var(ddof=1) / len(ci))) if len(bi) > 1 and len(ci) > 1 else float("nan")
    rng = np.random.default_rng(seed)
    ratios = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        dp = d[idx]
        sp = dp.std(ddof=1) / np.sqrt(n)
        ib = rng.integers(0, len(bi), len(bi))
        ic = rng.integers(0, len(ci), len(ci))
        si = np.sqrt(bi[ib].var(ddof=1) / len(bi) + ci[ic].var(ddof=1) / len(ci))
        if si > 0:
            ratios.append(sp / si)
    ratios = np.array(ratios)
    t = mean / se_p if se_p and se_p > 0 else float("nan")
    try:
        from scipy import stats
        p_val = float(2 * stats.t.sf(abs(t), n - 1)) if n > 1 and np.isfinite(t) else float("nan")
    except ImportError:
        p_val = float(2 * (1 - 0.5 * (1 + __import__("math").erf(abs(t) / np.sqrt(2))))) if np.isfinite(t) else float("nan")
    return {"n": n, "mean_diff": mean, "se_paired": se_p, "se_indep": se_i,
            "se_ratio": (se_p / se_i) if se_i and se_i > 0 else float("nan"),
            "se_ratio_ci90": (float(np.percentile(ratios, 5)), float(np.percentile(ratios, 95))) if len(ratios) else (float("nan"),) * 2,
            "corr": corr, "t": float(t), "p": p_val, "indep_from_paired_samples": flagged}


def holm(pvals, alpha=0.05):
    """Holm step-down: returns (adjusted p-values, reject flags) in the input order."""
    p = np.asarray(pvals, dtype=float)
    m = len(p)
    order = np.argsort(p)
    adj = np.empty(m)
    running = 0.0
    for rank, i in enumerate(order):
        val = min(1.0, (m - rank) * p[i])
        running = max(running, val)
        adj[i] = running
    return adj, adj <= alpha


def corr_decay(base_curves, cand_curves, quantiles=(0.25, 0.5, 0.75, 1.0)):
    """Paired correlation of cumulative P&L at fractions of the heat. `*_curves` are lists (one per
    seed) of arrays of cumulative pnl sampled at `quantiles` of run time. A decaying correlation
    diagnoses endogenous path divergence (fills changing what the two runs see)."""
    B = np.asarray(base_curves, float)
    C = np.asarray(cand_curves, float)
    out = []
    for j, q in enumerate(quantiles):
        b, c = B[:, j], C[:, j]
        out.append((q, float(np.corrcoef(b, c)[0, 1]) if b.std() > 0 and c.std() > 0 else float("nan")))
    return out


def fair_markout(df, tas, horizons_ms=(200, 1000, 5000)):
    """Which centre best predicts where trades actually print. For each trade, compare each fair
    candidate (evaluated from the last poll before the trade) with the volume-weighted trade price
    over the following horizon. RMSE per candidate and horizon; the right target for the
    fair-value question (a centre scored against its own future is a tautology)."""
    t = df["t_recv"].values
    mid_m = (df["bid_m"].values + df["ask_m"].values) / 2.0
    mid_a = (df["bid_a"].values + df["ask_a"].values) / 2.0

    def micro(b, a, bs, as_):
        tot = bs + as_
        return np.where(tot > 0, (a * bs + b * as_) / np.maximum(tot, 1), (b + a) / 2.0)
    mic_m = micro(df["bid_m"].values, df["ask_m"].values, df["bs_m"].values, df["as_m"].values)
    mic_a = micro(df["bid_a"].values, df["ask_a"].values, df["bs_a"].values, df["as_a"].values)
    dep_m = df["bs_m"].values + df["as_m"].values
    dep_a = df["bs_a"].values + df["as_a"].values
    cands = {"micro_depth": (mic_m * dep_m + mic_a * dep_a) / np.maximum(dep_m + dep_a, 1),
             "mean_mids": (mid_m + mid_a) / 2.0, "mean_micro": (mic_m + mic_a) / 2.0}
    tt = tas["t_recv"].values
    tp = tas["px"].values.astype(float)
    tq = tas["qty"].values.astype(float)
    order = np.argsort(tt)
    tt, tp, tq = tt[order], tp[order], tq[order]
    out = {}
    for h in horizons_ms:
        hn = int(h * 1e6)
        errs = {k: [] for k in cands}
        n = 0
        for j in range(len(tt)):
            i = np.searchsorted(t, tt[j]) - 1
            if i < 0:
                continue
            k = np.searchsorted(tt, tt[j] + hn)
            if k <= j + 1:
                continue
            w = tq[j:k]
            vwap = float((tp[j:k] * w).sum() / w.sum())
            n += 1
            for name, arr in cands.items():
                errs[name].append(arr[i] - vwap)
        out[h] = {name: float(np.sqrt(np.mean(np.square(e)))) if e else float("nan") for name, e in errs.items()}
        out[h]["n"] = n
    return out
