"""
Phoenix Eternal REST client against a fake HTTP transport. No network, no order.

  py -m unittest test_phoenix -v
"""
import unittest

import phoenix
from solwallet import LiveDisarmed


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls = []
        self.routes = {}

    def on(self, method, path, status, body):
        self.routes[(method, path)] = (status, body)

    def _handle(self, method, url, body=None):
        path = url.split("?")[0]
        if path.startswith(phoenix.BASE):
            path = path[len(phoenix.BASE):] or "/"
        self.calls.append({"method": method, "url": url, "path": path, "json": body})
        handler = self.routes.get((method, path))
        if handler is None:
            raise AssertionError(f"unexpected {method} {url}")
        status, payload = handler
        return FakeResponse(status, payload)

    def get(self, url, **kw):
        return self._handle("GET", url)

    def post(self, url, json=None, **kw):
        return self._handle("POST", url, json)


MARKETS = [
    {"symbol": "SOL", "marketStatus": "active", "tickSize": 1},
    {"symbol": "BTC", "marketStatus": "active", "tickSize": 1},
]
BOOK = {"slot": 99, "symbol": "SOL",
        "bids": [[150.4, 10.0], [150.3, 4.0]],
        "asks": [[150.6, 8.0], [150.7, 2.0]],
        "mid": 150.5}
MARK = {"slot": 99, "slotIndex": 0, "symbol": "SOL",
        "markPrice": {"price": 150.5, "slot": 99}}


class Reads(unittest.TestCase):
    def test_markets_orderbook_and_mark(self):
        s = FakeSession()
        s.on("GET", "/v1/view/exchange/markets", 200, MARKETS)
        s.on("GET", "/v1/view/orderbook/SOL", 200, BOOK)
        s.on("GET", "/v1/market/SOL/mark-price", 200, MARK)
        ms = phoenix.markets(session=s)
        self.assertTrue(ms)
        self.assertEqual(ms[0]["symbol"], "SOL")
        book = phoenix.orderbook("SOL", session=s)
        self.assertEqual(book["bids"][0][0], 150.4)
        self.assertEqual(book["asks"][0][0], 150.6)
        mark = phoenix.mark_price("SOL", session=s)
        self.assertEqual(mark["markPrice"]["price"], 150.5)
        self.assertTrue(all(c["method"] == "GET" for c in s.calls))


class PaperAndLive(unittest.TestCase):
    def test_paper_size_uses_mark_and_does_not_post(self):
        s = FakeSession()
        s.on("GET", "/v1/market/SOL/mark-price", 200, MARK)
        got = phoenix.paper_size("SOL", 1505, side="buy", session=s)
        self.assertEqual(got["price"], 150.5)
        self.assertEqual(got["tokens"], 10.0)
        self.assertFalse(got["broadcast"])
        self.assertTrue(all(c["method"] == "GET" for c in s.calls))
        self.assertFalse(any(c["method"] == "POST" for c in s.calls))

    def test_paper_size_falls_back_to_the_book(self):
        s = FakeSession()
        s.on("GET", "/v1/market/SOL/mark-price", 200,
             {"slot": 1, "slotIndex": 0, "symbol": "SOL", "markPrice": None})
        s.on("GET", "/v1/view/orderbook/SOL", 200, BOOK)
        got = phoenix.paper_size("SOL", 301, session=s)
        self.assertEqual(got["price"], 150.5)
        self.assertAlmostEqual(got["tokens"], 2.0)
        self.assertFalse(got["broadcast"])

    def test_live_order_is_refused_when_the_flag_is_off(self):
        s = FakeSession()
        with self.assertRaises(LiveDisarmed) as cm:
            phoenix.place_order("SOL", "buy", 1, session=s,
                                env={"FLY_SOL_LIVE": "0"})
        self.assertIn("FLY_SOL_LIVE=0", str(cm.exception))
        self.assertEqual(s.calls, [])

    def test_live_order_still_does_not_post_when_armed(self):
        s = FakeSession()
        with self.assertRaises(LiveDisarmed):
            phoenix.place_order("SOL", "buy", 1, session=s,
                                env={"FLY_SOL_LIVE": "1"}, live=True)
        self.assertEqual(s.calls, [])


if __name__ == "__main__":
    unittest.main()
