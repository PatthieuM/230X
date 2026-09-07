# HW1 report — refinements to the current draft

Scope: surgical edits to `MFE230X_HW1_report.pdf` (17 pages, the unrevised draft). The draft's tone and structure are kept; each item below is a replacement or an insertion, written in the same register, keyed to the section and the sentence it touches. Numbers come from the regenerated `outputs/*.csv` (notebook run of 7 September 2026). New appendix material is in `report/appendix_tables.tex` (seven `table` environments, booktabs) and `report/appendix_figures.tex` (three figures under `outputs/figures/A*.png`).

Three items are corrections; the rest are additions the discussion session asked for. Corrections first.

---

## Corrections

### C1. Section 4.1, "Spread", first sentence — the 09:45 disturbance is GPRO's line

Replace

> Figure 1 shows that AAPL begins the session with a visibly wider spread, including a sharp disturbance around 09:45, and then settles toward roughly 0.8–1.0 basis points.

with

> Figure 1 shows that AAPL begins the session with a visibly wider spread, about 1.7 basis points in the first five-minute interval, and then declines through the first hour toward roughly 0.8–1.0 basis points. The isolated bump near 09:50 in the upper panel belongs to GPRO (a 26-basis-point interval), not to AAPL.

(The five-minute AAPL series is 1.72, 1.61, 1.43, 1.23, 1.04 bps for 09:30–09:55; its maximum is the first interval.)

### C2. Section 2.3 and Tables 3–4 — the zeroing rule for depth at $2\bar s$

The draft states that both band-depth measures "are zero when the contemporaneous spread exceeds twice its daily time-weighted average", and the $2\bar s$ rows in Tables 3 and 4 were produced with that rule. The Discussion 2 definition is the band itself, $P_t \le m_t + 2\bar s$ on the ask side and symmetrically on the bid side, with no additional zeroing; the notebook implements the slide definition. Under it the band is empty only when the best quote lies outside it, i.e. when the spread exceeds $4\bar s$, which never happens in the sample.

Replace, in Section 2.3:

> Following the assignment convention, both band-depth measures are zero when the contemporaneous spread exceeds twice its daily time-weighted average.

with

> The band is empty only when the best quote itself lies outside it, that is, when the contemporaneous spread exceeds four times its daily time-weighted average. This does not occur in the present sample, so the measure never falls to zero.

Replace the $2\bar s$ rows of Tables 3 and 4 with the rows in `appendix_tables.tex`, table `tab:band_depth_corrected` (AAPL bid 1.2984 / 0.5277 / 0.6068 / 1.0061 / 1.1876 / 1.4375 / 5.7259; AAPL ask 1.3518 / 0.7329 / 0.5737 / 0.9892 / 1.1831 / 1.4761 / 7.5540; GPRO unchanged except rounding; EUR/USD bid 22.3351 / 7.3650 / 9.6717 / 18.0142 / 21.9975 / 26.1454 / 94.4548; EUR/USD ask 22.2012 / 14.2665 / 10.5517 / 16.9470 / 20.3017 / 24.0926 / 239.6083; USD/JPY bid 20.1812 / 6.0912 / 9.1133 / 15.1688 / 19.6667 / 24.7271 / 44.7550; USD/JPY ask 22.2272 / 12.7298 / 6.0667 / 14.5902 / 20.3136 / 24.7524 / 100.2245; EUR/JPY bid 16.9584 / 2.8477 / 8.1900 / 14.8579 / 16.8817 / 18.6867 / 30.7417; EUR/JPY ask 18.9408 / 8.9357 / 10.5717 / 14.2908 / 16.9467 / 20.6650 / 90.1317). In the notes to Tables 3 and 4 replace "and are set to zero when the contemporaneous spread exceeds $2\bar s$" with "on each side of the midquote".

In the paragraph after Table 4 replace "AAPL's $2\bar s$ bid depth is 5.19 times L1 and its ask depth is 6.03 times L1" with "5.23 times L1 and … 6.06 times L1". GPRO (2.35, 1.93) and the EUR/USD "roughly 11 times" are unchanged.

### C3. Appendix A, Table 7 and the paragraph below it — timings are from a superseded run

Replace Table 7 with `tab:timing` in `appendix_tables.tex` and replace the paragraph with:

> The previous version of this table recorded 481 seconds, of which 316 seconds were the currency loader and 136 seconds the stock loader. Timing the pipeline made the cause visible: the currency loader replayed each ten-level EBS ladder twice, once to learn the day's average spread and once to measure depth inside the $2\bar s$ band, in a per-timestamp Python loop. Rewriting it as a single replay that records the ladder and computes the band afterwards reduced it to about 9 seconds with bit-identical outputs. The stock loader still streams the MBP-10 file twice for the same reason; removing the second pass would require holding roughly 180 MB of level data for AAPL alone against a saving of about 9 seconds, so it was retained. The whole notebook now runs in about one minute.
>
> Expressed as tape seconds per compute second, both loaders run three orders of magnitude inside real time, but that is a batch figure for a session read from a file, not a latency. The relevant number for the arbitrage is the cost of one decision. Evaluated one second at a time, as a live loop would, with the wall clock read around each evaluation, the triangle check takes a median of 0.4 microseconds, a 99th percentile of about 1 microsecond, and a worst case of 64 microseconds per second (12 milliseconds for the whole session), against a mean episode length of 1.4 seconds and a maximum of 6 seconds. The lecture's "two-second arbitrage, five-second code" failure does not arise here. What binds is on the other side of the clock: Table A5 shows that re-pricing each opportunity one second after detection retains 35% of its marked profit and turns the average loop return negative. A live system must still receive three quotes, update three books, route three aggressive orders, and obtain simultaneous fills inside that window; the present table measures the research pipeline and cannot measure that end-to-end trading clock.

---

## Additions (each one or two sentences, placed where indicated)

### A1. Abstract — after "…fees, last-look rejections, or execution latency."

> Re-pricing each opportunity one second after detection retains about 35% of that marked profit, so the binding constraint is the opportunity's half-life rather than detection speed.

### A2. Section 1, main findings

Second bullet, replace "…on this holiday-week Monday." with "…on this holiday-week Monday in spread, impact, or variance, although trading activity does rise again into the close."

Fifth bullet, replace with: "Apparent triangular arbitrage is rare, brief, and small after accounting for realistic implementation limits; fewer than half of the flagged seconds exceed one price tick, and the marked profit largely disappears within one to two seconds of detection."

### A3. Section 2.3 — after "Reported impact means therefore refer only to identified minutes."

> Matching each trade to the last quote strictly before it avoids look-ahead but ignores quote updates between the trade and the next book message; taking the last state at or before $t+5$ s means that in a sparse book the horizon quote can itself be several seconds stale, which biases the estimate toward zero where trading is thinnest. The same regression is also estimated pooled over the session within dollar-size terciles (Table A3).

### A4. Section 2.4 — after "…carried forward through an empty interval."

> Such intervals contribute exact zero transaction returns by construction; their share is reported in Table A4 together with autocorrelations computed on the trade-to-trade series that omits them.

Also, in the same subsection, "Thirty-minute variance" → "Thirty-minute sample variance" (the realized variance of Section 5 is a sum of squares; the two are labelled apart).

### A5. Section 2.5 — after "An opportunity exists when either loop exceeds one."

> Because the three books are sampled independently at the end of each second on three different price grids, loop returns below one tick of the coarsest leg (USD/JPY, 0.001) are reported separately, and each flagged second is additionally re-priced at the executable quotes one to five seconds later (Table A5, Figure A2).

### A6. Section 3 — after the paragraph "Spread and depth comove negatively in every instrument…"

> Depth and price impact are also inversely related, with minute-level correlations between $-0.03$ (AAPL) and $-0.20$ (GPRO, EUR/JPY), but their product is not a constant: its coefficient of variation across minutes is 0.8–1.1 in every instrument, so the book does not simply exchange width for depth at a fixed price of liquidity.
>
> Trading is sparse at the one-second horizon even in the active names. The share of seconds with no transaction is 47% for AAPL, 97% for GPRO, 70% for EUR/USD, 85% for USD/JPY, and 95% for EUR/JPY, and the median minute contains a transaction-free gap of 6, 49, 12, 23, and 39 seconds respectively (Table A2). The ranking coincides with that of relative spreads. Missing trades are a liquidity measure in their own right, in the sense of the zero-return proxy of Lesmond, Ogden, and Trzcinka: a quote that is not being hit is untested, and these empty intervals are the zero returns that dominate the one-second transaction series in Section 5.

### A7. Section 4.1 — "Price impact", at the end of the paragraph

> Impact also increases with trade size in both names. Across dollar-size terciles the session-pooled slope rises from 0.61 to 0.69 basis points for AAPL and from 16.8 to 23.1 basis points for GPRO (Table A3, Figure A3), consistent with larger trades carrying more information.

### A8. Section 4.1 — new paragraph after "Price impact"

> **Activity.** The intraday U-shape that the spread and variance display only in part is present in trading activity. Relative to the rest of the session, AAPL executes 2.36 times as many trades in the first 30 minutes, 0.71 times between 12:00 and 13:30, and 1.60 times in the last 30 minutes; new-order arrivals follow the same shape (2.88, 0.68, 1.10). GPRO's ratios are 1.49, 0.49, and 1.95 (Table A2, Figure A1). The close therefore does return in prints and orders on 30 December 2019 even though it does not in spread or variance.

### A9. Section 4.2 — "Spread", at the end of the paragraph

> The discussion slides anticipate lower currency spreads at the open. EUR/USD (0.000110 versus 0.000114) and EUR/JPY (0.0164 versus 0.0192) are indeed tighter in the first 30 minutes than afterward, whereas USD/JPY (0.00826 versus 0.00799) is marginally wider; 09:30 New York is mid-session for these pairs. Trading activity confirms the London clock: all three pairs execute 1.5–2.2 times their rest-of-day trade rate between 12:00 and 13:30 and only 0.4–0.7 times in the last 30 minutes (Figure A1).

### A10. Section 4.2 — "Price impact", at the end

> Impact rises with trade size for all three pairs (Table A3).

### A11. Section 5 — first paragraph after Table 5

Replace "Mean returns are small relative to their standard deviations." with:

> Mean returns are small relative to their standard deviations. Summed over the session, the one-minute midquote returns reproduce the day's log price change: $+0.97\%$ for AAPL, $-0.47\%$ for GPRO, $+1.09\%$ for EUR/USD, $-0.41\%$ for USD/JPY, and $+0.68\%$ for EUR/JPY, so each series drifts in the direction of the price change, as expected. The $t$-statistics of the mean one-minute return are 0.86, $-0.13$, 2.04, $-0.78$, and 1.42; only EUR/USD is marginally distinguishable from zero (Table A4).

Then add, as a new paragraph before "AAPL's one-minute midquote autocorrelation…":

> The one-second transaction autocorrelations in Table 5 should be read with the sparsity of trading in mind. Because an interval without a transaction carries the previous price forward, 47% of AAPL's one-second transaction returns, 97% of GPRO's, and 70–95% of the currency pairs' are exact zeros. Realized variance is unaffected, but the lag-1 coefficient is diluted toward zero. On the trade-to-trade series that omits the filled intervals, $\rho(1)$ is $-0.077$ for EUR/USD, $-0.039$ for USD/JPY, and $+0.131$ for GPRO, against $-0.021$, $-0.013$, and $+0.072$ on the full grid (Table A4). The dollar pairs therefore do display the negative lag-1 signature of bid–ask bounce once the empty seconds are removed; AAPL, with a transaction in roughly every other second, is unchanged at 0.037. At one minute the filled share is 27% for GPRO and 21% for EUR/JPY and the correction is small.

In the sentence "EUR/JPY has one-minute transaction and midquote coefficients of $-0.149$ and $-0.177$, respectively, indicating temporary reversal on the thinner cross." append: "Since bid–ask bounce cannot affect a midquote, the $-0.177$ coefficient reflects quote overshoot and correction on a thin book rather than bounce."

### A12. Section 5 — "Currency variance", after "…than with L1 depth."

> Across the thirteen 30-minute windows this holds more generally. The sample variance is positively correlated with the window's mean spread (0.89 for AAPL, 0.52 for EUR/JPY), with mean impact (0.81 for AAPL, 0.43–0.56 for the currency pairs), negatively with level-one depth for the two stocks ($-0.43$ and $-0.56$), and strongly with the number of trades (0.77–0.85, EUR/JPY excepted). Volatile half-hours are busy, wide, and thin. The lag-1 autocorrelation shows no consistent relation with any liquidity measure (Table A6).

### A13. Section 6, Table 6 notes — append

> Thirteen of the 28 seconds exceed one tick of the coarsest leg (0.128 basis points) and one exceeds a full EUR/USD pip; see Table A5.

### A14. Section 6.2 — new paragraph after "…not a deployable live strategy."

> Two further checks sharpen the bound. First, the three books are sampled on separate price grids, and one tick of the coarsest leg (USD/JPY, 0.001) is 0.128 basis points at the day's mean rate; only 13 of the 28 seconds, in 11 episodes worth \$548, exceed it, and exactly one second, the 1.18-basis-point observation, exceeds a full EUR/USD pip. Second, re-pricing each flagged second at the executable quotes one second after detection leaves 8 seconds profitable and 35% of the \$678; after two seconds 26% remains and after three seconds 7%, while the average loop return at a one-second delay is $-0.60$ basis points (Table A5, Figure A2). The opportunity's half-life is therefore below one second, which is the relevant execution constraint; detection itself takes microseconds (Appendix A).

### A15. Section 7 — two sentence-level edits

"…while its close remains quiet on this particular holiday-week session." → "…while its close remains quiet in spread and variance on this particular holiday-week session, though not in trading activity."

"Triangular-arbitrage observations occur in the same broad afternoon window but are rare, short, and economically fragile." → "…rare, short, and economically fragile: most lie within one price tick, and the marked profit largely disappears within a second of detection."

### A16. New Appendix B, "Additional tables and figures"

Insert `tab:missing_trades_activity` (A2), `tab:impact_by_size` (A3), `tab:stale_drift` (A4), `tab:arbitrage_tiers_decay` (A5), `tab:variance_liquidity` (A6) from `appendix_tables.tex`, and the three figures from `appendix_figures.tex`, in that order. The table labels in the text above (A2–A6) refer to these. `tab:band_depth_corrected` is a working table for C2 and need not be printed.

---

## Not changed on purpose

- Figures 1–4 of the main text. The notebook's Figures 1 and 2 now carry two extra panels (missing trades, activity); the report keeps its own three-panel versions and puts the new panels in Figure A1 so the main figures stay as the reader knows them. If preferred, `outputs/figures/01_*.png` and `02_*.png` are the five-panel versions; in the impact panel, sparse series (GPRO, EUR/JPY) are now drawn as points rather than broken lines.
- Section 2.2 (the five-day scope discussion) and the conclusion's weekly-extension paragraph.
- All other numbers in Tables 1, 2, 5 and 6, which reproduce to the last printed digit.
