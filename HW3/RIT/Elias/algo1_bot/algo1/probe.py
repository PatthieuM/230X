"""probe — case-agnostic architecture tests against any live RIT case (a demo will do).

    python -m algo1 probe                          read-only: sheet, poll cadence, GET limit, lanes
    python -m algo1 probe --trade --ticker X       + order mechanics on one ticker (1-share orders,
                                                   far-from-touch LIMIT, cancel, bulk cancel, rate
                                                   limit, ack timing, position visibility, self-trade)

Each section prints the observation and the Config flag / assumption it informs. Nothing here
depends on two venues or on CRZY; only `gross_both_legs` needs the real ALGO1 case. The API key is
never printed. Results also go to runs/probe_<ts>.json for PROGRESS.md.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter_ns

import numpy as np

from .api.client import Lane, Lanes
from .api.messages import PathBuilder, as_dict, parse_ack, parse_case, to_ticks
from .core.secrets import key_status

_loads = json.loads


def pct(x, q):
    return float(np.percentile(x, q)) if len(x) else float("nan")


def _sec_rows(lane):
    st, body, ts, tr = lane.get(PathBuilder.SECURITIES)
    if st != 200:
        raise RuntimeError(f"GET /v1/securities → {st} {body[:200]!r}")
    return _loads(body), tr - ts


def probe(cfg, trade=False, ticker=None, seconds=3.0, out=None):
    res = {"host": f"{cfg.host}:{cfg.port}", "t": time.strftime("%Y-%m-%d %H:%M:%S")}
    print("##", key_status(cfg))
    if not cfg.api_key:
        return 2
    lanes = Lanes(cfg.host, cfg.port, cfg.api_key, cfg.timeout_s)
    try:
        lanes.connect_all()
    except OSError as e:
        print(f"cannot connect to {cfg.host}:{cfg.port}: {e}  (is the RIT client running with the API on?)")
        return 2
    st, body, ts, tr = lanes.poll.get(PathBuilder.SECURITIES)
    if st == 401:
        print("401 UNAUTHORIZED —", key_status(cfg))
        print("  (if a key was loaded: compare it with the client's API dialog; RIT regenerates it on restart)")
        return 2
    if st != 200:
        print(f"GET /v1/securities → {st}: {body[:200]!r}")
        return 2
    rows = _loads(body)

    # ---- 1. sheet: every security, the fields the bot reads ---------------------------------
    print("## 1. Sheet (facts the bot reads at tick 0)")
    keys = ("ticker", "type", "position", "bid", "bid_size", "ask", "ask_size", "quoted_decimals",
            "trading_fee", "limit_order_rebate", "min_trade_size", "max_trade_size",
            "api_orders_per_second", "execution_delay_ms", "is_tradeable", "is_shortable")
    for r in rows:
        print("  " + json.dumps({k: r.get(k) for k in keys}, default=str))
    stc, cb, _, _ = lanes.poll.get(PathBuilder.CASE)
    stl, lb, _, _ = lanes.poll.get(PathBuilder.LIMITS)
    stt, tb, _, _ = lanes.poll.get(PathBuilder.TRADER)
    print("  case:", cb.decode()[:300] if stc == 200 else stc)
    print("  limits:", lb.decode()[:300] if stl == 200 else stl)
    if stt == 200:
        tinfo = _loads(tb)
        res["trader_id"] = tinfo.get("trader_id")
        print("  trader_id:", tinfo.get("trader_id"), "(own-order flag in parse_book uses this)")
    res["securities"] = [{k: r.get(k) for k in keys} for r in rows]
    res["case"] = _loads(cb) if stc == 200 else None
    res["limits"] = _loads(lb) if stl == 200 else None
    sec_ticker = ticker or rows[0]["ticker"]
    row = next((r for r in rows if r["ticker"] == sec_ticker), rows[0])
    dec = int(row.get("quoted_decimals") or 2)
    scale = 10 ** dec
    pb = PathBuilder(scale, (sec_ticker, sec_ticker), dec)

    # ---- 2. poll cadence + GET rate limit + case clock ------------------------------------------
    print(f"\n## 2. Poll cadence ({seconds:.0f} s of tight GET /v1/securities)")
    rtts, n429, n = [], 0, 0
    tick0 = parse_case(cb)[0] if stc == 200 else None
    t_end = time.perf_counter() + seconds
    sizes = []
    while time.perf_counter() < t_end:
        st, body, ts, tr = lanes.poll.get(PathBuilder.SECURITIES)
        n += 1
        if st == 200:
            rtts.append((tr - ts) / 1e3)
            sizes.append(len(body))
        elif st == 429:
            n429 += 1
            try:
                time.sleep(float(_loads(body).get("wait", 0.05)))
            except Exception:
                time.sleep(0.05)
    stc2, cb2, _, _ = lanes.poll.get(PathBuilder.CASE)
    tick1 = parse_case(cb2)[0] if stc2 == 200 else None
    print(f"  {n} polls, RTT p50 {pct(rtts,50):.0f} µs p95 {pct(rtts,95):.0f} p99 {pct(rtts,99):.0f} · "
          f"payload {int(np.median(sizes)) if sizes else 0} B · 429s {n429} · tick {tick0} → {tick1}")
    print(f"  → get_costs_token = {bool(n429)}   (Config default False) · loop floor ≈ {pct(rtts,50):.0f} µs")
    res["poll"] = {"n": n, "rtt_p50_us": pct(rtts, 50), "rtt_p95_us": pct(rtts, 95), "n429": n429,
                   "tick0": tick0, "tick1": tick1, "seconds": seconds}

    # ---- 3. lanes: does the client serialise connections? ---------------------------------------
    print("\n## 3. Four keep-alive connections: sequential vs concurrent GETs")
    ls = [Lane(f"l{i}", cfg.host, cfg.port, cfg.api_key, cfg.timeout_s) for i in range(4)]
    for l in ls:
        l.connect()
    seq, conc = [], []
    with ThreadPoolExecutor(4) as pool:
        for _ in range(30):
            t = perf_counter_ns()
            for l in ls:
                l.get(PathBuilder.SECURITIES)
            seq.append((perf_counter_ns() - t) / 1e3)
            t = perf_counter_ns()
            list(pool.map(lambda l: l.get(PathBuilder.SECURITIES), ls))
            conc.append((perf_counter_ns() - t) / 1e3)
    for l in ls:
        l.close()
    sp = pct(seq, 50) / max(1e-9, pct(conc, 50))
    print(f"  4 GETs sequential p50 {pct(seq,50):.0f} µs · concurrent p50 {pct(conc,50):.0f} µs · speedup {sp:.2f}x")
    print(f"  → {'the client SERIALISES connections: concurrent pair legs gain nothing; prefer slow-first / confirm-then-hedge' if sp < 1.5 else 'concurrent lanes work: keep concurrent pair legs'}")
    res["lanes"] = {"seq_p50_us": pct(seq, 50), "conc_p50_us": pct(conc, 50), "speedup": sp}

    if not trade:
        print("\n## 4–9 skipped (pass --trade --ticker X on a demo where small orders are harmless).")
        _save(res, out)
        lanes.close_all()
        return 0

    # ---- 4. resting LIMIT far from the touch: ack, id, status; then cancel -------------------------
    print(f"\n## 4. Resting LIMIT on {sec_ticker}: ack + cancel latency")
    st, body, ts, tr = lanes.poll.get(PathBuilder.SECURITIES)
    row = next(r for r in _loads(body) if r["ticker"] == sec_ticker)
    bid, ask = to_ticks(row["bid"], scale), to_ticks(row["ask"], scale)
    far = max(1, bid - 20 * max(1, (ask - bid)))
    st, body, ts, tr = lanes.ord1.post(pb.order_path(0, 0, 1, 1, far))       # BUY 1 LIMIT far
    ack = parse_ack(st, body, scale)
    print(f"  POST ack {st} in {(tr-ts)/1e3:.0f} µs: {as_dict(ack) if st == 200 else body[:120]}")
    if st == 200:
        # is it visible in GET /orders and in the book? how fast?
        t0 = perf_counter_ns()
        seen_orders = seen_book = None
        for _ in range(50):
            so, ob, _, _ = lanes.poll.get(PathBuilder.ORDERS_OPEN)
            if so == 200 and any(o["order_id"] == ack.server_id for o in _loads(ob)):
                seen_orders = (perf_counter_ns() - t0) / 1e3
                break
        for _ in range(50):
            sb, bb, _, _ = lanes.poll.get(pb.book_path(0, 500))
            if sb == 200 and any(o.get("order_id") == ack.server_id for o in _loads(bb).get("bid", [])):
                seen_book = (perf_counter_ns() - t0) / 1e3
                break
        print(f"  visible in /orders after {seen_orders} µs · in /securities/book after {seen_book} µs"
              f" (own-order flag by order_id works: {seen_book is not None})")
        sc, cbody, tcs, tcr = lanes.cxl.delete(pb.cancel_path(ack.server_id))
        print(f"  DELETE → {sc} {cbody[:80]!r} in {(tcr-tcs)/1e3:.0f} µs   ← cancel latency (the maker's binding latency)")
        so, ob, _, _ = lanes.poll.get("/v1/orders?status=CANCELLED")
        print(f"  shows as CANCELLED in /orders: {so == 200 and any(o['order_id'] == ack.server_id for o in _loads(ob))}")
        res["rest_cancel"] = {"ack_us": (tr - ts) / 1e3, "cancel_us": (tcr - tcs) / 1e3, "cancel_status": sc,
                              "visible_orders_us": seen_orders, "visible_book_us": seen_book}

    # ---- 5. MARKET 1 share: ack timing vs execution_delay_ms; position visibility ------------------
    print(f"\n## 5. MARKET 1 share: ack after execution? position visible when?")
    delay = int(row.get("execution_delay_ms") or 0)
    st, body, ts, tr = lanes.poll.get(PathBuilder.SECURITIES)
    rows0 = _loads(body)
    pos0 = next(r for r in rows0 if r["ticker"] == sec_ticker)["position"]
    others0 = {r["ticker"]: r["position"] for r in rows0 if r["ticker"] != sec_ticker}
    st, body, ts, tr = lanes.ord1.post(pb.order_path(0, 0, 0, 1))              # BUY 1 MARKET
    ack = parse_ack(st, body, scale)
    ack_us = (tr - ts) / 1e3
    print(f"  ack {st} in {ack_us:.0f} µs (sheet execution_delay_ms = {delay}) → "
          f"{as_dict(ack) if st == 200 else body[:120]}")
    t0 = perf_counter_ns()
    pos_seen = None
    others1 = others0
    while (perf_counter_ns() - t0) < 3_000_000_000:
        s2, b2, _, _ = lanes.poll.get(PathBuilder.SECURITIES)
        if s2 != 200:
            continue
        rows1 = _loads(b2)
        if next(r for r in rows1 if r["ticker"] == sec_ticker)["position"] != pos0:
            pos_seen = (perf_counter_ns() - t0) / 1e3
            others1 = {r["ticker"]: r["position"] for r in rows1 if r["ticker"] != sec_ticker}
            break
    filled_in_ack = getattr(ack, "filled", None)
    aggregated = any(others1[k] != others0[k] for k in others0) if others0 else None
    print(f"  filled in the ack: {filled_in_ack} · position delta visible after "
          f"{f'{pos_seen/1e3:.1f} ms' if pos_seen is not None else 'NOT within 3 s'}")
    print(f"  other tickers' position before/after: {others0} → {others1}")
    print(f"  → ack-after-execution = {bool(filled_in_ack)} ; pos_per_ticker = {not aggregated} "
          f"({'the other ticker moved too: the field is the aggregated case position' if aggregated else 'only the traded ticker moved'})")
    res["market"] = {"ack_us": ack_us, "delay_ms": delay, "filled_in_ack": filled_in_ack, "pos_visible_us": pos_seen,
                     "aggregated_position": aggregated}

    # ---- 6. marketable LIMIT 1 share: does the remainder rest? --------------------------------------
    print("\n## 6. Marketable LIMIT (sell 1 at bid): ack fields")
    st, body, ts, tr = lanes.poll.get(PathBuilder.SECURITIES)
    row = next(r for r in _loads(body) if r["ticker"] == sec_ticker)
    bid = to_ticks(row["bid"], scale)
    st, body, ts, tr = lanes.ord2.post(pb.order_path(0, 1, 1, 1, bid))       # SELL 1 LIMIT at bid
    ack = parse_ack(st, body, scale)
    print(f"  ack {st} in {(tr-ts)/1e3:.0f} µs → {as_dict(ack) if st == 200 else body[:120]}")
    print(f"  → marketable LIMIT filled at once: {getattr(ack, 'filled', 0) > 0}; status_str '{getattr(ack, 'status_str', '?')}'")
    res["marketable_limit"] = getattr(ack, "__dict__", None) and dict(ack.__dict__)

    # ---- 7. rate limit: POSTs until 429; do DELETEs count? --------------------------------------------
    print("\n## 7. Rate limit: far LIMITs until 429, then cancels")
    ids, n_ok, wait = [], 0, None
    t0 = time.perf_counter()
    for i in range(60):
        st, body, ts, tr = lanes.ord1.post(pb.order_path(0, 0, 1, 1, far))
        if st == 200:
            n_ok += 1
            ids.append(parse_ack(st, body, scale).server_id)
        elif st == 429:
            wait = _loads(body).get("wait")
            break
        else:
            print(f"  unexpected {st} {body[:80]!r}")
            break
    dt = time.perf_counter() - t0
    print(f"  {n_ok} POSTs accepted in {dt*1e3:.0f} ms before a 429 (wait={wait}) · sheet api_orders_per_second={row.get('api_orders_per_second')}")
    n_c_ok = n_c_429 = 0
    for oid in ids:
        sc, cbody, _, _ = lanes.cxl.delete(pb.cancel_path(oid))
        n_c_ok += sc == 200
        n_c_429 += sc == 429
    print(f"  {n_c_ok} DELETEs ok, {n_c_429} × 429 → cancel_costs_token = {bool(n_c_429)} (default False)")
    res["rate"] = {"posts_before_429": n_ok, "ms": dt * 1e3, "wait": wait, "cancel_429": n_c_429}
    # ---- 7b. per-ticker or global? drain one ticker, then fire on the other at once ---------------
    other = next((r["ticker"] for r in rows if r["ticker"] != sec_ticker), None)
    if other is not None and wait is not None:
        time.sleep(1.2)                                              # both buckets full again
        pb2 = PathBuilder(scale, (other, other), dec)
        st2, body2, _, _ = lanes.poll.get(PathBuilder.SECURITIES)
        row2 = next(r for r in _loads(body2) if r["ticker"] == other)
        far2 = max(1, to_ticks(row2["bid"], scale) - 20 * max(1, to_ticks(row2["ask"], scale) - to_ticks(row2["bid"], scale)))
        n1 = n2 = 0
        for i in range(12):
            s1, b1, _, _ = lanes.ord1.post(pb.order_path(0, 0, 1, 1, far))
            if s1 == 200:
                n1 += 1
            elif s1 == 429:
                break
        for i in range(12):
            s2, b2, _, _ = lanes.ord2.post(pb2.order_path(0, 0, 1, 1, far2))
            if s2 == 200:
                n2 += 1
            elif s2 == 429:
                break
        per_ticker = n2 >= max(3, n1 - 2)
        print(f"  7b. {n1} POSTs on {sec_ticker} then immediately {n2} on {other} before a 429 → "
              f"budget_per_ticker = {per_ticker} ({'independent buckets' if per_ticker else 'one shared bucket'})")
        res["rate"]["per_ticker_probe"] = {"first": n1, "second": n2, "per_ticker": per_ticker}
        lanes.cxl.post(pb.cancel_all_path())

    # ---- 8. bulk cancel + self-trade ------------------------------------------------------------------
    print("\n## 8. Bulk cancel and self-trade")
    st, body, ts, tr = lanes.ord1.post(pb.order_path(0, 0, 1, 1, far))
    if st == 429:
        time.sleep(float(_loads(body).get("wait", 0.5)) + 0.05)
        st, body, ts, tr = lanes.ord1.post(pb.order_path(0, 0, 1, 1, far))
    sc, cbody, tcs, tcr = lanes.cxl.post(pb.cancel_all_path())
    print(f"  POST /commands/cancel?all=1 → {sc} {cbody[:100]!r} in {(tcr-tcs)/1e3:.0f} µs")
    time.sleep(0.3)
    st, body, ts, tr = lanes.poll.get(PathBuilder.SECURITIES)
    row = next(r for r in _loads(body) if r["ticker"] == sec_ticker)
    bid, ask = to_ticks(row["bid"], scale), to_ticks(row["ask"], scale)
    hit_own = None
    if ask - bid >= 2:
        px = ask - 1                                                            # alone at the best ask
        s3, b3, ts3, tr3 = lanes.ord1.post(pb.order_path(0, 1, 1, 1, px))       # SELL 1 LIMIT inside
        a3 = parse_ack(s3, b3, scale) if s3 == 200 else None
        time.sleep(0.3)
        sb, bb, _, _ = lanes.poll.get(pb.book_path(0, 500))
        asks = _loads(bb).get("ask", []) if sb == 200 else []
        in_book = any(o.get("order_id") == getattr(a3, "server_id", -1) for o in asks)
        by_trader = any(o.get("trader_id") == res.get("trader_id") for o in asks)
        print(f"  best ask rows in /securities/book while ours rests: {asks[:2]}")
        print(f"  own order found by order_id: {in_book} · by trader_id: {by_trader}")
        res["book_rows_sample"] = asks[:2]
        s4, b4, _, _ = lanes.ord2.post(pb.order_path(0, 0, 0, 1))               # BUY 1 MARKET
        a4 = parse_ack(s4, b4, scale) if s4 == 200 else None
        hit_own = a4 is not None and a4.filled == 1 and int(round(a4.vwap_ticks)) == px
        print(f"  rest SELL 1@{px} inside the spread ({s3}, ack {(tr3-ts3)/1e3:.0f} µs) → visible in book by order_id: {in_book}")
        print(f"  then BUY 1 MARKET ({s4}) → filled={getattr(a4, 'filled', None)} vwap={getattr(a4, 'vwap_ticks', None)} (ours at {px})")
        print(f"  → self_trade_prevented = {not hit_own}   (default False)")
    else:
        print("  self-trade test skipped: spread < 2 ticks right now; rerun")
    res["self_trade_prevented"] = (not hit_own) if hit_own is not None else None
    lanes.cxl.post(pb.cancel_all_path())

    # ---- 9. flatten the 1–2 share residual -------------------------------------------------------------
    st, body, ts, tr = lanes.poll.get(PathBuilder.SECURITIES)
    pos = int(round(next(r for r in _loads(body) if r["ticker"] == sec_ticker)["position"]))
    if pos:
        lanes.ord1.post(pb.order_path(0, 1 if pos > 0 else 0, 0, abs(pos)))
        print(f"\n## 9. flattened {pos} shares of {sec_ticker}")
    _save(res, out)
    lanes.close_all()
    print("\nSet the flags in Config / params.json and paste this into PROGRESS.md.")
    return 0


def _save(res, out):
    os.makedirs("runs", exist_ok=True)
    path = out or os.path.join("runs", f"probe_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w") as f:
        json.dump(res, f, indent=2, default=str)
    print(f"\n(saved {path})")
