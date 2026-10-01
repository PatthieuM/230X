import threading

import numpy as np

from algo1.monitor.ringbuf import LOOP_DTYPE, RingBuffer, RingSet, _attach


def rec(i):
    return (i, i, i, i, i, i, i, i, i, i, 0, 0, 0, 0, 0, 1.0, 0.0, 0, 0, 1.0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0)


def test_write_read_and_wrap():
    rb = RingBuffer("x", LOOP_DTYPE, 8, shared=False)
    for i in range(5):
        rb.write(rec(i))
    out = rb.read_new()
    assert list(out["i"]) == [0, 1, 2, 3, 4] and len(rb.read_new()) == 0
    for i in range(5, 20):
        rb.write(rec(i))
    out = rb.read_new()
    assert rb.lost == 7 and list(out["i"]) == list(range(12, 20))
    assert list(rb.read_all()["i"]) == list(range(12, 20))


def test_shared_no_lost_records_with_concurrent_reader():
    rs = RingSet("pytest_ring", 1 << 12, 16, 16, shared=True, create=True)
    got = []
    stop = threading.Event()

    def reader():
        r = _attach("pytest_ring")
        while not stop.is_set() or r.loops.count > r.loops.cursor:
            out = r.loops.read_new()
            if len(out):
                got.append(out)
        r.close()
    th = threading.Thread(target=reader)
    th.start()
    for i in range(3000):
        rs.write_loop(rec(i))
    stop.set()
    th.join()
    seen = np.concatenate(got)["i"]
    assert list(seen) == list(range(3000))
    rs.close(unlink=True)
