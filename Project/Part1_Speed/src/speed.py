"""Speed project engine: periodic technical signals with delayed execution.

Follows Scholtus and van Dijk (2012), section 3:
  * a signal is built every 60 seconds from information up to the interval change;
  * the resulting order is executed `delay` later at the prevailing best bid/ask;
  * signals are acted upon from 10 minutes after the open until 10 minutes
    before the close, when every open position is unwound;
  * returns are simple, non-compounded round-trip returns on a constant book.

Following the discussion-session instructions, every trade is for a fixed
number of units: the quantity worth $1,000,000 on the asset's first day.

The execution code is vectorised over rules and delays: all the round trips of
all the rules of an asset-day are held in flat arrays, which is what makes the
600-rule universe and the placebo test of the selection bias cheap to run.
"""
from __future__ import annotations

import lzma
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
EQUITY_DIR = ROOT / "data" / "equities" / "databento" / "mbp-1"
FX_DIR = ROOT / "data" / "fx" / "dukascopy" / "EURUSD"

NS = 1_000_000_000
MS = 1_000_000
BOOK = 1_000_000.0  # dollars; fixes the number of units traded (see first_day_units)
INTERVAL = 60 * NS

# Session in UTC. In September 2019 New York is UTC-4, so 13:30-20:00 UTC is
# 09:30-16:00 ET. EUR/USD is evaluated over the same window so that the three
# assets face the same clock, the same number of signals and the same news.
SESSION_OPEN = (13 * 3600 + 30 * 60) * NS
SESSION_CLOSE = 20 * 3600 * NS
NO_TRADE = 10 * 60 * NS  # no entries in the first/last 10 minutes

ASSETS = ("AAPL", "GPRO", "EURUSD")
DELAYS_MS = (0, 100, 1000)
DELAYS_FIG6_MS = (0, 10, 20, 50, 100, 200, 500, 1000)  # the paper's eight speed levels
DELAYS_CURVE_MS = (0, 10, 20, 50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000)
# Horizons of the per-order price response, beyond the delays that are traded.
HORIZONS_MS = (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000)


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
    volume input of OBV is tick volume: one unit per quote that moves the mid.
    Quoted sizes are in millions of euros."""
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


def iter_asset_days(assets=ASSETS):
    """Yield (asset, Day) pairs. An equity file holds both tickers, so the two
    stocks are produced from the same decoded file and nothing is kept in memory."""
    stocks = [a for a in assets if a != "EURUSD"]
    if stocks:
        for d in equity_dates():
            loaded = load_equity_day(d)
            for a in stocks:
                yield a, loaded[a]
    if "EURUSD" in assets:
        for d in fx_dates():
            day = load_fx_day(d)
            if day is not None and len(day.ts) > 1000:
                yield "EURUSD", day


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


# The three baseline rules behind the 27 P&L reports. Lookbacks and bandwidths
# are values of the paper's 60-second settings (Appendix A).
BASELINE = {
    "MA": (sig_ma, dict(s=5, l=30, b=0.0005)),
    "OBV": (sig_obv, dict(s=5, l=30, b=0.5)),
    "IMB": (sig_imb, dict(k=2, theta=0.3)),
}

# Rule universe for the Figure 6 / Figure 7 analysis. MA and OBV use every
# (short, long, band) combination of the paper's 60-second grid, with its two
# universal filters at their neutral values (d = 0, h = 1): 288 rules each.
# The OBV bands are in units of average one-minute volume (see sig_obv).
_SL = [(s, l) for l in (10, 15, 20, 25, 30, 35, 40, 45, 60) for s in (2, 5, 10, 15, 20, 25, 30) if s < l]
UNIVERSE = {
    "MA": [(sig_ma, dict(s=s, l=l, b=b)) for s, l in _SL for b in (0.0, 0.0005, 0.001, 0.0015, 0.0025, 0.004)],
    "OBV": [(sig_obv, dict(s=s, l=l, b=b)) for s, l in _SL for b in (0.0, 0.1, 0.25, 0.5, 1.0, 1.5)],
    "IMB": [(sig_imb, dict(k=k, theta=th)) for k in (1, 2, 3, 4, 5) for th in (0.1, 0.2, 0.3, 0.4, 0.5)],
}


# --------------------------------------------------------------------------- execution
def target_paths(f: dict, rules: list) -> np.ndarray:
    """Position each rule wants at each interval change, shape (rules, intervals).
    Nothing is held in the first and last 10 minutes, which forces the unwind."""
    g = f["grid"]
    t = np.stack([fn(f, **p) for fn, p in rules]).astype(np.int8)
    t[:, g < g[0] + NO_TRADE] = 0
    t[:, g >= g[-1] - NO_TRADE] = 0
    return t


def extract_trips(targets: np.ndarray) -> dict[str, np.ndarray]:
    """Round trips and orders implied by the target paths, as flat arrays over
    all rules. A round trip runs from the interval c0 where a position is taken
    to the interval c1 of the next change of that rule; an order is any change
    of position (delta = +-1, or +-2 when the rule flips side)."""
    d = np.diff(targets.astype(np.int16), axis=1, prepend=0)
    rule, k = np.nonzero(d)  # ordered by rule, then by time
    pos = targets[rule, k]
    held = np.flatnonzero(pos != 0)  # every path ends flat, so the next change is the same rule's
    return dict(rule=rule[held], c0=k[held], c1=k[held + 1], side=pos[held].astype(float),
                o_rule=rule, o_k=k, o_delta=d[rule, k].astype(float))


def quotes_at(day: Day, g: np.ndarray, offsets_ms) -> tuple[np.ndarray, np.ndarray]:
    """Best bid and ask prevailing `offset` after each interval change, shape (offsets, intervals)."""
    t = g[None, :] + np.asarray(offsets_ms, dtype=np.int64)[:, None] * MS
    q = asof(day.ts, t)
    return day.bid[q], day.ask[q]


def _by_rule(rule: np.ndarray, x: np.ndarray, n_rules: int) -> np.ndarray:
    """Sum the rows of x (offsets, events) within each rule: shape (rules, offsets)."""
    return np.stack([np.bincount(rule, weights=row, minlength=n_rules) for row in np.atleast_2d(x)], axis=1)


def trip_returns(trips: dict, bid: np.ndarray, ask: np.ndarray, n_rules: int) -> np.ndarray:
    """Sum of simple round-trip returns of each rule at each delay (paper eq. 1-2):
    buys lift the ask and sells hit the bid prevailing after the delay."""
    long_ = trips["side"] > 0
    entry = np.where(long_, ask[:, trips["c0"]], bid[:, trips["c0"]])
    exit_ = np.where(long_, bid[:, trips["c1"]], ask[:, trips["c1"]])
    return _by_rule(trips["rule"], trips["side"] * (exit_ - entry) / entry, n_rules)


def evaluate(trips: dict, bid: np.ndarray, ask: np.ndarray, n_rules: int) -> dict[str, np.ndarray]:
    """Everything the P&L reports need, per rule and delay (or per rule only)."""
    rule, side, c0, c1 = trips["rule"], trips["side"], trips["c0"], trips["c1"]
    long_ = side > 0
    entry = np.where(long_, ask[:, c0], bid[:, c0])
    exit_ = np.where(long_, bid[:, c1], ask[:, c1])
    mid = (bid + ask) / 2
    gross = side * (exit_ - entry)
    minutes = (c1 - c0) * (INTERVAL / (60 * NS))
    count = lambda w: np.bincount(rule, weights=w, minlength=n_rules)
    return dict(
        ret=_by_rule(rule, gross / entry, n_rules),
        pnl_unit=_by_rule(rule, gross, n_rules),  # dollars per unit traded
        pnl_mid_unit=_by_rule(rule, side * (mid[:, c1] - mid[:, c0]), n_rules),
        n_win=_by_rule(rule, (gross > 0).astype(float), n_rules),
        n_trips=count(np.ones(len(rule))), n_long=count(long_.astype(float)), n_short=count((~long_).astype(float)),
        min_long=count(minutes * long_), min_short=count(minutes * ~long_),
    )


def order_effect(trips: dict, bid0, ask0, bid_h, ask_h, n_rules: int) -> tuple[np.ndarray, np.ndarray]:
    """Effect of executing each order `h` later, in bps of the price, summed per
    rule: -(price move in the direction of the order). Negative means the delay
    hurts: the ask rose before a buy or the bid fell before a sell. A flip of
    side counts as two orders. Returns (sum of effects (rules, horizons), orders (rules,))."""
    k, delta = trips["o_k"], trips["o_delta"]
    buy = delta > 0
    p0 = np.where(buy, ask0[k], bid0[k])
    ph = np.where(buy, ask_h[:, k], bid_h[:, k])
    eff = -delta * (ph - p0) / p0 * 1e4  # delta carries both the direction and the size
    return _by_rule(trips["o_rule"], eff, n_rules), np.bincount(trips["o_rule"], weights=np.abs(delta), minlength=n_rules)


def first_day_units(day: Day) -> float:
    """Units worth $1,000,000 at the mid of the first tradable interval (09:40) of the first day."""
    t = grid(day)[0] + NO_TRADE
    i = asof(day.ts, t)
    return BOOK / ((day.bid[i] + day.ask[i]) / 2)


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
@dataclass
class Run:
    """Results of every rule on every asset-day. Arrays in `stats` are indexed
    (rule, day, delay), (rule, day, horizon) or (rule, day); `rules` describes the rule axis."""

    rules: pd.DataFrame  # family, kind ("baseline" / "universe"), params
    delays: tuple
    horizons: tuple
    dates: dict = field(default_factory=dict)  # asset -> list of dates
    units: dict = field(default_factory=dict)  # asset -> units traded per signal
    stats: dict = field(default_factory=dict)  # asset -> name -> array
    prep: dict = field(default_factory=dict)  # asset -> per day: trips and quotes, for the placebo
    micro: pd.DataFrame | None = None

    def rule_mask(self, kind: str, family: str | None = None) -> np.ndarray:
        m = (self.rules.kind == kind).to_numpy()
        return m & (self.rules.family == family).to_numpy() if family else m

    def delay_index(self, delays) -> list[int]:
        return [self.delays.index(d) for d in delays]


def run_all(assets=ASSETS, delays=DELAYS_CURVE_MS, horizons=HORIZONS_MS) -> Run:
    """Run the three baseline rules and the whole universe on every asset-day."""
    spec = [(fam, "baseline", fn, p) for fam, (fn, p) in BASELINE.items()]
    spec += [(fam, "universe", fn, p) for fam, lst in UNIVERSE.items() for fn, p in lst]
    run = Run(rules=pd.DataFrame([(fam, kind, str(p)) for fam, kind, _, p in spec], columns=["family", "kind", "params"]),
              delays=tuple(delays), horizons=tuple(horizons))
    n_rules, fns = len(spec), [(fn, p) for _, _, fn, p in spec]
    acc = {a: {} for a in assets}
    micro = []
    for asset, day in iter_asset_days(assets):
        run.units.setdefault(asset, first_day_units(day))
        run.dates.setdefault(asset, []).append(day.date)
        f = features(day)
        trips = extract_trips(target_paths(f, fns))
        bid, ask = quotes_at(day, f["grid"], delays)
        bid_h, ask_h = quotes_at(day, f["grid"], horizons)
        out = evaluate(trips, bid, ask, n_rules)
        out["effect"], out["n_orders"] = order_effect(trips, bid[0], ask[0], bid_h, ask_h, n_rules)
        for name, x in out.items():
            acc[asset].setdefault(name, []).append(x)
        run.prep.setdefault(asset, []).append(dict(trips=trips, bid=bid, ask=ask))
        micro.append(dict(asset=asset, units=run.units[asset], **microstructure(day)))
    for asset in assets:
        s = {name: np.stack(x, axis=1) for name, x in acc[asset].items()}
        s["pnl"], s["pnl_mid"] = s.pop("pnl_unit") * run.units[asset], s.pop("pnl_mid_unit") * run.units[asset]
        run.stats[asset] = s
    m = pd.DataFrame(micro)
    run.micro = pd.concat([m[m.asset == a] for a in assets], ignore_index=True)
    return run


def baseline_frame(run: Run) -> pd.DataFrame:
    """Tidy daily results of the nine baseline strategies: one row per asset / signal / day / delay."""
    rows = []
    for asset, s in run.stats.items():
        for r in np.flatnonzero(run.rule_mask("baseline")):
            for d, date in enumerate(run.dates[asset]):
                for x, delay in enumerate(run.delays):
                    rows.append(dict(
                        asset=asset, family=run.rules.family[r], params=run.rules.params[r], date=date, delay_ms=delay,
                        ret=s["ret"][r, d, x], n_trips=int(s["n_trips"][r, d]), n_win=int(s["n_win"][r, d, x]),
                        pnl=s["pnl"][r, d, x], pnl_mid=s["pnl_mid"][r, d, x],
                        n_long=int(s["n_long"][r, d]), n_short=int(s["n_short"][r, d]),
                        min_long=s["min_long"][r, d], min_short=s["min_short"][r, d]))
    return pd.DataFrame(rows)


def universe_frame(run: Run, delays=DELAYS_FIG6_MS) -> pd.DataFrame:
    """Daily return of every universe rule at the paper's delays, one row per asset / rule / day."""
    ix, parts = run.delay_index(delays), []
    for asset, s in run.stats.items():
        for r in np.flatnonzero(run.rule_mask("universe")):
            df = pd.DataFrame(s["ret"][r][:, ix], columns=[f"ret_{d}ms" for d in delays])
            df.insert(0, "n_trips", s["n_trips"][r].astype(int))
            df.insert(0, "date", run.dates[asset])
            df.insert(0, "params", run.rules.params[r])
            df.insert(0, "family", run.rules.family[r])
            df.insert(0, "asset", asset)
            parts.append(df)
    return pd.concat(parts, ignore_index=True)


# --------------------------------------------------------------------------- reporting: baseline
def _boot_days(n_days: int, n_boot: int = 5000, seed: int = 0) -> np.ndarray:
    """Day indices of `n_boot` bootstrap samples of the trading days."""
    return np.random.default_rng(seed).integers(0, n_days, size=(n_boot, n_days))


def pnl_report(base: pd.DataFrame, delays=DELAYS_MS) -> pd.DataFrame:
    """One P&L line per asset / signal / delay for the baseline rules."""
    b = base[base.delay_ms.isin(delays)]
    g = b.groupby(["asset", "family", "delay_ms"], sort=False)
    out = g.agg(pnl=("pnl", "sum"), pnl_mid=("pnl_mid", "sum"), round_trips=("n_trips", "sum"), wins=("n_win", "sum"),
                days=("date", "nunique"), best_day=("pnl", "max"), worst_day=("pnl", "min"),
                daily_std=("pnl", "std")).reset_index()
    out["total_return_pct"] = g.ret.sum().to_numpy() * 100  # sum of simple round-trip returns
    out["final_book"] = BOOK + out.pnl
    out["spread_paid"] = out.pnl_mid - out.pnl
    out["bps_per_trip"] = out.total_return_pct / out.round_trips * 100
    out["win_rate_pct"] = out.wins / out.round_trips * 100
    return out


def holding_report(base: pd.DataFrame) -> pd.DataFrame:
    """Number and average duration (minutes) of long and short positions, baseline rules, no delay."""
    b = base[base.delay_ms == 0]
    t = b.groupby(["asset", "family"], sort=False)[["n_long", "n_short", "min_long", "min_short"]].sum()
    return pd.DataFrame({
        "Average duration of long signals (min)": t.min_long / t.n_long,
        "Average duration of short signals (min)": t.min_short / t.n_short,
        "Number of long signals": t.n_long, "Number of short signals": t.n_short,
        "Share of session in a position (%)": (t.min_long + t.min_short) / (b.date.nunique() * 370) * 100,
    })


def cost_of_delay(base: pd.DataFrame, delays=(100, 1000)) -> pd.DataFrame:
    """Cost of delay of the baseline rules: total return with the delay minus total
    return with instantaneous execution. Negative means the delay hurts. The 90%
    interval resamples the 20 trading days (5,000 bootstrap draws)."""
    from scipy.stats import wilcoxon

    ret = base.pivot_table(index=["asset", "family", "date"], columns="delay_ms", values="ret", sort=False)
    pnl = base.pivot_table(index=["asset", "family", "date"], columns="delay_ms", values="pnl", sort=False)
    trips = base[base.delay_ms == 0].groupby(["asset", "family"], sort=False).n_trips.sum()
    rows = []
    for (asset, fam), d in ret.groupby(level=[0, 1], sort=False):
        p = pnl.loc[(asset, fam)]
        draws = _boot_days(len(d))
        for dl in delays:
            diff = (d[dl] - d[0]).to_numpy()
            nz = diff[diff != 0]
            lo, hi = np.percentile(diff[draws].sum(axis=1), [5, 95]) * 100
            rows.append(dict(
                asset=asset, family=fam, delay_ms=dl,
                return_0ms_pct=d[0].sum() * 100, return_delayed_pct=d[dl].sum() * 100,
                cost_pct=diff.sum() * 100, ci_lo_pct=lo, ci_hi_pct=hi, cost_usd=(p[dl] - p[0]).sum(),
                cost_bps_per_trip=diff.sum() / trips[(asset, fam)] * 1e4,
                days_worse=int((diff < 0).sum()), days_better=int((diff > 0).sum()),
                wilcoxon_p=wilcoxon(nz).pvalue if len(nz) >= 6 else np.nan,
            ))
    return pd.DataFrame(rows)


def delay_curve(base: pd.DataFrame) -> pd.DataFrame:
    """Cost of delay (%, total return with delay minus total return at 0 ms) for every
    baseline asset/signal pair and every delay level, plus the mean across pairs
    with its 90% day-bootstrap interval (the same resampled days for all pairs)."""
    tot = base.groupby(["asset", "family", "delay_ms"], sort=False).ret.sum().unstack("delay_ms") * 100
    curve = tot.sub(tot[0], axis=0).T
    curve.columns = [f"{a} | {f}" for a, f in curve.columns]
    n_pairs = curve.shape[1]
    curve["Mean"] = curve.mean(axis=1)
    per_day = base.groupby(["date", "delay_ms"], sort=False).ret.sum().unstack("delay_ms")  # summed over the pairs
    diff = per_day.sub(per_day[0], axis=0)[curve.index].to_numpy() / n_pairs * 100
    lo, hi = np.percentile(diff[_boot_days(len(diff))].sum(axis=1), [5, 95], axis=0)
    curve["Mean lo"], curve["Mean hi"] = lo, hi
    return curve


def order_effect_table(run: Run, kind: str = "baseline", horizons=(100, 1000)) -> pd.DataFrame:
    """Average effect of the delay per order (bps), by asset and signal family.
    Orders of every rule of the family are pooled; the 90% interval resamples days."""
    rows = []
    for asset, s in run.stats.items():
        draws = _boot_days(len(run.dates[asset]))
        for fam in BASELINE:
            m = run.rule_mask(kind, fam)
            num, den = s["effect"][m].sum(axis=0), s["n_orders"][m].sum(axis=0)  # (days, horizons), (days,)
            boot = num[draws].sum(axis=1) / den[draws].sum(axis=1)[:, None]
            for h in horizons:
                j = run.horizons.index(h)
                lo, hi = np.percentile(boot[:, j], [5, 95])
                rows.append(dict(asset=asset, family=fam, horizon_ms=h, orders=int(den.sum()),
                                 effect_bps=num[:, j].sum() / den.sum(), ci_lo=lo, ci_hi=hi,
                                 p_two_sided=min(1.0, 2 * min((boot[:, j] >= 0).mean(), (boot[:, j] <= 0).mean()))))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- reporting: universe
SUBSETS = {"all": lambda r0: r0 != 0, "positive": lambda r0: r0 > 0, "negative": lambda r0: r0 < 0}


def speed_stat(ret: np.ndarray, subset: str = "all") -> dict:
    """Importance of speed (paper, Figure 6 and footnote 22) from returns indexed
    (rule, day, delay), delay 0 first. Each day the average return per rule with
    a delay is compared with the average under instantaneous execution, among
    the rules of the subset (non-zero, positive or negative return at 0 ms that
    day); the daily relative differences are then averaged over days."""
    sel = SUBSETS[subset](ret[..., 0])
    n = sel.sum(axis=0)
    ok = n > 0
    if not ok.any():
        nan = np.full(ret.shape[-1], np.nan)
        return dict(importance=nan, median=nan, diff=np.empty((0, ret.shape[-1])), pooled=nan, n_days=0, rules_per_day=0.0)
    mean = np.where(sel[..., None], ret, 0.0).sum(axis=0)[ok] / n[ok, None]  # (days, delays)
    diff = mean - mean[:, :1]
    rel = diff / np.abs(mean[:, :1]) * 100
    return dict(importance=rel.mean(axis=0), median=np.median(rel, axis=0), diff=diff,
                pooled=diff.sum(axis=0) / abs(mean[:, 0].sum()) * 100,
                n_days=int(ok.sum()), rules_per_day=float(n[ok].mean()))


def _universe_groups(run: Run, by_family: bool):
    for asset, s in run.stats.items():
        for fam in (list(BASELINE) if by_family else [None]):
            yield asset, fam or "All", run.rule_mask("universe", fam), s


def importance_of_speed(run: Run, subset: str = "all", by_family: bool = False, delays=DELAYS_FIG6_MS) -> pd.DataFrame:
    """Figure 6 (all rules) and Figure 7 (positive / negative rules) of the paper."""
    from scipy.stats import wilcoxon

    ix, rows = run.delay_index(delays), []
    for asset, fam, m, s in _universe_groups(run, by_family):
        st = speed_stat(s["ret"][m][:, :, ix], subset)
        for j, dl in enumerate(delays[1:], start=1):
            nz = st["diff"][:, j][st["diff"][:, j] != 0]
            rows.append(dict(asset=asset, family=fam, subset=subset, delay_ms=dl, importance_pct=st["importance"][j],
                             median_pct=st["median"][j], pooled_pct=st["pooled"][j],
                             diff_bps_per_day=st["diff"][:, j].mean() * 1e4, n_days=st["n_days"],
                             rules_per_day=st["rules_per_day"],
                             wilcoxon_p=wilcoxon(nz).pvalue if len(nz) >= 6 else np.nan))
    return pd.DataFrame(rows)


def placebo(run: Run, n_draws: int = 1000, seed: int = 0, delays=DELAYS_FIG6_MS, by_family: bool = False) -> pd.DataFrame:
    """Selection bias, as in section 4 of the paper. Each draw applies the signals
    generated on a day to the prices of another, randomly chosen day: the rules
    keep their number of trades and holding times but carry no information.
    Splitting those random rules into winners and losers and measuring the
    importance of speed on each group gives what selection alone produces.

    Reported per asset and subset (and per signal family if `by_family`, which is
    noisy for the small families): the actual importance of speed, the mean and
    the 5th-95th percentiles over the draws, and the share of draws at or below
    the actual value (one-sided p-value for 'delay hurts more than selection alone')."""
    rng = np.random.default_rng(seed)
    ix = run.delay_index(delays)
    n_rules, uni = len(run.rules), run.rule_mask("universe")
    groups = {"All": uni}
    if by_family:
        groups.update({fam: run.rule_mask("universe", fam) for fam in BASELINE})
    rows = []
    for asset, s in run.stats.items():
        prep, n_days = run.prep[asset], len(run.dates[asset])
        sims = {(g, sub): [] for g in groups for sub in SUBSETS}
        for _ in range(n_draws):
            perm = rng.permutation(n_days)
            while (perm == np.arange(n_days)).any():  # every day gets another day's signals
                perm = rng.permutation(n_days)
            ret = np.stack([trip_returns(prep[src]["trips"], prep[d]["bid"][ix], prep[d]["ask"][ix], n_rules)
                            for d, src in enumerate(perm)], axis=1)
            for (g, sub), acc in sims.items():
                acc.append(speed_stat(ret[groups[g]], sub)["importance"])
        for (g, sub), acc in sims.items():
            sim = np.array(acc)
            actual = speed_stat(s["ret"][groups[g]][:, :, ix], sub)["importance"]
            for j, dl in enumerate(delays[1:], start=1):
                rows.append(dict(asset=asset, family=g, subset=sub, delay_ms=dl, actual_pct=actual[j],
                                 bias_pct=sim[:, j].mean(), bias_lo_pct=np.percentile(sim[:, j], 5),
                                 bias_hi_pct=np.percentile(sim[:, j], 95),
                                 p_below=(sim[:, j] <= actual[j]).mean(), draws=n_draws))
    return pd.DataFrame(rows)


def response_curve(run: Run, kind: str = "universe") -> pd.DataFrame:
    """Average effect of executing an order h later (bps per order), by asset and
    signal family, for every horizon, with a 90% day-bootstrap interval."""
    rows = []
    for asset, s in run.stats.items():
        draws = _boot_days(len(run.dates[asset]))
        for fam in BASELINE:
            m = run.rule_mask(kind, fam)
            num, den = s["effect"][m].sum(axis=0), s["n_orders"][m].sum(axis=0)
            boot = num[draws].sum(axis=1) / den[draws].sum(axis=1)[:, None]
            lo, hi = np.percentile(boot, [5, 95], axis=0)
            for j, h in enumerate(run.horizons):
                rows.append(dict(asset=asset, family=fam, horizon_ms=h, orders=int(den.sum()),
                                 effect_bps=num[:, j].sum() / den.sum(), ci_lo=lo[j], ci_hi=hi[j]))
    return pd.DataFrame(rows)


def daily_universe(run: Run) -> pd.DataFrame:
    """Per asset-day: average return of the universe rules that traded, signed cost of
    a 100 ms and 1 s delay (bps per rule-day, negative = delay hurts), mean absolute
    effect of the delay per round trip, joined with the day's microstructure."""
    i100, i1000 = run.delays.index(100), run.delays.index(1000)
    parts = []
    for asset, s in run.stats.items():
        uni = run.rule_mask("universe")
        r, trips = s["ret"][uni], s["n_trips"][uni]
        on = r[..., 0] != 0
        n = on.sum(axis=0)
        avg = lambda x: np.where(on, x, 0.0).sum(axis=0) / n * 1e4
        parts.append(pd.DataFrame(dict(
            asset=asset, date=run.dates[asset], rules_trading=n,
            ret0_bps=avg(r[..., 0]), cost100_bps=avg(r[..., i100] - r[..., 0]), cost1000_bps=avg(r[..., i1000] - r[..., 0]),
            abs100_bps=np.abs(r[..., i100] - r[..., 0]).sum(axis=0) / trips.sum(axis=0) * 1e4,
            abs1000_bps=np.abs(r[..., i1000] - r[..., 0]).sum(axis=0) / trips.sum(axis=0) * 1e4)))
    daily = pd.concat(parts, ignore_index=True).merge(run.micro, on=["asset", "date"])
    daily["vol_regime"] = daily.groupby("asset", sort=False).vol_1min_bps.transform(
        lambda x: np.where(x > x.median(), "high vol", "low vol"))
    return daily


def regime_by_signal(run: Run, daily: pd.DataFrame) -> pd.DataFrame:
    """Return at 0 ms and cost of a 1 s delay by asset, signal family and volatility
    regime: bps per rule-day over the universe rules that traded, plus the effect
    of the delay per order."""
    i1000, h1000 = run.delays.index(1000), run.horizons.index(1000)
    rows = []
    for asset, s in run.stats.items():
        regime = daily[daily.asset == asset].set_index("date").vol_regime.reindex(run.dates[asset]).to_numpy()
        for fam in BASELINE:
            m = run.rule_mask("universe", fam)
            r, eff, n_ord = s["ret"][m], s["effect"][m], s["n_orders"][m]
            for reg in ("high vol", "low vol"):
                d = regime == reg
                on = r[:, d, 0] != 0
                rows.append(dict(
                    asset=asset, family=fam, vol_regime=reg, days=int(d.sum()), rule_days=int(on.sum()),
                    ret0_bps=r[:, d, 0][on].mean() * 1e4,
                    cost1000_bps=(r[:, d, i1000] - r[:, d, 0])[on].mean() * 1e4,
                    effect_per_order_1s_bps=eff[:, d, h1000].sum() / n_ord[:, d].sum()))
    return pd.DataFrame(rows)


def family_table(run: Run) -> pd.DataFrame:
    """Universe rules by asset and signal family: return at 0 ms and signed cost of a
    100 ms and 1 s delay, in bps per rule-day over the rules that traded."""
    i100, i1000 = run.delays.index(100), run.delays.index(1000)
    rows = []
    for asset, s in run.stats.items():
        for fam in BASELINE:
            r = s["ret"][run.rule_mask("universe", fam)]
            on = r[..., 0] != 0
            rows.append(dict(asset=asset, family=fam, rules=len(r), rule_days=int(on.sum()),
                             trips_per_rule_day=s["n_trips"][run.rule_mask("universe", fam)][on].mean(),
                             ret0_bps=r[..., 0][on].mean() * 1e4,
                             cost100_bps=(r[..., i100] - r[..., 0])[on].mean() * 1e4,
                             cost1000_bps=(r[..., i1000] - r[..., 0])[on].mean() * 1e4))
    return pd.DataFrame(rows)


def capacity_table(run: Run) -> pd.DataFrame:
    """Size of the $1,000,000 order against the size displayed at the best quote.
    Dukascopy sizes are in millions of euros; equity sizes are in shares."""
    m = run.micro.groupby("asset", sort=False)[["mid", "depth", "units"]].mean()
    depth_units = np.where(m.index == "EURUSD", m.depth * 1e6, m.depth)
    return pd.DataFrame({"Units per order": m.units, "Displayed size at the best quote (units)": depth_units,
                         "Displayed size ($)": depth_units * m.mid, "Order / displayed size": m.units / depth_units})
