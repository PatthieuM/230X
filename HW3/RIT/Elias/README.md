# RIT ALGO1 — Elias Roubache

The write-up is **Section 6 of `HW3/MFE230X_HW3_solution.ipynb`** (strategy, what happened and size strategy, MARKET vs LIMIT, P&L), with Figures 6.1–6.4.

- `algo1_bot/` — the trading bot (Python 3.12 package, 85 tests, mock RIT server). Usage: Section 6b of the notebook, and `algo1_bot/WINDOWS.md` for the RIT machine. `configs/account1.json` = the heat configuration; `configs/fast.json` = the post-mortem fix (concurrent MARKET legs).
- `data/` — the two small CSVs the notebook plots (per-run summary; per-second timeline of the heat on account 1).
- `extract_tapes.py` — rebuilds `data/` from the bot's raw run tapes (`runs/<name>/loops.parquet`, not in the repo).
