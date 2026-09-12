"""
The fly's Solana wallet: create, show, sign, never print the secret.

  py -m unittest test_solwallet -v
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nacl.signing import VerifyKey

import solwallet


def fake_rpc(lamports):
    class Sess:
        def __init__(self):
            self.posts = []

        def post(self, url, json=None, **kw):
            self.posts.append({"url": url, "json": json})
            method = json["method"]

            class R:
                def json(_self):
                    if method == "getBalance":
                        return {"jsonrpc": "2.0", "id": 1,
                                "result": {"value": lamports}}
                    if method == "sendTransaction":
                        return {"jsonrpc": "2.0", "id": 1, "result": "sig"}
                    return {"jsonrpc": "2.0", "id": 1, "result": None}
            return R()
    return Sess()


class Generate(unittest.TestCase):
    def test_address_is_base58_of_the_pubkey_and_repr_hides_the_secret(self):
        kp = solwallet.generate()
        self.assertEqual(solwallet.b58decode(kp.address), kp.pubkey)
        self.assertEqual(len(kp.pubkey), 32)
        self.assertEqual(len(kp.secret64), 64)
        text = repr(kp)
        self.assertIn(kp.address, text)
        self.assertNotIn(solwallet.b58encode(kp.secret64), text)
        self.assertNotIn(kp.seed.hex(), text)


class NewAndShow(unittest.TestCase):
    def test_new_writes_the_secret_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            with mock.patch.object(solwallet, "ENV", env_path):
                with mock.patch.object(solwallet, "load_env", return_value={}):
                    buf = io.StringIO()
                    with mock.patch("sys.stdout", buf):
                        solwallet.new()
                text = env_path.read_text()
                self.assertIn("FLY_SOL_SECRET=", text)
                self.assertIn("FLY_SOL_RPC=", text)
                self.assertIn("FLY_SOL_LIVE=0", text)
                secret = [ln.split("=", 1)[1] for ln in text.splitlines()
                          if ln.startswith("FLY_SOL_SECRET=")][0]
                self.assertTrue(secret)
                printed = buf.getvalue()
                self.assertNotIn(secret, printed)
                kp = solwallet.parse_secret(secret)
                self.assertIn(kp.address, printed)
                self.assertIn("not shown", printed.lower())

                with mock.patch.object(solwallet, "load_env",
                                       return_value={"FLY_SOL_SECRET": secret}):
                    before = env_path.read_text()
                    buf2 = io.StringIO()
                    with mock.patch("sys.stdout", buf2):
                        solwallet.new()
                    self.assertIn("refusing to overwrite", buf2.getvalue())
                    self.assertEqual(env_path.read_text(), before)

    def test_show_prints_the_address_and_not_the_secret(self):
        kp = solwallet.generate()
        secret = solwallet.b58encode(kp.secret64)
        env = {"FLY_SOL_SECRET": secret, "FLY_SOL_RPC": "https://example-rpc.invalid",
               "FLY_SOL_LIVE": "0"}
        sess = fake_rpc(2_500_000_000)
        buf = io.StringIO()
        with mock.patch.object(solwallet, "load_env", return_value=env):
            with mock.patch("sys.stdout", buf):
                lamports = solwallet.balance(env, session=sess)
        out = buf.getvalue()
        self.assertEqual(lamports, 2_500_000_000)
        self.assertIn(kp.address, out)
        self.assertNotIn(secret, out)
        self.assertIn("2.500000000 SOL", out)
        self.assertIn("no (FLY_SOL_LIVE=0)", out)
        self.assertEqual(sess.posts[0]["url"], "https://example-rpc.invalid")
        self.assertEqual(sess.posts[0]["json"]["method"], "getBalance")
        self.assertEqual(sess.posts[0]["json"]["params"], [kp.address])

    def test_main_show_does_not_print_the_secret(self):
        kp = solwallet.generate()
        secret = solwallet.b58encode(kp.secret64)
        env = {"FLY_SOL_SECRET": secret, "FLY_SOL_LIVE": "0"}
        buf = io.StringIO()
        with mock.patch.object(solwallet, "load_env", return_value=env):
            with mock.patch.object(solwallet, "rpc_call",
                                   return_value={"result": {"value": 0}}):
                with mock.patch("sys.stdout", buf):
                    solwallet.main(["solwallet.py", "show"])
        self.assertIn(kp.address, buf.getvalue())
        self.assertNotIn(secret, buf.getvalue())


class SignAndSend(unittest.TestCase):
    def test_sign_transaction_is_ed25519_over_the_message(self):
        kp = solwallet.generate()
        tx = solwallet.dummy_legacy_tx(kp.pubkey)
        signed = solwallet.sign_transaction(tx, kp)
        sig_start, nsigs, msg = solwallet.split_tx(signed)
        self.assertEqual(nsigs, 1)
        sig = signed[sig_start:sig_start + 64]
        VerifyKey(kp.pubkey).verify(msg, sig)
        self.assertNotEqual(sig, bytes(64))

    def test_send_raw_is_refused_when_live_is_off(self):
        kp = solwallet.generate()
        tx = solwallet.sign_transaction(solwallet.dummy_legacy_tx(kp.pubkey), kp)
        sess = fake_rpc(0)
        env = {"FLY_SOL_SECRET": solwallet.b58encode(kp.secret64), "FLY_SOL_LIVE": "0",
               "FLY_SOL_RPC": "https://example-rpc.invalid"}
        with self.assertRaises(solwallet.LiveDisarmed) as cm:
            solwallet.send_raw(tx, env=env, session=sess)
        self.assertIn("FLY_SOL_LIVE=0", str(cm.exception))
        self.assertEqual(sess.posts, [])

    def test_send_raw_posts_to_the_configured_solana_rpc_when_armed(self):
        kp = solwallet.generate()
        tx = solwallet.sign_transaction(solwallet.dummy_legacy_tx(kp.pubkey), kp)
        sess = fake_rpc(0)
        env = {"FLY_SOL_LIVE": "1", "FLY_SOL_RPC": "https://example-rpc.invalid"}
        solwallet.send_raw(tx, env=env, session=sess, live=True)
        self.assertEqual(sess.posts[0]["url"], "https://example-rpc.invalid")
        self.assertEqual(sess.posts[0]["json"]["method"], "sendTransaction")
        params = sess.posts[0]["json"]["params"]
        self.assertEqual(params[1]["encoding"], "base64")
        dumped = json.dumps(sess.posts)
        self.assertNotIn(solwallet.b58encode(kp.secret64), dumped)


if __name__ == "__main__":
    unittest.main()
