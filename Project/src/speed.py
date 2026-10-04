"""Speed project engine: periodic technical signals with delayed execution.

Follows Scholtus and van Dijk (2012), section 3:
  * a signal is built every 60 seconds from information up to the interval change;
  * the resulting order is executed `delay` later at the prevailing best bid/ask;
  * signals are acted upon from 10 minutes after the open until 10 minutes
    before the close, when every open position is unwound;
  * returns are simple, non-compounded round-trip returns on a constant book.
"""
from __future__ import annotations

import lzma
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
EQUITY_DIR = ROOT / "data" / "equities" / "databento" / "mbp-1"
FX_DIR = ROOT / "data" / "fx" / "dukascopy" / "EURUSD"

NS = 1_000_000_000
MS = 1_000_000
BOOK = 1_000_000.0  # dollars committed to every round trip
INTERVAL = 60 * NS

# Session in UTC. In September 2019 New York is UTC-4, so 13:30-20:00 UTC is
# 09:30-16:00 ET. EUR/USD is evaluated over the same window so that the three
# assets face the same clock, the same number of signals and the same news.
SESSION_OPEN = (13 * 3600 + 30 * 60) * NS
SESSION_CLOSE = 20 * 3600 * NS
NO_TRADE = 10 * 60 * NS  # no entries in the first/last 10 minutes

DELAYS_MS = (0, 100, 1000)
DELAYS_FIG6_MS = (0, 10, 20, 50, 100, 200, 500, 1000)


@dataclass
class Day:
    """One asset-day: the top of book and the signed-volume tape."""

    date: pd.Timestamp
    ts: np.ndarray  # int64 ns since epoch, one entry per valid BBO state
    bid: np.ndarray
    ask: np.ndarray
    bsz: np.ndarray
    asz: np.ndarray
    tr_ts: np.ndarray  # trade (or tick-volume) timestamps
    tr_px: np.ndarray
    tr_sz: np.ndarray


# --------------------------------------------------------------------------- loaders
def equity_dates() -> list[str]:
    return sorted(p.name.split("-")[2].split(".")[0] for p in EQUITY_DIR.glob("*.dbn.zst"))


def load_equity_day(date: str) -> dict[str, Day]:
    import databento as db

    df = db.DBNStore.from_file(EQUITY_DIR / f"xnas-itch-{date}.mbp-1.dbn.zst").to_df()
    out = {}
    midnight = pd.Timestamp(date, tz="UTC").value
    lo, hi = midnight + SESSION_OPEN - 3600 * NS, midnight + SESSION_CLOSE + 60 * NS
    for sym, g in df.groupby("symbol", sort=False):
        ts = g["ts_event"].astype("int64").to_numpy()
        order = np.argsort(ts, kind="stable")
        g, ts = g.iloc[order], ts[order]
        keep = (ts >= lo) & (ts <= hi)
        g, ts = g[keep], ts[keep]
        bid, ask = g["bid_px_00"].to_numpy(), g["ask_px_00"].to_numpy()
        ok = np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask > bid)
        # Trades with no aggressor side are the opening/closing/halt crosses. They are
        # single prints of several hundred thousand shares and are left out of OBV.
        is_tr = ((g["action"] == "T") & (g["side"] != "N")).to_numpy()
        out[sym] = Day(
            date=pd.Timestamp(date),
            ts=ts[ok],
            bid=bid[ok],
            ask=ask[ok],
            bsz=g["bid_sz_00"].to_numpy()[ok].astype(float),
            asz=g["ask_sz_00"].to_numpy()[ok].astype(float),
            tr_ts=ts[is_tr],
            tr_px=g["price"].to_numpy()[is_tr],
            tr_sz=g["size"].to_numpy()[is_tr].astype(float),
        )
    return out


_BI5 = np.dtype([("ms", ">u4"), ("ask", ">u4"), ("bid", ">u4"), ("av", ">f4"), ("bv", ">f4")])


def fx_dates() -> list[str]:
    """FX days are restricted to the equity trading days so that the three
    assets are compared over the same 20 sessions (drops Labor Day, 2 Sep)."""
    days = {p.name[:8] for p in FX_DIR.glob("*.bi5") if p.stat().st_size > 0}
    return sorted(days & set(equity_dates()))


def load_fx_day(date: str) -> Day | None:
    """Dukascopy EUR/USD top of book. There is no public FX trade tape, so the
    volume input of OBV is tick volume: one unit per quote that moves the mid."""
    midnight = pd.Timestamp(date, tz="UTC").value
    parts = []
    for h in range(12, 21):
        p = FX_DIR / f"{date}_{h:02d}h_ticks.bi5"
        if not p.exists() or p.stat().st_size == 0:
            continue
        a = np.frombuffer(lzma.decompress(p.read_bytes()), dtype=_BI5)
        parts.append((midnight + h * 3600 * NS + a["ms"].astype("int64") * MS, a))
    if not parts:
        return None
    ts = np.concatenate([p[0] for p in parts])
    a = np.concatenate([p[1] for p in parts])
    bid, ask = a["bid"] / 1e5, a["ask"] / 1e5
    ok = ask > bid
    ts, bid, ask = ts[ok], bid[ok], ask[ok]
    mid = (bid + ask) / 2
    moved = np.r_[False, np.diff(mid) != 0]
    return Day(
        date=pd.Timestamp(date),
        ts=ts, bid=bid, ask=ask,
        bsz=a["bv"][ok].astype(float), asz=a["av"][ok].astype(float),
        tr_ts=ts[moved], tr_px=mid[moved], tr_sz=np.ones(int(moved.sum())),
    )


# --------------------------------------------------------------------------- features
def grid(day: Day) -> np.ndarray:
    midnight = day.date.tz_localize("UTC").value
    return np.arange(midnight + SESSION_OPEN, midnight + SESSION_CLOSE + 1, INTERVAL)


def asof(ts: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Index of the last observation with timestamp <= t (-1 if none)."""
    return np.searchsorted(ts, t, side="right") - 1


def features(day: Day) -> dict[str, np.ndarray]:
    """Mid, on-balance volume and time-weighted book imbalance at each interval change."""
    g = grid(day)
    i = asof(day.ts, g)
    mid = np.where(i >= 0, (day.bid[i] + day.ask[i]) / 2, np.nan)

    # OBV: running signed volume, +v on an uptick and -v on a downtick, session only.
    in_sess = day.tr_ts >= g[0]
    px, sz, tts = day.tr_px[in_sess], day.tr_sz[in_sess], day.tr_ts[in_sess]
    sign = np.r_[0.0, np.sign(np.diff(px))] if len(px) else np.array([])
    cum = np.r_[0.0, np.cumsum(sign * sz)]
    obv = cum[np.searchsorted(tts, g, side="right")]
    vol = np.r_[0.0, np.cumsum(sz)][np.searchsorted(tts, g, side="right")]

    # Time-weighted imbalance of the best quotes over each interval (t-1, t].
    tot = day.bsz + day.asz
    imb = np.divide(day.bsz - day.asz, tot, out=np.zeros_like(tot), where=tot > 0)
    integral = np.r_[0.0, np.cumsum(imb[:-1] * np.diff(day.ts))]
    at = np.where(i >= 0, integral[i] + imb[i] * (g - day.ts[i]), 0.0)
    twi = np.r_[np.nan, np.diff(at) / INTERVAL]
    return {"grid": g, "mid": mid, "obv": obv, "vol": vol, "imb": twi}


# --------------------------------------------------------------------------- signals
def _ma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.r_[0.0, x])
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def sig_ma(f, s: int, l: int, b: float) -> np.ndarray:
    """Paper eq. (3): short vs long moving average of the mid, with bandwidth b."""
    m = f["mid"][1:]  # interval changes after the open
    a, c = _ma(m, s), _ma(m, l)
    sig = np.where(a > (1 + b) * c, 1, np.where(a < (1 - b) * c, -1, 0))
    return np.r_[0, np.where(np.isnan(c), 0, sig)]


def sig_obv(f, s: int, l: int, b: float) -> np.ndarray:
    """Paper eq. (6): short vs long moving average of on-balance volume. OBV is
    a signed running sum that starts at zero each day, so a band proportional to
    its level (as for prices) is meaningless; the band is b times the average
    volume per interval over the long window instead."""
    o = f["obv"][1:]
    a, c = _ma(o, s), _ma(o, l)
    band = b * _ma(np.diff(f["vol"]), l)
    sig = np.where(a > c + band, 1, np.where(a < c - band, -1, 0))
    return np.r_[0, np.where(np.isnan(c), 0, sig)]


def sig_imb(f, k: int, theta: float) -> np.ndarray:
    """Own signal, persistent queue imbalance: go long (short) when the
    time-weighted imbalance of the best bid/ask sizes has stayed above theta
    (below -theta) in each of the last k one-minute intervals."""
    x = f["imb"]
    up = np.nan_to_num(x, nan=0.0) > theta
    dn = np.nan_to_num(x, nan=0.0) < -theta
    run_up = _ma(up.astype(float), k) == 1
    run_dn = _ma(dn.astype(float), k) == 1
    return np.where(run_up, 1, np.where(run_dn, -1, 0))


BASELINE = {
    "MA": (sig_ma, dict(s=2, l=10, b=0.00005)),
    "OBV": (sig_obv, dict(s=2, l=10, b=0.05)),
    "IMB": (sig_imb, dict(k=2, theta=0.2)),
}

# Small parameter universe per family, used for the Figure 6 style average.
UNIVERSE = {
    "MA": [(sig_ma, dict(s=s, l=l, b=b)) for s, l in ((1, 5), (2, 10), (5, 20)) for b in (0.0, 0.00005, 0.0001)],
    "OBV": [(sig_obv, dict(s=s, l=l, b=b)) for s, l in ((1, 5), (2, 10), (5, 20)) for b in (0.0, 0.05, 0.1)],
    "IMB": [(sig_imb, dict(k=k, theta=th)) for k in (1, 2, 3) for th in (0.1, 0.2, 0.3)],
}


# --------------------------------------------------------------------------- execution
def simulate(day: Day, g: np.ndarray, sig: np.ndarray, delay_ms: int) -> list[tuple]:
    """Round trips (entry_ts, exit_ts, direction, return, mid-to-mid return) for one asset-day.

    Buys lift the ask and sells hit the bid prevailing `delay_ms` after the
    interval change that produced the signal (paper eq. 1-2)."""
    target = sig.copy()
    target[g < g[0] + NO_TRADE] = 0
    target[g >= g[-1] - NO_TRADE] = 0  # forces the unwind 10 minutes before the close
    change = np.flatnonzero(np.diff(np.r_[0, target]) != 0)
    if not len(change):
        return []
    t_exec = g[change] + delay_ms * MS
    q = asof(day.ts, t_exec)
    bid, ask = day.bid[q], day.ask[q]
    mid = (bid + ask) / 2
    trips, pos, entry_px, entry_mid, entry_ts = [], 0, np.nan, np.nan, 0
    for j, k in enumerate(change):
        new = int(target[k])
        if pos != 0:
            exit_px = bid[j] if pos == 1 else ask[j]
            trips.append((entry_ts, t_exec[j], pos, pos * (exit_px - entry_px) / entry_px,
                          pos * (mid[j] - entry_mid) / entry_mid))
        if new != 0:
            entry_px, entry_mid, entry_ts = (ask[j] if new == 1 else bid[j]), mid[j], t_exec[j]
        pos = new
    return trips


def run_day(day: Day, rules: dict, delays=DELAYS_MS) -> list[dict]:
    """Daily result of every (rule, delay). `rules` maps a label to (fn, params)."""
    f = features(day)
    rows = []
    for label, (fn, params) in rules.items():
        sig = fn(f, **params)
        for d in delays:
            trips = simulate(day, f["grid"], sig, d)
            r = np.array([t[3] for t in trips])
            gross = sum(t[4] for t in trips)
            rows.append(dict(date=day.date, rule=label, delay_ms=d, ret=r.sum(), n_trips=len(r),
                             n_win=int((r > 0).sum()), pnl=BOOK * r.sum(), pnl_mid=BOOK * gross))
    return rows


# --------------------------------------------------------------------------- diagnostics
def microstructure(day: Day, delays_ms=(100, 1000)) -> dict:
    """Liquidity and tick-frequency statistics over the regular session."""
    g = grid(day)
    lo, hi = g[0], g[-1]
    s = (day.ts >= lo) & (day.ts < hi)
    ts, bid, ask = day.ts[s], day.bid[s], day.ask[s]
    mid = (bid + ask) / 2
    dt = np.diff(np.r_[ts, hi]).astype(float)
    secs = (hi - lo) / NS
    out = dict(
        date=day.date,
        mid=np.average(mid, weights=dt),
        spread_bps=np.average((ask - bid) / mid, weights=dt) * 1e4,
        spread_ticks=np.average(ask - bid, weights=dt),
        depth=np.average((day.bsz[s] + day.asz[s]) / 2, weights=dt),
        bbo_updates_per_s=len(ts) / secs,
        quote_moves_per_s=((np.diff(bid) != 0) | (np.diff(ask) != 0)).sum() / secs,
        trades_per_s=((day.tr_ts >= lo) & (day.tr_ts < hi)).sum() / secs,
    )
    m = (day.bid[asof(day.ts, g)] + day.ask[asof(day.ts, g)]) / 2
    out["vol_1min_bps"] = np.nanstd(np.diff(np.log(m))) * 1e4
    # What a delay does to the quote you trade against, at the interval changes.
    t0 = g[10:-10]
    i0 = asof(day.ts, t0)
    for d in delays_ms:
        i1 = asof(day.ts, t0 + d * MS)
        m0, m1 = (day.bid[i0] + day.ask[i0]) / 2, (day.bid[i1] + day.ask[i1]) / 2
        out[f"p_quote_change_{d}ms"] = np.mean((day.bid[i0] != day.bid[i1]) | (day.ask[i0] != day.ask[i1]))
        out[f"abs_mid_move_{d}ms_bps"] = np.mean(np.abs(m1 / m0 - 1)) * 1e4
    return out


# --------------------------------------------------------------------------- driver
def iter_days(asset: str, _eq_cache: dict = {}):
    """Yield the Day objects of one asset. Equity files hold both tickers, so
    decoded days are kept in memory and reused for the second ticker."""
    if asset == "EURUSD":
        for d in fx_dates():
            day = load_fx_day(d)
            if day is not None and len(day.ts) > 1000:
                yield day
    else:
        for d in equity_dates():
            if d not in _eq_cache:
                _eq_cache[d] = load_equity_day(d)
            yield _eq_cache[d][asset]


def run_all(assets=("AAPL", "GPRO", "EURUSD"), delays=DELAYS_FIG6_MS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (daily results for every rule/config/delay, daily microstructure stats)."""
    rules = {(fam, "baseline", str(p)): (fn, p) for fam, (fn, p) in BASELINE.items()}
    for fam, lst in UNIVERSE.items():
        for fn, p in lst:
            rules[(fam, "universe", str(p))] = (fn, p)

    res, micro = [], []
    for asset in assets:
        for day in iter_days(asset):
            for row in run_day(day, rules, delays):
                fam, kind, params = row.pop("rule")
                res.append(dict(asset=asset, family=fam, kind=kind, params=params, **row))
            micro.append(dict(asset=asset, **microstructure(day)))
    return pd.DataFrame(res), pd.DataFrame(micro)


# --------------------------------------------------------------------------- reporting
def pnl_report(res: pd.DataFrame, delays=DELAYS_MS) -> pd.DataFrame:
    """One P&L line per asset / signal / delay for the baseline rules."""
    b = res[(res.kind == "baseline") & res.delay_ms.isin(delays)]
    g = b.groupby(["asset", "family", "delay_ms"], sort=False)
    out = g.agg(pnl=("pnl", "sum"), pnl_mid=("pnl_mid", "sum"), round_trips=("n_trips", "sum"), wins=("n_win", "sum"),
                days=("date", "nunique"), best_day=("pnl", "max"), worst_day=("pnl", "min"),
                daily_std=("pnl", "std")).reset_index()
    out["total_return_pct"] = out.pnl / BOOK * 100
    out["final_book"] = BOOK + out.pnl
    out["spread_paid"] = out.pnl_mid - out.pnl
    out["bps_per_trip"] = out.pnl / BOOK / out.round_trips * 1e4
    out["win_rate_pct"] = out.wins / out.round_trips * 100
    return out


def cost_of_delay(res: pd.DataFrame, delays=(100, 1000)) -> pd.DataFrame:
    """Cost of delay of the baseline rules: P&L given up relative to instantaneous execution."""
    from scipy.stats import wilcoxon

    b = res[res.kind == "baseline"]
    piv = b.pivot_table(index=["asset", "family", "date"], columns="delay_ms", values="pnl", sort=False)
    trips = b[b.delay_ms == 0].groupby(["asset", "family"], sort=False).n_trips.sum()
    rows = []
    for (asset, fam), d in piv.groupby(level=[0, 1], sort=False):
        for dl in delays:
            diff = d[dl] - d[0]
            nz = diff[diff != 0]
            rows.append(dict(
                asset=asset, family=fam, delay_ms=dl,
                pnl_0ms=d[0].sum(), pnl_delayed=d[dl].sum(), cost_usd=-diff.sum(),
                cost_bps_per_trip=-diff.sum() / BOOK / trips[(asset, fam)] * 1e4,
                cost_pct_of_abs_pnl=-diff.sum() / abs(d[0].sum()) * 100,
                days_worse=int((diff < 0).sum()), days_better=int((diff > 0).sum()),
                wilcoxon_p=wilcoxon(nz).pvalue if len(nz) >= 6 else np.nan,
            ))
    return pd.DataFrame(rows)


def importance_of_speed(res: pd.DataFrame, by=("asset",), subset: str = "all") -> pd.DataFrame:
    """Figure 6 of the paper. Each day the average return per strategy with delay d
    is compared with the average under instantaneous execution; the daily relative
    differences (in %) are then averaged over days. Strategies with a zero return
    are excluded. `subset` keeps all strategies or only those with a positive or
    negative instantaneous return that day (the paper's Figure 7 split)."""
    from scipy.stats import wilcoxon

    u = res[res.kind == "universe"]
    keys = list(by)
    w = u.pivot_table(index=keys + [k for k in ("family", "params", "date") if k not in keys], columns="delay_ms", values="ret", sort=False)
    w = w[w[0] != 0]
    if subset == "positive":
        w = w[w[0] > 0]
    elif subset == "negative":
        w = w[w[0] < 0]
    daily = w.groupby(level=keys + ["date"], sort=False).mean()
    rows = []
    for key, d in daily.groupby(level=keys, sort=False):
        key = key if isinstance(key, tuple) else (key,)
        for dl in [c for c in d.columns if c != 0]:
            rel = (d[dl] - d[0]) / d[0].abs() * 100
            diff = (d[dl] - d[0])
            nz = diff[diff != 0]
            rows.append(dict(zip(keys, key), delay_ms=dl, importance_pct=rel.mean(),
                             median_pct=rel.median(),
                             pooled_pct=diff.sum() / abs(d[0].sum()) * 100,
                             diff_bps_per_day=diff.mean() * 1e4, n_days=len(d),
                             wilcoxon_p=wilcoxon(nz).pvalue if len(nz) >= 6 else np.nan))
    return pd.DataFrame(rows)
