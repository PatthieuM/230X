"""Benchmarks (µs, p50/p95). Numbers go into PROGRESS.md after every change to the loop.

    python -m algo1 bench                 micro: parse, state.update, each alpha, risk, plan, ring.write
    python -m algo1 bench --pair --delay-a 50
                                          pair modes against an in-process mock: sequential /
                                          concurrent / confirm-then-hedge → send→ack, leg gap
    python -m algo1 bench --lanes         4 GETs concurrent vs sequential: does the server serialise?

Budget: sum of the in-loop stages (parse → plan) < 200 µs p50 (asserted with a generous bound in
tests/test_bench.py).
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from time import perf_counter_ns

import numpy as np

from .alpha import cross, quotes, stale
from .api.budget import Budget
from .api.messages import PathBuilder, parse_securities
from .core.clock import Clock
from .core.params import Config
from .core.types import A, ACKED, BUY, Inventory, LIMIT, M, Order, P_QUOTE, SELL, Snapshot
from .execution.reconciler import Reconciler
from .execution.risk import Risk
from .market.state import MarketState
from .monitor.ringbuf import RingSet


def timeit(fn, n=2000, warm=50):
    for _ in range(warm):
        fn()
    xs = np.empty(n)
    for k in range(n):
        t = perf_counter_ns()
        fn()
        xs[k] = perf_counter_ns() - t
    return float(np.percentile(xs, 50)) / 1e3, float(np.percentile(xs, 95)) / 1e3


def sample_body():
    from .sim.mock_server import MockConfig, MockMarket
    m = MockMarket(MockConfig())
    return json.dumps(m.securities()).encode()


def micro(n=2000):
    cfg = Config()
    cfg.enable_quotes = True
    body = sample_body()
    inv = Inventory()
    ms = MarketState(cfg)
    clock = Clock()
    risk = Risk(cfg)
    rec = Reconciler(cfg)
    budget = Budget(1e9, 1e9, 0)      # never denies: we time the plan, not the budget
    ring = RingSet("bench", 4096, 4096, 4096, shared=False)
    bbo, pos, pv = parse_securities(body, cfg.scale)
    # a crossed book so cross.evaluate does work
    bbo.bid[A] = bbo.ask[M] + 2
    snap = Snapshot(1, 0, perf_counter_ns(), bbo, 0, [0, 0])
    ms.update(snap, [], inv)
    # a resting quote so quotes.evaluate walks the hysteresis branch
    o = Order(M, BUY, LIMIT, 500, bbo.bid[M] - 3, P_QUOTE)
    o.state = ACKED
    o.id_server = 1
    inv.register(o)
    inv.add_resting(o)
    rows = []

    def bump():
        snap.t_recv = perf_counter_ns()
        ms.update(snap, [], inv)
    rows.append(("parse_securities", *timeit(lambda: parse_securities(body, cfg.scale), n)))
    rows.append(("Inventory.net_out", *timeit(lambda: inv.net_out(bbo), n)))
    rows.append(("state.update", *timeit(bump, n)))
    rows.append(("cross.evaluate (crossed)", *timeit(lambda: cross.evaluate(ms, inv, cfg), n)))
    rows.append(("quotes.evaluate", *timeit(lambda: quotes.evaluate(ms, inv, cfg), n)))
    rows.append(("stale.evaluate (gated off)", *timeit(lambda: stale.evaluate(ms, inv, cfg), n)))
    intents = cross.evaluate(ms, inv, cfg) + quotes.evaluate(ms, inv, cfg)
    rows.append(("risk.gate", *timeit(lambda: risk.gate(list(intents), ms, inv, clock), n)))
    gated = risk.gate(list(intents), ms, inv, clock)
    rows.append(("reconciler.plan", *timeit(lambda: rec.plan(list(gated), inv, budget), n)))
    loop_rec = (1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 1000.0, 0.0, 0, 0, 10.0, 998, 1000, 998, 1000, 1.0, 1.0, 3.0, 3.0, 0, 1.0, 1.0)
    rows.append(("ring.write_loop", *timeit(lambda: ring.write_loop(loop_rec), n)))
    rows.append(("ring.write_order", *timeit(lambda: ring.write_order(o), n)))
    total = sum(r[1] for r in rows if r[0] in ("parse_securities", "state.update", "cross.evaluate (crossed)",
                                               "quotes.evaluate", "risk.gate", "reconciler.plan", "ring.write_loop"))
    ring.close()
    return rows, total


def print_micro(rows, total, out=sys.stdout):
    print("| stage | p50 µs | p95 µs |", file=out)
    print("|---|---|---|", file=out)
    for name, p50, p95 in rows:
        print(f"| {name} | {p50:.1f} | {p95:.1f} |", file=out)
    print(f"| **in-loop sum (p50)** | **{total:.1f}** | budget 200 |", file=out)


# ---- pair modes ----------------------------------------------------------------------------
def pair_bench(host, port, key, delay_a=0, n=20, own_mock=True):
    from .api.client import Lanes
    from .core.types import P_CROSS
    from .execution.pair import PairSender
    srv = None
    if own_mock:
        from .sim.mock_server import MockConfig, MockServer
        srv = MockServer(MockConfig(port=0, delay_ms=(0, delay_a), speed=0.0, cross_every_s=1e9, passive_fill=False,
                                    orders_per_sec=100000, sigma_step=0.0, offset_sigma=0.0)).start()
        host, port, key = "127.0.0.1", srv.port, "mock"
    cfg = Config(host=host, port=port, api_key=key)
    lanes = Lanes(host, port, key)
    lanes.connect_all()
    st, body, _, _ = lanes.poll.get("/v1/securities")
    bbo, _, _ = parse_securities(body, cfg.scale)
    pb = PathBuilder(cfg.scale)
    ps = PairSender()
    results = {}
    for mode in ("sequential", "concurrent", "slow_first", "confirm_then_hedge"):
        cfg.pair_mode = "sequential" if mode == "sequential" else "concurrent"
        cfg.confirm_then_hedge = mode == "confirm_then_hedge"
        cfg.delay_ms = (0, delay_a) if mode != "concurrent" else (0, 0)
        cfg.slow_first = mode == "slow_first"
        s2a, gaps, tot, filled = [], [], [], 0
        for _ in range(n):
            if srv is not None:
                with srv.market.lock:
                    srv.market.end_cross()
                    srv.market.force_cross(venue=1, direction="up", ticks=3, life_ms=10000, qty=100000)
                st, body, _, _ = lanes.poll.get("/v1/securities")
                bbo, _, _ = parse_securities(body, cfg.scale)
            o1 = Order(M, BUY, LIMIT, 100, bbo.ask[M], P_CROSS)
            o2 = Order(A, SELL, LIMIT, 100, bbo.bid[A], P_CROSS)
            t0 = perf_counter_ns()
            ps.send(lanes, pb, o1, o2, cfg)
            t1 = perf_counter_ns()
            s2a += [(o.t_ack - o.t_send) / 1e3 for o in (o1, o2) if o.t_send]
            if o1.t_send and o2.t_send:
                gaps.append(abs(o1.t_send - o2.t_send) / 1e3)
            filled += int(o1.filled > 0 and o2.filled > 0)
            tot.append((t1 - t0) / 1e3)
            lanes.cxl.post(pb.cancel_all_path())
        results[mode] = {"send_ack_p50": statistics.median(s2a), "leg_gap_p50": statistics.median(gaps) if gaps else float("nan"),
                         "pair_total_p50": statistics.median(tot), "pair_total_p95": float(np.percentile(tot, 95)),
                         "both_filled": f"{filled}/{n}"}
    ps.close()
    lanes.close_all()
    if srv:
        srv.stop()
    return results


def lanes_bench(host, port, key, n=50, own_mock=True):
    """4 GETs at once vs one after the other. Ratio ≈ 1 → the server serialises."""
    from concurrent.futures import ThreadPoolExecutor
    from .api.client import Lane
    srv = None
    if own_mock:
        from .sim.mock_server import MockConfig, MockServer
        srv = MockServer(MockConfig(port=0, speed=0.0, cross_every_s=1e9)).start()
        host, port, key = "127.0.0.1", srv.port, "mock"
    lanes = [Lane(f"l{i}", host, port, key) for i in range(4)]
    for l in lanes:
        l.connect()
    seq, conc = [], []
    with ThreadPoolExecutor(4) as pool:
        for _ in range(n):
            t = perf_counter_ns()
            for l in lanes:
                l.get("/v1/securities")
            seq.append((perf_counter_ns() - t) / 1e3)
            t = perf_counter_ns()
            list(pool.map(lambda l: l.get("/v1/securities"), lanes))
            conc.append((perf_counter_ns() - t) / 1e3)
    for l in lanes:
        l.close()
    if srv:
        srv.stop()
    return {"sequential_4_p50": statistics.median(seq), "concurrent_4_p50": statistics.median(conc),
            "speedup": statistics.median(seq) / max(1e-9, statistics.median(conc))}


# ---- A/B: inventory skew ------------------------------------------------------------------------
def ab_bench(seconds=15.0, reps=2, speed=5.0, variants=None):
    """Run the engine with quotes against a fresh in-process mock per rep and variant.
    Reports edge pnl, fills, share of loops at the inventory cap, and fill asymmetry.
    Caveat: the mock's passive fills are 'touch trades through' only (no AI flow yet), so maker
    numbers are indicative until sim/mock_server.py gets AI flow (Friday)."""
    import os
    import tempfile
    import pandas as pd
    from .engine.loop import Engine
    from .sim.mock_server import MockConfig, MockServer
    variants = variants or {"no_skew": {"skew_at_limit": 0.0}, "skew10": {"skew_at_limit": 10.0}}
    out = {}
    for name, over in variants.items():
        rows = []
        for rep in range(reps):
            srv = MockServer(MockConfig(port=0, speed=speed, ticks=int(seconds * speed) + 50, cross_every_s=2.0,
                                        cross_life_ms=400, hunters=1, delay_ms=(0, 5), seed=100 + rep)).start()
            cfg = Config(host="127.0.0.1", port=srv.port, api_key="mock", enable_quotes=True, run_name=f"ab_{name}_{rep}",
                         params_path="/nonexistent", kill_file="/nonexistent/KILL", max_abs_pos=3000)
            for k, v in over.items():
                setattr(cfg, k, v)
            d = tempfile.mkdtemp()
            eng = Engine(cfg, run_dir=d, shared=False, max_seconds=seconds)
            eng.init()
            eng.run()
            L = pd.read_parquet(os.path.join(d, "loops.parquet"))
            F = pd.read_parquet(os.path.join(d, "fills.parquet"))
            at_cap = float((L["pos"].abs() >= cfg.max_abs_pos).mean()) if len(L) else 0.0
            pq = F[F["purpose"] == 1]
            buys = int((pq["side"] == 0).sum())
            sells = int((pq["side"] == 1).sum())
            rows.append({"edge": float((F["edge"] * F["qty"]).sum()), "fills_quote": len(pq), "at_cap": at_cap,
                         "asym": (buys - sells) / max(1, buys + sells), "abs_pos_mean": float(L["pos"].abs().mean()) if len(L) else 0.0,
                         "pairs": eng.stats["pairs"]})
            srv.stop()
        df = pd.DataFrame(rows)
        out[name] = {"mean": df.mean().to_dict(), "se": (df.std() / np.sqrt(max(1, len(df)))).to_dict(), "n": len(df)}
    return out


def _one_run(seed, seconds, speed, over, max_abs_pos=3000, quantiles=(0.25, 0.5, 0.75, 1.0)):
    """One heat on a fresh mock with `seed`; returns outcomes + cumulative-edge curve."""
    import os
    import tempfile
    import pandas as pd
    from .engine.loop import Engine
    from .sim.mock_server import MockConfig, MockServer
    srv = MockServer(MockConfig(port=0, speed=speed, ticks=int(seconds * speed) + 50, cross_every_s=2.0,
                                cross_life_ms=400, hunters=1, delay_ms=(0, 5), seed=seed)).start()
    cfg = Config(host="127.0.0.1", port=srv.port, api_key="mock", enable_quotes=True, run_name=f"crn_{seed}",
                 params_path="/nonexistent", kill_file="/nonexistent/KILL", max_abs_pos=max_abs_pos, seed=seed)
    for k, v in over.items():
        setattr(cfg, k, v)
    d = tempfile.mkdtemp()
    eng = Engine(cfg, run_dir=d, shared=False, max_seconds=seconds)
    eng.init()
    eng.run()
    L = pd.read_parquet(os.path.join(d, "loops.parquet"))
    F = pd.read_parquet(os.path.join(d, "fills.parquet"))
    srv.stop()
    t0 = int(L["t0"].iloc[0]) if len(L) else 0
    t1 = int(L["t6"].iloc[-1]) if len(L) else 1
    curve = []
    for q in quantiles:
        cut = t0 + q * (t1 - t0)
        sub = F[F["t_seen"] <= cut]
        curve.append(float((sub["edge"] * sub["qty"]).sum()))
    rms = float(np.sqrt((L["pos"].astype(float) ** 2).mean())) if len(L) else 0.0
    return {"seed": seed, "edge": curve[-1], "rms_pos": rms, "pairs": eng.stats["pairs"],
            "fills_quote": int((F["purpose"] == 1).sum()), "curve": curve}


def crn_bench(pairs=10, seconds=8.0, speed=5.0, baseline=None, candidates=None, alpha=0.05, out_path=None):
    """Common-random-number pilot. For each candidate: paired runs on seeds 1..pairs (baseline and
    candidate on the same seed) plus an equal-budget independent design (baseline on seeds
    1..pairs reused, candidate on disjoint seeds). Reports paired vs independent SE (bootstrap
    CI), outcome correlation, correlation decay over the heat, RMS inventory as a secondary paired
    outcome, and Holm-adjusted selection across all candidates. Everything is saved to
    runs/crn_<ts>.json with the seeds so runs can be re-aligned later."""
    import json
    import os
    import time as _time
    from .sim.analysis import corr_decay, holm, paired_stats
    baseline = baseline or {}          # the shipped Config: one change at a time (10 Sep: a skew=0
                                       # default baseline confounded the tol / per-ticker A/B)
    candidates = candidates or {"noskew": {"skew_at_limit": 0.0}}
    seeds = list(range(1, pairs + 1))
    seeds_ind = list(range(pairs + 1, 2 * pairs + 1))
    base_runs = {s: _one_run(s, seconds, speed, baseline) for s in seeds}
    results = {}
    for name, over in candidates.items():
        cand_runs = {s: _one_run(s, seconds, speed, over) for s in seeds}
        cand_ind = {s: _one_run(s, seconds, speed, over) for s in seeds_ind}
        b = [base_runs[s]["edge"] for s in seeds]
        c = [cand_runs[s]["edge"] for s in seeds]
        ci = [cand_ind[s]["edge"] for s in seeds_ind]
        st = paired_stats(b, c, b, ci)
        rms = paired_stats([base_runs[s]["rms_pos"] for s in seeds], [cand_runs[s]["rms_pos"] for s in seeds])
        decay = corr_decay([base_runs[s]["curve"] for s in seeds], [cand_runs[s]["curve"] for s in seeds])
        results[name] = {"config": over, "edge": st, "rms_pos": rms, "corr_decay": decay,
                         "paired": {"base": b, "cand": c}, "independent_cand": ci}
    adj, rej = holm([results[n]["edge"]["p"] for n in results], alpha)
    for (n, r), a, ok in zip(results.items(), adj, rej):
        r["holm_p"] = float(a)
        r["promote"] = bool(ok) and r["edge"]["mean_diff"] > 0
    out = {"t": _time.strftime("%Y-%m-%d %H:%M:%S"), "pairs": pairs, "seconds": seconds, "speed": speed,
           "baseline": baseline, "seeds_paired": seeds, "seeds_independent": seeds_ind,
           "baseline_edge": {s: base_runs[s]["edge"] for s in seeds}, "results": results}
    os.makedirs("runs", exist_ok=True)
    out_path = out_path or os.path.join("runs", f"crn_{_time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    out["path"] = out_path
    return out


def print_crn(out):
    print(f"CRN pilot: {out['pairs']} pairs × {out['seconds']} s at {out['speed']}x, baseline {out['baseline']} → {out['path']}")
    print("| candidate | mean Δedge | SE paired | SE indep | ratio [90% CI] | corr | RMS pos Δ (corr) | corr@25/50/75/100% | p | Holm p | promote |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for n, r in out["results"].items():
        e, m = r["edge"], r["rms_pos"]
        dec = "/".join(f"{c:.2f}" for _, c in r["corr_decay"])
        print(f"| {n} | {e['mean_diff']:+.0f} | {e['se_paired']:.0f} | {e['se_indep']:.0f} | {e['se_ratio']:.2f} "
              f"[{e['se_ratio_ci90'][0]:.2f}, {e['se_ratio_ci90'][1]:.2f}] | {e['corr']:.2f} | {m['mean_diff']:+.0f} ({m['corr']:.2f}) | "
              f"{dec} | {e['p']:.3f} | {r['holm_p']:.3f} | {'yes' if r['promote'] else 'no'} |")


def main(a):
    from .core.secrets import apply_to_config
    live = a.host is not None
    cfg = Config()
    if live:
        apply_to_config(cfg, a.secrets)
        cfg.apply_overrides(host=a.host, port=a.port, api_key=a.key)
    if getattr(a, "crn", False):
        cands = {}
        for spec in (a.candidate or ["skew10:skew_at_limit=10"]):
            name, _, kv = spec.partition(":")
            over = {}
            for pair in kv.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    over[k.strip()] = float(v) if v.replace(".", "", 1).replace("-", "", 1).isdigit() else v
            cands[name] = over
        out = crn_bench(a.pairs, a.seconds, candidates=cands)
        print_crn(out)
        return 0
    if getattr(a, "ab", False):
        r = ab_bench(a.seconds, a.reps)
        print("| variant | edge pnl (ticks·sh) | quote fills | loops at cap | fill asym (buy−sell)/n | mean |pos| | pairs |")
        print("|---|---|---|---|---|---|---|")
        for k, v in r.items():
            m, se = v["mean"], v["se"]
            print(f"| {k} (n={v['n']}) | {m['edge']:.0f} ± {se['edge']:.0f} | {m['fills_quote']:.1f} | {m['at_cap']:.3f} | "
                  f"{m['asym']:+.2f} | {m['abs_pos_mean']:.0f} | {m['pairs']:.1f} |")
        return 0
    if not (a.pair or a.lanes):
        rows, total = micro(a.n)
        print_micro(rows, total)
        return 0
    if a.pair:
        r = pair_bench(cfg.host, cfg.port, cfg.api_key, a.delay_a, n=min(a.n, 50), own_mock=not live)
        print(f"| mode (delay_a={a.delay_a} ms) | send→ack p50 µs | leg gap p50 µs | pair total p50 µs | p95 | both filled |")
        print("|---|---|---|---|---|---|")
        for k, v in r.items():
            print(f"| {k} | {v['send_ack_p50']:.0f} | {v['leg_gap_p50']:.0f} | {v['pair_total_p50']:.0f} | {v['pair_total_p95']:.0f} | {v['both_filled']} |")
    if a.lanes:
        r = lanes_bench(cfg.host, cfg.port, cfg.api_key, own_mock=not live)
        print(f"4 GETs sequential p50 {r['sequential_4_p50']:.0f} µs · concurrent p50 {r['concurrent_4_p50']:.0f} µs · "
              f"speedup {r['speedup']:.2f}x  ({'server serialises' if r['speedup'] < 1.5 else 'parallel OK'})")
    return 0
