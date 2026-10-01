"""Alpha functions: situation → intents.

Common signature: `evaluate(ms: MarketState, inv: Inventory, cfg: Config) -> list[Intent]`.
Pure: no I/O, no sends, no state beyond what the caller passes. Each has a unit test on a
hand-built MarketState and a micro-benchmark in `bench.py`.

Situations (from the strategy list):
    crossed books, incl. deeper than L1      → cross.evaluate
    1–3 quote both venues, passive hedge,
        queue hysteresis                     → quotes.evaluate
    4 one venue lags the other               → stale.evaluate   (gated: cfg.ec_coef > 0)
    6 sweep harvesting                       → layers.evaluate  (Friday)
    7 fade the impact of a dislocation       → dislocation.evaluate (Friday)
"""
