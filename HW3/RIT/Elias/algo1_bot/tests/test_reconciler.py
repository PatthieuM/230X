from algo1.api.budget import Budget
from algo1.core.types import (A, BUY, CANCEL, Intent, Inventory, LIMIT, M, P0, P1, P3, P_CROSS,
                              P_QUOTE, PAIR, QUOTE, SELL)
from algo1.execution.reconciler import CXL, NEW, PAIRMSG, Reconciler
from conftest import make_cfg, resting


def quote(v=M, side=BUY, px=996, pri=P3):
    return Intent(QUOTE, pri, v, side, px, 500, P_QUOTE)


def test_one_message_per_key():
    rec = Reconciler(make_cfg())
    msgs = rec.plan([quote(px=996), quote(px=995)], Inventory(), Budget(100, 100, 0))
    assert len(msgs) == 1 and msgs[0].kind == NEW and msgs[0].orders[0].px == 996 and rec.n_dup_key == 1


def test_replace_is_cancel_then_new_and_never_two_resting():
    inv = Inventory()
    o = resting(inv, M, BUY, 990)
    rec = Reconciler(make_cfg())
    # without a cancel: a live order on the key blocks the new quote
    assert rec.plan([quote()], inv, Budget(100, 100, 0)) == []
    msgs = rec.plan([Intent(CANCEL, P3, M, BUY, ref=o), quote()], inv, Budget(100, 100, 0))
    assert [m.kind for m in msgs] == [CXL, NEW] and msgs[0].ref is o


def test_pair_never_legs_in_on_budget():
    b = Budget(10, 10, 2)
    b.tokens = [1.0]
    rec = Reconciler(make_cfg())
    it = Intent(PAIR, P0, qty=500, purpose=P_CROSS, legs=((M, BUY, 1000, LIMIT), (A, SELL, 1003, LIMIT)))
    assert rec.plan([it], Inventory(), b, t=b.t_last[0]) == [] and rec.n_pair_denied == 1
    b.tokens = [2.0]
    msgs = rec.plan([it], Inventory(), b, t=b.t_last[0])
    assert len(msgs) == 1 and msgs[0].kind == PAIRMSG and len(msgs[0].orders) == 2
    assert msgs[0].orders[0].priority == P0 and msgs[0].orders[1].px == 1003


def test_priority_order_and_deny_counters():
    b = Budget(10, 10, 2)
    b.tokens = [4.0]
    rec = Reconciler(make_cfg())
    its = [quote(M, BUY, 996), quote(A, SELL, 1002), quote(A, BUY, 996, pri=P1)]
    msgs = rec.plan(its, Inventory(), b, t=b.t_last[0])
    # P1 first (4→3); one P3 fits above the reserve of 2 (3→2); the second is denied
    assert [m.priority for m in msgs] == [P1, P3] and b.deny[P3] == 1


def test_cancel_of_non_resting_is_skipped():
    from algo1.core.types import Order
    o = Order(M, BUY, LIMIT, 100, 990)
    rec = Reconciler(make_cfg())
    assert rec.plan([Intent(CANCEL, P1, M, BUY, ref=o)], Inventory(), Budget(100, 100, 0)) == []
