"""Shared fixtures: hand-built MarketState, resting orders, and an in-process mock server."""
from __future__ import annotations

import os
import sys
from time import perf_counter_ns

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algo1.core.params import Config                      # noqa: E402
from algo1.core.types import (A, ACKED, BBO, BUY, Inventory, LIMIT, M, Order, P_QUOTE,   # noqa: E402
                              SELL, Snapshot)
from algo1.market.state import MarketState                # noqa: E402


def make_cfg(**kw):
    cfg = Config()
    cfg.fee2 = cfg.fee[0] + cfg.fee[1]
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def make_bbo(bm=998, am=1000, ba=998, aa=1000, bsm=1000, asm=1000, bsa=1000, asa=1000):
    return BBO((bm, ba), (am, aa), (bsm, bsa), (asm, asa))


def make_ms(cfg=None, inv=None, bbo=None, i=1, fills=()):
    cfg = cfg or make_cfg()
    inv = inv if inv is not None else Inventory()
    bbo = bbo or make_bbo()
    ms = MarketState(cfg)
    ms.update(Snapshot(i, perf_counter_ns() - 1000, perf_counter_ns(), bbo, inv.pos, list(inv.pos_venue)), list(fills), inv)
    return ms


def resting(inv, venue, side, px, qty=500, purpose=P_QUOTE, id_server=None):
    o = Order(venue, side, LIMIT, qty, px, purpose)
    o.state = ACKED
    o.id_server = id_server or o.id_local + 1000
    o.t_send = o.t_ack = perf_counter_ns()
    inv.register(o)
    inv.add_resting(o)
    return o


@pytest.fixture
def cfg():
    return make_cfg()


@pytest.fixture
def mock():
    """Ephemeral-port mock server, frozen clock (speed 0), no random crosses, no passive fills."""
    from algo1.sim.mock_server import MockConfig, MockServer
    srv = MockServer(MockConfig(port=0, speed=0.0, ticks=300, cross_every_s=1e9, passive_fill=False,
                                sigma_step=0.0, offset_sigma=0.0)).start()
    yield srv
    srv.stop()


def engine_cfg(srv, tmp_path, **kw):
    cfg = make_cfg(host="127.0.0.1", port=srv.port, api_key="mock", run_name="t" + str(os.getpid() % 1000),
                   kill_file=str(tmp_path / "KILL"), params_path=str(tmp_path / "none.json"),
                   poll_case_every=5, poll_book_every=3, poll_orders_every=10, kill_check_every=5,
                   ring_loops=4096, ring_orders=2048, ring_fills=2048,
                   pos_per_ticker=True, gross_both_legs=True)      # the default mock reports per-ticker positions
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg
