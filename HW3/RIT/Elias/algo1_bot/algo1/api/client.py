"""HTTP lanes: one persistent keep-alive connection per lane, timed requests.

HTTP/1.1 is sequential per connection, so a cancel queued behind a slow POST on the same lane is
a late cancel — and cancels are the maker's binding latency. Hence four lanes:

    poll   GET  /v1/securities, /v1/case, /v1/securities/book, /v1/orders
    ord1   POST /v1/orders          (a pair's two legs go one per lane, concurrently)
    ord2   POST /v1/orders
    cxl    DELETE /v1/orders/{id}, POST /v1/commands/cancel

Every request returns `(status, body, t_send, t_recv)`; status 0 = transport failure (timeout,
refused, reset). One reconnect-and-retry is attempted on a stale keep-alive socket.

VERIFY LIVE: does the RIT client accept 4 concurrent connections and does it serialise them?
`bench.py --pair` measures sequential vs concurrent and a 4-lane variant.
"""
from __future__ import annotations

import http.client
import socket
from time import perf_counter_ns


class Lane:
    __slots__ = ("name", "host", "port", "headers", "timeout", "conn", "n_req", "n_fail", "n_reconnect")

    def __init__(self, name, host, port, api_key, timeout=2.0):
        self.name = name
        self.host = host
        self.port = port
        self.headers = {"X-API-Key": api_key, "Connection": "keep-alive"}
        self.timeout = timeout
        self.conn = None
        self.n_req = 0
        self.n_fail = 0
        self.n_reconnect = 0

    def connect(self):
        self.close()
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        self.conn.connect()
        try:
            self.conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.n_reconnect += 1

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def request(self, method, path, body=None):
        """Timed request. Returns (status, body_bytes, t_send_ns, t_recv_ns)."""
        self.n_req += 1
        if self.conn is None:
            try:
                self.connect()
            except OSError:
                self.n_fail += 1
                t = perf_counter_ns()
                return 0, b"", t, t
        t_send = perf_counter_ns()
        try:
            self.conn.request(method, path, body=body, headers=self.headers)
            resp = self.conn.getresponse()
            data = resp.read()
            t_recv = perf_counter_ns()
            return resp.status, data, t_send, t_recv
        except (http.client.HTTPException, OSError):
            # stale keep-alive or server hiccup: reconnect once and retry
            try:
                self.connect()
                t_send = perf_counter_ns()
                self.conn.request(method, path, body=body, headers=self.headers)
                resp = self.conn.getresponse()
                data = resp.read()
                t_recv = perf_counter_ns()
                return resp.status, data, t_send, t_recv
            except (http.client.HTTPException, OSError):
                self.n_fail += 1
                self.close()
                t_recv = perf_counter_ns()
                return 0, b"", t_send, t_recv

    def get(self, path):
        return self.request("GET", path)

    def post(self, path):
        return self.request("POST", path)

    def delete(self, path):
        return self.request("DELETE", path)


class Lanes:
    """The four lanes of one bot. `ord` rotates ord1/ord2 for single orders."""
    __slots__ = ("poll", "ord1", "ord2", "cxl", "_rr")

    def __init__(self, host, port, api_key, timeout=2.0):
        self.poll = Lane("poll", host, port, api_key, timeout)
        self.ord1 = Lane("ord1", host, port, api_key, timeout)
        self.ord2 = Lane("ord2", host, port, api_key, timeout)
        self.cxl = Lane("cxl", host, port, api_key, timeout)
        self._rr = 0

    def connect_all(self):
        for l in (self.poll, self.ord1, self.ord2, self.cxl):
            l.connect()

    def close_all(self):
        for l in (self.poll, self.ord1, self.ord2, self.cxl):
            l.close()

    def ord(self):
        self._rr ^= 1
        return self.ord1 if self._rr else self.ord2

    def all(self):
        return (self.poll, self.ord1, self.ord2, self.cxl)
