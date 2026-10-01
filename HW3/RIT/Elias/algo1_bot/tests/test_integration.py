"""Integration against the in-process mock. Each test runs the real Engine for a few hundred loops."""
import json
import os
import time
import urllib.request

import pytest

from algo1.core.types import A, M
from algo1.engine.loop import Engine
from conftest import engine_cfg


def ctl(srv, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}{path}", timeout=2) as r:
        return json.loads(r.read())


def run_engine(cfg, tmp_path, max_loops=300, **kw):
    eng = Engine(cfg, run_dir=str(tmp_path / "run"), shared=False, max_loops=max_loops, **kw)
    eng.init()
    eng.run()
    return eng


def test_forced_cross_pair_both_filled_edge_realised(mock, tmp_path):
    ctl(mock, "/mock/cross?venue=1&dir=up&ticks=3&life_ms=5000&qty=5000")
    cfg = engine_cfg(mock, tmp_path)
    eng = run_engine(cfg, tmp_path, max_loops=60)
    s = eng.summary()
    assert s["pairs_sent"] >= 1 and s["pairs_both_filled"] >= 1
    assert s["pos"] == 0 and eng.inv.pos_venue[M] == -eng.inv.pos_venue[A]
    assert s["pnl_edge_ticks_UNRELIABLE"] > 0
    st = ctl(mock, "/mock/state")
    assert st["pos"][0] + st["pos"][1] == 0
    assert os.path.exists(tmp_path / "run" / "loops.parquet")


def test_venue_kill_trips_breaker_and_cancels_other_venue(mock, tmp_path):
    cfg = engine_cfg(mock, tmp_path, enable_quotes=True, breaker_n=2, breaker_cooldown_s=100, breaker_5xx_hard=True)
    eng = Engine(cfg, run_dir=str(tmp_path / "run"), shared=False, max_loops=40)
    eng.init()
    for _ in range(40):
        eng.step()
    assert eng.inv.own_resting[(M, 0)] and eng.inv.own_resting[(A, 0)], "quotes should be resting on both venues"
    ctl(mock, "/mock/kill?venue=1&on=1")
    ctl(mock, "/mock/cross?venue=1&dir=up&ticks=5&life_ms=5000&qty=5000")
    eng.max_loops = 0
    for _ in range(120):
        eng.step()
    assert eng.breaker.disabled[A] and eng.breaker.n_trips >= 1      # 503 = hard failure
    assert not eng.inv.own_resting[(M, 0)] and not eng.inv.own_resting[(M, 1)], "other venue cancelled"
    # the first pair after the kill is what detects the dead venue; after the trip, none go out
    n_pairs = eng.stats["pairs"]
    assert n_pairs <= 2
    for _ in range(60):
        eng.step()
    assert eng.stats["pairs"] == n_pairs and eng.breaker.disabled[A]
    eng.shutdown()


def test_end_of_case_stops_flat(mock, tmp_path):
    mock.market.cfg.ticks = 3
    mock.market.cfg.speed = 10.0
    mock.market.reset_clock()
    cfg = engine_cfg(mock, tmp_path, enable_quotes=True)
    eng = run_engine(cfg, tmp_path, max_loops=0, max_seconds=5.0)
    assert eng.stopped_reason.startswith("case")
    assert eng.inv.pos == 0
    assert ctl(mock, "/mock/state")["venues"][0]["resting"] == 0


def test_kill_switch_file(mock, tmp_path):
    cfg = engine_cfg(mock, tmp_path, enable_quotes=True)
    eng = Engine(cfg, run_dir=str(tmp_path / "run"), shared=False)
    eng.init()
    for _ in range(20):
        eng.step()
    (tmp_path / "KILL").write_text("x")
    alive = True
    for _ in range(100):
        alive = eng.step()
        if not alive:
            break
    assert not alive and eng.stopped_reason.startswith("kill switch") and not eng.inv.all_resting()
    eng.shutdown()


def test_rate_limit_penalises_budget(mock, tmp_path):
    mock.market.venues[0].bucket.rate = 2.0
    mock.market.venues[0].bucket.tokens = 0.0
    ctl(mock, "/mock/cross?venue=1&dir=up&ticks=3&life_ms=5000&qty=5000")
    cfg = engine_cfg(mock, tmp_path)
    eng = run_engine(cfg, tmp_path, max_loops=30)
    assert eng.budget.n429 >= 1


def test_aggregated_position_mode_does_not_double_count(mock, tmp_path):
    """pos_per_ticker=False against a server that reports the case position on both tickers."""
    mock.market.cfg.aggregate_positions = True
    ctl(mock, "/mock/cross?venue=1&dir=up&ticks=3&life_ms=5000&qty=5000")
    cfg = engine_cfg(mock, tmp_path, pos_per_ticker=False, gross_both_legs=False)   # = the live defaults
    eng = run_engine(cfg, tmp_path, max_loops=60)
    s = eng.summary()
    assert s["pairs_both_filled"] >= 1
    st = ctl(mock, "/mock/state")
    assert eng.inv.pos == st["pos"][0] + st["pos"][1] == 0


def test_lagged_aggregated_position_does_not_runaway(mock, tmp_path):
    """The p2 incident on the mock: aggregated position field lagging 150 ms, a one-leg residual
    to flatten at the end of the case. Must converge, not fire a flatten per loop."""
    mock.market.cfg.aggregate_positions = True
    mock.market.cfg.position_lag_ms = 150.0
    cfg = engine_cfg(mock, tmp_path, pos_per_ticker=False, gross_both_legs=False, k_flat=400, k_quotes=400)
    eng = Engine(cfg, run_dir=str(tmp_path / "run"), shared=False)
    eng.init()
    eng.inv.pos = 0
    # a residual: buy 1000 on M directly through the mock, tell the engine via a fake ack
    from algo1.core.types import FILLED, Order, P_CROSS
    o = Order(M, 0, 0, 1000, None, P_CROSS)
    r = mock.market.place("CRZY_M", "MARKET", 1000, "BUY", None)
    o.state, o.filled, o.t_ack, o.id_server = FILLED, 1000, time.perf_counter_ns(), r["order_id"]
    eng.inv.apply_orders([o], False, 1000.0, o.t_ack)
    for _ in range(600):
        if not eng.step():
            break
    st = ctl(mock, "/mock/state")
    assert st["pos"][0] + st["pos"][1] == 0, "residual flattened"
    assert eng.inv.pos == 0 and eng.inv.n_unattributed == 0
    assert eng.dispatcher.n_sent <= 4, f"flatten re-fired: {eng.dispatcher.n_sent} orders"
    eng.shutdown()


def test_record_writes_tas(mock, tmp_path):
    from algo1.sim.recorder import record
    ctl(mock, "/mock/cross?venue=1&dir=up&ticks=3&life_ms=5000&qty=5000")
    mock.market.place("CRZY_M", "MARKET", 300, "BUY", None)
    cfg = engine_cfg(mock, tmp_path)
    df, path = record(cfg, seconds=0.6, out_dir=str(tmp_path / "rec"), tas_every_ms=100)
    import pandas as pd
    tas = pd.read_parquet(tmp_path / "rec" / "tas.parquet")
    assert len(df) > 50 and len(tas) >= 1 and set(tas.columns) == {"t_recv", "venue", "id", "tick", "px", "qty"}


def test_second_leg_market_sizes_to_first_fill(mock, tmp_path):
    ctl(mock, "/mock/cross?venue=1&dir=up&ticks=3&life_ms=5000&qty=5000")
    cfg = engine_cfg(mock, tmp_path, second_leg_market=True, confirm_then_hedge=False)
    eng = run_engine(cfg, tmp_path, max_loops=40)
    O = [o for o in eng.inv.orders.values() if o.purpose == 0]
    assert O and eng.summary()["pairs_both_filled"] >= 1 and eng.inv.pos == 0
    seconds = [o for o in O if o.kind == 0]                     # MARKET legs
    firsts = [o for o in O if o.kind == 1]
    assert seconds and all(o.filled == o.qty for o in seconds) and len(seconds) == len(firsts)
