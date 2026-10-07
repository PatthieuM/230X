"""Write the tables and figures of the written report from the notebook's outputs.

Usage (from Part1_Speed/, after running the notebook):  python src/report_assets.py

Tables are bare `tabular` environments in report/tables/, pulled into
speed_report.tex with \\input. Figures are vector PDFs in report/figures/, drawn
at the width of the text block so that their type is the size of the body text.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

ROOT = Path(__file__).resolve().parents[1]
OUT, REPORT = ROOT / "outputs", ROOT / "report"
TABLES, FIGURES = REPORT / "tables", REPORT / "figures"
ASSETS = ["AAPL", "GPRO", "EURUSD"]
SIGNALS = ["MA", "OBV", "IMB"]
LABEL = {"AAPL": "AAPL", "GPRO": "GPRO", "EURUSD": "EUR/USD"}


# =========================================================================== tables
def num(x: float, d: int = 2, sign: bool = False) -> str:
    """Number in math mode, with a real minus sign and grouped thousands."""
    if pd.isna(x):
        return "n/a"
    s = f"{abs(x):,.{d}f}".replace(",", "{,}")
    if float(f"{abs(x):.{d}f}") == 0:
        return f"${s}$"
    return f"${'-' if x < 0 else ('+' if sign else '')}{s}$"


def ci(lo: float, hi: float, d: int = 2) -> str:
    return f"[{num(lo, d)}, {num(hi, d)}]"


def pval(p: float) -> str:
    if pd.isna(p):
        return "n/a"
    return "$<0.001$" if p < 0.001 else f"${p:.3f}$" if p < 0.1 else f"${p:.2f}$"


def write(name: str, spec: str, header: list[str], rows: list[list[str]], rules_after: set[int] = frozenset()) -> None:
    """rows: list of cell lists; rules_after: row indices followed by a \\midrule."""
    lines = [f"\\begin{{tabular}}{{{spec}}}", "\\toprule", *header, "\\midrule"]
    for i, r in enumerate(rows):
        lines.append(" & ".join(r) + " \\\\")
        if i in rules_after:
            lines.append("\\midrule")
    lines += ["\\bottomrule", "\\end{tabular}", ""]
    (TABLES / f"{name}.tex").write_text("\n".join(lines))


def block_rows(make_row) -> tuple[list[list[str]], set[int]]:
    """One row per asset / signal, the asset name on the first row of its block."""
    rows, rules = [], set()
    for a in ASSETS:
        for j, s in enumerate(SIGNALS):
            rows.append([LABEL[a] if j == 0 else "", s, *make_row(a, s)])
        rules.add(len(rows) - 1)
    rules.discard(len(rows) - 1)
    return rows, rules


def tables() -> None:
    TABLES.mkdir(parents=True, exist_ok=True)

    # ---- markets: liquidity, tick frequency, order size against displayed size
    t1 = pd.read_csv(OUT / "table1_liquidity_tick_frequency.csv", index_col=0)
    cap = pd.read_csv(OUT / "table1b_order_size_vs_displayed_size.csv", index_col=0)
    tick = {"AAPL": 0.01, "GPRO": 0.01, "EURUSD": 0.00001}
    r = lambda label, f: [label, *[f(a) for a in ASSETS]]
    rows = [
        r("Mid price", lambda a: num(t1.loc["Mid price", a], 4 if a == "EURUSD" else 2)),
        r("Quoted spread (bps)", lambda a: num(t1.loc["Quoted spread (bps)", a], 2)),
        r("One tick, in bps of the price", lambda a: num(tick[a] / t1.loc["Mid price", a] * 1e4, 2)),
        r("One-minute volatility (bps)", lambda a: num(t1.loc["1-minute volatility (bps)", a], 1)),
        r("Quote price changes per second", lambda a: num(t1.loc["Quote price changes per second", a], 2)),
        r("Quote changed after 100 ms (\\%)", lambda a: num(t1.loc["P(quote changes within 100 ms)", a] * 100, 1)),
        r("Quote changed after 1 s (\\%)", lambda a: num(t1.loc["P(quote changes within 1 s)", a] * 100, 1)),
        r("Units in a \\$1{,}000{,}000 order", lambda a: num(cap.loc[a, "Units per order"], 0)),
        r("Units displayed at the best quote", lambda a: num(cap.loc[a, "Displayed size at the best quote (units)"], 0)),
        r("Order size / displayed size", lambda a: num(cap.loc[a, "Order / displayed size"], 1 if a != "EURUSD" else 2)),
    ]
    write("markets", "lrrr", [" & AAPL & GPRO & EUR/USD \\\\"], rows, {3, 6})

    # ---- the 27 P&L reports
    rep = pd.read_csv(OUT / "table2_pnl_reports_27.csv").set_index(["asset", "family", "delay_ms"])
    rows, rules = block_rows(lambda a, s: [
        num(rep.loc[(a, s, 0), "round_trips"], 0),
        *[num(rep.loc[(a, s, d), "total_return_pct"], 2) for d in (0, 100, 1000)],
        *[num(rep.loc[(a, s, d), "pnl"], 0) for d in (0, 100, 1000)]])
    write("pnl27", "llrrrrrrr",
          [" & & & \\multicolumn{3}{c}{Total return (\\%)} & \\multicolumn{3}{c}{P\\&L (\\$)} \\\\",
           "\\cmidrule(lr){4-6}\\cmidrule(lr){7-9}",
           "Asset & Signal & Trips & 0 ms & 100 ms & 1 s & 0 ms & 100 ms & 1 s \\\\"], rows, rules)

    # ---- where the P&L comes from, at 0 ms
    hold = pd.read_csv(OUT / "table2c_holding_periods.csv").set_index(["asset", "family"])
    def decomp(a, s):
        p, h = rep.loc[(a, s, 0)], hold.loc[(a, s)]
        n_l, n_s = h["Number of long signals"], h["Number of short signals"]
        minutes = (n_l * h["Average duration of long signals (min)"] + n_s * h["Average duration of short signals (min)"]) / (n_l + n_s)
        return [num(p.pnl_mid, 0, sign=True), num(p.spread_paid, 0), num(p.bps_per_trip, 1), num(p.win_rate_pct, 0),
                num(minutes, 1), num(h["Share of session in a position (%)"], 0)]
    rows, rules = block_rows(decomp)
    write("decomposition", "llrrrrrr",
          ["Asset & Signal & P\\&L at mid (\\$) & Spread paid (\\$) & Bps/trip & Wins (\\%) & Holding (min) & In pos.\\ (\\%) \\\\"],
          rows, rules)

    # ---- cost of delay of the nine baseline strategies
    cod = pd.read_csv(OUT / "table3_cost_of_delay_baseline.csv").set_index(["asset", "family", "delay_ms"])
    def cost(a, s):
        c1, c2 = cod.loc[(a, s, 100)], cod.loc[(a, s, 1000)]
        d = 3 if a == "EURUSD" else 2  # EUR/USD effects are an order of magnitude smaller
        return [num(c1.cost_pct, d, True), ci(c1.ci_lo_pct, c1.ci_hi_pct, d), num(c2.cost_pct, d, True),
                ci(c2.ci_lo_pct, c2.ci_hi_pct, d), num(c2.cost_usd, 0, True), pval(c2.wilcoxon_p)]
    rows, rules = block_rows(cost)
    curve = pd.read_csv(OUT / "table3b_cost_of_delay_curve.csv").set_index("delay_ms")
    m1, m2 = curve.loc[100], curve.loc[1000]
    rows.append(["\\multicolumn{2}{l}{Mean of the nine}", num(m1["Mean"], 2, True), ci(m1["Mean lo"], m1["Mean hi"]),
                 num(m2["Mean"], 2, True), ci(m2["Mean lo"], m2["Mean hi"]), "", ""])
    write("cost_baseline", "llrlrlrr",
          [" & & \\multicolumn{2}{c}{100 ms} & \\multicolumn{4}{c}{1 second} \\\\", "\\cmidrule(lr){3-4}\\cmidrule(lr){5-8}",
           "Asset & Signal & Cost (\\%) & 90\\% interval & Cost (\\%) & 90\\% interval & Cost (\\$) & Wilcoxon $p$ \\\\"],
          rows, rules | {len(rows) - 2})

    # ---- Figure 6: all rules
    ios = pd.read_csv(OUT / "table4_importance_of_speed.csv")
    al = ios[ios.subset == "all"].set_index(["asset", "delay_ms"])
    rows, rules = [], set()
    for a in ASSETS:
        for j, d in enumerate((100, 200, 500, 1000)):
            x = al.loc[(a, d)]
            rows.append([LABEL[a] if j == 0 else "", num(d, 0), num(x.importance_pct, 2, True), num(x.median_pct, 2, True),
                         num(x.diff_bps_per_day, 2, True), pval(x.wilcoxon_p)])
        rules.add(len(rows) - 1)
    rules.discard(len(rows) - 1)
    write("figure6", "lrrrrr",
          ["Asset & Delay (ms) & Mean of daily ratios (\\%) & Median (\\%) & Bps per rule-day & Wilcoxon $p$ \\\\"], rows, rules)

    # ---- Figure 7: winning and losing rules against the selection bias
    pl = pd.read_csv(OUT / "table4b_selection_bias.csv").set_index(["subset", "asset", "delay_ms"])
    rows, rules = [], set()
    for a in ASSETS:
        for j, d in enumerate((100, 200, 500, 1000)):
            cells = [LABEL[a] if j == 0 else "", num(d, 0)]
            for sub in ("positive", "negative"):
                x = pl.loc[(sub, a, d)]
                cells += [num(x.actual_pct, 2, True) + ("*" if x.p_below < 0.05 else ""), ci(x.bias_lo_pct, x.bias_hi_pct), pval(x.p_below)]
            rows.append(cells)
        rules.add(len(rows) - 1)
    rules.discard(len(rows) - 1)
    write("selection_bias", "lr" + "rlr" * 2,
          [" & & \\multicolumn{3}{c}{Winning rules} & \\multicolumn{3}{c}{Losing rules} \\\\",
           "\\cmidrule(lr){3-5}\\cmidrule(lr){6-8}",
           "Asset & Delay (ms) & Actual (\\%) & Random rules & $p$ & Actual (\\%) & Random rules & $p$ \\\\"], rows, rules)

    # ---- comparison with the paper
    cmp = pd.read_csv(OUT / "table4e_comparison_with_paper.csv", index_col=0, dtype=str)
    star = lambda v: num(float(v.rstrip("*")), 2, True) + ("*" if v.endswith("*") else "")
    cmp.index = cmp.index.astype(int)
    rows = [[num(d, 0), *[star(cmp.loc[d, c]) for c in cmp.columns]] for d in cmp.index]
    write("paper", "rrrrrrr",
          [" & \\multicolumn{3}{c}{Paper, Jan--Sep 2009} & \\multicolumn{3}{c}{This project, Sep 2019} \\\\",
           "\\cmidrule(lr){2-4}\\cmidrule(lr){5-7}", "Delay (ms) & SPY & QQQQ & IWM & AAPL & GPRO & EUR/USD \\\\"], rows)

    # ---- effect of the delay per order, universe
    ou = pd.read_csv(OUT / "table4c_effect_per_order_universe.csv").set_index(["asset", "family", "horizon_ms"])
    def order(a, s):
        x1, x2 = ou.loc[(a, s, 100)], ou.loc[(a, s, 1000)]
        flag = lambda x: "*" if x.ci_lo > 0 or x.ci_hi < 0 else ""
        d = 4 if a == "EURUSD" else 3
        return [num(x1.orders, 0), num(x1.effect_bps, d, True) + flag(x1), ci(x1.ci_lo, x1.ci_hi, d),
                num(x2.effect_bps, d, True) + flag(x2), ci(x2.ci_lo, x2.ci_hi, d)]
    rows, rules = block_rows(order)
    write("order_effect", "llrrlrl",
          [" & & & \\multicolumn{2}{c}{100 ms} & \\multicolumn{2}{c}{1 second} \\\\", "\\cmidrule(lr){4-5}\\cmidrule(lr){6-7}",
           "Asset & Signal & Orders & Bps / order & 90\\% interval & Bps / order & 90\\% interval \\\\"], rows, rules)

    # ---- signals across volatility regimes
    rs = pd.read_csv(OUT / "table5b_signal_by_volatility_regime.csv").set_index(["asset", "family", "vol_regime"])
    def regime(a, s):
        hi, lo = rs.loc[(a, s, "high vol")], rs.loc[(a, s, "low vol")]
        d = 4 if a == "EURUSD" else 3
        return [num(hi.ret0_bps, 1), num(lo.ret0_bps, 1), num(hi.cost1000_bps, 2, True), num(lo.cost1000_bps, 2, True),
                num(hi.effect_per_order_1s_bps, d, True), num(lo.effect_per_order_1s_bps, d, True)]
    rows, rules = block_rows(regime)
    write("regimes", "llrrrrrr",
          [" & & \\multicolumn{2}{c}{Return at 0 ms} & \\multicolumn{2}{c}{Cost of 1 s} & \\multicolumn{2}{c}{Effect of 1 s per order} \\\\",
           " & & \\multicolumn{2}{c}{(bps / rule-day)} & \\multicolumn{2}{c}{(bps / rule-day)} & \\multicolumn{2}{c}{(bps)} \\\\",
           "\\cmidrule(lr){3-4}\\cmidrule(lr){5-6}\\cmidrule(lr){7-8}",
           "Asset & Signal & High vol & Low vol & High vol & Low vol & High vol & Low vol \\\\"], rows, rules)

    # ---- tick frequency and sensitivity to delay
    tk = pd.read_csv(OUT / "table7_tick_frequency_and_delay.csv", index_col=0)
    r = lambda label, col, d: [label, *[num(tk.loc[a, col], d) for a in ASSETS]]
    rows = [
        r("Quote price changes per second", "quote_moves_per_s", 2),
        ["Quote changed after 100 ms / 1 s (\\%)", *[f"{num(tk.loc[a, 'p_quote_change_100ms'] * 100, 1)} / {num(tk.loc[a, 'p_quote_change_1000ms'] * 100, 1)}" for a in ASSETS]],
        r("Mean absolute mid move after 1 s (bps)", "abs_mid_move_1000ms_bps", 2),
        r("\\quad in half-spreads", "|mid move| 1 s / half-spread", 2),
        r("Absolute effect of 100 ms per round trip (bps)", "abs100_bps", 2),
        r("Absolute effect of 1 s per round trip (bps)", "abs1000_bps", 2),
        r("Correlation of daily sensitivity with quote changes", "corr(sensitivity, quote changes)", 2),
        r("Correlation of daily sensitivity with volatility", "corr(sensitivity, volatility)", 2),
    ]
    write("tick", "lrrr", [" & AAPL & GPRO & EUR/USD \\\\"], rows, {3, 5})


# =========================================================================== figures
WIDTH = 6.5  # inches: the text block of the report
ASSET_COLOR = {"AAPL": "#2a78d6", "GPRO": "#eb6834", "EURUSD": "#1baf7a"}
SIGNAL_COLOR = {"MA": "#2a78d6", "OBV": "#eb6834", "IMB": "#1baf7a"}
DELAY_COLOR = {0: "#86b6ef", 100: "#2a78d6", 1000: "#0d366b"}
INK, MUTED, GRID = "#1a1a1a", "#555555", "#e3e3e3"
RING = dict(ls="", marker="o", ms=8, mfc="none", mec=INK, mew=0.9)


def style() -> None:
    plt.rcParams.update({
        "font.size": 8, "axes.titlesize": 8.5, "axes.titleweight": "bold", "axes.labelsize": 8,
        "xtick.labelsize": 7.5, "ytick.labelsize": 7.5, "legend.fontsize": 7.5, "legend.frameon": False,
        "axes.edgecolor": "#b5b5b5", "axes.linewidth": 0.6, "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
        "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.5,
        "axes.spines.top": False, "axes.spines.right": False, "lines.linewidth": 1.4,
        "xtick.major.width": 0.5, "ytick.major.width": 0.5, "xtick.major.size": 2.5, "ytick.major.size": 2.5,
        "savefig.bbox": "tight", "savefig.pad_inches": 0.03, "pdf.fonttype": 42,
    })


def save(fig, name: str) -> None:
    fig.savefig(FIGURES / f"{name}.pdf")
    plt.close(fig)


def zero(ax) -> None:
    ax.axhline(0, color=MUTED, lw=0.6, zorder=1)


def fig_book_value() -> None:
    base = pd.read_csv(OUT / "daily_results_baseline.csv", parse_dates=["date"])
    fig, axes = plt.subplots(3, 3, figsize=(WIDTH, 5.0), sharex=True)
    for i, a in enumerate(ASSETS):
        for j, s in enumerate(SIGNALS):
            ax = axes[i, j]
            for d in (0, 100, 1000):
                x = base[(base.asset == a) & (base.family == s) & (base.delay_ms == d)].sort_values("date")
                ax.plot(x.date, 1 + x.pnl.cumsum() / 1e6, color=DELAY_COLOR[d], lw=1.2, label="0 ms" if d == 0 else f"{d:,} ms")
            ax.axhline(1, color=MUTED, lw=0.6, zorder=1)
            ax.set_title(f"{LABEL[a]}, {s}", loc="left")
            ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0, interval=2))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        axes[i, 0].set_ylabel("Book value ($ million)")
    axes[0, 0].legend(loc="lower left", title="Execution delay", title_fontsize=7.5)
    fig.tight_layout(h_pad=1.0, w_pad=0.8)
    save(fig, "book_value")


def fig_cost_curve() -> None:
    curve = pd.read_csv(OUT / "table3b_cost_of_delay_curve.csv").set_index("delay_ms")
    fig, axes = plt.subplots(2, 2, figsize=(WIDTH, 4.3), sharex=True)
    for ax, a in zip(axes.flat, ASSETS):
        for s in SIGNALS:
            ax.plot(curve.index, curve[f"{a} | {s}"], color=SIGNAL_COLOR[s], label=s)
        ax.set_title(LABEL[a], loc="left")
    ax = axes[1, 1]
    ax.fill_between(curve.index, curve["Mean lo"], curve["Mean hi"], color=INK, alpha=0.12, lw=0, label="90% interval")
    ax.plot(curve.index, curve["Mean"], color=INK, label="Mean")
    ax.set_title("Mean of the nine pairs", loc="left")
    ax.legend(loc="lower left")
    for ax in axes.flat:
        zero(ax)
    for ax in axes[1]:
        ax.set_xlabel("Execution delay (ms)")
    for ax in axes[:, 0]:
        ax.set_ylabel("Cost of delay (% return)")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.045))
    fig.tight_layout(h_pad=1.0, w_pad=1.2)
    save(fig, "cost_curve")


def fig_importance_all() -> None:
    tab = pd.read_csv(OUT / "table4_importance_of_speed.csv")
    tab = tab[tab.subset == "all"]
    fig, axes = plt.subplots(1, 3, figsize=(WIDTH, 2.35))
    for ax, a in zip(axes, ASSETS):
        x = tab[tab.asset == a]
        pos = np.arange(len(x))
        ax.plot(pos, x.importance_pct, color=ASSET_COLOR[a], marker="o", ms=3.5)
        ax.plot(pos, x.median_pct, color=ASSET_COLOR[a], ls="--", lw=1.1)
        sig = x.wilcoxon_p.to_numpy() < 0.10
        ax.plot(pos[sig], x.importance_pct.to_numpy()[sig], **RING)
        zero(ax)
        ax.set_xticks(pos); ax.set_xticklabels([f"{d:,}" for d in x.delay_ms], rotation=45); ax.set_xlabel("Execution delay (ms)")
        ax.set_title(LABEL[a], loc="left")
    axes[0].set_ylabel("Change in return (%)")
    handles = [Line2D([], [], color=INK, marker="o", ms=3.5), Line2D([], [], color=INK, ls="--", lw=1.1), Line2D([], [], **RING)]
    fig.legend(handles, ["Mean of the daily relative differences (the paper's statistic)", "Median", "Wilcoxon $p$ < 0.10"],
               loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.1), columnspacing=1.5)
    fig.tight_layout(w_pad=1.0)
    save(fig, "importance_all")


def fig_selection_bias() -> None:
    pl = pd.read_csv(OUT / "table4b_selection_bias.csv")
    fig, axes = plt.subplots(2, 3, figsize=(WIDTH, 4.3), sharex=True)
    for i, (sub, name) in enumerate((("positive", "winning rules"), ("negative", "losing rules"))):
        for j, a in enumerate(ASSETS):
            ax, x = axes[i, j], pl[(pl.asset == a) & (pl.subset == sub)]
            pos = np.arange(len(x))
            ax.fill_between(pos, x.bias_lo_pct, x.bias_hi_pct, color=MUTED, alpha=0.18, lw=0)
            ax.plot(pos, x.bias_pct, color=MUTED, lw=1.0, ls="--")
            ax.plot(pos, x.actual_pct, color=ASSET_COLOR[a], marker="o", ms=3.5)
            sig = (x.p_below < 0.05).to_numpy()
            ax.plot(pos[sig], x.actual_pct.to_numpy()[sig], **RING)
            zero(ax)
            ax.set_xticks(pos); ax.set_xticklabels([f"{d:,}" for d in x.delay_ms], rotation=45)
            ax.set_title(f"{LABEL[a]}, {name}", loc="left")
        axes[i, 0].set_ylabel("Change in return (%)")
    for ax in axes[1]:
        ax.set_xlabel("Execution delay (ms)")
    handles = [Line2D([], [], color=INK, marker="o", ms=3.5), Line2D([], [], color=MUTED, lw=1.0, ls="--"),
               Patch(facecolor=MUTED, alpha=0.18, lw=0), Line2D([], [], **RING)]
    fig.legend(handles, ["Actual rules", "Random rules: mean (selection bias)", "Random rules: 5th to 95th percentile",
                         "Below the 5th percentile"], loc="lower center", ncol=4, bbox_to_anchor=(0.5, -0.045), columnspacing=1.2)
    fig.tight_layout(h_pad=1.0, w_pad=1.0)
    save(fig, "selection_bias")


def fig_order_response() -> None:
    rc = pd.read_csv(OUT / "table4d_order_response_curve.csv")
    fig, axes = plt.subplots(1, 3, figsize=(WIDTH, 2.3))
    for ax, a in zip(axes, ASSETS):
        for s in SIGNALS:
            x = rc[(rc.asset == a) & (rc.family == s)]
            ax.fill_between(x.horizon_ms, x.ci_lo, x.ci_hi, color=SIGNAL_COLOR[s], alpha=0.15, lw=0)
            ax.plot(x.horizon_ms, x.effect_bps, color=SIGNAL_COLOR[s], marker="o", ms=2.5, label=s)
        for v in (100, 1000):
            ax.axvline(v, color=MUTED, lw=0.6, ls=":")
        zero(ax)
        ax.set_xscale("log"); ax.set_xticks([10, 100, 1000, 10000, 60000]); ax.set_xticklabels(["10 ms", "0.1 s", "1 s", "10 s", "60 s"])
        ax.minorticks_off()
        ax.set_xlabel("Execution delay"); ax.set_title(LABEL[a], loc="left")
    axes[0].set_ylabel("Effect per order (bps)")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.08))
    fig.tight_layout(w_pad=1.0)
    save(fig, "order_response")


def fig_tick() -> None:
    daily = pd.read_csv(OUT / "daily_cost_and_microstructure.csv")
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.6), sharey=True)
    for a in ASSETS:
        x = daily[daily.asset == a]
        kw = dict(s=14, color=ASSET_COLOR[a], edgecolor="white", lw=0.4, label=LABEL[a])
        axes[0].scatter(x.quote_moves_per_s, x.abs1000_bps, **kw)
        axes[1].scatter(x.p_quote_change_1000ms * 100, x.abs1000_bps, **kw)
    axes[0].set_xscale("log"); axes[0].set_xlabel("Quote price changes per second (log scale)")
    axes[1].set_xlabel("Probability that the quote changes within 1 s (%)")
    axes[0].set_ylabel("Absolute effect of a 1 s delay\n(bps per round trip)")
    axes[0].set_title("Tick frequency", loc="left"); axes[1].set_title("Probability of a stale quote", loc="left")
    axes[1].legend(loc="upper center")
    fig.tight_layout(w_pad=1.2)
    save(fig, "tick_sensitivity")


def fig_signals_day() -> None:
    import speed

    day = speed.load_equity_day("20190918")["AAPL"]
    f = speed.features(day)
    t = pd.to_datetime(f["grid"]).tz_localize("UTC").tz_convert("America/New_York").tz_localize(None)
    panels = [("MA", f["mid"], "Mid quote ($)", "Moving average: mid quote"),
              ("OBV", f["obv"] / 1e3, "Thousand shares", "On-balance volume"),
              ("IMB", f["imb"], "Imbalance", "Persistent imbalance: one-minute average of the queue imbalance")]
    fig, axes = plt.subplots(3, 1, figsize=(WIDTH, 4.9), sharex=True)
    for ax, (name, y, lab, title) in zip(axes, panels):
        fn, prm = speed.BASELINE[name]
        sig = fn(f, **prm)
        ax.plot(t, y, color=INK, lw=0.8)
        lo, hi = np.nanmin(y), np.nanmax(y)
        ax.fill_between(t, lo, hi, where=sig == 1, color="#2a78d6", alpha=0.2, lw=0, step="post")
        ax.fill_between(t, lo, hi, where=sig == -1, color="#eb6834", alpha=0.2, lw=0, step="post")
        ax.set_title(title, loc="left"); ax.set_ylabel(lab)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M")); axes[-1].set_xlabel("New York time, 18 September 2019")
    fig.legend([Patch(facecolor="#2a78d6", alpha=0.2), Patch(facecolor="#eb6834", alpha=0.2)], ["Rule is long", "Rule is short"],
               loc="lower center", ncol=2, bbox_to_anchor=(0.5, -0.035))
    fig.tight_layout(h_pad=0.8)
    save(fig, "signals_day")


def figures() -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    style()
    for f in (fig_book_value, fig_cost_curve, fig_importance_all, fig_selection_bias, fig_order_response, fig_tick, fig_signals_day):
        f()


if __name__ == "__main__":
    tables()
    figures()
    print("tables:", sorted(p.name for p in TABLES.glob("*.tex")))
    print("figures:", sorted(p.name for p in FIGURES.glob("*.pdf")))
