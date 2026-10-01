from algo1.api.budget import Budget
from algo1.core.types import P0, P3


def test_p0_reserve_denies_quotes_not_pairs():
    b = Budget(10, 10, p0_reserve=2)
    t = 10 ** 9
    for _ in range(8):
        assert b.try_acquire(1, P3, t)
    assert not b.try_acquire(1, P3, t)              # would leave < reserve
    assert b.deny[P3] == 1
    assert b.try_acquire(2, P0, t)                  # the pair still goes
    assert not b.try_acquire(2, P0, t)              # a pair never legs in: 0 tokens → deny
    assert b.deny[P0] == 1


def test_penalize_freezes_refill():
    b = Budget(10, 10, 0)
    t = 10 ** 9
    b.penalize(0.5, t)
    assert not b.try_acquire(1, P3, t + int(0.3e9))
    assert b.try_acquire(1, P3, t + int(1.0e9))     # refilled after the freeze
    assert b.n429 == 1


def test_per_ticker_buckets_pair_takes_one_each():
    b = Budget(10, 10, p0_reserve=2, per_ticker=True)
    t = 10 ** 9
    b.t_last = [t, t]                                    # synthetic clock
    for _ in range(8):
        assert b.try_acquire(1, P3, t, venue=0)          # drain M's bucket to the reserve
    assert not b.try_acquire(1, P3, t, venue=0)
    assert b.try_acquire(1, P3, t, venue=1)              # A's bucket is untouched
    assert b.try_acquire_pair(P0, t, (0, 1))             # 1 from each: M 2→1, A 9→8
    assert b.tokens == [1.0, 8.0]
    assert b.try_acquire_pair(P0, t, (0, 1))             # P0 may take a bucket's last token: M 1→0, A 8→7
    assert b.tokens == [0.0, 7.0]
    assert not b.try_acquire_pair(P0, t, (0, 1))         # M empty: both or neither
    assert b.tokens == [0.0, 7.0] and b.deny[P0] == 1
    b.penalize(0.5, t, venue=1)                          # a 429 on A freezes only A
    assert b.tokens == [0.0, 0.0]
    assert b.try_acquire(1, P0, t + int(0.3e9), venue=0)     # M refilled 3 tokens; A still frozen
    assert not b.try_acquire(1, P0, t + int(0.3e9), venue=1)
