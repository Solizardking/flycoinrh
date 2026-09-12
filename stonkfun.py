"""
Stonkfun public HTTP: market data and launches, no API key.

Base: https://www.stonkfun.xyz/api/public/v1

A launch is two calls. /launches/prepare returns an unsigned payment
transaction; the fly's Solana wallet signs it locally; /launches/submit is
handed signedQuote plus the signed tx. The private key is never placed in a
request body, query, or header.

Submit stays disarmed unless FLY_SOL_LIVE=1. `processing` means the launch is
on chain — do not pay again. `service_unavailable` with charged: false is a
retry from a fresh /prepare.

  py stonkfun.py tokens
  py stonkfun.py pairs
"""
import base64
import json
import sys

import requests

from envcfg import load_env
from solwallet import LiveDisarmed, live_armed, parse_secret, sign_transaction

BASE = "https://www.stonkfun.xyz/api/public/v1"


class StonkfunError(Exception):
    def __init__(self, code, message, charged=None, body=None):
        self.code = code
        self.message = message
        self.charged = charged
        self.body = body if body is not None else {}
        super().__init__(f"{code}: {message}")


def _charged_of(body):
    if not isinstance(body, dict):
        return None
    if "charged" in body:
        return body["charged"]
    data = body.get("data")
    if isinstance(data, dict) and "charged" in data:
        return data["charged"]
    return None


def unwrap(response):
    """
    Success bodies are `{ data: ... }`. Error bodies are
    `{ error: { code, message } }`. Either shape is accepted from a
    FakeResponse or a requests.Response.
    """
    body = response.json() if hasattr(response, "json") else response
    if not isinstance(body, dict):
        raise StonkfunError("invalid_response", "response is not an object", body=body)
    ok = getattr(response, "ok", None)
    if ok is None:
        ok = getattr(response, "status_code", 200) < 400
    err = body.get("error")
    if err or not ok:
        if isinstance(err, dict):
            code = err.get("code") or "http_error"
            message = err.get("message") or ""
        else:
            code = "http_error" if not err else str(err)
            message = "" if isinstance(err, dict) else (str(err) if err else body.get("message") or "")
        raise StonkfunError(code, message, charged=_charged_of(body), body=body)
    return body.get("data", body)


def api_base(env=None):
    env = env if env is not None else load_env()
    return env.get("FLY_STK_API") or BASE


def call(path, method="GET", body=None, session=None, base=None, env=None):
    sess = session or requests
    if base is None:
        base = ((env or {}).get("FLY_STK_API") or BASE) if env is not None else BASE
    url = path if path.startswith("http") else base.rstrip("/") + path
    if method == "GET":
        r = sess.get(url, timeout=25)
    else:
        r = sess.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=25)
    return unwrap(r)


def tokens(sort="newest", session=None, env=None, **params):
    q = "&".join(f"{k}={v}" for k, v in {"sort": sort, **params}.items() if v is not None)
    return call(f"/tokens?{q}" if q else "/tokens", session=session, env=env)


def pairs(launchable=True, session=None, env=None, **params):
    q = dict(params)
    if launchable:
        q["launchable"] = "true"
    qs = "&".join(f"{k}={v}" for k, v in q.items() if v is not None)
    return call(f"/pairs?{qs}" if qs else "/pairs", session=session, env=env)


def prepare(payload, session=None, env=None):
    return call("/launches/prepare", method="POST", body=payload, session=session, env=env)


def sign_payment(payment_transaction, secret):
    """
    Sign the unsigned payment tx from /prepare. `secret` is the env value
    (base58 / hex / JSON array). Returns base64 of the signed transaction.
    The secret is not returned and must not be put on the wire.
    """
    kp = parse_secret(secret) if not hasattr(secret, "sign") else secret
    raw = (payment_transaction if isinstance(payment_transaction, (bytes, bytearray))
           else base64.b64decode(payment_transaction))
    signed = sign_transaction(raw, kp)
    return base64.b64encode(signed).decode("ascii")


def submit_payload(signed_quote, signed_transaction, logo=None):
    """The exact JSON body /launches/submit expects. No secret fields."""
    body = {"signedQuote": signed_quote, "signedTransaction": signed_transaction}
    if logo is not None:
        body["logo"] = logo
    return body


def submit(signed_quote, signed_transaction, logo=None, session=None, env=None, live=None):
    env = env if env is not None else load_env()
    if live is None:
        live = live_armed(env)
    if not live:
        raise LiveDisarmed("FLY_SOL_LIVE=0 - Stonkfun submit refused")
    body = submit_payload(signed_quote, signed_transaction, logo=logo)
    secret = env.get("FLY_SOL_SECRET") or ""
    dumped = json.dumps(body)
    if secret and secret in dumped:
        raise RuntimeError("refusing to send: payload contains FLY_SOL_SECRET")
    return call("/launches/submit", method="POST", body=body, session=session, env=env)


def launch_status(payment_signature, session=None):
    return call(f"/launches/{payment_signature}", session=session)


def interpret_submit(data=None, error=None):
    """
    The two outcomes of submit, as the docs describe them.

    processing  → on chain; never pay again.
    service_unavailable + charged: false → retry from a fresh /prepare.
    """
    if error is not None:
        if error.code == "service_unavailable" and error.charged is False:
            return {"outcome": "retry_prepare", "retry_payment": False,
                    "charged": False, "status": error.code}
        return {"outcome": "error", "retry_payment": False,
                "charged": error.charged, "status": error.code,
                "message": error.message}
    status = (data or {}).get("status")
    if status == "processing":
        return {"outcome": "on_chain", "retry_payment": False,
                "charged": True, "status": "processing",
                "paymentSignature": (data or {}).get("paymentSignature"),
                "mint": (data or {}).get("mint")}
    if status == "completed":
        return {"outcome": "on_chain", "retry_payment": False,
                "charged": True, "status": "completed",
                "paymentSignature": (data or {}).get("paymentSignature"),
                "mint": (data or {}).get("mint")}
    return {"outcome": "ok", "retry_payment": False, "status": status, "data": data}


def launch(name, symbol, quote_mint, mode="standard", logo=None,
           env=None, session=None, extra=None):
    """
    Prepare → sign locally → submit. Submit is still gated by FLY_SOL_LIVE.
    """
    env = env if env is not None else load_env()
    sec = env.get("FLY_SOL_SECRET")
    if not sec:
        raise RuntimeError("no Solana wallet yet - run: py solwallet.py new")
    kp = parse_secret(sec)
    payload = {"creatorWallet": kp.address, "quoteMint": quote_mint,
               "name": name, "symbol": symbol, "mode": mode}
    if extra:
        payload.update(extra)
    if logo is not None:
        payload["logo"] = logo
    prepared = prepare(payload, session=session)
    signed = sign_payment(prepared["paymentTransaction"], kp)
    try:
        result = submit(prepared["signedQuote"], signed, logo=logo,
                        session=session, env=env)
        return interpret_submit(data=result)
    except StonkfunError as e:
        return interpret_submit(error=e)


def main(argv=None):
    argv = argv if argv is not None else sys.argv
    cmd = argv[1] if len(argv) > 1 else "tokens"
    if cmd == "tokens":
        print(json.dumps(tokens(env=load_env()), indent=2)[:8000])
        return
    if cmd == "pairs":
        print(json.dumps(pairs(launchable=True, env=load_env()), indent=2)[:8000])
        return
    print(__doc__)


if __name__ == "__main__":
    main()
