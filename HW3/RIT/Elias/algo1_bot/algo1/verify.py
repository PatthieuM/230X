"""Pre-heat checklist — run at tick 0 of any live session, before any strategy is enabled.

    python -m algo1 verify            read-only: sheet dump, GET rate-limit probe, 4-lane bench
    python -m algo1 verify --trade    + a 100-share pair (how is gross counted?) and a 1-share
                                      self-trade test (does RIT prevent self-execution?)

Each VERIFY LIVE item maps to a Config flag; this prints the observed value next to the flag's
current default so the flag can be set consciously before `run`. The API key is never printed.
"""
from __future__ import annotations

import json
import time

from .api.client import Lanes
from .api.messages import PathBuilder, as_dict, parse_ack, parse_securities, parse_sheet
from .core.secrets import key_status
from .core.types import A, BUY, LIMIT, M, MARKET, SELL


def verify(cfg, trade=False):
    lanes = Lanes(cfg.host, cfg.port, cfg.api_key, cfg.timeout_s)
    try:
        lanes.connect_all()
    except OSError as e:
        print(f"cannot connect to {cfg.host}:{cfg.port}: {e}")
        return 2
    st, body, _, _ = lanes.poll.get(PathBuilder.SECURITIES)
    if st == 401:
        print("401 UNAUTHORIZED —", key_status(cfg))
        return 2
    if st != 200:
        print(f"GET /v1/securities → {st}: {body[:200]!r}")
        return 2
    stl, lim, _, _ = lanes.poll.get(PathBuilder.LIMITS)
    sh = parse_sheet(body, lim if stl == 200 else None, cfg.tickers)
    cfg.apply_sheet(sh)
    print("## Sheet (facts)")
    print(json.dumps(sh.__dict__, indent=2, default=str))
    stc, cb, _, _ = lanes.poll.get(PathBuilder.CASE)
    print("## Case:", cb.decode()[:300] if stc == 200 else stc)
    bbo, pos, pv = parse_securities(body, cfg.scale, cfg.tickers)
    print(f"## BBO {bbo}  pos={pos} per venue {pv}   → pos_per_ticker={cfg.pos_per_ticker} (default)")
    # ---- GET rate limit probe -------------------------------------------------------------
    n429 = 0
    t = time.perf_counter()
    n = 0
    while time.perf_counter() - t < 1.0:
        s, b, _, _ = lanes.poll.get(PathBuilder.SECURITIES)
        n += 1
        if s == 429:
            n429 += 1
    print(f"## GET probe: {n} polls in 1 s, {n429} × 429  → get_costs_token={cfg.get_costs_token} (default); "
          f"{'GETs ARE rate-limited: set get_costs_token=True' if n429 else 'GETs not limited at this rate'}")
    # ---- lanes -----------------------------------------------------------------------------
    from .bench import lanes_bench
    r = lanes_bench(cfg.host, cfg.port, cfg.api_key, n=20, own_mock=False)
    print(f"## 4-lane: sequential {r['sequential_4_p50']:.0f} µs vs concurrent {r['concurrent_4_p50']:.0f} µs "
          f"(speedup {r['speedup']:.2f}x) → {'server serialises connections' if r['speedup'] < 1.5 else 'concurrent lanes OK'}")
    if not trade:
        print("## Trading tests skipped (pass --trade at tick 0 of a practice session).")
        lanes.close_all()
        return 0
    pb = PathBuilder(cfg.scale, cfg.tickers, sh.quoted_decimals)
    # ---- 100-share pair: how is gross counted? ---------------------------------------------
    s1, b1, _, _ = lanes.ord1.post(pb.order_path(M, BUY, MARKET, 100))
    s2, b2, _, _ = lanes.ord2.post(pb.order_path(A, SELL, MARKET, 100))
    print("## 100-share pair acks:", s1, as_dict(parse_ack(s1, b1, cfg.scale)) if s1 == 200 else b1[:120],
          s2, as_dict(parse_ack(s2, b2, cfg.scale)) if s2 == 200 else b2[:120])
    time.sleep(2.0)                      # the position field lags (probe §5 measures how much)
    stl, lim, _, _ = lanes.poll.get(PathBuilder.LIMITS)
    st, body, _, _ = lanes.poll.get(PathBuilder.SECURITIES)
    bbo, pos, pv = parse_securities(body, cfg.scale, cfg.tickers)
    print(f"   after: limits={lim.decode()[:200] if stl == 200 else stl}  pos={pos} per venue {pv}")
    print(f"   → gross_both_legs={cfg.gross_both_legs} (default); set from the 'gross' field above (200 → True, 0 → False)")
    print(f"   → pos_per_ticker: per-venue positions {pv}; identical non-zero values after a one-sided trade mean the")
    print(f"     field is the aggregated case position → set pos_per_ticker=False")
    # ---- 1-share self-trade test: our ask alone inside the spread, then a MARKET buy ------------
    bid, ask = bbo.bid[M], bbo.ask[M]
    if ask - bid >= 2:
        px = ask - 1
        s3, b3, _, _ = lanes.ord1.post(pb.order_path(M, SELL, LIMIT, 1, px))
        time.sleep(0.3)
        s4, b4, _, _ = lanes.ord2.post(pb.order_path(M, BUY, MARKET, 1))
        a4 = parse_ack(s4, b4, cfg.scale) if s4 == 200 else None
        hit_own = a4 is not None and a4.filled == 1 and int(round(a4.vwap_ticks)) == px
        print(f"## self-trade: rest SELL 1@{px} inside the spread ({s3}) then BUY 1 MARKET ({s4}) → "
              f"filled={getattr(a4, 'filled', None)} vwap={getattr(a4, 'vwap_ticks', None)} (ours at {px})")
        print(f"   → self_trade_prevented={'False' if hit_own else 'True (did not hit our ask)'}  (default {cfg.self_trade_prevented})")
    else:
        print("## self-trade test skipped: spread < 2 ticks on M right now; rerun")
    lanes.cxl.post(pb.cancel_all_path())
    # flatten the 1-share residual if any
    st, body, _, _ = lanes.poll.get(PathBuilder.SECURITIES)
    bbo, pos, pv = parse_securities(body, cfg.scale, cfg.tickers)
    if pos:
        lanes.ord1.post(pb.order_path(M, SELL if pos > 0 else BUY, MARKET, abs(pos)))
    lanes.close_all()
    print("## done — set the flags in Config / params.json before `run`.")
    return 0
