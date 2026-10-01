"""Live monitor — a separate process reading the rings every 200 ms.

    python -m algo1 monitor --name run            one status line, refreshed in place
    python -m algo1 monitor --name run --plots    + matplotlib panels (debrief, not the heat)

Rolling windows: the last 10 s and the whole run. Nothing here touches the engine process.
"""
from __future__ import annotations

import sys
import time

import numpy as np

from ..core.types import PURPOSES, STATE_NAME
from .ringbuf import _attach


def pct(x, q):
    return float(np.percentile(x, q)) if len(x) else float("nan")


class Stats:
    """Rolling stats over the three record streams."""

    def __init__(self, window_s=10.0):
        self.window_ns = int(window_s * 1e9)
        self.loops = []
        self.orders = []
        self.fills = []

    def add(self, loops, orders, fills):
        if len(loops):
            self.loops.append(loops)
        if len(orders):
            self.orders.append(orders)
        if len(fills):
            self.fills.append(fills)

    def _cat(self, parts, dtype):
        return np.concatenate(parts) if parts else np.zeros(0, dtype=dtype)

    def line(self, budget_tokens=None, lost=0):
        from .ringbuf import FILL_DTYPE, LOOP_DTYPE, ORDER_DTYPE
        L = self._cat(self.loops, LOOP_DTYPE)
        O = self._cat(self.orders, ORDER_DTYPE)
        F = self._cat(self.fills, FILL_DTYPE)
        if not len(L):
            return "no loops yet"
        t_now = int(L["t6"][-1])
        w = L[L["t6"] >= t_now - self.window_ns]
        rtt = (w["tr"] - w["ts"]) / 1e3
        inloop = (w["t5"] - w["tr"]) / 1e3
        acks = O[(O["t_ack"] > 0) & (O["t_send"] > 0)]
        acks_w = acks[acks["t_ack"] >= t_now - self.window_ns]
        s2a = (acks_w["t_ack"] - acks_w["t_send"]) / 1e3
        pairs = O[(O["priority"] == 0) & (O["t_send"] > 0)]
        n_pair_orders = len(pairs)
        n_pair_filled = int((pairs["state"] == 4).sum())
        pnl = float((F["edge"] * F["qty"]).sum()) if len(F) else 0.0
        by_purpose = {}
        for p in range(len(PURPOSES)):
            m = F["purpose"] == p
            if m.any():
                by_purpose[PURPOSES[p]] = int(m.sum())
        rate = len(w) / max(1e-9, (w["t6"][-1] - w["t6"][0]) / 1e9) if len(w) > 1 else 0.0
        last = L[-1]
        crossed = "X" if (last["bid_a"] > last["ask_m"] or last["bid_m"] > last["ask_a"]) else " "
        resting = 0
        if len(O):
            final_state = {}
            for rec in O:                       # last record per order wins
                final_state[int(rec["id_local"])] = int(rec["state"])
            resting = sum(1 for st_ in final_state.values() if st_ in (2, 3))
        book = (f"M {int(last['bid_m'])}/{int(last['ask_m'])} A {int(last['bid_a'])}/{int(last['ask_a'])}{crossed} | "
                f"resting {resting} | ")
        return (f"tick {int(L['tick'][-1]):3d} loops {len(L):6d} ({rate:5.0f}/s) {book}"
                f"rtt p50 {pct(rtt,50):6.0f} p95 {pct(rtt,95):6.0f} us | inloop p50 {pct(inloop,50):5.0f} us | "
                f"send→ack p50 {pct(s2a,50):6.0f} p95 {pct(s2a,95):6.0f} us | "
                f"pair legs {n_pair_orders} filled {n_pair_filled} | fills {len(F)} {by_purpose} | "
                f"edge {pnl:8.0f} t | pos {int(L['pos'][-1]):6d} | fair {L['fair'][-1]:8.1f} s {L['s'][-1]:5.1f} | "
                f"tok {L['tokens'][-1]:4.1f} deny {int(L['n_deny'][-1])} 429 {int(L['n429'][-1])}"
                + (f" | LOST {lost}" if lost else ""))


def monitor(run, interval=0.2, plots=False, once=False, out=sys.stdout):
    rs = None
    for _ in range(50):
        try:
            rs = _attach(run)
            break
        except FileNotFoundError:
            time.sleep(0.2)
    if rs is None:
        print(f"no ring buffers for run '{run}' (is the engine running?)", file=out)
        return 1
    st = Stats()
    plotter = None
    if plots:
        from .plots import LivePlots
        plotter = LivePlots()
    idle_since = None
    try:
        while True:
            new = rs.loops.read_new()
            if len(new):
                idle_since = None
            elif idle_since is None:
                idle_since = time.time()
            elif time.time() - idle_since > 10 and not once:
                out.write("\n(engine idle for 10 s — stopped? run `report`)\n")
                break
            st.add(new, rs.orders.read_new(), rs.fills.read_new())
            lost = rs.loops.lost + rs.orders.lost + rs.fills.lost
            out.write("\r" + st.line(lost=lost)[:220])
            out.flush()
            if plotter is not None:
                plotter.update(st)
            if once:
                out.write("\n")
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        out.write("\n")
    finally:
        rs.close()
    return 0
