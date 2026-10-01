from algo1.core.types import (A, ACKED, BUY, CANCELLED, FILLED, Inventory, LIMIT, M, MARKET,
                              Order, P_CROSS, PARTIAL, SELL)
from conftest import make_bbo, resting


def test_net_out_subtracts_own_at_touch():
    inv = Inventory()
    resting(inv, M, BUY, 998, 300)
    b = inv.net_out(make_bbo(bsm=1000))
    assert b.bid_size[M] == 700 and b.bid[M] == 998


def test_net_out_own_is_the_touch():
    inv = Inventory()
    resting(inv, A, SELL, 1000, 1000)
    b = inv.net_out(make_bbo(asa=1000))
    assert b.ask_size[A] == 0 and b.ask[A] == 1000    # px kept, size 0 until a ladder poll


def test_detect_fills_attributes_passive_to_best_resting():
    inv = Inventory()
    o_far = resting(inv, A, BUY, 995, 500)
    o_best = resting(inv, A, BUY, 999, 500)
    fills = inv.detect_fills(300, [0, 300], 1, 1000.0)
    assert len(fills) == 1 and fills[0].order_local == o_best.id_local and fills[0].passive
    assert o_best.filled == 300 and o_best.state == PARTIAL and o_far.filled == 0
    assert inv.pos == 300 and inv.pos_venue == [0, 300]
    fills = inv.detect_fills(600, [0, 600], 2, 1000.0)
    assert o_best.state == FILLED and o_far.filled == 100 and o_best not in inv.own_resting[(A, BUY)]


def test_detect_fills_pending_aggressive_first():
    inv = Inventory()
    o_rest = resting(inv, M, BUY, 999, 500)
    o_mkt = Order(M, BUY, MARKET, 400, None, P_CROSS)
    o_mkt.state = ACKED
    o_mkt.expected_px = 1000
    inv.register(o_mkt)
    inv.pending_aggr.append(o_mkt)
    fills = inv.detect_fills(400, [400, 0], 1, 1000.0)
    assert fills[0].order_local == o_mkt.id_local and not fills[0].passive and fills[0].px == 1000
    assert o_mkt.state == FILLED and o_mkt not in inv.pending_aggr and o_rest.filled == 0


def test_detect_fills_residual_unknown():
    inv = Inventory()
    fills = inv.detect_fills(-200, [0, -200], 1, 1000.0)
    assert fills[0].order_local == -1 and fills[0].side == SELL and inv.n_unattributed == 200
    assert inv.pos == -200


def test_apply_orders_no_double_count():
    inv = Inventory()
    o = Order(M, BUY, LIMIT, 300, 1000, P_CROSS)
    o.state = FILLED
    o.filled = 300
    o.t_ack = 5
    inv.apply_orders([o])
    assert inv.pos == 300 and o.pos_applied == 300
    inv.apply_orders([o])                       # same ack twice: nothing new
    assert inv.pos == 300
    fills = inv.detect_fills(300, [300, 0], 6, 1000.0)
    assert fills == [] and inv.pos == 300


def test_apply_orders_partial_then_cancelled_keeps_fill():
    inv = Inventory()
    o = Order(M, BUY, LIMIT, 300, 1000, P_CROSS)
    o.state = CANCELLED
    o.filled = 120
    o.t_ack = 5
    inv.apply_orders([o])
    assert inv.pos == 120 and o not in inv.all_resting()


def test_apply_orders_registers_resting_quote():
    inv = Inventory()
    o = Order(A, SELL, LIMIT, 500, 1005)
    o.state = ACKED
    o.id_server = 7
    inv.apply_orders([o])
    assert inv.own_resting[(A, SELL)] == [o] and inv.by_server[7] is o


def test_apply_sweep_corrects_state():
    inv = Inventory()
    o = resting(inv, M, SELL, 1003, 500, id_server=42)
    changed = inv.apply_sweep([(42, 500, 1003.0, "TRANSACTED")], 9)
    assert o.state == FILLED and o.filled == 500 and o not in inv.all_resting() and changed


def test_headroom_and_pair_headroom():
    inv = Inventory(gross_limit=25000, net_limit=25000, slack=1000)
    inv.pos = 4000
    inv.pos_venue = [6000, -2000]
    assert inv.headroom() == 20000
    assert inv.headroom_gross() == 16000 and inv.pair_headroom() == 8000
    # direction-aware: buying on A (short 2000) and selling on M (long 6000) unwinds both legs
    assert inv.pair_headroom(A, M) == 16000          # |-2000+q| + |6000-q| <= 24000 → q <= 16000
    assert inv.pair_headroom(M, A) == 8000           # adds to both
    inv.pos_venue = [-12000, 12000]                  # at the limit after many same-direction pairs
    assert inv.pair_headroom(M, A) == 24000 and inv.pair_headroom(A, M) == 0
    inv.pos_venue = [-13000, 13000]                  # over the limit: only unwinding, only to flat
    assert inv.pair_headroom(M, A) == 13000 and inv.pair_headroom(A, M) == 0
    inv.gross_both_legs = False                      # RIT nets the two tickers: gross = |net| = 4000
    inv.pos_venue = [6000, -2000]
    assert inv.pair_headroom() == 20000


def test_headroom_uses_net_when_exchange_nets_tickers():
    inv = Inventory(gross_limit=25000, net_limit=25000, slack=1000, gross_both_legs=False)
    inv.pos = 0
    inv.pos_venue = [24000, -24000]                  # after three pairs, as on the demo (p1, 10 Sep)
    assert inv.headroom_gross() == 24000 and inv.pair_headroom(M, A) == 24000   # the reverse cross can fire
    inv.gross_both_legs = True
    assert inv.headroom_gross() == 0


def test_lagging_server_position_is_not_a_fill():
    """The p2 incident: acked fill moves pos; the server still shows the old value for 200 ms."""
    from algo1.core.types import FILLED, P_FLATTEN
    inv = Inventory(pos_per_ticker=False, gross_both_legs=False, pos_lag_window_ms=1000)
    t = 10 ** 12
    o = Order(M, SELL, MARKET, 1000, None, P_FLATTEN)
    o.state = FILLED
    o.filled = 1000
    o.t_ack = t
    inv.apply_orders([o], False, 1000.0, t)
    assert inv.pos == -1000
    assert inv.detect_fills(0, None, t + 50_000_000, 1000.0) == [] and inv.pos == -1000        # lag: ignored
    assert inv.detect_fills(-1000, None, t + 200_000_000, 1000.0) == [] and inv.pos == -1000    # caught up
    fills = inv.detect_fills(-1500, None, t + 300_000_000, 1000.0)                                 # a real sell of 500
    assert len(fills) == 1 and fills[0].side == SELL and fills[0].qty == 500 and inv.pos == -1500
    assert inv.detect_fills(-1000, None, t + 400_000_000, 1000.0) == [] and inv.pos == -1500    # inside the envelope: lag
    fills = inv.detect_fills(-1000, None, t + 2_000_000_000, 1000.0)                                # window expired: server wins
    assert len(fills) == 1 and fills[0].side == BUY and fills[0].qty == 500 and inv.pos == -1000
    assert inv.n_lag_ignored == 2
