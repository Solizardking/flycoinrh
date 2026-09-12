"""
Live state for the flybrain site.

Everything here is read straight off Robinhood Chain over JSON-RPC. Blockscout
sits behind Cloudflare and answers a challenge page to servers, so there is no
indexer in the path - only eth_call, eth_getBalance and eth_getLogs, which any
reader can repeat against the same public node.

Nothing is written, no key is loaded, and there is no code path here that can
sign anything. The site is a window, not a control panel.

Optional Solana fields (clawd-ws /health, a Phoenix SOL mark, Stonkfun
listings) are the same kind of read: public GETs, no secret, no submit.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

CLAWD_HEALTH = os.environ.get("FLY_CLAWD_HEALTH",
                              "https://clawd-ws.fly.dev/health")
PHOENIX_MARK = os.environ.get(
    "FLY_PX_MARK", "https://perp-api.phoenix.trade/v1/market/SOL/mark-price")
STK_TOKENS = "https://www.stonkfun.xyz/api/public/v1/tokens?sort=newest"
STK_PAIRS = "https://www.stonkfun.xyz/api/public/v1/pairs?launchable=true"

RPC = os.environ.get("FLY_RH_RPC", "https://rpc.mainnet.chain.robinhood.com")
CHAIN_ID = 4663
WALLET = os.environ.get("FLY_WALLET",
                        "0x6ce4085EfB52a6eBDb7d6989beb8860847f4b42A")
TOKEN = os.environ.get("FLY_TOKEN",
                       "0x4eb990547bce4a982432ca88cf5fae7eed1a2d35")
# the wallet that made the first eight launches, still the fee recipient there
WALLET_V1 = os.environ.get("FLY_WALLET_V1",
                           "0x739Ccc9dd8Ed6412F00782927dbd087c4e72bFc3")
# the block the token was launched in - scanning logs from 0 gets the public
# node to answer 429, and there is nothing to find before this anyway
BIRTH_BLOCK = int(os.environ.get("FLY_TOKEN_BLOCK", "59614342"))

TRANSFER = ("0xddf252ad1be2c89b69c2b068fc378daa"
            "952ba7f163c4a11628f55a4df523b3ef")

app = FastAPI(title="flybrain")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"],
    allow_headers=["*"])

_cache = {"at": 0.0, "data": None}
TTL = 20.0
# the log scan is the expensive call; hold it far longer than the rest
_hold = {"at": 0.0, "val": (None, None)}
HOLD_TTL = 300.0


def _holders_cached(token):
    now = time.time()
    if now - _hold["at"] < HOLD_TTL:
        return _hold["val"]
    v = holders(token)
    if v[0] is not None:              # keep the last good answer on a 429
        _hold.update(at=now, val=v)
        return v
    return _hold["val"]


def rpc(method, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1,
                                 "method": method, "params": params},
                      timeout=15).json()
    if "error" in r:
        raise RuntimeError(str(r["error"])[:200])
    return r.get("result")


def as_int(v, default=0):
    if v is None:
        return default
    return int(v, 16) if str(v).startswith("0x") else int(v)


def call_str(to, selector):
    """Decode an ABI-encoded string return, e.g. name() or symbol()."""
    x = rpc("eth_call", [{"to": to, "data": selector}, "latest"])
    if not x or len(x) < 130:
        return ""
    n = int(x[66:130], 16)
    try:
        return bytes.fromhex(x[130:130 + n * 2]).decode("utf-8", "replace")
    except Exception:
        return ""


def holders(token):
    """
    Unique addresses that have ever received the token.

    Counted from Transfer logs rather than trusted to an indexer. It is an
    upper bound on holders - an address that sold everything still shows - so
    the site labels it as addresses touched, not holders.
    """
    try:
        logs = rpc("eth_getLogs", [{"address": token,
                                    "fromBlock": hex(BIRTH_BLOCK),
                                    "toBlock": "latest",
                                    "topics": [TRANSFER]}])
        seen = set()
        for lg in logs or []:
            t = lg.get("topics") or []
            if len(t) >= 3:
                seen.add("0x" + t[2][-40:])
        seen.discard("0x" + "0" * 40)
        return len(seen), len(logs or [])
    except Exception:
        return None, None


def state():
    now = time.time()
    if _cache["data"] and now - _cache["at"] < TTL:
        return _cache["data"]

    out = {"chain": {"name": "Robinhood Chain", "id": CHAIN_ID, "rpc": RPC},
           "ok": True, "error": None}
    try:
        out["chain"]["block"] = as_int(rpc("eth_blockNumber", []))
        out["chain"]["gas_gwei"] = round(as_int(rpc("eth_gasPrice", [])) / 1e9, 4)

        bal = as_int(rpc("eth_getBalance", [WALLET, "latest"]))
        old = as_int(rpc("eth_getBalance", [WALLET_V1, "latest"]))
        out["wallet"] = {
            "address": WALLET, "eth": bal / 1e18,
            "launches_left": int(bal / 1e18 / 0.00055),
            "previous": {"address": WALLET_V1, "eth": old / 1e18},
        }

        sup = as_int(rpc("eth_call", [{"to": TOKEN, "data": "0x18160ddd"},
                                      "latest"]))
        h, transfers = _holders_cached(TOKEN)
        out["token"] = {
            "address": TOKEN,
            "name": call_str(TOKEN, "0x06fdde03"),
            "symbol": call_str(TOKEN, "0x95d89b41"),
            "supply": sup / 1e18,
            "addresses_touched": h,
            "transfers": transfers,
            "pair": "GOOGL",
            "creator_tax_pct": 1,
            "url": f"https://www.ponsfamily.com/launchpad/{TOKEN}",
        }
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)[:200]

    out["solana"] = solana_window()
    out["updated"] = int(now)
    _cache.update(at=now, data=out)
    return out


def _cut(s, n):
    x = "" if s is None else str(s)
    return x[:n]


def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n else None  # NaN


def _get(url):
    r = requests.get(url, timeout=6)
    r.raise_for_status()
    return r.json()


def _sol_health():
    h = _get(CLAWD_HEALTH)
    return {
        "ok": True,
        "status": _cut(h.get("status"), 24) or None,
        "clients": _num(h.get("clients")),
        "totalLaunches": _num(h.get("totalLaunches")),
        "solana": h.get("solana") if isinstance(h.get("solana"), bool) else None,
    }


def _sol_mark():
    m = _get(PHOENIX_MARK)
    if m.get("error"):
        return {"ok": False, "symbol": "SOL", "error": _cut(m.get("error"), 80)}
    mp = m.get("markPrice")
    price = _num(mp.get("price") if isinstance(mp, dict) else mp)
    return {"ok": price is not None, "symbol": _cut(m.get("symbol") or "SOL", 12),
            "mark": price, "slot": _num(m.get("slot"))}


def _sol_tokens():
    body = _get(STK_TOKENS)
    data = body.get("data") or body
    rows = data.get("tokens") or []
    return [{
        "name": _cut(t.get("name"), 40),
        "symbol": _cut(t.get("symbol"), 16),
        "mint": _cut(t.get("mint"), 64),
        "quote": _cut(((t.get("quote") or {}).get("symbol")), 12),
        "marketCapUsd": _num((t.get("market") or {}).get("marketCapUsd")),
        "status": _cut(t.get("status"), 16),
        "createdAt": _cut(t.get("createdAt"), 40),
    } for t in rows[:8] if isinstance(t, dict)]


def _sol_pairs():
    body = _get(STK_PAIRS)
    data = body.get("data") or body
    rows = data.get("pairs") or []
    return [{
        "symbol": _cut(p.get("symbol"), 16),
        "name": _cut(p.get("name"), 28),
        "mint": _cut(p.get("mint"), 64),
        "category": _cut(p.get("categoryLabel") or p.get("category"), 20),
        "launchable": True,
    } for p in rows if isinstance(p, dict) and p.get("launchable")][:24]


def solana_window():
    """
    Public Solana reads for the site. Failures stay on their own field so a
    down tape does not take the Robinhood numbers with it. RPC URLs from
    clawd-ws /health are dropped — they are not a window the page should show.
    """
    out = {"ok": True, "tape": "https://clawd-ws.fly.dev/"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        fh = pool.submit(_sol_health)
        fm = pool.submit(_sol_mark)
        ft = pool.submit(_sol_tokens)
        fp = pool.submit(_sol_pairs)
        try:
            out["clawdws"] = fh.result()
        except Exception as exc:
            out["clawdws"] = {"ok": False, "error": str(exc)[:120]}
        try:
            out["phoenix"] = fm.result()
        except Exception as exc:
            out["phoenix"] = {"ok": False, "symbol": "SOL", "error": str(exc)[:120]}
        try:
            out["tokens"] = ft.result()
        except Exception as exc:
            out["tokens"] = []
            out["tokens_error"] = str(exc)[:120]
        try:
            out["pairs"] = fp.result()
        except Exception as exc:
            out["pairs"] = []
            out["pairs_error"] = str(exc)[:120]
    return out


@app.get("/api/state")
def api_state():
    return state()


@app.get("/api/health")
def health():
    return {"ok": True, "t": int(time.time())}


@app.get("/")
def root():
    return {"service": "flybrain", "see": "/api/state"}
