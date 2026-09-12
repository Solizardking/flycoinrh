"""
Phoenix Eternal perps over REST. Reads are open; live orders stay disarmed.

  GET https://perp-api.phoenix.trade/v1/view/exchange/markets
  GET https://perp-api.phoenix.trade/v1/view/orderbook/{symbol}
  GET https://perp-api.phoenix.trade/v1/market/{symbol}/mark-price

Paper/dry sizing uses those reads and never POSTs. A live order helper exists
so the gate can be tested; it refuses unless FLY_SOL_LIVE=1, and even then
does not broadcast — the live path is not built here.

  py phoenix.py markets
  py phoenix.py mark SOL
  py phoenix.py book SOL
"""
import json
import sys

import requests

from envcfg import load_env
from solwallet import LiveDisarmed, live_armed

BASE = "https://perp-api.phoenix.trade"


class PhoenixError(Exception):
    def __init__(self, message, body=None):
        self.message = message
        self.body = body if body is not None else {}
        super().__init__(message)


def unwrap(response):
    body = response.json() if hasattr(response, "json") else response
    ok = getattr(response, "ok", None)
    if ok is None:
        ok = getattr(response, "status_code", 200) < 400
    if not ok:
        if isinstance(body, dict):
            raise PhoenixError(str(body.get("error") or body), body=body)
        raise PhoenixError(str(body), body=body)
    return body


def api_base(env=None):
    env = env if env is not None else load_env()
    return env.get("FLY_PX_API") or BASE


def call(path, method="GET", body=None, session=None, base=None, env=None):
    sess = session or requests
    if base is None:
        base = ((env or {}).get("FLY_PX_API") or BASE) if env is not None else BASE
    url = path if path.startswith("http") else base.rstrip("/") + path
    if method == "GET":
        r = sess.get(url, timeout=25)
    else:
        r = sess.post(url, json=body, timeout=25)
    return unwrap(r)


def markets(session=None, env=None):
    data = call("/v1/view/exchange/markets", session=session, env=env)
    if isinstance(data, dict):
        data = data.get("data") or data.get("markets") or data
    if not isinstance(data, list):
        raise PhoenixError("markets response is not a list", body=data)
    return data


def orderbook(symbol, session=None, env=None):
    data = call(f"/v1/view/orderbook/{symbol}", session=session, env=env)
    if isinstance(data, dict) and "bids" not in data and isinstance(data.get("data"), dict):
        data = data["data"]
    return data


def mark_price(symbol, session=None, env=None):
    data = call(f"/v1/market/{symbol}/mark-price", session=session, env=env)
    if isinstance(data, dict) and "markPrice" not in data and isinstance(data.get("data"), dict):
        data = data["data"]
    return data


def _price_from_mark(mark):
    mp = (mark or {}).get("markPrice")
    if isinstance(mp, dict):
        return mp.get("price")
    if isinstance(mp, (int, float)):
        return mp
    return None


def _price_from_book(book):
    mid = (book or {}).get("mid")
    if mid:
        return mid
    bids = (book or {}).get("bids") or []
    asks = (book or {}).get("asks") or []
    if bids and asks:
        return (float(bids[0][0]) + float(asks[0][0])) / 2.0
    if bids:
        return float(bids[0][0])
    if asks:
        return float(asks[0][0])
    return None


def paper_size(symbol, notional_usdc, side="buy", session=None):
    """
    Size a paper order from mark (falling back to the book mid). GET only —
    this function never POSTs and never sends a transaction.
    """
    mark = mark_price(symbol, session=session)
    price = _price_from_mark(mark)
    book = None
    if not price:
        book = orderbook(symbol, session=session)
        price = _price_from_book(book)
    if not price:
        raise PhoenixError(f"no mark or book price for {symbol}")
    price = float(price)
    notional = float(notional_usdc)
    tokens = notional / price
    return {
        "symbol": symbol,
        "side": side,
        "price": price,
        "notional_usdc": notional,
        "tokens": tokens,
        "broadcast": False,
        "mark": mark,
        "orderbook": book,
    }


def place_order(symbol, side, size, session=None, env=None, live=None):
    """
    Live order helper. Refused when FLY_SOL_LIVE is off. Even when armed, this
    build does not POST — the live path is a stop, not a send.
    """
    env = env if env is not None else load_env()
    if live is None:
        live = live_armed(env)
    if not live:
        raise LiveDisarmed("FLY_SOL_LIVE=0 - Phoenix order refused")
    raise LiveDisarmed("FLY_SOL_LIVE=1 but the Phoenix live path is not built; nothing was sent")


def main(argv=None):
    argv = argv if argv is not None else sys.argv
    cmd = argv[1] if len(argv) > 1 else "markets"
    if cmd == "markets":
        ms = markets(env=load_env())
        print(json.dumps([{"symbol": m.get("symbol")} for m in ms[:25]], indent=2))
        return
    if cmd == "mark":
        print(json.dumps(mark_price(argv[2] if len(argv) > 2 else "SOL", env=load_env()), indent=2))
        return
    if cmd == "book":
        print(json.dumps(orderbook(argv[2] if len(argv) > 2 else "SOL", env=load_env()), indent=2)[:8000])
        return
    print(__doc__)


if __name__ == "__main__":
    main()
