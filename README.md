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
- `matthieu/`: `RIT_Algorithmic_Trading_Matthieu_Pascal.pdf` and `rit_adaptive.py`.
