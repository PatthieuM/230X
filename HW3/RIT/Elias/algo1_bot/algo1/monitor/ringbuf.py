"""Shared-memory ring buffers: the loop's only monitoring cost is one memcpy per record.

Layout of one buffer in a `multiprocessing.shared_memory` block:
    [head: uint64][capacity: uint64][records: numpy structured array of `capacity` rows]
The capacity lives in the header because Windows rounds a mapping's size up to its allocation
granularity, so a reader cannot infer the row count from `shm.size`.
Single writer: `arr[head % cap] = rec; head += 1` — no lock. A reader snapshots `head`, copies
the new slice, and keeps its own cursor; if it falls more than `capacity` behind it loses the
oldest records and reports the gap. `shared=False` gives a plain in-process buffer for tests.

Three buffers per run: loops (one per iteration), orders (one per state change), fills.
Names are `algo1_<run>_<kind>`; macOS caps POSIX shm names at 31 chars, so long run names are
hashed. `dump(dir)` writes each buffer to parquet for `monitor/report.py`.

Why a ring and not UDP to localhost (FORK): UDP is simpler (sendto ≈ 5–10 µs) but lossy; the
ring is lossless and one memcpy. Why not the logging module: formatting a line is 20–50 µs.
"""
from __future__ import annotations

import hashlib
import os

import numpy as np

LOOP_DTYPE = np.dtype([
    ("i", "u8"), ("t0", "i8"), ("ts", "i8"), ("tr", "i8"), ("t1", "i8"), ("t2", "i8"),
    ("t3", "i8"), ("t4", "i8"), ("t5", "i8"), ("t6", "i8"), ("n_intents", "u2"),
    ("n_msgs", "u2"), ("n_deny", "u4"), ("n429", "u4"), ("pos", "i4"), ("fair", "f8"),
    ("s", "f4"), ("fired", "u2"), ("tick", "u2"), ("tokens", "f4"),
    ("bid_m", "i4"), ("ask_m", "i4"), ("bid_a", "i4"), ("ask_a", "i4"),   # raw BBO, ticks
    ("sig_m", "f4"), ("sig_a", "f4"), ("d_raw", "f4"), ("d_final", "f4"), ("d_bind", "u1"),   # width stack
    ("sig_lag_m", "f4"), ("sig_lag_a", "f4"),                                                   # horizon-lag sigma
])
ORDER_DTYPE = np.dtype([
    ("id_local", "u4"), ("id_server", "i8"), ("venue", "u1"), ("side", "u1"), ("kind", "u1"),
    ("purpose", "u1"), ("state", "u1"), ("priority", "u1"), ("qty", "u4"), ("px", "i4"),
    ("filled", "u4"), ("vwap", "f8"), ("expected_px", "i4"), ("expected_edge", "f4"),
    ("snapshot_id", "u8"), ("t_intent", "i8"), ("t_send", "i8"), ("t_ack", "i8"),
    ("t_first_fill", "i8"), ("t_cancel_send", "i8"), ("t_cancel_ack", "i8"), ("t_done", "i8"),
    ("fair_at_intent", "f8"), ("http", "u2"),
])
FILL_DTYPE = np.dtype([
    ("order_local", "i4"), ("venue", "u1"), ("side", "u1"), ("qty", "u4"), ("px", "i4"),
    ("t_seen", "i8"), ("passive", "u1"), ("purpose", "u1"), ("fair_at_fill", "f8"), ("edge", "f4"),
])
KINDS = {"loops": LOOP_DTYPE, "orders": ORDER_DTYPE, "fills": FILL_DTYPE}
_HEAD = 16
_OWNED = set()      # shm names created by this process (a same-process reader must not unregister them)


def shm_name(run, kind):
    name = f"algo1_{run}_{kind}"
    if len(name) > 30:
        name = "algo1_" + hashlib.md5(run.encode()).hexdigest()[:10] + "_" + kind
    return name[:30]


class RingBuffer:
    __slots__ = ("name", "dtype", "cap", "shm", "buf", "arr", "head", "_head_view", "cursor",
                 "shared", "lost", "owner")

    def __init__(self, name, dtype, capacity, shared=True, create=True):
        self.name = name
        self.dtype = dtype
        self.cap = int(capacity)
        self.shared = shared
        self.cursor = 0
        self.lost = 0
        self.owner = create
        nbytes = _HEAD + self.cap * dtype.itemsize
        if shared:
            from multiprocessing import shared_memory
            if create:
                try:
                    shared_memory.SharedMemory(name=name, create=False).unlink()
                except FileNotFoundError:
                    pass
                self.shm = shared_memory.SharedMemory(name=name, create=True, size=nbytes)
                _OWNED.add(name)
            else:
                self.shm = shared_memory.SharedMemory(name=name, create=False)
                # a reader in another process must not unlink the block when it exits
                if name not in _OWNED:
                    try:
                        from multiprocessing import resource_tracker
                        resource_tracker.unregister(self.shm._name, "shared_memory")
                    except Exception:
                        pass
            self.buf = self.shm.buf
        else:
            self.shm = None
            self.buf = bytearray(nbytes)
        self._head_view = np.ndarray((1,), dtype="u8", buffer=self.buf, offset=0)
        cap_view = np.ndarray((1,), dtype="u8", buffer=self.buf, offset=8)
        if create:
            self._head_view[0] = 0
            cap_view[0] = self.cap
        else:
            self.cap = int(cap_view[0])
        self.arr = np.ndarray((self.cap,), dtype=dtype, buffer=self.buf, offset=_HEAD)
        self.head = int(self._head_view[0])

    # ---- writer --------------------------------------------------------------------------
    def write(self, rec):
        h = self.head
        self.arr[h % self.cap] = rec
        self.head = h + 1
        self._head_view[0] = h + 1

    # ---- reader --------------------------------------------------------------------------
    def read_new(self):
        """Copy of the records written since the last call (oldest first)."""
        h = int(self._head_view[0])
        c = self.cursor
        if h - c > self.cap:
            self.lost += (h - c) - self.cap
            c = h - self.cap
        if h == c:
            self.cursor = h
            return self.arr[:0].copy()
        lo, hi = c % self.cap, h % self.cap
        if lo < hi:
            out = self.arr[lo:hi].copy()
        else:
            out = np.concatenate((self.arr[lo:], self.arr[:hi]))
        self.cursor = h
        return out

    def read_all(self):
        h = int(self._head_view[0])
        n = min(h, self.cap)
        start = (h - n) % self.cap
        if n == 0:
            return self.arr[:0].copy()
        if start + n <= self.cap:
            return self.arr[start:start + n].copy()
        return np.concatenate((self.arr[start:], self.arr[:(start + n) % self.cap]))

    @property
    def count(self):
        return int(self._head_view[0])

    def close(self, unlink=False):
        self._head_view = None
        self.arr = None
        if self.shm is not None:
            try:
                self.shm.close()
                if unlink and self.owner:
                    self.shm.unlink()
                    _OWNED.discard(self.name)
            except Exception:
                pass
            self.shm = None


class RingSet:
    """The three buffers of one run + typed writers that turn objects into records."""

    def __init__(self, run, n_loops=1 << 16, n_orders=1 << 14, n_fills=1 << 14, shared=True, create=True):
        self.run = run
        self.loops = RingBuffer(shm_name(run, "loops"), LOOP_DTYPE, n_loops, shared, create)
        self.orders = RingBuffer(shm_name(run, "orders"), ORDER_DTYPE, n_orders, shared, create)
        self.fills = RingBuffer(shm_name(run, "fills"), FILL_DTYPE, n_fills, shared, create)

    def write_loop(self, rec):
        self.loops.write(rec)

    def write_order(self, o):
        self.orders.write((o.id_local, o.id_server, o.venue, o.side, o.kind, o.purpose, o.state,
                           o.priority, o.qty, o.px or 0, o.filled, o.vwap_ticks, o.expected_px,
                           o.expected_edge, o.snapshot_id, o.t_intent, o.t_send, o.t_ack,
                           o.t_first_fill, o.t_cancel_send, o.t_cancel_ack, o.t_done,
                           o.fair_at_intent, o.http_status))

    def write_fill(self, f):
        self.fills.write((f.order_local, f.venue, f.side, f.qty, f.px, f.t_seen, f.passive,
                          f.purpose, f.fair_at_fill, f.edge))

    def dump(self, run_dir):
        import pandas as pd
        os.makedirs(run_dir, exist_ok=True)
        for kind, rb in (("loops", self.loops), ("orders", self.orders), ("fills", self.fills)):
            pd.DataFrame(rb.read_all()).to_parquet(os.path.join(run_dir, f"{kind}.parquet"))

    def close(self, unlink=False):
        for rb in (self.loops, self.orders, self.fills):
            rb.close(unlink)

    @classmethod
    def attach(cls, run):
        return _attach(run)


def _attach(run):
    """Reader-side attach: capacities come from each block's header."""
    from multiprocessing import shared_memory
    rs = RingSet.__new__(RingSet)
    rs.run = run
    for kind, dt in KINDS.items():
        name = shm_name(run, kind)
        setattr(rs, kind, RingBuffer(name, dt, 1, shared=True, create=False))   # cap read from the header
    return rs
