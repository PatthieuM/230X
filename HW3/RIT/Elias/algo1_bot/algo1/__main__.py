"""Command line: python -m algo1 <command>

    run       trade against the RIT client (or the mock): reads secrets.env, params.json
    mock      start the mock RIT server
    monitor   live status line from the shared-memory rings (separate process)
    report    post-run report from runs/<name>/*.parquet
    record    tape /v1/securities to runs/<name>/recording.parquet
    analyze   recording → params.json (d, c_min, ec_coef, stale_leader)
    bench     micro-benchmarks and the pair / 4-lane transport benchmarks
    verify    the pre-heat checklist at tick 0 (sheet dump, rate-limit probe, lanes); no trading
              unless --trade
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _cfg_from_args(a):
    from .core.params import Config
    from .core.secrets import apply_to_config
    cfg = Config()
    apply_to_config(cfg, a.secrets)
    cfg.apply_overrides(host=a.host, port=a.port, api_key=a.key, run_name=a.name)
    if getattr(a, "tickers", None):
        parts = [t.strip() for t in a.tickers.split(",") if t.strip()]
        cfg.tickers = (parts[0], parts[1] if len(parts) > 1 else parts[0])
    return cfg


def _add_conn(p):
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--key", default=None, help="API key (prefer secrets.env)")
    p.add_argument("--secrets", default="secrets.env")
    p.add_argument("--name", default="run", help="run name → runs/<name>/ and ring buffer names")
    p.add_argument("--tickers", default=None, help="venue M,venue A tickers (default CRZY_M,CRZY_A); one name = same ticker both")


def main(argv=None):
    try:                                   # Windows consoles default to cp1252; the reports use −, →, ·
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="algo1", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run the engine")
    _add_conn(p)
    p.add_argument("--params", default="params.json")
    p.add_argument("--quotes", action="store_true", help="enable the quoting alpha")
    p.add_argument("--d", type=float, default=None, help="quote half-distance, ticks")
    p.add_argument("--quote-size", type=int, default=None)
    p.add_argument("--max-seconds", type=float, default=0.0)
    p.add_argument("--max-loops", type=int, default=0)
    p.add_argument("--no-shm", action="store_true", help="in-process rings (no monitor)")
    p.add_argument("--confirm-then-hedge", action="store_true")
    p.add_argument("--pair-mode", choices=("sequential", "concurrent"), default=None)
    p.add_argument("--second-leg-market", action="store_true", help="second pair leg as MARKET sized to the first fill")
    p.add_argument("--leg-market", action="store_true", help="MARKET legs instead of LIMIT at the touch")
    p.add_argument("--dry", action="store_true", help="poll-only: no alphas, no sends (exercise parse/rings/monitor/report live)")

    p = sub.add_parser("mock", help="start the mock RIT server")
    p.add_argument("--port", type=int, default=9999)
    p.add_argument("--key", default="mock")
    p.add_argument("--speed", type=float, default=1.0, help="ticks per real second")
    p.add_argument("--ticks", type=int, default=300)
    p.add_argument("--delay-m", type=int, default=0)
    p.add_argument("--delay-a", type=int, default=0)
    p.add_argument("--rate", type=int, default=10, help="orders per second per ticker")
    p.add_argument("--cross-every", type=float, default=4.0)
    p.add_argument("--cross-life", type=int, default=600, help="ms")
    p.add_argument("--cross-ticks", type=int, default=3)
    p.add_argument("--cross-qty", type=int, default=6000)
    p.add_argument("--hunters", type=int, default=0)
    p.add_argument("--hunter-latency", type=int, default=250)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--self-trade", choices=("allow", "prevent"), default="allow")
    p.add_argument("--fee", type=float, default=0.0, help="$/share on both venues")
    p.add_argument("--rebate", type=float, default=0.0)
    p.add_argument("--limit-gets", action="store_true")
    p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("monitor", help="live status line")
    p.add_argument("--name", default="run")
    p.add_argument("--interval", type=float, default=0.2)
    p.add_argument("--plots", action="store_true")
    p.add_argument("--once", action="store_true")

    p = sub.add_parser("report", help="post-run report")
    p.add_argument("--name", default="run")
    p.add_argument("--dir", default=None)

    p = sub.add_parser("record", help="tape the BBO")
    _add_conn(p)
    p.add_argument("--seconds", type=float, default=20.0)

    p = sub.add_parser("analyze", help="recording → params.json")
    p.add_argument("--name", default="run")
    p.add_argument("--dir", default=None)
    p.add_argument("--out", default="params.json")
    p.add_argument("--fee", type=float, default=0.0)
    p.add_argument("--rebate", type=float, default=0.0)
    p.add_argument("--fills", action="store_true", help="fit the fill-intensity decay k from runs/<name>/orders.parquet")

    p = sub.add_parser("bench", help="benchmarks")
    p.add_argument("--ab", action="store_true", help="A/B the inventory skew on the mock (edge pnl, time at cap)")
    p.add_argument("--crn", action="store_true", help="common-random-number paired pilot vs equal-budget independent design")
    p.add_argument("--pairs", type=int, default=10, help="paired seeds (independent design uses as many disjoint seeds)")
    p.add_argument("--candidate", action="append", default=None,
                   help="name:key=val,key=val (repeatable) vs the shipped Config; default noskew:skew_at_limit=0")
    p.add_argument("--seconds", type=float, default=15.0)
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--pair", action="store_true", help="pair execution modes against a mock")
    p.add_argument("--lanes", action="store_true", help="4-lane transport test against a mock")
    p.add_argument("--delay-a", type=int, default=0)
    p.add_argument("--n", type=int, default=2000)
    p.add_argument("--host", default=None, help="bench --pair/--lanes against a live server instead of a mock")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--key", default=None)
    p.add_argument("--secrets", default="secrets.env")

    p = sub.add_parser("probe", help="case-agnostic architecture tests against any live RIT case")
    _add_conn(p)
    p.add_argument("--trade", action="store_true", help="also run the order-mechanics sections (1-share orders)")
    p.add_argument("--ticker", default=None, help="ticker for the trade sections (default: first security)")
    p.add_argument("--seconds", type=float, default=3.0, help="poll-cadence window")

    p = sub.add_parser("verify", help="pre-heat checklist at tick 0")
    _add_conn(p)
    p.add_argument("--trade", action="store_true", help="also send the 100-share pair and the 1-share self-trade test")

    a = ap.parse_args(argv)

    if a.cmd == "run":
        cfg = _cfg_from_args(a)
        cfg.params_path = a.params
        cfg.apply_overrides(enable_quotes=a.quotes or None, d=a.d, quote_size=a.quote_size,
                            confirm_then_hedge=a.confirm_then_hedge or None, pair_mode=a.pair_mode,
                            second_leg_market=a.second_leg_market or None)
        if a.leg_market:
            cfg.leg_kind = 0
        if not cfg.api_key:
            print("no API key: put RIT_API_KEY in secrets.env (see secrets.env.example) or pass --key", file=sys.stderr)
            return 2
        from .engine.loop import Engine
        eng = Engine(cfg, shared=not a.no_shm, max_loops=a.max_loops, max_seconds=a.max_seconds)
        eng.init()
        if a.dry:
            eng.alphas = []
            eng.risk.dry = True
        print(f"algo1 run '{cfg.run_name}' → {cfg.host}:{cfg.port}  sheet: fee={cfg.fee} rebate={cfg.rebate} "
              f"max_order={cfg.max_order} delay_ms={cfg.delay_ms} orders/s={cfg.orders_per_second} "
              f"alphas={[f.__module__.split('.')[-1] for f in eng.alphas]}", file=sys.stderr)
        eng.run()
        print(json.dumps(eng.summary(), indent=2, default=str))
        return 0

    if a.cmd == "mock":
        from .sim.mock_server import MockConfig, MockServer
        mc = MockConfig(port=a.port, api_key=a.key, speed=a.speed, ticks=a.ticks, delay_ms=(a.delay_m, a.delay_a),
                        orders_per_sec=a.rate, cross_every_s=a.cross_every, cross_life_ms=a.cross_life,
                        cross_ticks=a.cross_ticks, cross_qty=a.cross_qty, hunters=a.hunters,
                        hunter_latency_ms=a.hunter_latency, seed=a.seed, self_trade=a.self_trade,
                        fee=(a.fee, a.fee), rebate=(a.rebate, a.rebate), limit_gets=a.limit_gets, quiet=not a.verbose)
        srv = MockServer(mc)
        print(f"mock RIT on http://127.0.0.1:{srv.port}/v1  key={a.key}  speed={a.speed}x ticks={a.ticks} "
              f"delay_ms={mc.delay_ms} rate={a.rate}/s cross every ~{a.cross_every}s life {a.cross_life}ms", file=sys.stderr)
        srv.serve_forever()
        return 0

    if a.cmd == "monitor":
        from .monitor.live import monitor
        return monitor(a.name, a.interval, a.plots, a.once)

    if a.cmd == "report":
        from .monitor.report import report
        report(a.dir or os.path.join("runs", a.name))
        return 0

    if a.cmd == "record":
        cfg = _cfg_from_args(a)
        from .sim.recorder import record
        df, path = record(cfg, a.seconds)
        print(f"{len(df)} polls → {path}")
        return 0

    if a.cmd == "analyze":
        import pandas as pd
        from .sim.analysis import analyze, write_params
        d = a.dir or os.path.join("runs", a.name)
        if a.fills:
            from .sim.analysis import fit_fill_decay
            res = fit_fill_decay(pd.read_parquet(os.path.join(d, "orders.parquet")))
            print(json.dumps(res, indent=2, default=str))
            if res.get("success") and res["k"] > 0:
                pj = {}
                if os.path.exists(a.out):
                    with open(a.out) as f:
                        pj = json.load(f)
                pj["k_fill"] = res["k"]
                with open(a.out, "w") as f:
                    json.dump(pj, f, indent=2)
                print("→", a.out, "k_fill =", res["k"])
            return 0
        df = pd.read_parquet(os.path.join(d, "recording.parquet"))
        res = analyze(df, (a.fee, a.fee), (a.rebate, a.rebate))
        tas_path = os.path.join(d, "tas.parquet")
        if os.path.exists(tas_path):
            from .sim.analysis import fair_markout
            res["fair_markout_rmse_vs_next_trades"] = fair_markout(df, pd.read_parquet(tas_path))
        print(json.dumps(res, indent=2, default=str))
        with open(os.path.join(d, "analysis.json"), "w") as f:
            json.dump(res, f, indent=2, default=str)
        print("→", write_params(res, a.out))
        return 0

    if a.cmd == "bench":
        from .bench import main as bench_main
        return bench_main(a)

    if a.cmd == "probe":
        cfg = _cfg_from_args(a)
        from .probe import probe
        return probe(cfg, trade=a.trade, ticker=a.ticker, seconds=a.seconds)

    if a.cmd == "verify":
        cfg = _cfg_from_args(a)
        from .verify import verify
        return verify(cfg, trade=a.trade)
    return 1


if __name__ == "__main__":
    sys.exit(main())
