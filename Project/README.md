# MFE 230X Final Project

## Part 1: Speed

Study of the cost of execution delay for three trading signals, after Scholtus and van Dijk (2012).

| Choice | Value |
|---|---|
| Assets | AAPL (liquid equity), GPRO (less liquid equity), EUR/USD |
| Period | September 2019, 20 trading days, 13:30 to 20:00 UTC (09:30 to 16:00 New York) |
| Signals | Moving average, on-balance volume, persistent best-quote imbalance (own) |
| Delays | 0 ms, 100 ms, 1 second |
| Book | $1,000,000, trades at the prevailing best bid/ask |

**Deliverable:** `MFE230X_project_speed.ipynb` (executed, with the 27 P&L reports, the cost of delay, the Figure 6 replication and the discussion).

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

- All 27 strategies lose money net of the spread (0.3 bps per round trip on EUR/USD, 0.7 on AAPL, 22.5 on GPRO).
- The cost of delay is at most 0.06 bps per round trip for eight of the nine baseline strategies. The imbalance signal on GPRO gives up 0.33 bps per round trip ($26,198) with a 1 second delay.
- On AAPL, where the rules lose money, a delay of 10 to 500 ms improves performance by 1 to 7%.
- Tick frequency sets how often a delayed order meets a changed quote; tick size relative to price sets what each miss costs.

### Data limits

- MBP-1 has the top of book only: orders are assumed to fill in full at the best quote.
- No FX trade tape: OBV on EUR/USD uses tick volume.

## Part 2: RIT simulation

Not in this folder yet.
