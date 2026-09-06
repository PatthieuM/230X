from __future__ import annotations

from collections import defaultdict
from pathlib import Path

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


def session_bounds(date: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    return (
        pd.Timestamp(f"{date} {SESSION_START}"),
        pd.Timestamp(f"{date} {SESSION_END}"),
    )


def session_grid(date: str, freq: str) -> pd.DatetimeIndex:
    start, end = session_bounds(date)
    return pd.date_range(start, end, freq=freq, inclusive="left")


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
    event_ns = state.index.view("i8")
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

    boundary_ns = pd.date_range(start, end, freq="1min").view("i8")
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
    out.index = idx.tz_convert(TZ).tz_localize(None)
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
    out["ts"] = pd.to_datetime(out["ts"], format="%Y/%m/%d %H:%M:%S.%f")
    # Use exact integer price ticks as book keys.  The source has 5 decimals for
    # EUR/USD and 3 decimals for the JPY pairs.
    out["price_ticks"] = np.rint(out["price"] * FX_PRICE_SCALE[pair]).astype("int64")
    out.set_index("ts", inplace=True)
    out.sort_index(kind="stable", inplace=True)
    return out


def _ebs_quote_events(
    events: pd.DataFrame,
    pair: str,
    avg_spread: float | None = None,
    band_multiplier: float = DEPTH_BAND_MULTIPLIER,
) -> pd.DataFrame:
    scale = FX_PRICE_SCALE[pair]
    bids: dict[int, float] = {}
    asks: dict[int, float] = {}
    records: list[tuple] = []
    for ts, group in events.groupby(level=0, sort=True):
        for row in group.itertuples():
            book = bids if int(row.buy_sell_flag) == 0 else asks
            price, size = int(row.price_ticks), float(row.size)
            if size > 0:
                book[price] = size
            else:
                book.pop(price, None)
        if bids and asks:
            bid_tick, ask_tick = max(bids), min(asks)
            if ask_tick > bid_tick:
                bid, ask = bid_tick / scale, ask_tick / scale
                bidsz, asksz = bids[bid_tick], asks[ask_tick]
                mid, spread = (bid + ask) / 2.0, ask - bid
                if avg_spread is None:
                    bid_2x = ask_2x = np.nan
                else:
                    lower_tick = int(
                        np.ceil((mid - band_multiplier * avg_spread) * scale - 1e-9)
                    )
                    upper_tick = int(
                        np.floor((mid + band_multiplier * avg_spread) * scale + 1e-9)
                    )
                    bid_2x = sum(size for tick, size in bids.items() if tick >= lower_tick)
                    ask_2x = sum(size for tick, size in asks.items() if tick <= upper_tick)
                records.append(
                    (
                        ts, bid, ask, bidsz, asksz, mid, spread,
                        bidsz, asksz, bidsz + asksz,
                        bid_2x, ask_2x, bid_2x + ask_2x,
                    )
                )
                continue
        # Preserve an invalid state transition so later matching/integration
        # cannot silently bridge a locked or crossed book.
        records.append((ts, *([np.nan] * 12)))
    return pd.DataFrame(
        records,
        columns=[
            "ts", "bid", "ask", "bidsz", "asksz", "mid", "spread",
            "bid_depth", "ask_depth", "depth",
            "bid_depth_2x", "ask_depth_2x", "depth_2x",
        ],
    ).set_index("ts")


def _replay_ebs_seconds(
    events: pd.DataFrame,
    pair: str,
    date: str,
    avg_spread: float | None = None,
) -> pd.DataFrame:
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
        quote_events = _ebs_quote_events(events, pair)
        avg_spread = _time_weighted_average(quote_events, "spread", date)
        book_1s = _replay_ebs_seconds(events, pair, date, avg_spread)
        book_1s["bid_depth"] = book_1s["bidsz"]
        book_1s["ask_depth"] = book_1s["asksz"]
        book_1s["depth"] = book_1s["bidsz"] + book_1s["asksz"]
        book_1s["depth_2x"] = (
            book_1s["bid_depth_2x"] + book_1s["ask_depth_2x"]
        )
        book_1s["avg_spread_day"] = avg_spread
        book_1s["depth_band_multiplier"] = DEPTH_BAND_MULTIPLIER
        liquidity_events = _ebs_quote_events(events, pair, avg_spread)
        liquidity_minute = _time_weighted_minute(
            liquidity_events,
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


def _impact_by_minute(
    trades: pd.DataFrame, quote_events: pd.DataFrame, date: str
) -> pd.Series:
    output = pd.Series(np.nan, index=session_grid(date, "1min"), dtype=float)
    signed = trades.dropna(subset=["sign", "price"])
    # Keep invalid/crossed state rows: dropping them would incorrectly carry an
    # earlier valid quote through a crossed interval.
    quotes = quote_events.sort_index(kind="stable")
    if signed.empty or quotes.empty:
        return output
    qt = quotes.index.view("i8")
    tt = signed.index.view("i8")
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
    regression = pd.DataFrame(
        {"impact": impact, "sign": signed["sign"].to_numpy()}, index=signed.index
    ).dropna()

    def slope(group: pd.DataFrame) -> float:
        if len(group) < 5 or group["sign"].nunique() < 2:
            return np.nan
        x = group["sign"].to_numpy(dtype=float)
        y = group["impact"].to_numpy(dtype=float)
        x = x - x.mean()
        denom = float(x @ x)
        return float(x @ (y - y.mean()) / denom) if denom > 0 else np.nan

    values = regression.groupby(regression.index.floor("1min")).apply(slope)
    return values.reindex(output.index)


def minute_stats(data: dict[str, object]) -> pd.DataFrame:
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
    return out


def price_series(data: dict[str, object], freq: str) -> pd.DataFrame:
    date = str(data["date"])
    grid = session_grid(date, freq)
    mid = data["book_1s"]["mid"].resample(freq).last().reindex(grid).ffill()
    trade = data["trades"]["price"].resample(freq).last().reindex(grid).ffill()
    out = pd.DataFrame({"mid": mid, "trade": trade})
    out["mid_ret"] = np.log(out["mid"]).diff()
    out["trade_ret"] = np.log(out["trade"]).diff()
    return out


def realized_variance(series: pd.Series) -> float:
    values = pd.Series(series).dropna().to_numpy(dtype=float)
    return float(values @ values)


def autocorrelations(series: pd.Series, lags: int = 10) -> pd.Series:
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


def triangular_arbitrage(
    fx: dict[str, dict[str, object]]
) -> tuple[pd.DataFrame, pd.Series]:
    eu = fx["EURUSD"]["book_1s"]
    uj = fx["USDJPY"]["book_1s"]
    ej = fx["EURJPY"]["book_1s"]
    index = eu.index.intersection(uj.index).intersection(ej.index)
    df = pd.DataFrame(index=index)
    df["loop_a"] = eu.loc[index, "bid"] * uj.loc[index, "bid"] / ej.loc[index, "ask"]
    df["loop_b"] = ej.loc[index, "bid"] / (
        uj.loc[index, "ask"] * eu.loc[index, "ask"]
    )
    df["best_loop"] = df[["loop_a", "loop_b"]].max(axis=1)
    df["profit_bps"] = (df["best_loop"] - 1.0) * 10_000.0
    df["direction"] = np.where(df["loop_a"] >= df["loop_b"], "A", "B")
    df["arb"] = df["best_loop"] > 1.0

    # Quantities are stated in EUR, using the binding executable BBO size.
    qty_a = np.minimum.reduce(
        [
            eu.loc[index, "bidsz"].to_numpy(),
            (uj.loc[index, "bidsz"] / eu.loc[index, "bid"]).to_numpy(),
            (ej.loc[index, "asksz"] / df["loop_a"]).to_numpy(),
        ]
    )
    qty_b = np.minimum.reduce(
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
    df["qty_eur"] = np.where(df["direction"] == "A", qty_a, qty_b)
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
        }
    )
    return df, stats
