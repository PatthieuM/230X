# MFE 230X Final Project

## Scope

The project studies the cost of execution delay for three trading signals across:

- AAPL (liquid equity)
- GPRO (less-liquid equity)
- EUR/USD (FX; data still to be added)

The equity sample covers September 2019. The requested execution delays are 0 ms, 100 ms, and 1 second.

## Equity data

`data/equities/databento/mbp-1/` contains Nasdaq TotalView-ITCH MBP-1 data for AAPL and GPRO from 2019-09-01 (inclusive) through 2019-10-01 (exclusive).

- Encoding: DBN
- Compression: zstd
- Split: one file per trading day
- Contents: every trade and every update affecting the best bid and offer

The original 376 MB batch ZIP is intentionally not duplicated. Its extracted daily files and Databento support metadata are included.

## Remaining input

A full month of EUR/USD trades and quotes for September 2019 is still required. The existing course FX files contain only one trading day and are not sufficient for the project.
