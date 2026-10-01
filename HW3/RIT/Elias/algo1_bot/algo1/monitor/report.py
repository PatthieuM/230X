"""Post-run report from the parquet dumps of the three rings (+ optional recording).

    python -m algo1 report --name run

Sections:
    latency     poll RTT, in-loop stages, send→ack per lane-ish (by venue), leg gap,
                decision→ack per pair
    pairs       sent / both filled / hit ratio; expected vs realised edge by direction
    fills       count and edge by purpose; pnl attribution = Σ edge·qty
                (Fill.edge = side·(fair_at_fill − px) − fee + rebate)
    passive     spread capture at the fill vs the drift of fair at +0.2 / +1 / +5 s after it
                (adverse selection) — the only evidence that decides whether quoting should exist;
                it must come from live runs, the mock has no such joint law
    budget      denials by class, 429 count
    survival    if a recording is present: cross lifetimes vs our decision→ack → P(fill | ℓ)

Output is printed and written to `<run_dir>/report.md`.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from ..core.types import PURPOSES, STATE_NAME, VENUE_NAME


def q(x, p):
    x = np.asarray(x, dtype=float)
    return float(np.percentile(x, p)) if x.size else float("nan")


def fmt_lat(name, x):
    x = np.asarray(x, dtype=float) / 1e3
    if x.size == 0:
        return f"| {name} | – | – | – | 0 |"
    return f"| {name} | {q(x,50):.0f} | {q(x,95):.0f} | {q(x,99):.0f} | {x.size} |"


def build(run_dir):
    L = pd.read_parquet(os.path.join(run_dir, "loops.parquet"))
    O = pd.read_parquet(os.path.join(run_dir, "orders.parquet"))
    F = pd.read_parquet(os.path.join(run_dir, "fills.parquet"))
    summ = {}
    sp = os.path.join(run_dir, "summary.json")
    if os.path.exists(sp):
        with open(sp) as f:
            summ = json.load(f)
    lines = [f"# ALGO1 run report — `{run_dir}`", ""]
    if summ:
        lines += [f"loops {summ.get('loops')} · stopped: {summ.get('stopped')} · final pos {summ.get('pos')} "
                  f"· pairs {summ.get('pairs_sent')} / both filled {summ.get('pairs_both_filled')} "
                  f"· fills {summ.get('fills')} · edge pnl {summ.get('pnl_edge_ticks')} ticks·sh "
                  f"· 429s {summ.get('n429')} · breaker trips {summ.get('breaker_trips')}", ""]
    # ---- latency ---------------------------------------------------------------------------
    lines += ["## Latency (µs)", "", "| stage | p50 | p95 | p99 | n |", "|---|---|---|---|---|"]
    if len(L):
        lines.append(fmt_lat("poll RTT (tr−ts)", L["tr"] - L["ts"]))
        lines.append(fmt_lat("parse (t1−tr)", L["t1"] - L["tr"]))
        lines.append(fmt_lat("state (t2−t1)", L["t2"] - L["t1"]))
        lines.append(fmt_lat("alphas (t3−t2)", L["t3"] - L["t2"]))
        lines.append(fmt_lat("risk (t4−t3)", L["t4"] - L["t3"]))
        lines.append(fmt_lat("plan (t5−t4)", L["t5"] - L["t4"]))
        lines.append(fmt_lat("in-loop total (t5−tr)", L["t5"] - L["tr"]))
        lines.append(fmt_lat("dispatch (t6−t5)", L["t6"] - L["t5"]))
        lines.append(fmt_lat("loop period", np.diff(L["t0"].values) if len(L) > 1 else []))
    Od = O.drop_duplicates("id_local", keep="last") if len(O) else O
    acked = Od[(Od["t_ack"] > 0) & (Od["t_send"] > 0)] if len(Od) else Od
    for v in (0, 1):
        sub = acked[acked["venue"] == v] if len(acked) else acked
        lines.append(fmt_lat(f"send→ack venue {VENUE_NAME[v]}", (sub["t_ack"] - sub["t_send"]) if len(sub) else []))
    cx = Od[(Od["t_cancel_ack"] > 0)] if len(Od) else Od
    lines.append(fmt_lat("cancel send→ack", (cx["t_cancel_ack"] - cx["t_cancel_send"]) if len(cx) else []))
    # pairs: group P0 orders by snapshot_id + t_intent
    pairs = acked[acked["priority"] == 0] if len(acked) else acked
    gaps, d2a, both, n_pairs, exp_edge, real_edge = [], [], 0, 0, [], []
    if len(pairs):
        bursts = [g for _, g in pairs.groupby(["snapshot_id", "t_intent"])]
        legs_pairs = []
        for g in bursts:                       # a burst holds k pairs; legs were created buy, sell, buy, sell…
            g = g.sort_values("id_local")
            for j in range(0, len(g) - 1, 2):
                legs_pairs.append(g.iloc[j:j + 2])
        for g in legs_pairs:
            sid = int(g["snapshot_id"].iloc[0])
            ti = int(g["t_intent"].iloc[0])
            n_pairs += 1
            gaps.append(abs(int(g["t_send"].iloc[0]) - int(g["t_send"].iloc[1])))
            lp = L[L["i"] == sid]
            t0 = int(lp["t0"].iloc[0]) if len(lp) else int(ti)
            d2a.append(int(g["t_ack"].max()) - t0)
            if (g["state"] == 4).all():
                both += 1
                buy = g[g["side"] == 0].iloc[0]
                sell = g[g["side"] == 1].iloc[0]
                exp_edge.append(float(buy["expected_edge"]))
                real_edge.append(float(sell["vwap"] - buy["vwap"]))
    lines.append(fmt_lat("pair leg gap", gaps))
    lines.append(fmt_lat("pair decision→ack (max t_ack − t0)", d2a))
    lines += ["", "## Pairs", "",
              f"pairs with both legs acked: {n_pairs} · both filled: {both} · hit ratio: "
              f"{(both / n_pairs if n_pairs else float('nan')):.2f}"]
    if real_edge:
        lines.append(f"expected edge p50 {q(exp_edge,50):.2f} ticks · realised (vwap sell − vwap buy) p50 "
                     f"{q(real_edge,50):.2f} ticks · mean slippage {np.mean(np.array(exp_edge)-np.array(real_edge)):.2f}")
    if n_pairs:
        one_leg = sum(1 for g in legs_pairs if (g["state"] == 4).sum() == 1)
        hit = both / n_pairs
        # completion-adjusted value per pair: hit·edge − (1−hit)·(unwind cost ≈ half the tighter spread)
        s_half = float(np.median(np.minimum(L["ask_m"] - L["bid_m"], L["ask_a"] - L["bid_a"])) / 2) if len(L) and "ask_m" in L else float("nan")
        e_med = q(exp_edge, 50) if exp_edge else float("nan")
        lines.append(f"one-leg pairs: {one_leg} ({one_leg / n_pairs:.1%}) · completion-adjusted edge ≈ "
                     f"{hit:.2f}·{e_med:.2f} − {1 - hit:.2f}·{s_half:.1f} = {hit * e_med - (1 - hit) * s_half:.2f} ticks "
                     f"(vs {e_med:.2f} displayed)")
    # ---- fills -----------------------------------------------------------------------------
    lines += ["", "## Fills and P&L attribution (ticks·shares)", "", "| purpose | fills | qty | edge pnl | passive |", "|---|---|---|---|---|"]
    if len(F):
        for p, g in F.groupby("purpose"):
            lines.append(f"| {PURPOSES[int(p)]} | {len(g)} | {int(g['qty'].sum())} | {float((g['edge']*g['qty']).sum()):.0f} | {int(g['passive'].sum())} |")
        lines.append(f"| **total** | {len(F)} | {int(F['qty'].sum())} | {float((F['edge']*F['qty']).sum()):.0f} | |")
    # ---- adverse selection: post-fill drift of fair for passive fills ----------------------------
    # the quantity a passive quote is judged on: side·(fair_{t+h} − px) − side·(fair_t − px) = side·Δfair.
    # Positive spread capture at the fill that is eaten by an adverse move is the maker's loss.
    if len(F) and len(L):
        pf = F[F["passive"] == 1]
        if len(pf):
            tl = L["t6"].values
            fl = L["fair"].values
            lines += ["", "## Passive fills: spread capture vs post-fill drift (ticks, per share)", "",
                      "a = mean(Δfair·side)/σ at +sigma_horizon is the adverse-selection term for d_star (`cfg.adverse_a`).", "",
                      "| horizon | n | edge at fill | Δfair·side | net | a (σ units) |", "|---|---|---|---|---|---|"]
            has_sig = "sig_m" in L
            for h_s in (0.2, 1.0, 5.0):
                h = int(h_s * 1e9)
                idx = np.searchsorted(tl, pf["t_seen"].values + h)
                ok = idx < len(tl)
                if ok.sum() == 0:
                    continue
                sign = np.where(pf["side"].values[ok] == 0, 1.0, -1.0)
                drift = sign * (fl[idx[ok]] - pf["fair_at_fill"].values[ok])
                edge0 = sign * (pf["fair_at_fill"].values[ok] - pf["px"].values[ok])
                a_txt = "–"
                if has_sig:
                    i0 = np.searchsorted(tl, pf["t_seen"].values[ok])
                    i0 = np.minimum(i0, len(tl) - 1)
                    sig = np.where(pf["venue"].values[ok] == 0, L["sig_m"].values[i0], L["sig_a"].values[i0])
                    oks = sig > 0.05                     # a flat tape makes a meaningless
                    a_txt = f"{float(np.mean(drift[oks] / sig[oks])):.2f}" if oks.sum() >= 10 else "– (σ too small)"
                lines.append(f"| +{h_s:g} s | {int(ok.sum())} | {edge0.mean():.2f} | {drift.mean():.2f} | {(edge0 + drift).mean():.2f} | {a_txt} |")
    # ---- orders by state -------------------------------------------------------------------
    lines += ["", "## Orders by final state", ""]
    if len(Od):
        vc = Od["state"].value_counts()
        lines.append(", ".join(f"{STATE_NAME[int(s)]} {int(n)}" for s, n in vc.items()))
    # ---- budget ------------------------------------------------------------------------------
    if summ:
        lines += ["", "## Budget", "", f"deny by class {summ.get('deny')} · 429 {summ.get('n429')} · "
                  f"pair denied on budget {summ.get('pair_denied_budget')} · poll 429 {summ.get('poll_429')}"]
    # ---- survival vs recording -------------------------------------------------------------
    rec = os.path.join(run_dir, "recording.parquet")
    if os.path.exists(rec) and d2a:
        from ..sim.analysis import cross_events
        R = pd.read_parquet(rec)
        ev = cross_events(R)
        if len(ev):
            life = ev["life_ns"].values / 1e3
            ours = np.array(d2a) / 1e3
            p_fill = float((life[:, None] > ours[None, :]).mean())
            lines += ["", "## Survival", "",
                      f"cross lifetimes p50 {q(life,50):.0f} µs p90 {q(life,90):.0f} µs (n={len(life)}) · "
                      f"our decision→ack p50 {q(ours,50):.0f} µs · P(cross outlives us) ≈ {p_fill:.2f}"]
    text = "\n".join(lines) + "\n"
    with open(os.path.join(run_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


def report(run_dir):
    text = build(run_dir)
    print(text)
    return text
