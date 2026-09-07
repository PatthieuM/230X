from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import time

import numpy as np
import pandas as pd


TZ = "America/New_York"
SESSION_START = "09:30:00"
SESSION_END = "16:00:00"
STOCKS = ["AAPL", "GPRO"]
FX_PAIRS = ["EURUSD", "USDJPY", "EURJPY"]
FX_PRICE_SCALE = {"EURUSD": 100_000, "USDJPY": 1_000, "EURJPY": 1_000}
# Discussion Session 02, slide "Depth at twice average spread":
# ask prices <= midquote + 2*average spread, symmetrically for bids.
DEPTH_BAND_MULTIPLIER = 2.0
TIMINGS: dict[str, float] = {}


def time_block(name: str):
    """Record wall-clock seconds for a named load or analysis step in TIMINGS."""

    class _Timer:
        def __enter__(self) -> None:
            self._start = time.perf_counter()

        def __exit__(self, exc_type, exc, tb) -> None:
            TIMINGS[name] = time.perf_counter() - self._start

    return _Timer()


def session_bounds(date: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return the regular-session [09:30, 16:00) timestamps for ``date``."""
    return (
        pd.Timestamp(f"{date} {SESSION_START}").as_unit("ns"),
        pd.Timestamp(f"{date} {SESSION_END}").as_unit("ns"),
    )


def session_grid(date: str, freq: str) -> pd.DatetimeIndex:
    """Build a left-closed regular-session grid at the requested frequency."""
    start, end = session_bounds(date)
    return pd.date_range(start, end, freq=freq, inclusive="left", unit="ns")


def _state_events_for_session(
    events: pd.DataFrame, date: str, fields: list[str]
) -> pd.DataFrame:
    """Return state-change rows clipped to the session, including a 09:30 seed."""
    start, end = session_bounds(date)
    state = events[fields].sort_index(kind="stable")
    state = state[~state.index.duplicated(keep="last")]
    seed = state.loc[state.index <= start].tail(1).copy()
    if seed.empty:
        if state.loc[(state.index > start) & (state.index < end)].empty:
            return state.iloc[0:0]
        seed = pd.DataFrame([[np.nan] * len(fields)], index=[start], columns=fields)
    else:
        seed.index = pd.DatetimeIndex([start])
    inside = state.loc[(state.index > start) & (state.index < end)]
    return pd.concat([seed, inside]).sort_index(kind="stable")


def _time_weighted_average(
    events: pd.DataFrame, field: str, date: str
) -> float:
    """Session-wide time-weighted mean of a piecewise-constant quote field."""
    state = _state_events_for_session(events, date, [field])
    if state.empty:
        return np.nan
    _, end = session_bounds(date)
    boundaries = state.index.to_numpy(dtype="datetime64[ns]").astype("int64")
    next_boundaries = np.r_[boundaries[1:], end.value]
    duration_s = (next_boundaries - boundaries) / 1e9
    values = state[field].to_numpy(dtype=float)
    valid = np.isfinite(values) & (duration_s > 0)
    return float(np.average(values[valid], weights=duration_s[valid])) if valid.any() else np.nan


def _time_weighted_minute(
    events: pd.DataFrame, fields: list[str], date: str
) -> pd.DataFrame:
    """Integrate piecewise-constant state exactly over each one-minute bin."""
    state = _state_events_for_session(events, date, fields)
    minute_index = session_grid(date, "1min")
    result = pd.DataFrame(np.nan, index=minute_index, columns=fields, dtype=float)
    if state.empty:
        return result

    start, end = session_bounds(date)
    event_ns = state.index.as_unit("ns").asi8
    end_ns = end.value
    next_ns = np.r_[event_ns[1:], end_ns]
    values = state[fields].to_numpy(dtype=float)
    valid = np.isfinite(values)
    segment_ns = (next_ns - event_ns)[:, None]
    cumulative = np.vstack([
        np.zeros(len(fields)),
        np.cumsum(np.where(valid, values, 0.0) * segment_ns, axis=0),
    ])
    valid_cumulative = np.vstack([
        np.zeros(len(fields)),
        np.cumsum(valid.astype(float) * segment_ns, axis=0),
    ])

    boundary_ns = pd.date_range(start, end, freq="1min", unit="ns").asi8
    positions = np.searchsorted(event_ns, boundary_ns, side="right") - 1
    area = np.empty((len(boundary_ns), len(fields)), dtype=float)
    valid_area = np.empty((len(boundary_ns), len(fields)), dtype=float)
    for i, (boundary, pos) in enumerate(zip(boundary_ns, positions)):
        if pos < 0:
            area[i] = np.nan
            valid_area[i] = np.nan
        elif pos >= len(event_ns):
            area[i] = cumulative[-1]
            valid_area[i] = valid_cumulative[-1]
        else:
            partial = boundary - event_ns[pos]
            area[i] = cumulative[pos] + np.where(valid[pos], values[pos], 0.0) * partial
            valid_area[i] = valid_cumulative[pos] + valid[pos].astype(float) * partial
    numerator = np.diff(area, axis=0)
    denominator = np.diff(valid_area, axis=0)
    result.loc[:, fields] = np.divide(
        numerator,
        denominator,
        out=np.full_like(numerator, np.nan),
        where=denominator > 0,
    )
    return result


def _local_event_index(frame: pd.DataFrame) -> pd.DataFrame:
    """Use exchange-event time, convert UTC to New York, and remove the timezone."""
    out = frame.copy()
    if "ts_event" not in out.columns:
        raise KeyError("Databento frame does not contain ts_event")
    idx = pd.DatetimeIndex(pd.to_datetime(out["ts_event"], utc=True))
    out.index = idx.tz_convert(TZ).tz_localize(None).as_unit("ns")
    out.index.name = "ts"
    return out


def _dbn_frame_iterator(path: str | Path, chunk_records: int = 250_000):
    import databento as db

    store = db.DBNStore.from_file(path)
    frames = store.to_df(
        price_type="float",
        pretty_ts=True,
        map_symbols=True,
        count=chunk_records,
    )
    yield from frames


def _finish_databento_book(
    quote_parts: list[pd.DataFrame],
    date: str,
    avg_spread: float,
    band_multiplier: float = DEPTH_BAND_MULTIPLIER,
) -> pd.DataFrame:
    """Build a 1-second NASDAQ book, BBO stats, and 2×-spread band depth."""
    if not quote_parts:
        return pd.DataFrame(index=session_grid(date, "1s"))

    q = pd.concat(quote_parts).sort_index(kind="stable")
    q = q.groupby(q.index.floor("1s"), sort=True).tail(1)
    q.index = q.index.floor("1s")
    q = q[~q.index.duplicated(keep="last")]
    grid = session_grid(date, "1s")
    seed = q.loc[q.index <= grid[0]].tail(1).copy()
    if not seed.empty:
        seed.index = pd.DatetimeIndex([grid[0]])
        q = pd.concat([seed, q.loc[q.index > grid[0]]]).sort_index(kind="stable")
        q = q[~q.index.duplicated(keep="last")]
    q = q.reindex(grid).ffill()

    q["bid"] = q["bid_px_00"]
    q["ask"] = q["ask_px_00"]
    q["bidsz"] = q["bid_sz_00"].astype(float)
    q["asksz"] = q["ask_sz_00"].astype(float)
    q["mid"] = (q["bid"] + q["ask"]) / 2.0
    q["spread"] = q["ask"] - q["bid"]
    valid = (
        q["bid"].gt(0)
        & q["ask"].gt(q["bid"])
        & np.isfinite(q["mid"])
    )
    q.loc[~valid, ["bid", "ask", "bidsz", "asksz", "mid", "spread"]] = np.nan
    q[["bid", "ask", "bidsz", "asksz", "mid", "spread"]] = q[
        ["bid", "ask", "bidsz", "asksz", "mid", "spread"]
    ].ffill()

    lower = q["mid"] - band_multiplier * avg_spread
    upper = q["mid"] + band_multiplier * avg_spread
    bid_depth = np.zeros(len(q), dtype=float)
    ask_depth = np.zeros(len(q), dtype=float)
    for level in range(10):
        bid_px = q[f"bid_px_{level:02d}"]
        ask_px = q[f"ask_px_{level:02d}"]
        bid_sz = q[f"bid_sz_{level:02d}"].fillna(0.0)
        ask_sz = q[f"ask_sz_{level:02d}"].fillna(0.0)
        bid_depth += np.where(bid_px.ge(lower) & bid_px.gt(0), bid_sz, 0.0)
        ask_depth += np.where(ask_px.le(upper) & ask_px.gt(0), ask_sz, 0.0)

    q["bid_depth_2x"] = bid_depth
    q["ask_depth_2x"] = ask_depth
    q["depth_2x"] = q["bid_depth_2x"] + q["ask_depth_2x"]
    q["bid_depth"] = q["bidsz"]
    q["ask_depth"] = q["asksz"]
    q["depth"] = q["bidsz"] + q["asksz"]
    q["avg_spread_day"] = avg_spread
    q["depth_band_multiplier"] = band_multiplier
    q["depth_2x_may_be_truncated"] = (
        q["bid_px_09"].ge(lower) | q["ask_px_09"].le(upper)
    )
    return q


def load_databento_stocks(
    mbp10_path: str | Path,
    mbo_path: str | Path,
    date: str = "2019-12-30",
    symbols: list[str] | tuple[str, ...] = tuple(STOCKS),
    chunk_records: int = 250_000,
) -> dict[str, dict[str, pd.DataFrame | pd.Series]]:
    """Load Databento MBP-10/MBO without materializing either full DBN at once."""
    symbols = list(symbols)
    start, end = session_bounds(date)
    seed_start = start - pd.Timedelta(minutes=15)
    level_cols = [
        col
        for level in range(10)
        for col in (
            f"bid_px_{level:02d}",
            f"ask_px_{level:02d}",
            f"bid_sz_{level:02d}",
            f"ask_sz_{level:02d}",
        )
    ]
    quote_parts: dict[str, list[pd.DataFrame]] = defaultdict(list)
    quote_event_parts: dict[str, list[pd.DataFrame]] = defaultdict(list)
    trade_parts: dict[str, list[pd.DataFrame]] = defaultdict(list)

    for raw in _dbn_frame_iterator(mbp10_path, chunk_records):
        chunk = _local_event_index(raw)
        chunk = chunk[(chunk.index >= seed_start) & (chunk.index < end)]
        if chunk.empty:
            continue
        for symbol in symbols:
            sub = chunk[chunk["symbol"] == symbol]
            if sub.empty:
                continue

            quote = sub[level_cols]
            quote = quote[
                quote["bid_px_00"].gt(0)
                & quote["ask_px_00"].gt(quote["bid_px_00"])
            ]
            if not quote.empty:
                reduced = quote.groupby(quote.index.floor("1s"), sort=False).tail(1)
                quote_parts[symbol].append(reduced)

                events = quote[
                    ["bid_px_00", "ask_px_00", "bid_sz_00", "ask_sz_00"]
                ].rename(
                    columns={
                        "bid_px_00": "bid",
                        "ask_px_00": "ask",
                        "bid_sz_00": "bidsz",
                        "ask_sz_00": "asksz",
                    }
                )
                events["mid"] = (events["bid"] + events["ask"]) / 2.0
                events["spread"] = events["ask"] - events["bid"]
                quote_event_parts[symbol].append(events)

            trades = sub[(sub["action"] == "T") & (sub.index >= start)]
            if not trades.empty:
                trades = trades[
                    ["price", "size", "side", "bid_px_00", "ask_px_00"]
                ].copy()
                trades.rename(columns={"size": "shares"}, inplace=True)
                trades["sign"] = trades["side"].map({"B": 1.0, "A": -1.0})
                valid_trade_bbo = (
                    trades["bid_px_00"].gt(0)
                    & trades["ask_px_00"].gt(trades["bid_px_00"])
                )
                trades["mid_at_trade"] = np.where(
                    valid_trade_bbo,
                    (trades["bid_px_00"] + trades["ask_px_00"]) / 2.0,
                    np.nan,
                )
                trades.drop(columns=["bid_px_00", "ask_px_00"], inplace=True)
                trades["px_qty"] = trades["price"] * trades["shares"]
                trades["dollar"] = trades["px_qty"]
                trade_parts[symbol].append(trades)

    quote_events_by_symbol: dict[str, pd.DataFrame] = {}
    avg_spreads: dict[str, float] = {}
    for symbol in symbols:
        quote_events = (
            pd.concat(quote_event_parts[symbol]).sort_index(kind="stable")
            if quote_event_parts[symbol]
            else pd.DataFrame(
                columns=["bid", "ask", "bidsz", "asksz", "mid", "spread"]
            )
        )
        if not quote_events.empty:
            changed = (
                quote_events[["bid", "ask", "bidsz", "asksz"]]
                .ne(quote_events[["bid", "ask", "bidsz", "asksz"]].shift())
                .any(axis=1)
            )
            quote_events = quote_events.loc[changed]
        quote_events_by_symbol[symbol] = quote_events
        avg_spreads[symbol] = _time_weighted_average(
            quote_events, "spread", date
        )

    # A second streaming pass computes exact event-time liquidity means without
    # retaining all 40 top-of-book level columns in memory.
    liquidity_parts: dict[str, list[pd.DataFrame]] = defaultdict(list)
    for raw in _dbn_frame_iterator(mbp10_path, chunk_records):
        chunk = _local_event_index(raw)
        chunk = chunk[(chunk.index >= seed_start) & (chunk.index < end)]
        if chunk.empty:
            continue
        for symbol in symbols:
            q = chunk.loc[chunk["symbol"] == symbol, level_cols].copy()
            q = q[q["bid_px_00"].gt(0) & q["ask_px_00"].gt(q["bid_px_00"])]
            if q.empty:
                continue
            mid = (q["bid_px_00"] + q["ask_px_00"]) / 2.0
            lower = mid - DEPTH_BAND_MULTIPLIER * avg_spreads[symbol]
            upper = mid + DEPTH_BAND_MULTIPLIER * avg_spreads[symbol]
            bid_2x = np.zeros(len(q), dtype=float)
            ask_2x = np.zeros(len(q), dtype=float)
            for level in range(10):
                bid_2x += np.where(
                    q[f"bid_px_{level:02d}"].ge(lower),
                    q[f"bid_sz_{level:02d}"],
                    0.0,
                )
                ask_2x += np.where(
                    q[f"ask_px_{level:02d}"].le(upper)
                    & q[f"ask_px_{level:02d}"].gt(0),
                    q[f"ask_sz_{level:02d}"],
                    0.0,
                )
            metrics = pd.DataFrame(index=q.index)
            metrics["spread"] = q["ask_px_00"] - q["bid_px_00"]
            metrics["bid_depth"] = q["bid_sz_00"].astype(float)
            metrics["ask_depth"] = q["ask_sz_00"].astype(float)
            metrics["depth"] = metrics["bid_depth"] + metrics["ask_depth"]
            metrics["bid_depth_2x"] = bid_2x
            metrics["ask_depth_2x"] = ask_2x
            metrics["depth_2x"] = bid_2x + ask_2x
            liquidity_parts[symbol].append(metrics)

    new_order_counts: dict[str, pd.Series] = {
        symbol: pd.Series(dtype="int64") for symbol in symbols
    }
    add_message_counts: dict[str, pd.Series] = {
        symbol: pd.Series(dtype="int64") for symbol in symbols
    }
    carry_cancel_key: tuple[str, int, int] | None = None
    for raw in _dbn_frame_iterator(mbo_path, chunk_records):
        chunk = _local_event_index(raw)
        chunk = chunk[
            chunk["symbol"].isin(symbols)
            & (chunk.index >= start)
            & (chunk.index < end)
        ]
        if chunk.empty:
            continue
        keys = ["symbol", "channel_id", "sequence"]
        cancel_keys = set(
            map(tuple, chunk.loc[chunk["action"] == "C", keys].itertuples(index=False, name=None))
        )
        if carry_cancel_key is not None:
            cancel_keys.add(carry_cancel_key)
        adds = chunk.loc[chunk["action"] == "A"].copy()
        add_keys = list(map(tuple, adds[keys].itertuples(index=False, name=None)))
        is_replacement = np.fromiter(
            (key in cancel_keys for key in add_keys), dtype=bool, count=len(add_keys)
        )
        strict_adds = adds.loc[~is_replacement]
        for symbol in symbols:
            for source, target in (
                (adds, add_message_counts),
                (strict_adds, new_order_counts),
            ):
                s = source.loc[source["symbol"] == symbol]
                if not s.empty:
                    counts = s.groupby(s.index.floor("1min")).size().astype("int64")
                    target[symbol] = target[symbol].add(counts, fill_value=0)
        last = chunk.iloc[-1]
        carry_cancel_key = (
            (str(last["symbol"]), int(last["channel_id"]), int(last["sequence"]))
            if last["action"] == "C"
            else None
        )

    out: dict[str, dict[str, pd.DataFrame | pd.Series]] = {}
    minute_index = session_grid(date, "1min")
    for symbol in symbols:
        trades = (
            pd.concat(trade_parts[symbol]).sort_index(kind="stable")
            if trade_parts[symbol]
            else pd.DataFrame(
                columns=[
                    "price", "shares", "side", "sign", "mid_at_trade", "px_qty", "dollar"
                ]
            )
        )
        quote_events = quote_events_by_symbol[symbol]
        book_1s = _finish_databento_book(
            quote_parts[symbol], date, avg_spreads[symbol]
        )
        liquidity_events = pd.concat(liquidity_parts[symbol]).sort_index(kind="stable")
        liquidity_minute = _time_weighted_minute(
            liquidity_events,
            [
                "spread", "bid_depth", "ask_depth", "depth",
                "bid_depth_2x", "ask_depth_2x", "depth_2x",
            ],
            date,
        )
        orders_min = (
            new_order_counts[symbol]
            .reindex(minute_index, fill_value=0)
            .fillna(0)
            .astype("int64")
        )
        orders_min.index.name = "ts"
        add_messages_min = (
            add_message_counts[symbol]
            .reindex(minute_index, fill_value=0)
            .fillna(0)
            .astype("int64")
        )
        add_messages_min.index.name = "ts"
        out[symbol] = {
            "trades": trades,
            "book_1s": book_1s,
            "quote_events": quote_events,
            "orders_min": orders_min,
            "order_add_messages_min": add_messages_min,
            "liquidity_minute": liquidity_minute,
            "is_stock": True,
            "date": date,
        }
    return out


def _extract_ebs_pair(
    wide: pd.DataFrame, pair: str, fields: list[str]
) -> pd.DataFrame:
    """Unpivot one currency-pair block from the wide EBS CSV."""
    shown = {"EURUSD": "EUR/USD", "USDJPY": "USD/JPY", "EURJPY": "EUR/JPY"}[pair]
    prefix = f"EBS_BOOK::{shown}."
    wanted = [prefix + field for field in fields]
    missing = [col for col in wanted if col not in wide.columns]
    if missing:
        raise KeyError(f"Missing EBS columns for {pair}: {missing}")
    out = wide[["Time", *wanted]].copy()
    out.rename(
        columns={"Time": "ts", **{prefix + field: field.lower() for field in fields}},
        inplace=True,
    )
    out.dropna(subset=["price"], inplace=True)
    out["ts"] = pd.to_datetime(
        out["ts"], format="%Y/%m/%d %H:%M:%S.%f"
    ).astype("datetime64[ns]")
    # Use exact integer price ticks as book keys.  The source has 5 decimals for
    # EUR/USD and 3 decimals for the JPY pairs.
    out["price_ticks"] = np.rint(out["price"] * FX_PRICE_SCALE[pair]).astype("int64")
    out.set_index("ts", inplace=True)
    out.sort_index(kind="stable", inplace=True)
    return out


def _ebs_quote_events(
    events: pd.DataFrame, pair: str
) -> tuple[pd.DataFrame, dict[str, np.ndarray]]:
    """Replay the EBS ladder once and record the book after every timestamp.

    Returns the per-timestamp top-of-book frame (bid, ask, sizes, mid, spread,
    L1 depth) and a compact snapshot of the full ladder at every timestamp, so
    depth-in-band can be computed afterwards, once the day's average spread is
    known, without replaying the tape a second time.  Invalid (empty, locked or
    crossed) states are kept as NaN rows so later integration cannot bridge
    them with an earlier valid quote.
    """
    from array import array

    scale = FX_PRICE_SCALE[pair]
    bids: dict[int, float] = {}
    asks: dict[int, float] = {}
    stamps: list = []
    top: list[tuple[int, int, float, float]] = []
    bid_ticks, bid_sizes = array("q"), array("d")
    ask_ticks, ask_sizes = array("q"), array("d")
    bid_off, ask_off = [0], [0]

    def record(ts) -> None:
        stamps.append(ts)
        if bids and asks:
            bid_tick, ask_tick = max(bids), min(asks)
            if ask_tick > bid_tick:
                top.append((bid_tick, ask_tick, bids[bid_tick], asks[ask_tick]))
                bid_ticks.extend(bids.keys())
                bid_sizes.extend(bids.values())
                ask_ticks.extend(asks.keys())
                ask_sizes.extend(asks.values())
                bid_off.append(len(bid_ticks))
                ask_off.append(len(ask_ticks))
                return
        # Preserve an invalid state transition so later matching/integration
        # cannot silently bridge a locked or crossed book.
        top.append((0, 0, np.nan, np.nan))
        bid_off.append(len(bid_ticks))
        ask_off.append(len(ask_ticks))

    previous = None
    for ts, side, price, size in events[
        ["buy_sell_flag", "price_ticks", "size"]
    ].itertuples(name=None):
        if previous is not None and ts != previous:
            record(previous)
        previous = ts
        book = bids if int(side) == 0 else asks
        price, size = int(price), float(size)
        if size > 0:
            book[price] = size
        else:
            book.pop(price, None)
    if previous is not None:
        record(previous)

    if not top:
        empty = pd.DataFrame(
            columns=[
                "bid", "ask", "bidsz", "asksz", "mid", "spread",
                "bid_depth", "ask_depth", "depth",
            ]
        )
        empty.index.name = "ts"
        return empty, {}

    top_arr = np.array(top, dtype=float)
    bidsz, asksz = top_arr[:, 2], top_arr[:, 3]
    valid = np.isfinite(bidsz)
    bid = np.where(valid, top_arr[:, 0] / scale, np.nan)
    ask = np.where(valid, top_arr[:, 1] / scale, np.nan)
    mid = (bid + ask) / 2.0
    frame = pd.DataFrame(
        {
            "bid": bid, "ask": ask, "bidsz": bidsz, "asksz": asksz,
            "mid": mid, "spread": ask - bid,
            "bid_depth": bidsz, "ask_depth": asksz, "depth": bidsz + asksz,
        },
        index=pd.DatetimeIndex(stamps, name="ts"),
    )
    ladders = {
        "bid_ticks": np.frombuffer(bid_ticks, dtype=np.int64),
        "bid_sizes": np.frombuffer(bid_sizes, dtype=np.float64),
        "bid_off": np.asarray(bid_off, dtype=np.int64),
        "ask_ticks": np.frombuffer(ask_ticks, dtype=np.int64),
        "ask_sizes": np.frombuffer(ask_sizes, dtype=np.float64),
        "ask_off": np.asarray(ask_off, dtype=np.int64),
    }
    return frame, ladders


def _ebs_band_depth(
    frame: pd.DataFrame,
    ladders: dict[str, np.ndarray],
    pair: str,
    avg_spread: float,
    band_multiplier: float = DEPTH_BAND_MULTIPLIER,
) -> pd.DataFrame:
    """Vectorised depth at ``mid ± band_multiplier × avg_spread`` per timestamp.

    Same rule as the notebook methodology: bid size at ticks ≥ the lower bound,
    ask size at ticks ≤ the upper bound, each side separately.  Uses the ladder
    snapshots recorded by :func:`_ebs_quote_events`, so no second replay.
    """
    out = frame.copy()
    n = len(out)
    if n == 0 or not ladders or not np.isfinite(avg_spread):
        out["bid_depth_2x"] = np.nan
        out["ask_depth_2x"] = np.nan
        out["depth_2x"] = np.nan
        return out
    scale = FX_PRICE_SCALE[pair]
    mid = out["mid"].to_numpy(dtype=float)
    valid = np.isfinite(mid)
    lower = np.ceil((mid - band_multiplier * avg_spread) * scale - 1e-9)
    upper = np.floor((mid + band_multiplier * avg_spread) * scale + 1e-9)

    def side_sum(ticks, sizes, off, bound, keep_at_or_above: bool) -> np.ndarray:
        event_id = np.repeat(np.arange(n), np.diff(off))
        if keep_at_or_above:
            mask = ticks >= bound[event_id]
        else:
            mask = ticks <= bound[event_id]
        total = np.bincount(event_id[mask], weights=sizes[mask], minlength=n)
        return np.where(valid, total, np.nan)

    out["bid_depth_2x"] = side_sum(
        ladders["bid_ticks"], ladders["bid_sizes"], ladders["bid_off"], lower, True
    )
    out["ask_depth_2x"] = side_sum(
        ladders["ask_ticks"], ladders["ask_sizes"], ladders["ask_off"], upper, False
    )
    out["depth_2x"] = out["bid_depth_2x"] + out["ask_depth_2x"]
    return out


def _replay_ebs_seconds(
    events: pd.DataFrame,
    pair: str,
    date: str,
    avg_spread: float | None = None,
) -> pd.DataFrame:
    """Advance the EBS book second by second and snapshot the executable BBO."""
    grid = session_grid(date, "1s")
    scale = FX_PRICE_SCALE[pair]
    bids: dict[int, float] = {}
    asks: dict[int, float] = {}
    iterator = iter(
        events[["buy_sell_flag", "price_ticks", "size"]].itertuples(name=None)
    )
    current = next(iterator, None)
    records = []
    for second in grid:
        cutoff = second + pd.Timedelta(seconds=1)
        while current is not None and current[0] < cutoff:
            ts, side, price, size = current
            book = bids if int(side) == 0 else asks
            price, size = int(price), float(size)
            if size > 0:
                book[price] = size
            else:
                book.pop(price, None)
            current = next(iterator, None)
        if bids and asks:
            bid_tick, ask_tick = max(bids), min(asks)
            bidsz, asksz = bids[bid_tick], asks[ask_tick]
            bid, ask = bid_tick / scale, ask_tick / scale
            spread = ask - bid
            if spread > 0:
                mid = (bid + ask) / 2.0
                if avg_spread is None:
                    bid_2x = ask_2x = np.nan
                else:
                    lower_tick = int(
                        np.ceil(
                            (mid - DEPTH_BAND_MULTIPLIER * avg_spread) * scale
                            - 1e-9
                        )
                    )
                    upper_tick = int(
                        np.floor(
                            (mid + DEPTH_BAND_MULTIPLIER * avg_spread) * scale
                            + 1e-9
                        )
                    )
                    bid_2x = sum(size for tick, size in bids.items() if tick >= lower_tick)
                    ask_2x = sum(size for tick, size in asks.items() if tick <= upper_tick)
                records.append(
                    (
                        bid,
                        ask,
                        bidsz,
                        asksz,
                        mid,
                        spread,
                        bid_2x,
                        ask_2x,
                    )
                )
                continue
        records.append((np.nan,) * 8)
    cols = [
        "bid",
        "ask",
        "bidsz",
        "asksz",
        "mid",
        "spread",
        "bid_depth_2x",
        "ask_depth_2x",
    ]
    return pd.DataFrame(records, index=grid, columns=cols)


def load_currency_data(
    order_path: str | Path,
    trade_path: str | Path,
    date: str = "2012-01-25",
) -> dict[str, dict[str, pd.DataFrame | pd.Series]]:
    """Replay the supplied EBS order and trade files into per-pair books and trades."""
    order_wide = pd.read_csv(order_path, low_memory=False)
    trade_wide = pd.read_csv(trade_path, low_memory=False)
    order_fields = ["BUY_SELL_FLAG", "PRICE", "SIZE", "NUM_PARTCP", "OMDSEQ"]
    trade_fields = [
        "BUY_SELL_FLAG",
        "PRICE",
        "SIZE",
        "TOTAL_SIZE",
        "NUM_PARTCP",
        "OMDSEQ",
    ]
    out: dict[str, dict[str, pd.DataFrame | pd.Series]] = {}
    for pair in FX_PAIRS:
        events = _extract_ebs_pair(order_wide, pair, order_fields)
        top_of_book, ladders = _ebs_quote_events(events, pair)
        avg_spread = _time_weighted_average(top_of_book, "spread", date)
        # One replay only: band depth is filled from the recorded ladders.
        quote_events = _ebs_band_depth(top_of_book, ladders, pair, avg_spread)
        del ladders
        book_1s = _replay_ebs_seconds(events, pair, date, avg_spread)
        book_1s["bid_depth"] = book_1s["bidsz"]
        book_1s["ask_depth"] = book_1s["asksz"]
        book_1s["depth"] = book_1s["bidsz"] + book_1s["asksz"]
        book_1s["depth_2x"] = (
            book_1s["bid_depth_2x"] + book_1s["ask_depth_2x"]
        )
        book_1s["avg_spread_day"] = avg_spread
        book_1s["depth_band_multiplier"] = DEPTH_BAND_MULTIPLIER
        liquidity_minute = _time_weighted_minute(
            quote_events,
            [
                "spread", "bid_depth", "ask_depth", "depth",
                "bid_depth_2x", "ask_depth_2x", "depth_2x",
            ],
            date,
        )

        trades = _extract_ebs_pair(trade_wide, pair, trade_fields)
        trades.rename(
            columns={
                "size": "execution_slice_size",
                "total_size": "shares",
                "buy_sell_flag": "side",
            },
            inplace=True,
        )
        trades["sign"] = trades["side"].map({0.0: -1.0, 1.0: 1.0})
        trades["px_qty"] = trades["price"] * trades["shares"]
        if pair == "EURUSD":
            trades["dollar"] = trades["px_qty"]
        elif pair == "USDJPY":
            trades["dollar"] = trades["shares"]
        else:
            trades["dollar"] = np.nan  # filled after USD/JPY is available
        out[pair] = {
            "trades": trades,
            "book_1s": book_1s,
            "quote_events": quote_events,
            "orders_min": pd.Series(dtype="int64"),
            "liquidity_minute": liquidity_minute,
            "is_stock": False,
            "date": date,
        }

    # EUR/JPY quote notional is JPY; convert it to USD with the same-second USD/JPY mid.
    ej = out["EURJPY"]["trades"]
    uj_mid = out["USDJPY"]["book_1s"]["mid"]
    trade_seconds = ej.index.floor("1s")
    conversion = uj_mid.reindex(trade_seconds).to_numpy()
    ej["dollar"] = ej["px_qty"].to_numpy() / conversion
    return out


def _impact_observations(
    trades: pd.DataFrame, quote_events: pd.DataFrame
) -> pd.DataFrame:
    """Per signed trade: 5-second midpoint return, trade sign and dollar size.

    ``mid(t)`` is the pre-trade BBO carried on the trade record when the feed
    supplies it, otherwise the latest book state strictly before the trade;
    ``mid(t+5s)`` is the latest state at or before the horizon.
    """
    signed = trades.dropna(subset=["sign", "price"])
    # Keep invalid/crossed state rows: dropping them would incorrectly carry an
    # earlier valid quote through a crossed interval.
    quotes = quote_events.sort_index(kind="stable")
    if signed.empty or quotes.empty:
        return pd.DataFrame(columns=["impact", "sign", "dollar"])
    qt = quotes.index.as_unit("ns").asi8
    tt = signed.index.as_unit("ns").asi8
    after = tt + 5_000_000_000
    pos0 = np.searchsorted(qt, tt, side="left") - 1
    pos5 = np.searchsorted(qt, after, side="right") - 1
    valid = (pos0 >= 0) & (pos5 >= 0)
    m = quotes["mid"].to_numpy()
    matched_m0 = m[np.clip(pos0, 0, len(m) - 1)]
    if "mid_at_trade" in signed.columns:
        direct_m0 = signed["mid_at_trade"].to_numpy(dtype=float)
        m0 = np.where(np.isfinite(direct_m0), direct_m0, matched_m0)
    else:
        m0 = matched_m0
    m5 = m[np.clip(pos5, 0, len(m) - 1)]
    impact = np.where(valid & (m0 > 0), (m5 - m0) / m0, np.nan)
    dollar = (
        signed["dollar"].to_numpy(dtype=float)
        if "dollar" in signed.columns
        else np.full(len(signed), np.nan)
    )
    return pd.DataFrame(
        {"impact": impact, "sign": signed["sign"].to_numpy(), "dollar": dollar},
        index=signed.index,
    ).dropna(subset=["impact", "sign"])


def _impact_slope(group: pd.DataFrame, min_trades: int = 5) -> float:
    """OLS slope of 5-second impact on trade sign (with intercept)."""
    if len(group) < min_trades or group["sign"].nunique() < 2:
        return np.nan
    x = group["sign"].to_numpy(dtype=float)
    y = group["impact"].to_numpy(dtype=float)
    x = x - x.mean()
    denom = float(x @ x)
    return float(x @ (y - y.mean()) / denom) if denom > 0 else np.nan


def _impact_by_minute(
    trades: pd.DataFrame, quote_events: pd.DataFrame, date: str
) -> pd.Series:
    """Estimate one 5-second signed midpoint-impact slope in each minute."""
    output = pd.Series(np.nan, index=session_grid(date, "1min"), dtype=float)
    regression = _impact_observations(trades, quote_events)
    if regression.empty:
        return output
    values = regression.groupby(regression.index.floor("1min")).apply(_impact_slope)
    return values.reindex(output.index)


def impact_by_size_tercile(
    trades: pd.DataFrame, quote_events: pd.DataFrame
) -> pd.DataFrame:
    """Day-pooled 5-second impact slope by dollar-size tercile of the trade.

    Discussion 2: larger trades should be more informed, so the slope should
    rise across terciles if that holds on this tape.
    """
    obs = _impact_observations(trades, quote_events).dropna(subset=["dollar"])
    if obs.empty:
        return pd.DataFrame(columns=["n", "dollar_min", "dollar_max", "lambda"])
    obs = obs.copy()
    obs["tercile"] = pd.qcut(
        obs["dollar"].rank(method="first"), 3, labels=["small", "medium", "large"]
    )
    rows = []
    for label, group in obs.groupby("tercile", observed=True):
        rows.append({
            "tercile": label,
            "n": int(len(group)),
            "dollar_min": float(group["dollar"].min()),
            "dollar_max": float(group["dollar"].max()),
            "lambda": _impact_slope(group),
        })
    return pd.DataFrame(rows).set_index("tercile")


def minute_stats(data: dict[str, object]) -> pd.DataFrame:
    """Build the per-minute (a)–(g) table for one instrument."""
    date = str(data["date"])
    index = session_grid(date, "1min")
    trades = data["trades"]
    book = data["book_1s"]
    out = pd.DataFrame(index=index)

    trade_groups = trades.resample("1min")
    out["dollar_vol"] = trade_groups["dollar"].sum().reindex(index, fill_value=0.0)
    out["n_trades"] = trade_groups["price"].count().reindex(index, fill_value=0)
    out["open"] = trade_groups["price"].first().reindex(index)
    out["close"] = trade_groups["price"].last().reindex(index)
    out["high"] = trade_groups["price"].max().reindex(index)
    out["low"] = trade_groups["price"].min().reindex(index)
    qty = trade_groups["shares"].sum().reindex(index)
    out["vwap"] = trade_groups["px_qty"].sum().reindex(index) / qty

    book_fields = [
        "spread",
        "bid_depth",
        "ask_depth",
        "depth",
        "bid_depth_2x",
        "ask_depth_2x",
        "depth_2x",
    ]
    if "liquidity_minute" in data:
        out[book_fields] = data["liquidity_minute"][book_fields].reindex(index)
    else:
        out[book_fields] = book[book_fields].resample("1min").mean().reindex(index)
    if bool(data["is_stock"]):
        out["n_orders"] = data["orders_min"].reindex(index, fill_value=0).astype(int)
    out["price_impact_5s"] = _impact_by_minute(
        trades, data["quote_events"], date
    )

    # Missing trades as a liquidity measure: seconds in the minute with no
    # print, and the longest print-free gap inside the minute (60 s when the
    # minute is silent).  Trade arrival is the flip side of depth: a book
    # nobody hits is not being tested.
    start, end = session_bounds(date)
    trade_ns = np.sort(trades.index.as_unit("ns").asi8)
    trade_ns = trade_ns[(trade_ns >= start.value) & (trade_ns < end.value)]
    bounds = pd.date_range(start, end, freq="1min", unit="ns").asi8
    active_seconds = np.zeros(len(index), dtype=int)
    max_gap = np.full(len(index), 60.0)
    if len(trade_ns):
        seconds = np.unique(trade_ns // 1_000_000_000)
        minute_of_second = np.searchsorted(bounds // 1_000_000_000, seconds, side="right") - 1
        counts = np.bincount(minute_of_second, minlength=len(index))
        active_seconds = counts[: len(index)]
        lo_pos = np.searchsorted(trade_ns, bounds[:-1], side="left")
        hi_pos = np.searchsorted(trade_ns, bounds[1:], side="left")
        for i in range(len(index)):
            inside = trade_ns[lo_pos[i]:hi_pos[i]]
            points = np.concatenate(([bounds[i]], inside, [bounds[i + 1]]))
            max_gap[i] = np.diff(points).max() / 1e9
    out["no_trade_seconds"] = 60 - active_seconds
    out["max_trade_gap_s"] = max_gap
    return out


def activity_profile_5min(minute: pd.DataFrame) -> pd.DataFrame:
    """Trade activity per 5-minute bin, normalised by its day mean.

    Columns: n_trades, dollar_vol and (stocks) n_orders, each divided by the
    mean 5-minute value so all instruments share one axis; 1.0 = an average
    bin, > 1 busier than average.
    """
    cols = [c for c in ("n_trades", "dollar_vol", "n_orders") if c in minute]
    five = minute[cols].resample("5min").sum()
    return five / five.mean()


def intraday_activity_ratios(minute: pd.DataFrame) -> dict[str, float]:
    """Open / lunch / close activity relative to the rest of the day.

    For n_trades, dollar_vol and (stocks) n_orders: mean per minute inside the
    window divided by the mean outside it.  A U-shape is open > 1, close > 1
    and lunch < 1.
    """
    windows = {
        "open30": ("09:30", "10:00"),
        "lunch": ("12:00", "13:30"),
        "last30": ("15:30", "16:00"),
    }
    out: dict[str, float] = {}
    for col in (c for c in ("n_trades", "dollar_vol", "n_orders") if c in minute):
        for name, (a, b) in windows.items():
            inside = minute.between_time(a, b, inclusive="left")[col]
            outside = minute.loc[~minute.index.isin(inside.index), col]
            out[f"{col}_{name}_over_rest"] = (
                float(inside.mean() / outside.mean()) if outside.mean() else np.nan
            )
    return out


def price_series(data: dict[str, object], freq: str) -> pd.DataFrame:
    """Last midquote and trade price on ``freq``, plus log returns."""
    date = str(data["date"])
    grid = session_grid(date, freq)
    mid = data["book_1s"]["mid"].resample(freq).last().reindex(grid).ffill()
    last_trade = data["trades"]["price"].resample(freq).last().reindex(grid)
    out = pd.DataFrame({"mid": mid, "trade": last_trade.ffill()})
    # A bar with no print carries the previous trade forward, so its
    # transaction return is an exact zero by construction, not an observation.
    out["trade_is_stale"] = last_trade.isna()
    out["mid_ret"] = np.log(out["mid"]).diff()
    out["trade_ret"] = np.log(out["trade"]).diff()
    return out


def nonstale_trade_returns(prices: pd.DataFrame) -> pd.Series:
    """Log returns between consecutive bars that actually contain a trade.

    Drops the forward-filled bars flagged by :func:`price_series`, so the
    series is the trade-to-trade return sampled on the bar grid: no mechanical
    zeros from non-synchronous trading.
    """
    actual = prices.loc[~prices["trade_is_stale"], "trade"].dropna()
    return np.log(actual).diff().dropna()


def realized_variance(series: pd.Series) -> float:
    """Sum of squared log returns, skipping missing values."""
    values = pd.Series(series).dropna().to_numpy(dtype=float)
    return float(values @ values)


def autocorrelations(series: pd.Series, lags: int = 10) -> pd.Series:
    """Sample ACF at lags 0..``lags``. Lag 0 is 1 by construction."""
    values = pd.Series(series).dropna()
    return pd.Series(
        [1.0] + [values.autocorr(lag=lag) for lag in range(1, lags + 1)],
        index=range(lags + 1),
        name="acf",
    )


def transaction_diagnostics_30min(data: dict[str, object]) -> pd.DataFrame:
    """Variance and lag-1 autocorrelation of 1-minute transaction returns."""
    returns = price_series(data, "1min")["trade_ret"]
    groups = returns.groupby(returns.index.floor("30min"))
    out = pd.DataFrame(
        {
            "variance": groups.var(),
            "autocorr_lag1": groups.apply(
                lambda x: x.dropna().autocorr(lag=1) if x.notna().sum() >= 3 else np.nan
            ),
            "n_returns": groups.count(),
        }
    )
    return out.reindex(session_grid(str(data["date"]), "30min"))


def summarize_instrument(name: str, data: dict[str, object]) -> tuple[pd.Series, pd.DataFrame]:
    """Return a flat summary Series and the underlying per-minute table."""
    per_minute = minute_stats(data)
    result: dict[str, float | str] = {"instrument": name}
    fields = [
        "dollar_vol",
        "n_trades",
        "open",
        "close",
        "high",
        "low",
        "vwap",
        "spread",
        "bid_depth",
        "ask_depth",
        "depth",
        "bid_depth_2x",
        "ask_depth_2x",
        "depth_2x",
        "price_impact_5s",
    ]
    if bool(data["is_stock"]):
        fields.append("n_orders")
    for field in fields:
        values = per_minute[field].dropna()
        for statistic, value in (
            ("mean", values.mean()),
            ("std", values.std()),
            ("min", values.min()),
            ("max", values.max()),
        ):
            result[f"{field}_{statistic}"] = float(value) if len(values) else np.nan

    for freq, tag in (("1s", "1s"), ("1min", "1min")):
        prices = price_series(data, freq)
        for field in ("mid_ret", "trade_ret"):
            result[f"RV_{field}_{tag}"] = realized_variance(prices[field])
            result[f"ac1_{field}_{tag}"] = prices[field].dropna().autocorr(lag=1)
    return pd.Series(result), per_minute


def _loop_returns(
    eu: pd.DataFrame, uj: pd.DataFrame, ej: pd.DataFrame, index: pd.Index
) -> pd.DataFrame:
    """Both aggressive EUR triangle loops and their binding EUR quantities.

    Loop A sells EUR/USD and USD/JPY and buys EUR/JPY (hit those bids / lift
    that ask). Loop B is the reverse.  ``valid_book`` requires a strictly
    positive uncrossed BBO on every leg.
    """
    df = pd.DataFrame(index=index)
    df["loop_a"] = eu.loc[index, "bid"] * uj.loc[index, "bid"] / ej.loc[index, "ask"]
    df["loop_b"] = ej.loc[index, "bid"] / (
        uj.loc[index, "ask"] * eu.loc[index, "ask"]
    )
    df["valid_book"] = (
        eu.loc[index, "bid"].gt(0) & eu.loc[index, "ask"].gt(eu.loc[index, "bid"])
        & uj.loc[index, "bid"].gt(0) & uj.loc[index, "ask"].gt(uj.loc[index, "bid"])
        & ej.loc[index, "bid"].gt(0) & ej.loc[index, "ask"].gt(ej.loc[index, "bid"])
    ).to_numpy()
    # Quantities are stated in EUR, using the binding executable BBO size.
    df["qty_a"] = np.minimum.reduce(
        [
            eu.loc[index, "bidsz"].to_numpy(),
            (uj.loc[index, "bidsz"] / eu.loc[index, "bid"]).to_numpy(),
            (ej.loc[index, "asksz"] / df["loop_a"]).to_numpy(),
        ]
    )
    df["qty_b"] = np.minimum.reduce(
        [
            ej.loc[index, "bidsz"].to_numpy(),
            (
                uj.loc[index, "asksz"]
                * uj.loc[index, "ask"]
                / ej.loc[index, "bid"]
            ).to_numpy(),
            (eu.loc[index, "asksz"] / df["loop_b"]).to_numpy(),
        ]
    )
    return df


def one_tick_bps(fx: dict[str, dict[str, object]]) -> float:
    """Coarsest one-tick move across the three legs, in basis points.

    Each EBS book is quoted on its own integer grid (EUR/USD 0.00001, the JPY
    pairs 0.001).  A loop return smaller than the largest of those ticks at
    the day's mean midpoint is within the rounding of the three grids, so it
    is the natural floor for calling a second an arbitrage.
    """
    ticks = []
    for pair in FX_PAIRS:
        mid = np.nanmean(fx[pair]["book_1s"]["mid"].to_numpy(dtype=float))
        ticks.append(1e4 / (FX_PRICE_SCALE[pair] * mid))
    return float(max(ticks))


def one_pip_bps(fx: dict[str, dict[str, object]]) -> float:
    """One full EUR/USD pip (0.0001) at the day's mean midpoint, in bps."""
    mid = np.nanmean(fx["EURUSD"]["book_1s"]["mid"].to_numpy(dtype=float))
    return float(1e4 * 1e-4 / mid)


def triangular_arbitrage(
    fx: dict[str, dict[str, object]],
    min_profit_bps: float | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """Flag executable EUR triangle arbitrage on synchronized 1-second books.

    ``arb`` is the raw screen (loop > 1 on a valid book).  ``arb_gt_1tick``
    additionally requires the loop profit to clear ``min_profit_bps``, by
    default the coarsest one-tick move across the three legs: the books are
    sampled independently at the end of each second, so a sub-tick loop is
    within the rounding of the three grids rather than a price discrepancy.
    ``arb_gt_1pip`` uses a full EUR/USD pip.  Quantity is the binding BBO
    size in EUR.
    """
    eu = fx["EURUSD"]["book_1s"]
    uj = fx["USDJPY"]["book_1s"]
    ej = fx["EURJPY"]["book_1s"]
    index = eu.index.intersection(uj.index).intersection(ej.index)
    loops = _loop_returns(eu, uj, ej, index)
    df = pd.DataFrame(index=index)
    df["loop_a"] = loops["loop_a"]
    df["loop_b"] = loops["loop_b"]
    df["best_loop"] = df[["loop_a", "loop_b"]].max(axis=1)
    df["profit_bps"] = (df["best_loop"] - 1.0) * 10_000.0
    df["direction"] = np.where(df["loop_a"] >= df["loop_b"], "A", "B")
    # Locked or crossed quotes are not a tradable triangle. Require a valid
    # BBO on every leg instead of treating NaN comparisons as opportunities.
    df["arb"] = (df["best_loop"] > 1.0) & loops["valid_book"]
    if min_profit_bps is None:
        min_profit_bps = one_tick_bps(fx)
    pip_bps = one_pip_bps(fx)
    df["arb_gt_1tick"] = df["arb"] & (df["profit_bps"] >= min_profit_bps)
    df["arb_gt_1pip"] = df["arb"] & (df["profit_bps"] >= pip_bps)

    df["qty_eur"] = np.where(
        df["direction"] == "A", loops["qty_a"].to_numpy(), loops["qty_b"].to_numpy()
    )
    df.loc[~df["arb"], "qty_eur"] = 0.0
    df["profit_usd"] = (
        df["qty_eur"]
        * np.maximum(df["best_loop"] - 1.0, 0.0)
        * eu.loc[index, "mid"]
    )

    active = df[df["arb"]]
    if active.empty:
        durations = np.array([], dtype=int)
    else:
        new_episode = active.index.to_series().diff().dt.total_seconds().fillna(2) > 1
        durations = active.groupby(new_episode.cumsum()).size().to_numpy()
    stats = pd.Series(
        {
            "seconds_with_arb": int(df["arb"].sum()),
            "fraction_of_day": float(df["arb"].mean()),
            "n_episodes": int(len(durations)),
            "mean_duration_s": float(durations.mean()) if len(durations) else 0.0,
            "max_duration_s": int(durations.max()) if len(durations) else 0,
            "mean_profit_bps": float(active["profit_bps"].mean()) if len(active) else 0.0,
            "max_profit_bps": float(active["profit_bps"].max()) if len(active) else 0.0,
            "mean_tradable_eur": float(active["qty_eur"].mean()) if len(active) else 0.0,
            "max_tradable_eur": float(active["qty_eur"].max()) if len(active) else 0.0,
            "total_profit_usd_if_every_second": float(active["profit_usd"].sum())
            if len(active)
            else 0.0,
            "one_tick_bps": float(min_profit_bps),
            "seconds_with_arb_gt_1tick": int(df["arb_gt_1tick"].sum()),
            "n_episodes_gt_1tick": int(_episode_lengths(df.index[df["arb_gt_1tick"]]).size),
            "total_profit_usd_gt_1tick": float(
                df.loc[df["arb_gt_1tick"], "profit_usd"].sum()
            ),
            "one_pip_bps": float(pip_bps),
            "seconds_with_arb_gt_1pip": int(df["arb_gt_1pip"].sum()),
            "total_profit_usd_gt_1pip": float(
                df.loc[df["arb_gt_1pip"], "profit_usd"].sum()
            ),
        }
    )
    return df, stats


def _episode_lengths(stamps: pd.DatetimeIndex) -> np.ndarray:
    """Lengths (in consecutive seconds) of runs of flagged seconds."""
    if len(stamps) == 0:
        return np.array([], dtype=int)
    gaps = stamps.to_series().diff().dt.total_seconds().fillna(2) > 1
    return pd.Series(1, index=stamps).groupby(gaps.cumsum().to_numpy()).size().to_numpy()


def arbitrage_execution_decay(
    seconds: pd.DataFrame,
    fx: dict[str, dict[str, object]],
    delays: tuple[int, ...] = (0, 1, 2, 3, 5),
    min_profit_bps: float | None = None,
) -> pd.DataFrame:
    """Execution cost of acting late: re-price each detected second ``d`` seconds on.

    For every second flagged by :func:`triangular_arbitrage`, keep the loop
    direction chosen at detection and re-evaluate it at the executable quotes
    and binding size ``d`` seconds later.  Reports how much of the frictionless
    marked profit survives and how many seconds are still profitable, so the
    detector's own latency can be read against the opportunity's half-life.
    """
    eu = fx["EURUSD"]["book_1s"]
    uj = fx["USDJPY"]["book_1s"]
    ej = fx["EURJPY"]["book_1s"]
    grid = eu.index.intersection(uj.index).intersection(ej.index)
    active = seconds.loc[seconds["arb"]]
    if min_profit_bps is None:
        min_profit_bps = one_tick_bps(fx)
    frictionless = float(active["profit_usd"].sum())
    rows = []
    for d in delays:
        shifted = active.index + pd.Timedelta(seconds=d)
        keep = shifted.isin(grid)
        loops = _loop_returns(eu, uj, ej, shifted[keep])
        is_a = (active["direction"].to_numpy() == "A")[keep]
        loop = np.where(is_a, loops["loop_a"].to_numpy(), loops["loop_b"].to_numpy())
        qty = np.where(is_a, loops["qty_a"].to_numpy(), loops["qty_b"].to_numpy())
        ok = loops["valid_book"].to_numpy() & np.isfinite(loop)
        profit_bps = np.where(ok, (loop - 1.0) * 1e4, np.nan)
        still = ok & (loop > 1.0)
        profit_usd = np.where(
            still, qty * (loop - 1.0) * eu.loc[shifted[keep], "mid"].to_numpy(), 0.0
        )
        rows.append({
            "delay_s": d,
            "seconds_still_profitable": int(still.sum()),
            "share_still_profitable": float(still.sum() / len(active)) if len(active) else np.nan,
            "seconds_gt_1tick": int((still & (profit_bps >= min_profit_bps)).sum()),
            "mean_profit_bps_if_taken": float(np.nanmean(profit_bps)) if ok.any() else np.nan,
            "profit_usd_if_taken": float(profit_usd.sum()),
            "share_of_frictionless_profit": float(profit_usd.sum() / frictionless) if frictionless else np.nan,
        })
    return pd.DataFrame(rows).set_index("delay_s")


def triangular_arbitrage_incremental(
    fx: dict[str, dict[str, object]]
) -> tuple[pd.Series, np.ndarray]:
    """Evaluate the triangle one second at a time, as a live loop would.

    Same loop arithmetic as :func:`triangular_arbitrage`, but executed per
    bar with the wall clock read around each evaluation, so the per-decision
    latency can be compared with how long an opportunity lasts.  Returns the
    latency summary and the raw per-second latencies in microseconds.
    """
    eu = fx["EURUSD"]["book_1s"]
    uj = fx["USDJPY"]["book_1s"]
    ej = fx["EURJPY"]["book_1s"]
    grid = eu.index.intersection(uj.index).intersection(ej.index)
    cols = {}
    for tag, book in (("eu", eu), ("uj", uj), ("ej", ej)):
        for side in ("bid", "ask"):
            cols[f"{tag}_{side}"] = book.loc[grid, side].to_numpy(dtype=float).tolist()
    eu_bid, eu_ask = cols["eu_bid"], cols["eu_ask"]
    uj_bid, uj_ask = cols["uj_bid"], cols["uj_ask"]
    ej_bid, ej_ask = cols["ej_bid"], cols["ej_ask"]
    n = len(grid)
    latency_ns = np.empty(n, dtype=np.int64)
    flagged = 0
    clock = time.perf_counter_ns
    for i in range(n):
        t0 = clock()
        b1, a1, b2, a2, b3, a3 = eu_bid[i], eu_ask[i], uj_bid[i], uj_ask[i], ej_bid[i], ej_ask[i]
        valid = b1 > 0 and a1 > b1 and b2 > 0 and a2 > b2 and b3 > 0 and a3 > b3
        loop_a = b1 * b2 / a3
        loop_b = b3 / (a2 * a1)
        if loop_a != loop_a:
            best = loop_b
        elif loop_b != loop_b:
            best = loop_a
        else:
            best = loop_a if loop_a >= loop_b else loop_b
        if valid and best > 1.0:
            flagged += 1
        latency_ns[i] = clock() - t0
    latency_us = latency_ns / 1_000.0
    summary = pd.Series(
        {
            "seconds_evaluated": n,
            "seconds_with_arb": flagged,
            "median_us": float(np.median(latency_us)),
            "p99_us": float(np.percentile(latency_us, 99)),
            "max_us": float(latency_us.max()),
            "total_s": float(latency_ns.sum() / 1e9),
        }
    )
    return summary, latency_us


def timing_table(
    timings: dict[str, float], bars_by_step: dict[str, int] | None = None
) -> pd.DataFrame:
    """Wall-clock per step, plus throughput against the tape it processed.

    ``bars_by_step`` maps a step label to the number of 1-second bars the step
    covered (23,400 per instrument-session); the table then reports tape
    seconds per compute second and microseconds per bar, the two numbers that
    say whether the step could keep up with a live feed.
    """
    table = pd.Series(timings, name="seconds").rename_axis("step").to_frame().sort_index()
    bars = pd.Series(bars_by_step or {}, dtype=float).reindex(table.index)
    table["bars_1s"] = bars
    table["tape_s_per_compute_s"] = bars / table["seconds"]
    table["us_per_bar"] = 1e6 * table["seconds"] / bars
    return table
