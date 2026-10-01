"""algo1 — ALGO1 (RIT Algorithmic Arbitrage) trading bot.

Layout (see CLAUDE.md):
    core/       trading objects, clock, config
    api/        HTTP lanes, message builders/parsers, token budget
    market/     market state and fair value
    alpha/      pure situation -> intent functions
    execution/  risk gate -> reconciler -> dispatcher (the only send path)
    engine/     the loop with stage timings
    monitor/    shared-memory ring buffer, live status, post-run report
    sim/        mock RIT server, recorder, analysis
"""
__version__ = "0.1.0"
