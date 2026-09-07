# MFE 230X

Coursework repository for MFE 230X.

## Homework 1

`HW1/` contains the complete, executed analysis for NASDAQ TotalView-ITCH and the supplied currency order/trade data:

- the final self-contained notebook;
- the compiled report and LaTeX source;
- Databento MBO and MBP-10 downloads with support files;
- the two supplied currency CSV files;
- course instructions and discussion documents;
- generated summary tables and figures;
- the standalone pipeline source used by the notebook.

To reproduce the analysis:

```bash
cd HW1
python -m pip install -r requirements.txt
jupyter lab MFE230X_HW1_Notebook.ipynb
```

The notebook expects to be run with `HW1/` as its working directory.

