"""Recorder: poll /v1/securities as fast as the server allows and save the tape.

    python -m algo1 record --name warmup --seconds 20

Output `runs/<name>/recording.parquet` with columns t_send, t_recv, bid/ask/size per venue (ticks),
pos_M, pos_A, tick, and `runs/<name>/tas.parquet` with the trades of both tickers (incremental
GET /v1/securities/tas?after=<id> every `tas_every_ms`, ~2% of the polls). Trades are what the
fair-value and quoting questions need: a mark-out target that is not a tautology of the centre
being tested. Feeds `sim/analysis.py` and the replay engine (Friday). Record the first 20 s of any
live session if there is a warm-up window.
"""
from __future__ import annotations

import os
import time

import numpy as np
import pandas as pd

from ..api.client import Lane
from ..api.messages import PathBuilder, parse_case, parse_securities, parse_sheet, parse_tas


def record(cfg, seconds=20.0, out_dir=None, max_rows=2_000_000, tas_every_ms=250.0):
    lane = Lane("rec", cfg.host, cfg.port, cfg.api_key, cfg.timeout_s)
    st, body, _, _ = lane.get(PathBuilder.SECURITIES)
    if st != 200:
        raise RuntimeError(f"GET /v1/securities → {st}")
    sheet = parse_sheet(body, None, cfg.tickers)
    scale = sheet.scale
    rows = []
    tas_rows = []
    last_id = {v: 0 for v in (0, 1)}
    t_tas = time.perf_counter()
    t_end = time.perf_counter() + seconds
    tick = 0
    i = 0
    while time.perf_counter() < t_end and len(rows) < max_rows:
        st, body, ts, tr = lane.get(PathBuilder.SECURITIES)
        if st == 200:
            bbo, pos, pv = parse_securities(body, scale, cfg.tickers)
            rows.append((ts, tr, bbo.bid[0], bbo.ask[0], bbo.bid_size[0], bbo.ask_size[0],
                         bbo.bid[1], bbo.ask[1], bbo.bid_size[1], bbo.ask_size[1], pv[0], pv[1], tick))
        elif st == 429:
            time.sleep(0.05)
        i += 1
        if i % 50 == 0:
            stc, cb, _, _ = lane.get(PathBuilder.CASE)
            if stc == 200:
                tick = parse_case(cb)[0]
        if time.perf_counter() - t_tas >= tas_every_ms / 1000.0:
            t_tas = time.perf_counter()
            for v in (0, 1):
                st2, b2, ts2, tr2 = lane.get(f"/v1/securities/tas?ticker={cfg.tickers[v]}&after={last_id[v]}")
                if st2 == 200:
                    for tid, tk, px, qty in parse_tas(b2, scale):
                        tas_rows.append((tr2, v, tid, tk, px, qty))
                        if tid > last_id[v]:
                            last_id[v] = tid
    lane.close()
    df = pd.DataFrame(rows, columns=["t_send", "t_recv", "bid_m", "ask_m", "bs_m", "as_m", "bid_a", "ask_a",
                                     "bs_a", "as_a", "pos_m", "pos_a", "tick"])
    out_dir = out_dir or os.path.join("runs", cfg.run_name)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "recording.parquet")
    df.to_parquet(path)
    if tas_rows:
        pd.DataFrame(tas_rows, columns=["t_recv", "venue", "id", "tick", "px", "qty"]).to_parquet(os.path.join(out_dir, "tas.parquet"))
    return df, path
