import json

from algo1.api.messages import (Ack, PathBuilder, RateLimited, Reject, parse_ack, parse_book,
                                parse_securities, parse_sheet)
from algo1.core.types import A, BUY, LIMIT, M, MARKET, SELL


def test_order_path():
    pb = PathBuilder(100)
    assert pb.order_path(M, BUY, LIMIT, 500, 1005) == "/v1/orders?ticker=CRZY_M&type=LIMIT&action=BUY&quantity=500&price=10.05"
    assert pb.order_path(A, SELL, MARKET, 100) == "/v1/orders?ticker=CRZY_A&type=MARKET&action=SELL&quantity=100"
    assert pb.px_str(999) == "9.99" and pb.px_str(1000) == "10.00"


def test_parse_ack_variants():
    a = parse_ack(200, json.dumps({"order_id": 5, "quantity": 100, "quantity_filled": 40, "vwap": 10.02, "status": "OPEN"}).encode(), 100)
    assert isinstance(a, Ack) and a.server_id == 5 and a.filled == 40 and a.vwap_ticks == 1002
    r = parse_ack(429, b'{"code":"RATE_LIMIT_EXCEEDED","wait":0.25}', 100)
    assert isinstance(r, RateLimited) and r.wait_s == 0.25
    j = parse_ack(400, b'{"code":"BAD","message":"x"}', 100)
    assert isinstance(j, Reject) and j.code == "BAD"


def test_parse_securities_and_sheet():
    from algo1.sim.mock_server import MockConfig, MockMarket
    m = MockMarket(MockConfig(delay_ms=(5, 20), fee=(0.01, 0.02)))
    m.pos = [300, -100]
    body = json.dumps(m.securities()).encode()
    bbo, pos, pv = parse_securities(body, 100)
    assert pos == 200 and pv == [300, -100]
    assert bbo.ask[M] > bbo.bid[M] > 0 and bbo.bid_size[M] > 0
    sh = parse_sheet(body)
    assert sh.execution_delay_ms == (5, 20) and sh.fee == (0.01, 0.02) and sh.scale == 100


def test_parse_book_aggregates_and_flags_own():
    body = json.dumps({
        "bid": [{"order_id": 1, "trader_id": "x", "price": 9.99, "quantity": 100, "quantity_filled": 0},
                {"order_id": 2, "trader_id": "me", "price": 9.99, "quantity": 50, "quantity_filled": 10},
                {"order_id": 3, "trader_id": "x", "price": 9.98, "quantity": 200, "quantity_filled": 0}],
        "ask": [{"order_id": 4, "trader_id": "x", "price": 10.01, "quantity": 70, "quantity_filled": 0}],
    }).encode()
    lad = parse_book(body, M, 100, own_server_ids={2}, trader_id="me")
    assert lad.bids == [(999, 140, 40), (998, 200, 0)] and lad.asks == [(1001, 70, 0)]
