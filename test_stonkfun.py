"""
Stonkfun public-v1 client against a fake HTTP transport. No network, no spend.

  py -m unittest test_stonkfun -v
"""
import base64
import json
import unittest

import solwallet
import stonkfun
from solwallet import LiveDisarmed
from stonkfun import StonkfunError


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
        self.routes = {}          # (METHOD, path) -> (status, body) or callable
        self.default = None

    def on(self, method, path, status, body):
        self.routes[(method, path)] = (status, body)

    def _handle(self, method, url, body=None):
        path = url.split("?")[0]
        if path.startswith(stonkfun.BASE):
            path = path[len(stonkfun.BASE):] or "/"
        rec = {"method": method, "url": url, "path": path, "json": body}
        self.calls.append(rec)
        key = (method, path)
        handler = self.routes.get(key, self.default)
        if handler is None:
            raise AssertionError(f"unexpected {method} {url}")
        if callable(handler):
            handler = handler(url, body)
        status, payload = handler
        return FakeResponse(status, payload)

    def get(self, url, **kw):
        return self._handle("GET", url)

    def post(self, url, json=None, **kw):
        return self._handle("POST", url, json)


TOKENS_DATA = {"tokens": [{"mint": "Mint111", "symbol": "AAA", "name": "Alpha"}],
               "page": 1}
PAIRS_DATA = {"pairs": [{"mint": "Quote111", "symbol": "NVDAx", "launchable": True}]}


class Reads(unittest.TestCase):
    def test_tokens_and_pairs_unwrap_data(self):
        s = FakeSession()
        s.on("GET", "/tokens", 200, {"data": TOKENS_DATA})
        s.on("GET", "/pairs", 200, {"data": PAIRS_DATA})
        tokens = stonkfun.tokens(session=s)
        pairs = stonkfun.pairs(launchable=True, session=s)
        self.assertEqual(tokens["tokens"][0]["symbol"], "AAA")
        self.assertEqual(pairs["pairs"][0]["symbol"], "NVDAx")
        self.assertIn("sort=newest", s.calls[0]["url"])
        self.assertIn("launchable=true", s.calls[1]["url"])

    def test_error_body_raises_on_code(self):
        s = FakeSession()
        s.on("GET", "/tokens", 400,
             {"error": {"code": "invalid_request", "message": "bad sort"}})
        with self.assertRaises(StonkfunError) as cm:
            stonkfun.tokens(session=s)
        self.assertEqual(cm.exception.code, "invalid_request")
        self.assertEqual(cm.exception.message, "bad sort")


class PrepareSignSubmit(unittest.TestCase):
    def setUp(self):
        self.kp = solwallet.generate()
        self.secret = solwallet.b58encode(self.kp.secret64)
        self.env = {"FLY_SOL_SECRET": self.secret, "FLY_SOL_LIVE": "0"}
        unsigned = solwallet.dummy_legacy_tx(self.kp.pubkey)
        self.payment_b64 = base64.b64encode(unsigned).decode("ascii")
        self.quote = "quote-bytes-not-a-secret"

    def test_sign_payment_and_submit_payload_do_not_carry_the_secret(self):
        signed = stonkfun.sign_payment(self.payment_b64, self.kp)
        body = stonkfun.submit_payload(self.quote, signed, logo="data:image/png;base64,xx")
        dumped = json.dumps(body)
        self.assertIn("signedQuote", body)
        self.assertIn("signedTransaction", body)
        self.assertEqual(body["signedQuote"], self.quote)
        self.assertNotIn(self.secret, dumped)
        self.assertNotIn(self.kp.seed.hex(), dumped)
        self.assertNotIn(list(self.kp.secret64).__repr__(), dumped)
        raw = base64.b64decode(body["signedTransaction"])
        sig_start, nsigs, msg = solwallet.split_tx(raw)
        self.assertEqual(nsigs, 1)
        self.assertNotEqual(raw[sig_start:sig_start + 64], bytes(64))

    def test_submit_is_refused_when_live_is_off(self):
        s = FakeSession()
        signed = stonkfun.sign_payment(self.payment_b64, self.kp)
        with self.assertRaises(LiveDisarmed) as cm:
            stonkfun.submit(self.quote, signed, session=s, env=self.env)
        self.assertIn("FLY_SOL_LIVE=0", str(cm.exception))
        self.assertEqual(s.calls, [])

    def test_submit_posts_quote_and_tx_never_the_secret(self):
        s = FakeSession()
        s.on("POST", "/launches/submit", 200,
             {"data": {"status": "completed", "mint": "NewMint",
                       "paymentSignature": "PaySig"}})
        signed = stonkfun.sign_payment(self.payment_b64, self.kp)
        env = dict(self.env, FLY_SOL_LIVE="1")
        result = stonkfun.submit(self.quote, signed, session=s, env=env)
        self.assertEqual(result["mint"], "NewMint")
        rec = s.calls[0]
        self.assertEqual(rec["method"], "POST")
        self.assertTrue(rec["url"].endswith("/launches/submit"))
        self.assertEqual(rec["json"]["signedQuote"], self.quote)
        self.assertIn("signedTransaction", rec["json"])
        dumped = json.dumps(rec["json"])
        self.assertNotIn(self.secret, dumped)

    def test_processing_is_on_chain_do_not_pay_again(self):
        got = stonkfun.interpret_submit(
            data={"status": "processing", "paymentSignature": "PaySig"})
        self.assertEqual(got["outcome"], "on_chain")
        self.assertFalse(got["retry_payment"])

    def test_service_unavailable_uncharged_retries_from_prepare(self):
        err = StonkfunError("service_unavailable", "bundle missed", charged=False)
        got = stonkfun.interpret_submit(error=err)
        self.assertEqual(got["outcome"], "retry_prepare")
        self.assertFalse(got["retry_payment"])
        self.assertIs(got["charged"], False)

    def test_launch_prepare_sign_submit_end_to_end_on_the_fake(self):
        s = FakeSession()
        s.on("POST", "/launches/prepare", 200, {
            "data": {"paymentTransaction": self.payment_b64,
                     "signedQuote": self.quote,
                     "devBuy": None},
        })
        s.on("POST", "/launches/submit", 200, {
            "data": {"status": "processing", "paymentSignature": "PaySig"},
        })
        env = dict(self.env, FLY_SOL_LIVE="1")
        got = stonkfun.launch("Fly", "FLY", "Quote111", env=env, session=s)
        self.assertEqual(got["outcome"], "on_chain")
        self.assertFalse(got["retry_payment"])
        prep = s.calls[0]["json"]
        self.assertEqual(prep["creatorWallet"], self.kp.address)
        self.assertEqual(prep["quoteMint"], "Quote111")
        dumped = json.dumps(s.calls)
        self.assertNotIn(self.secret, dumped)

    def test_launch_submit_error_uncharged_is_retry_prepare(self):
        s = FakeSession()
        s.on("POST", "/launches/prepare", 200, {
            "data": {"paymentTransaction": self.payment_b64,
                     "signedQuote": self.quote},
        })
        s.on("POST", "/launches/submit", 503, {
            "error": {"code": "service_unavailable", "message": "bundle missed"},
            "charged": False,
        })
        env = dict(self.env, FLY_SOL_LIVE="1")
        got = stonkfun.launch("Fly", "FLY", "Quote111", env=env, session=s)
        self.assertEqual(got["outcome"], "retry_prepare")
        self.assertFalse(got["retry_payment"])


if __name__ == "__main__":
    unittest.main()
