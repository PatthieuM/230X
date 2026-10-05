# MFE 230X Final Project

## Part 1: Speed

Study of the cost of execution delay for three trading signals, after Scholtus and van Dijk (2012).

| Choice | Value |
|---|---|
| Assets | AAPL (liquid equity), GPRO (less liquid equity), EUR/USD |
| Period | September 2019, 20 trading days, 13:30 to 20:00 UTC (09:30 to 16:00 New York) |
| Signals | Moving average, on-balance volume, persistent best-quote imbalance (own) |
| Delays | 0 ms, 100 ms, 1 second |
| Book | $1,000,000: every trade is for the units worth $1,000,000 on the first day, at the prevailing best bid/ask |
| Trading window | 09:40 to 15:50 New York time, book closed daily at 15:50 |

**Deliverable:** `MFE230X_project_speed.ipynb` (executed, with the 27 P&L reports, holding times, the cost of delay, the Figure 6 replication and the discussion). It follows the instructions of Discussion Session 06.

### Layout

- `MFE230X_project_speed.ipynb`: analysis and write-up.
- `src/speed.py`: data loaders, signals, delayed-execution simulator, reporting.
- `src/download_fx.py`: downloads the EUR/USD ticks.
- `outputs/`: tables (CSV) and `outputs/figures/` (PNG) written by the notebook.
- `data/equities/databento/mbp-1/`: Nasdaq TotalView-ITCH MBP-1 for AAPL and GPRO, one DBN/zstd file per trading day (every trade and every change of the best bid and offer).
- `data/fx/dukascopy/EURUSD/`: Dukascopy EUR/USD ticks (best bid/ask and quoted sizes, millisecond stamps), one `.bi5` file per UTC hour, 12:00 to 21:00 UTC on weekdays.
- `instructions/`, `references/`: assignment and paper.

### Reproduce

```bash
pip install databento jupyter matplotlib numpy pandas scipy
jupyter nbconvert --to notebook --execute --inplace MFE230X_project_speed.ipynb
```

The data are in the repository; the notebook runs in about one minute.

### Headline results

- All 27 strategies lose money net of the spread, from -0.19% (EUR/USD, MA) to -109% (GPRO, IMB). On GPRO the signals earn money at the mid and the 22.5 bps spread takes it back.
- The cost of delay, averaged over the nine asset/signal pairs, is +0.02% at 100 ms and -0.12% at 1 second.
- The less liquid stock pays the most at 1 second: -0.72% for GPRO IMB and -0.62% for GPRO MA, against -0.27% for AAPL MA and +0.01% for EUR/USD MA.
- On days when a rule makes money, a 1 second delay lowers its return on all three assets, as in the paper.
- Tick frequency sets how often a delayed order meets a changed quote; tick size relative to price sets what each miss costs.

### Data limits

- MBP-1 has the top of book only: orders are assumed to fill in full at the best quote.
- No FX trade tape: OBV on EUR/USD uses tick volume.

## Part 2: RIT simulation

Part 2 is individual. Each team member's ALGO2 write-up and trading script are in `RIT Individual/<name>/`:

- `Jean/`: `RIT_ALGO2_Writeup_Jean.md` (also as `RIT_ALGO2_Writeup_Jean.pdf`), `algo2_mm.py` and the charts in `figures/`.
