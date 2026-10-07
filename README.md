# MFE 230X

Coursework repository for MFE 230X.

## Homework 1

`HW1/` contains the complete, executed analysis for NASDAQ TotalView-ITCH and the supplied currency order/trade data:

- the final self-contained notebook;
- its original backup;
- Databento MBO and MBP-10 downloads with support files;
- the two supplied currency CSV files;
- course instructions and discussion documents;
- generated summary tables and figures;
- the standalone pipeline source used by the notebook.

To reproduce the analysis:

```bash
cd HW1
python -m pip install -r requirements.txt
jupyter lab MFE230X_HW1_solution.ipynb
```

The notebook expects to be run with `HW1/` as its working directory.

## Homework 3

`HW3/` contains the executed Hidden Liquidity replication for Part 1 of Homework 3:

- the final notebook reproducing Tables 2 and 3 of Avellaneda, Reed, and Stoikov (2011);
- Nasdaq estimates of hidden liquidity for XLF, QQQQ, JPM, and AAPL;
- AAPL estimates conditional on one-, two-, and three-cent spreads;
- generated tables, robustness checks, and empirical-versus-model heatmaps;
- the assignment and WRDS access instructions.

The approximately 1 GB WRDS TAQ input file is intentionally excluded. To reproduce the analysis, place `data.csv` in `HW3/`, in a sibling `Data WRDS/` directory, or set the `HW3_DATA` environment variable, then run:

```bash
cd HW3
python -m pip install -r requirements.txt
jupyter lab MFE230X_HW3_solution.ipynb
```

### Part 2: RIT ALGO1 write-ups

Part 2 is individual. Each team member's write-up and trading script are in `HW3/RIT/<name>/`:

- `Alex/`: `RIT_ALGO1_Writeup_Alex.md` and `arbitrage.py`;
- `Elias/`: `README.md`, the `algo1_bot/` source and the plotted run data (the write-up itself is Section 6 of the notebook);
- `Elouan/`: `RIT_ALGO1_Writeup_Elouan.md` and `algo1_arbitrage.py`;
- `Jean/`: `RIT_ALGO1_Writeup_Jean.md`, `algo1_v3.py`, `flatten.py` and the session fill log `all_fills.csv`;
- `matthieu/`: `RIT_Algorithmic_Trading_Matthieu_Pascal.pdf` and `rit_adaptive.py`.

## Final Project

`Project/` contains the initial setup for the execution-speed project:

- the project instructions and Scholtus and van Dijk reference paper;
- Nasdaq TotalView-ITCH MBP-1 data for AAPL and GPRO;
- 20 daily files covering September 2019;
- Databento metadata and integrity manifests.

The equity files include all trades and best-bid/best-offer updates needed to simulate execution delays of 0 ms, 100 ms, and 1 second. Full-month EUR/USD data still needs to be added.

## Final RIT Simulation (ALGO2e)

`Project/FinalSimulation/` contains the group write-up of the final RIT simulation (Algorithmic Market Making: Extensions, CNR/RY/AC):

- `final_simulation_writeup.pdf` and its LaTeX source;
- `last_simulation.py`, the momentum liquidity-taker algorithm that was run (`python last_simulation.py --key <API_KEY>`);
- the RIT P&L screenshots used in the write-up (final P&L $2,064,738.33, no fines).
