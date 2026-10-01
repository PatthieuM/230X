"""Reduce the ALGO1 bot's raw run tapes to the small CSVs the notebook plots.

The raw tapes (runs/<name>/loops.parquet, 10-40 MB each, one row per poll of /v1/securities) stay
off the repo. Usage:

    python extract_tapes.py <runs_dir>      # writes data/ next to this file

Runs: t1 = test session (10 orders/s, nearly uncontested); a1 / a2 = the heat, accounts 1 and 2
(500 orders/s, ~50 bots). Prices are integer ticks of $0.01. `pos` is the server's aggregated
CRZY position (CRZY_M + CRZY_A), which lags acknowledgements by 30-200 ms.

Note: orders.parquet in the heat runs is not used. Its 16,384-row ring holds duplicated records,
so every number here comes from loops.parquet and summary.json.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

RUNS = {"t1": "test", "a1": "heat_acct1", "a2": "heat_acct2"}
OUT = Path(__file__).resolve().parent / "data"


def load_loops(run_dir):
    l = pd.read_parquet(run_dir / "loops.parquet").sort_values("t0")
    ok = (l.t0 > 0) & (l[["bid_m", "ask_m", "bid_a", "ask_a"]] > 0).all(axis=1)
    l = l[ok].copy()
    l["t"] = (l.t0 - l.t0.iloc[0]) / 1e9
    l["edge"] = np.maximum(l.bid_m - l.ask_a, l.bid_a - l.ask_m)   # ticks; > 0 = crossed
    return l


def timeline(l):
    """1-second buckets: venue mids ($), min/max position (keeps one-leg spikes), number of polls."""
    b = l.t.astype(int)
    g = l.groupby(b)
    return pd.DataFrame({
        "t_s": g.t.first().index,
        "mid_m": g.apply(lambda x: ((x.bid_m + x.ask_m) / 200).mean()),
        "mid_a": g.apply(lambda x: ((x.bid_a + x.ask_a) / 200).mean()),
        "pos_min": g.pos.min(), "pos_max": g.pos.max(),
        "polls": g.size(),
    }).reset_index(drop=True)


def crosses(l):
    """One row per cross episode: consecutive polls with edge > 0."""
    t, e = l.t.values, l.edge.values
    cr = e > 0
    st = np.flatnonzero(cr & ~np.r_[False, cr[:-1]])
    en = np.flatnonzero(cr & ~np.r_[cr[1:], False])
    nxt = np.minimum(en + 1, len(t) - 1)                    # life runs to the first uncrossed poll
    return pd.DataFrame({
        "t_start_s": t[st], "life_ms": (t[nxt] - t[st]) * 1e3,
        "max_edge_ticks": [e[a:z + 1].max() for a, z in zip(st, en)],
        "polls": en - st + 1,
    })


def main(runs_dir):
    OUT.mkdir(exist_ok=True)
    rows = []
    for run, label in RUNS.items():
        d = Path(runs_dir) / run
        l, s = load_loops(d), json.load(open(d / "summary.json"))
        cx = crosses(l)
        if run == "a1":                                    # the run Figure 6.1 plots
            timeline(l).to_csv(OUT / f"timeline_{label}.csv", index=False, float_format="%.4f")
        rows.append({
            "run": label, "minutes": l.t.iloc[-1] / 60,
            "ticks": int(l.tick.max() - l.tick.min()),
            "orders_per_s_limit": s["sheet"]["api_orders_per_second"],
            "polls_per_s": len(l) / l.t.iloc[-1],
            "cross_episodes": len(cx), "median_max_edge_ticks": cx.max_edge_ticks.median(),
            "pairs_sent": s["pairs_sent"], "pairs_both_filled": s["pairs_both_filled"],
            "completion_pct": 100 * s["pairs_both_filled"] / s["pairs_sent"],
            "orders_sent": s["orders_sent"], "cancels": s["cancels"],
            "max_abs_pos": int(l.pos.abs().max()), "stopped": s["stopped"],
        })
    pd.DataFrame(rows).to_csv(OUT / "runs_summary.csv", index=False, float_format="%.2f")


if __name__ == "__main__":
    main(sys.argv[1])
