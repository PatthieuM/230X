from algo1.core.clock import Clock
from algo1.core.types import (A, BUY, CANCEL, Intent, Inventory, LIMIT, M, MARKET, P0, P1, P2,
                              P_CROSS, P_FLATTEN, P_QUOTE, PAIR, QUOTE, SELL, TAKE)
from algo1.execution.risk import Breaker, Risk
from conftest import make_bbo, make_cfg, make_ms, resting


def pair(qty=500):
    return Intent(PAIR, P0, qty=qty, purpose=P_CROSS, legs=((M, BUY, 1000, LIMIT), (A, SELL, 1003, LIMIT)))


def quote(v=M, side=BUY, px=996):
    return Intent(QUOTE, 3, v, side, px, 500, P_QUOTE)


def test_self_trade_drops_leg_and_prepends_cancel():
    cfg = make_cfg()
    inv = Inventory()
    o = resting(inv, M, SELL, 1000)          # our ask on M at the pair's buy price
    r = Risk(cfg)
    out = r.gate([pair()], make_ms(cfg, inv), inv, Clock())
    assert [i.kind for i in out] == [CANCEL] and out[0].ref is o and out[0].priority == P1
    assert r.n_self_trade == 1


def test_breaker_disables_venue_and_cancels_other():
    cfg = make_cfg()
    inv = Inventory()
    o = resting(inv, M, BUY, 996)
    br = Breaker(3)
    for _ in range(3):
        br.record(A, False)
    assert br.disabled[A]
    br2 = Breaker(3, hard_5xx=True)
    br2.record(M, False, hard=True)          # the mock's 503 = halted: trips at once when configured
    assert br2.disabled[M]
    br3 = Breaker(3, hard_5xx=False, backoff_5xx_ms=200)
    t = 10 ** 12
    br3.record(M, False, t, hard=True)       # RIT's 500 = overloaded: count 1, back off 200 ms
    assert not br3.disabled[M] and br3.fails[M] == 1 and br3.backing_off(M, t + 100_000_000)
    assert not br3.backing_off(M, t + 300_000_000)
    r = Risk(cfg, br)
    ms = make_ms(cfg, inv)
    out = r.gate([pair(), quote(A)], ms, inv, Clock())
    assert not ms.venue_enabled[A]
    assert all(i.kind != PAIR for i in out)
    assert any(i.kind == CANCEL and i.ref is o for i in out)
    out2 = r.gate([quote(A)], ms, inv, Clock())
    assert out2 == []                          # cancel emitted once, venue-A quote dropped


def test_end_of_case_ordering():
    cfg = make_cfg(k_quotes=10, k_flat=5)
    inv = Inventory()
    oq = resting(inv, A, SELL, 1002, purpose=P_QUOTE)
    clk = Clock()
    clk.set_case(292, 300, "ACTIVE")
    r = Risk(cfg)
    ms = make_ms(cfg, inv)
    out = r.gate([quote(), pair()], ms, inv, clk)
    kinds = [i.kind for i in out]
    assert CANCEL in kinds and QUOTE not in kinds and PAIR in kinds
    assert kinds.index(CANCEL) < kinds.index(PAIR)
    inv.pos = 700
    clk.set_case(297, 300, "ACTIVE")
    out = r.gate([pair()], ms, inv, clk)
    takes = [i for i in out if i.kind == TAKE]
    assert len(takes) == 1 and takes[0].order_kind == MARKET and takes[0].purpose == P_FLATTEN
    assert takes[0].side == SELL and takes[0].qty == 700 and takes[0].priority == P2
    assert any(i.kind == PAIR for i in out)


def test_kill_switch():
    cfg = make_cfg()
    inv = Inventory()
    o = resting(inv, M, BUY, 996)
    inv.pos = -300
    r = Risk(cfg)
    r.killed = True
    out = r.gate([pair(), quote()], make_ms(cfg, inv), inv, Clock())
    assert [i.kind for i in out] == [CANCEL, TAKE] and out[0].ref is o and out[1].side == BUY


def test_unhedged_flatten_after_hold():
    cfg = make_cfg(hedge_trigger=1000, hold_ms=10, enable_quotes=True)
    inv = Inventory()
    inv.pos = 2500
    r = Risk(cfg)
    ms = make_ms(cfg, inv)
    t = 10 ** 12
    assert not any(i.kind == TAKE for i in r.gate([], ms, inv, Clock(), t))
    out = r.gate([], ms, inv, Clock(), t + 20_000_000)
    takes = [i for i in out if i.kind == TAKE]
    assert len(takes) == 1 and takes[0].order_kind == LIMIT and takes[0].side == SELL


def test_pair_capped_at_pair_headroom():
    cfg = make_cfg()
    inv = Inventory(gross_limit=25000, slack=1000)
    inv.pos_venue = [22000, 0]
    out = Risk(cfg).gate([pair(5000)], make_ms(cfg, inv), inv, Clock())
    assert out[0].qty == 1000


def test_stray_aggressive_remainder_cancelled_after_delay_and_retried():
    from algo1.core.types import ACKED, Order, P_CROSS
    cfg = make_cfg(cancel_retry_ms=15)
    inv = Inventory()
    o = Order(M, BUY, LIMIT, 500, 1000, P_CROSS)
    o.state = ACKED
    o.id_server = 9
    t_ack = 10 ** 12
    o.t_ack = t_ack
    inv.register(o)
    inv.pending_aggr.append(o)
    r = Risk(cfg)
    ms = make_ms(cfg, inv)
    assert not any(i.kind == CANCEL for i in r.gate([], ms, inv, Clock(), t_ack + 5_000_000))     # too early: 404 land
    out = r.gate([], ms, inv, Clock(), t_ack + 20_000_000)
    assert [i.kind for i in out] == [CANCEL] and out[0].ref is o and out[0].priority == P1
    o.t_cancel_send = t_ack + 20_000_000                                                          # DELETE failed (404): still ACKED
    assert not any(i.kind == CANCEL for i in r.gate([], ms, inv, Clock(), t_ack + 25_000_000))
    assert any(i.kind == CANCEL for i in r.gate([], ms, inv, Clock(), t_ack + 40_000_000))        # retried


def test_one_leg_residual_hedged_at_once_when_quotes_off():
    cfg = make_cfg(enable_quotes=False, hedge_trigger_no_quotes=0, hold_ms_no_quotes=30)
    inv = Inventory()
    inv.pos = 400                                  # a one-leg residual well below hedge_trigger
    r = Risk(cfg)
    ms = make_ms(cfg, inv)
    t = 10 ** 12
    assert not any(i.kind == TAKE for i in r.gate([], ms, inv, Clock(), t))
    out = r.gate([], ms, inv, Clock(), t + 40_000_000)
    takes = [i for i in out if i.kind == TAKE]
    assert len(takes) == 1 and takes[0].side == SELL and takes[0].qty == 400 and takes[0].order_kind == LIMIT
    cfg.enable_quotes = True                       # with quotes on, 400 shares wait for the passive hedge
    assert not any(i.kind == TAKE for i in Risk(cfg).gate([], ms, inv, Clock(), t + 40_000_000))


def test_flatten_is_throttled_and_runaway_guard_kills():
    from algo1.core.types import ACKED, Order, P_FLATTEN
    cfg = make_cfg(k_flat=5, hedge_retry_ms=300)
    inv = Inventory()
    inv.pos = 700
    t = 10 ** 12
    clk = Clock()
    clk.set_case(297, 300, "ACTIVE", t_seen=t)      # synthetic clock: 3 ticks left at t
    r = Risk(cfg)
    ms = make_ms(cfg, inv)
    assert sum(i.kind == TAKE for i in r.gate([], ms, inv, clk, t)) == 1
    assert sum(i.kind == TAKE for i in r.gate([], ms, inv, clk, t + 100_000_000)) == 0      # within retry
    assert sum(i.kind == TAKE for i in r.gate([], ms, inv, clk, t + 400_000_000)) == 1
    o = Order(M, SELL, LIMIT, 700, 998, P_FLATTEN)
    o.state = ACKED
    inv.pending_aggr.append(o)
    assert sum(i.kind == TAKE for i in r.gate([], ms, inv, clk, t + 900_000_000)) == 0      # one in flight
    inv.n_unattributed = cfg.max_unattributed + 1
    out = r.gate([], ms, inv, clk, t + 2_000_000_000)
    assert r.killed and "unattributed" in r.kill_reason


def test_hedge_is_a_clamped_limit_never_market():
    cfg = make_cfg(enable_quotes=False, hedge_trigger_no_quotes=0, hold_ms_no_quotes=30, max_hedge_ticks=4)
    inv = Inventory()
    inv.pos = 400
    r = Risk(cfg)
    b = make_bbo(bm=940, am=1052, ba=1006, aa=1010)          # M 58 ticks wide, A tight; fair ≈ 1002
    ms = make_ms(cfg, inv, b)
    t = 10 ** 12
    r.gate([], ms, inv, Clock(), t)
    takes = [i for i in r.gate([], ms, inv, Clock(), t + 40_000_000) if i.kind == TAKE]
    assert len(takes) == 1 and takes[0].order_kind == LIMIT and takes[0].side == SELL
    assert takes[0].venue == A and takes[0].px >= int(ms.fair - 4)   # the better touch, never below fair − 4


def test_runaway_guard_counts_filled_hedges_not_cross_or_rested():
    from algo1.core.types import Fill, P_CROSS, P_HEDGE
    cfg = make_cfg(max_aggr_shares_10s=50_000)
    r = Risk(cfg)
    t = 10 ** 12
    cross = [Fill(1, M, BUY, 10000, 1000, t + i * 50_000_000, False, P_CROSS, 1000.0) for i in range(20)]
    r.note_fills(cross, t + 1_000_000_000)
    assert r.aggr_10s == 0                               # cross legs never count
    hs = [Fill(2, M, SELL, 10000, 998, t + i * 50_000_000, False, P_HEDGE, 1000.0) for i in range(6)]
    r.note_fills(hs, t + 1_000_000_000)
    assert r.aggr_10s == 60_000
    inv = Inventory()
    r.gate([], make_ms(cfg, inv), inv, Clock(), t + 1_100_000_000)
    assert r.killed and "hedge/flatten" in r.kill_reason
