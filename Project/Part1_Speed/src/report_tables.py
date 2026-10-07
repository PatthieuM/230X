"""Write the LaTeX tables of the report from the CSV files produced by the notebook.

Usage (from Part1_Speed/, after running the notebook):  python src/report_tables.py
Each table is a bare `tabular` in report/tables/, pulled into speed_report.tex with \\input.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT, REPORT = ROOT / "outputs", ROOT / "report"
TABLES, FIGURES = REPORT / "tables", REPORT / "figures"
ASSETS = ["AAPL", "GPRO", "EURUSD"]
SIGNALS = ["MA", "OBV", "IMB"]
LABEL = {"AAPL": "AAPL", "GPRO": "GPRO", "EURUSD": "EUR/USD"}


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


def main() -> None:
    TABLES.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    for f in sorted((OUT / "figures").glob("*.png")):
        shutil.copy(f, FIGURES / f.name)

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
    write("markets", "lrrr", ["Regular-session average & AAPL & GPRO & EUR/USD \\\\"], rows, {3, 6})

    # ---- the 27 P&L reports
    rep = pd.read_csv(OUT / "table2_pnl_reports_27.csv").set_index(["asset", "family", "delay_ms"])
    rows, rules = block_rows(lambda a, s: [
        num(rep.loc[(a, s, 0), "round_trips"], 0),
        *[num(rep.loc[(a, s, d), "total_return_pct"], 2) for d in (0, 100, 1000)],
        *[num(rep.loc[(a, s, d), "pnl"], 0) for d in (0, 100, 1000)]])
    write("pnl27", "llrrrrrrr",
          [" & & & \\multicolumn{3}{c}{Total return (\\%)} & \\multicolumn{3}{c}{P\\&L (\\$)} \\\\",
           "\\cmidrule(lr){4-6}\\cmidrule(lr){7-9}",
           "Asset & Signal & Round trips & 0 ms & 100 ms & 1 s & 0 ms & 100 ms & 1 s \\\\"], rows, rules)

    # ---- where the P&L comes from, at 0 ms
    hold = pd.read_csv(OUT / "table2c_holding_periods.csv").set_index(["asset", "family"])
    def decomp(a, s):
        p, h = rep.loc[(a, s, 0)], hold.loc[(a, s)]
        n_l, n_s = h["Number of long signals"], h["Number of short signals"]
        minutes = (n_l * h["Average duration of long signals (min)"] + n_s * h["Average duration of short signals (min)"]) / (n_l + n_s)
        return [num(p.pnl_mid, 0, sign=True), num(p.spread_paid, 0), num(p.bps_per_trip, 1), num(p.win_rate_pct, 0),
                num(n_l, 0), num(n_s, 0), num(minutes, 1), num(h["Share of session in a position (%)"], 0)]
    rows, rules = block_rows(decomp)
    write("decomposition", "llrrrrrrrr",
          ["Asset & Signal & P\\&L at mid (\\$) & Spread paid (\\$) & Net bps / trip & Wins (\\%) & Longs & Shorts & "
           "Holding (min) & Time in position (\\%) \\\\"], rows, rules)

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
    for sub, label in (("positive", "Winning rules"), ("negative", "Losing rules")):
        for j, d in enumerate((100, 200, 500, 1000)):
            cells = [label if j == 0 else "", num(d, 0)]
            for a in ASSETS:
                x = pl.loc[(sub, a, d)]
                cells += [num(x.actual_pct, 2, True) + ("*" if x.p_below < 0.05 else ""), ci(x.bias_lo_pct, x.bias_hi_pct), pval(x.p_below)]
            rows.append(cells)
        rules.add(len(rows) - 1)
    rules.discard(len(rows) - 1)
    write("selection_bias", "lr" + "rlr" * 3,
          [" & & " + " & ".join(f"\\multicolumn{{3}}{{c}}{{{LABEL[a]}}}" for a in ASSETS) + " \\\\",
           "\\cmidrule(lr){3-5}\\cmidrule(lr){6-8}\\cmidrule(lr){9-11}",
           " & Delay (ms) & " + " & ".join(["Actual (\\%) & Random rules & $p$"] * 3) + " \\\\"], rows, rules)

    # ---- comparison with the paper
    cmp = pd.read_csv(OUT / "table4e_comparison_with_paper.csv", index_col=0, dtype=str)
    star = lambda v: num(float(v.rstrip("*")), 2, True) + ("*" if v.endswith("*") else "")
    cmp.index = cmp.index.astype(int)
    rows = [[num(d, 0), *[star(cmp.loc[d, c]) for c in cmp.columns]] for d in cmp.index]
    write("paper", "rrrrrrr",
          [" & \\multicolumn{3}{c}{Paper, January to September 2009} & \\multicolumn{3}{c}{This project, September 2019} \\\\",
           "\\cmidrule(lr){2-4}\\cmidrule(lr){5-7}", "Delay (ms) & SPY & QQQQ & IWM & AAPL & GPRO & EUR/USD \\\\"], rows)

    # ---- effect of the delay per order, universe
    ou = pd.read_csv(OUT / "table4c_effect_per_order_universe.csv").set_index(["asset", "family", "horizon_ms"])
    def order(a, s):
        x1, x2 = ou.loc[(a, s, 100)], ou.loc[(a, s, 1000)]
        flag = lambda x: "*" if x.ci_lo > 0 or x.ci_hi < 0 else ""
        d = 4 if a == "EURUSD" else 3
        return [num(x1.rules, 0), num(x1.orders, 0), num(x1.effect_bps, d, True) + flag(x1), ci(x1.ci_lo, x1.ci_hi, d),
                num(x2.effect_bps, d, True) + flag(x2), ci(x2.ci_lo, x2.ci_hi, d), num(x1.ret0_bps, 1)]
    rows, rules = block_rows(order)
    write("order_effect", "llrrrlrlr",
          [" & & & & \\multicolumn{2}{c}{100 ms} & \\multicolumn{2}{c}{1 second} & \\\\", "\\cmidrule(lr){5-6}\\cmidrule(lr){7-8}",
           "Asset & Signal & Rules & Orders & Bps / order & 90\\% interval & Bps / order & 90\\% interval & Return at 0 ms \\\\"],
          rows, rules)

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
    print("tables:", sorted(p.name for p in TABLES.glob("*.tex")))
    print("figures:", sorted(p.name for p in FIGURES.glob("*.png")))


if __name__ == "__main__":
    main()
