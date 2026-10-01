"""matplotlib panels for the monitor process (debrief material; never in the engine).

    (a) BBO both venues + our resting quotes + fills, last 30 s
    (b) s_t = mid_A - mid_M with ± cost band, cross markers, our pair fires
    (c) latency stage histograms
    (d) cumulative pnl by purpose + inventory
    (e) budget: tokens, denials, 429s
matplotlib over plotly (FORK): already installed, and the plots are for the debrief, not the heat.
"""
from __future__ import annotations

import numpy as np


class LivePlots:
    def __init__(self):
        import matplotlib
        matplotlib.use("MacOSX" if matplotlib.get_backend().lower().startswith("macosx") else matplotlib.get_backend())
        import matplotlib.pyplot as plt
        self.plt = plt
        plt.ion()
        self.fig, self.ax = plt.subplots(3, 2, figsize=(13, 8))
        self.fig.tight_layout()

    def update(self, st):
        from .ringbuf import FILL_DTYPE, LOOP_DTYPE, ORDER_DTYPE
        L = np.concatenate(st.loops) if st.loops else np.zeros(0, LOOP_DTYPE)
        O = np.concatenate(st.orders) if st.orders else np.zeros(0, ORDER_DTYPE)
        F = np.concatenate(st.fills) if st.fills else np.zeros(0, FILL_DTYPE)
        if not len(L):
            return
        ax = self.ax
        for a in ax.flat:
            a.cla()
        t0 = L["t0"][0]
        t = (L["t6"] - t0) / 1e9
        w = L[L["t6"] >= L["t6"][-1] - 30_000_000_000]
        tw = (w["t6"] - t0) / 1e9
        ax[0, 0].plot(tw, w["bid_m"], lw=0.6, color="tab:blue", label="M bid/ask")
        ax[0, 0].plot(tw, w["ask_m"], lw=0.6, color="tab:blue")
        ax[0, 0].plot(tw, w["bid_a"], lw=0.6, color="tab:orange", label="A bid/ask")
        ax[0, 0].plot(tw, w["ask_a"], lw=0.6, color="tab:orange")
        ax[0, 0].plot(tw, w["fair"], lw=0.8, color="k", ls="--", label="fair")
        if len(O):
            q = O[(O["purpose"] == 1) & (O["t_ack"] > 0) & (O["t_ack"] >= L["t6"][-1] - 30_000_000_000)]
            for side, mk in ((0, "^"), (1, "v")):
                qs = q[q["side"] == side]
                ax[0, 0].scatter((qs["t_ack"] - t0) / 1e9, qs["px"], marker=mk, s=12,
                                 c=["tab:blue" if v == 0 else "tab:orange" for v in qs["venue"]], alpha=0.6)
        if len(F):
            fw = F[F["t_seen"] >= L["t6"][-1] - 30_000_000_000]
            ax[0, 0].scatter((fw["t_seen"] - t0) / 1e9, fw["px"], marker="o", s=30, c="red", label="fills")
        ax[0, 0].legend(loc="upper left", fontsize=7)
        ax[0, 0].set_title("(a) BBO both venues, our quotes (▲ bid ▼ ask), fills — last 30 s")
        ax[0, 1].plot(t, L["s"], lw=0.8)
        ax[0, 1].axhline(0, color="k", lw=0.5)
        ax[0, 1].set_title("(b) s = mid_A − mid_M")
        rtt = (L["tr"] - L["ts"]) / 1e3
        inloop = (L["t5"] - L["tr"]) / 1e3
        ax[1, 0].hist(rtt, bins=50, alpha=0.6, label="poll RTT µs")
        ax[1, 0].hist(inloop, bins=50, alpha=0.6, label="in-loop µs")
        ax[1, 0].legend()
        ax[1, 0].set_title("(c) latency")
        if len(F):
            tf = (F["t_seen"] - t0) / 1e9
            ax[1, 1].step(tf, np.cumsum(F["edge"] * F["qty"]), where="post", label="edge pnl")
        ax[1, 1].plot(t, L["pos"], lw=0.6, label="pos", color="gray")
        ax[1, 1].legend()
        ax[1, 1].set_title("(d) pnl (ticks·sh) + inventory")
        ax[2, 0].plot(t, L["tokens"], lw=0.8, label="tokens")
        ax[2, 0].plot(t, L["n_deny"], lw=0.8, label="deny")
        ax[2, 0].plot(t, L["n429"], lw=0.8, label="429")
        ax[2, 0].legend()
        ax[2, 0].set_title("(e) budget")
        if len(O):
            s2a = (O["t_ack"] - O["t_send"])[O["t_ack"] > 0] / 1e3
            ax[2, 1].hist(s2a, bins=50)
        ax[2, 1].set_title("send→ack µs")
        self.fig.canvas.draw_idle()
        self.plt.pause(0.001)
