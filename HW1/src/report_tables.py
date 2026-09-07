"""Write the report appendix tables (LaTeX) from the notebook's output CSVs.

Run from HW1/ after executing the notebook:

    python src/report_tables.py

Produces report/appendix_tables.tex with one ``table`` environment per
appendix table, plus the corrected depth-in-band rows for Tables 3 and 4 of
the main text. Uses booktabs rules (\\toprule etc.).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("outputs")
DST = Path("report") / "appendix_tables.tex"
INSTR = ["AAPL", "GPRO", "EURUSD", "USDJPY", "EURJPY"]
SHOWN = {"AAPL": "AAPL", "GPRO": "GPRO", "EURUSD": "EUR/USD", "USDJPY": "USD/JPY", "EURJPY": "EUR/JPY"}
# Report units for depth: thousands of shares (stocks), millions of base (FX).
DEPTH_SCALE = {"AAPL": 1e-3, "GPRO": 1e-3, "EURUSD": 1e-6, "USDJPY": 1e-6, "EURJPY": 1e-6}


def f(x, nd=2, comma=False):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "--"
    return f"{x:,.{nd}f}" if comma else f"{x:.{nd}f}"


def table(caption, label, header, rows, notes, colspec=None):
    ncol = len(header)
    colspec = colspec or ("l" + "r" * (ncol - 1))
    lines = [
        "\\begin{table}[htbp]",
        "\\centering",
        f"\\caption{{{caption}}}",
        f"\\label{{{label}}}",
        "\\footnotesize",
        f"\\begin{{tabular}}{{{colspec}}}",
        "\\toprule",
        " & ".join(header) + " \\\\",
        "\\midrule",
    ]
    for r in rows:
        if r == "MIDRULE":
            lines.append("\\midrule")
        elif isinstance(r, str) and r.startswith("PANEL:"):
            lines.append(f"\\multicolumn{{{ncol}}}{{l}}{{\\emph{{{r[6:]}}}}} \\\\")
        else:
            lines.append(" & ".join(r) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "", f"\\begin{{minipage}}{{0.95\\linewidth}}\\footnotesize\\emph{{Notes:}} {notes}\\end{{minipage}}", "\\end{table}", ""]
    return "\n".join(lines)


def main() -> None:
    summ = pd.read_csv(OUT / "summary_statistics.csv", index_col=[0, 1])
    liq = pd.read_csv(OUT / "liquidity_diagnostics.csv", index_col=0)
    ret = pd.read_csv(OUT / "return_diagnostics.csv", index_col=[0, 1, 2])
    size = pd.read_csv(OUT / "impact_by_trade_size.csv", index_col=[0, 1])
    arb = pd.read_csv(OUT / "triangular_arbitrage_summary.csv", index_col=0)["value"]
    decay = pd.read_csv(OUT / "triangular_arbitrage_execution_decay.csv", index_col=0)
    varliq = pd.read_csv(OUT / "variance_liquidity_30min.csv", index_col=0)
    timing = pd.read_csv(OUT / "timings.csv", index_col=0)
    parts = []

    # ---- corrected depth-in-band rows for Tables 3 and 4 --------------------
    rows = []
    for inst in INSTR:
        unit = "000 shares" if inst in ("AAPL", "GPRO") else "millions base"
        for side in ("bid", "ask"):
            s = summ.loc[(inst, f"{side}_depth_2x")] * DEPTH_SCALE[inst]
            n = int(summ.loc[(inst, f"{side}_depth_2x"), "n"])
            rows.append([SHOWN[inst], f"{side.capitalize()} depth, $2\\bar s$ ({unit})", str(n)] + [f(s[c], 4) for c in ("mean", "std", "min", "p25", "median", "p75", "max")])
    parts.append(table(
        "Depth at twice the daily average spread, without the zeroing rule (replacement rows for Tables 3 and 4)",
        "tab:band_depth_corrected",
        ["Instrument", "Measure", "N", "Mean", "Std. dev.", "Min.", "P25", "Median", "P75", "Max."], rows,
        "Sums displayed quantity at prices within $m_t \\pm 2\\bar s_{\\text{day}}$ on each side, exactly as on the Discussion 2 slide; the band is empty only when the best quote itself lies outside it, i.e.\\ when the contemporaneous spread exceeds four times its daily average, which never occurs in this sample. "
        "The earlier draft additionally set both sides to zero whenever the spread exceeded $2\\bar s$; that rule is not part of the slide definition and affected a few AAPL and currency minutes.",
        colspec="llrrrrrrrr"))

    # ---- A2 missing trades and activity ---------------------------------------
    rows = []
    for inst in INSTR:
        r = liq.loc[inst]
        rows.append([SHOWN[inst], f(100 * r["zero_trade_share_1s"], 1), str(int(r["zero_trade_minutes"])), f(r["median_max_trade_gap_s"], 1),
                     f(r["n_trades_open30_over_rest"], 2), f(r["n_trades_lunch_over_rest"], 2), f(r["n_trades_last30_over_rest"], 2),
                     f(r.get("n_orders_open30_over_rest", np.nan), 2), f(r.get("n_orders_lunch_over_rest", np.nan), 2), f(r.get("n_orders_last30_over_rest", np.nan), 2)])
    parts.append(table(
        "Missing trades and intraday trading activity",
        "tab:missing_trades_activity",
        ["Instrument", "No-trade seconds (\\%)", "No-trade minutes", "Median longest gap (s)", "Trades 09:30--10:00", "Trades 12:00--13:30", "Trades 15:30--16:00", "Orders 09:30--10:00", "Orders 12:00--13:30", "Orders 15:30--16:00"], rows,
        "No-trade seconds is the share of the 23,400 one-second bars in 09:30--16:00 with no transaction; no-trade minutes is the count of the 390 minutes with no transaction; the longest gap is the median across minutes of the longest transaction-free interval inside the minute (60 s for a silent minute). "
        "Activity columns are the mean per-minute number of trades (new displayed orders) inside the window divided by the mean outside it; a U-shape is open $>1$, close $>1$ and lunch $<1$. Order counts exist only for NASDAQ.",
        colspec="lrrrrrrrrr"))

    # ---- A3 impact by size ----------------------------------------------------
    rows = []
    for inst in INSTR:
        s = size.loc[inst]
        rows.append([SHOWN[inst]] + [str(int(s.loc[t, "n"])) for t in ("small", "medium", "large")]
                    + [f(s.loc["medium", "dollar_min"], 0, True), f(s.loc["large", "dollar_min"], 0, True)]
                    + [f(s.loc[t, "lambda_bps"], 2) for t in ("small", "medium", "large")])
    parts.append(table(
        "Five-second price impact by dollar-size tercile of the trade",
        "tab:impact_by_size",
        ["Instrument", "N small", "N medium", "N large", "Medium from (\\$)", "Large from (\\$)", "$\\lambda$ small (bps)", "$\\lambda$ medium (bps)", "$\\lambda$ large (bps)"], rows,
        "Same regression as equation (4), pooled over the session and estimated separately within each tercile of signed-trade dollar size. Tercile boundaries are the lower dollar bound of the medium and large groups. Impact rises with trade size in every instrument.",
        colspec="lrrrrrrrr"))

    # ---- A4 stale bars and drift ------------------------------------------------
    rows = []
    for inst in INSTR:
        for freq, shown in (("1s", "1 second"), ("1min", "1 minute")):
            t = ret.loc[(inst, freq, "trade_ret")]
            m = ret.loc[(inst, freq, "mid_ret")]
            rows.append([SHOWN[inst], shown, f(100 * t["stale_share"], 1), str(int(t["n"])), f(t["autocorr_lag1"], 3), str(int(t["n_nonstale"])), f(t["autocorr_lag1_nonstale"], 3),
                         f(100 * m["sum_of_returns"], 2), f(m["t_stat_mean"], 2)])
    parts.append(table(
        "Transaction returns with and without forward-filled bars, and drift",
        "tab:stale_drift",
        ["Instrument", "Frequency", "Filled bars (\\%)", "N (grid)", "$\\rho(1)$ grid", "N (trade-to-trade)", "$\\rho(1)$ trade-to-trade", "Open-to-close (\\%)", "$t$(mean)"], rows,
        "A bar with no transaction carries the previous trade forward, so its transaction return is an exact zero by construction; ``filled bars'' is their share. The grid columns are the full series used in Table 5; the trade-to-trade columns drop the filled bars before differencing. Realized variance is unaffected because the filled bars contribute zeros to $\\sum r_t^2$. "
        "Open-to-close is the sum of the midquote log returns, i.e.\\ the session's log price change; $t$(mean) is the mean midquote return divided by its standard error.",
        colspec="llrrrrrrr"))

    # ---- A5 arbitrage tiers and decay -------------------------------------------
    rows = ["PANEL:Panel A: eligible seconds by minimum edge",
            ["Raw screen (loop $>1$ on a valid book)", str(int(arb["seconds_with_arb"])), str(int(arb["n_episodes"])), f"\\${arb['total_profit_usd_if_every_second']:.2f}"],
            [f"Edge above one tick of the coarsest leg ({arb['one_tick_bps']:.3f} bps)", str(int(arb["seconds_with_arb_gt_1tick"])), str(int(arb["n_episodes_gt_1tick"])), f"\\${arb['total_profit_usd_gt_1tick']:.2f}"],
            [f"Edge above one EUR/USD pip ({arb['one_pip_bps']:.3f} bps)", str(int(arb["seconds_with_arb_gt_1pip"])), "1", f"\\${arb['total_profit_usd_gt_1pip']:.2f}"],
            "MIDRULE", "PANEL:Panel B: re-pricing each eligible second $d$ seconds after detection"]
    for d, r in decay.iterrows():
        rows.append([f"Delay {int(d)} s", str(int(r["seconds_still_profitable"])), f"{100 * r['share_of_frictionless_profit']:.1f}\\%", f"{r['mean_profit_bps_if_taken']:+.2f} bps"])
    parts.append(table(
        "Triangular arbitrage: minimum-edge tiers and the cost of acting late",
        "tab:arbitrage_tiers_decay",
        ["", "Seconds", "Episodes / profit share", "Profit / mean edge"], rows,
        "Panel A: the three books are sampled independently at the end of each second on three price grids (0.00001 for EUR/USD, 0.001 for the yen pairs); one tick of the coarsest leg at the day's mean rate is the natural floor for calling a loop an arbitrage. "
        "Panel B: every second flagged by the raw screen is re-priced at the executable quotes and binding size $d$ seconds later, keeping the loop direction chosen at detection. Columns give the seconds still profitable, the share of the frictionless \\$" + f"{arb['total_profit_usd_if_every_second']:.0f}" + " that survives, and the mean loop edge if every flagged second were taken at that delay.",
        colspec="lrrr"))

    # ---- A6 variance vs liquidity -------------------------------------------------
    rows = []
    for inst in INSTR:
        r = varliq.loc[inst]
        rows.append([SHOWN[inst]] + [f(r[c], 2) for c in ("corr_var_spread", "corr_var_depth", "corr_var_impact", "corr_var_n_trades", "corr_acf1_spread", "corr_acf1_depth", "corr_acf1_n_trades")])
    parts.append(table(
        "Thirty-minute return variance and autocorrelation against liquidity",
        "tab:variance_liquidity",
        ["Instrument", "Var--spread", "Var--depth", "Var--impact", "Var--trades", "$\\rho(1)$--spread", "$\\rho(1)$--depth", "$\\rho(1)$--trades"], rows,
        "Pearson correlations across the thirteen 30-minute windows between the sample variance (or lag-1 autocorrelation) of one-minute transaction returns and the window mean of the quoted spread, level-one depth, five-second impact and number of trades. Thirteen observations per instrument: indicative, not tests.",
        colspec="lrrrrrrr"))

    # ---- Table 7 replacement: timing -----------------------------------------------
    labels = {
        "1. load_databento_stocks": "Load and reconstruct NASDAQ books",
        "2. load_currency_data": "Load and reconstruct currency books",
        "3. minute_stats_all_instruments": "Compute per-minute statistics",
        "3b. impact_by_trade_size": "Impact by trade-size tercile",
        "4. price_series_returns_acf": "Construct price series, returns, and ACFs",
        "5. transaction_diagnostics_30min": "Compute 30-minute diagnostics",
        "6. triangular_arbitrage": "Detect triangular arbitrage (batch)",
        "6b. arbitrage_execution_decay": "Re-price arbitrage at execution delays",
        "6c. triangular_arbitrage_incremental": "Detect triangular arbitrage (one second at a time)",
        "7a. plot_stock_liquidity": "Plot NASDAQ liquidity",
        "7b. plot_fx_liquidity": "Plot currency liquidity",
        "7c. plot_30min_variance_acf": "Plot 30-minute variance and ACF",
        "7d. plot_transaction_acf": "Plot full-sample ACF",
        "7e. plot_appendix_missing_trades_activity": "Plot missing trades and activity (appendix)",
        "7f. plot_appendix_execution_decay": "Plot execution decay (appendix)",
        "7g. plot_appendix_impact_by_size": "Plot impact by trade size (appendix)",
    }
    rows = []
    for step, r in timing.iterrows():
        rows.append([labels.get(step, step), f(r["seconds"], 3), f(r["tape_s_per_compute_s"], 0, True) if np.isfinite(r["tape_s_per_compute_s"]) else "--", f(r["us_per_bar"], 1) if np.isfinite(r["us_per_bar"]) else "--"])
    rows += ["MIDRULE", ["Total timed wall clock", f(timing["seconds"].sum(), 3), "", ""]]
    parts.append(table(
        "Wall-clock timing of the notebook run (replacement for Table 7)",
        "tab:timing",
        ["Pipeline step", "Seconds", "Tape s per compute s", "$\\mu$s per 1-s bar"], rows,
        "Timings are from the final notebook outputs and depend on hardware, local file access, and software state. Tape seconds per compute second divides the market time covered by the step (23,400 one-second bars per instrument-session) by its wall-clock time. "
        "The incremental detector evaluates the triangle one second at a time with the wall clock read around every decision; its per-decision latency is reported in the text. Detection is timed after the synchronized one-second books already exist in memory.",
        colspec="lrrr"))

    DST.parent.mkdir(exist_ok=True)
    DST.write_text("% Generated by src/report_tables.py from outputs/*.csv. Requires \\usepackage{booktabs}.\n\n" + "\n".join(parts))
    print(f"wrote {DST} ({len(parts)} tables)")


if __name__ == "__main__":
    main()
