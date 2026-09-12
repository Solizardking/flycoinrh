"""
The public site is a window. These tests read the shipped files; they do
not hit the network and they do not import a wallet.

  py -m unittest test_site_window -v
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
SITE = ROOT / "site"


def read(*parts):
    return (SITE.joinpath(*parts)).read_text(encoding="utf-8")


class WindowNotPanel(unittest.TestCase):
    def test_site_files_have_no_secret_or_submit_path(self):
        blob = "\n".join([
            read("web", "index.html"),
            read("web", "tape.html"),
            read("api", "state.js"),
            read("server", "main.py"),
        ])
        for needle in (
            "process.env.FLY_SOL_SECRET",
            "os.environ.get(\"FLY_SOL_SECRET\")",
            "sendTransaction",
            "/launches/submit",
            "/launches/prepare",
            "place_order",
            "sign_transaction",
        ):
            self.assertNotIn(needle, blob, needle + " must not appear in site/")

    def test_health_proxy_drops_rpc_urls(self):
        js = read("api", "state.js")
        py = read("server", "main.py")
        self.assertIn("pickHealth", js)
        self.assertIn("rpc URLs", js)
        self.assertNotIn("rpcHttp", js)
        self.assertNotIn("rpcWs", js)
        self.assertNotIn("rpcHttp", py)
        self.assertNotIn("rpcWs", py)

    def test_tape_and_listings_are_on_the_page(self):
        html = read("web", "index.html")
        self.assertIn("id=\"solana\"", html)
        self.assertIn("tape.html?embed=1", html)
        self.assertIn("https://clawd-ws.fly.dev/", html)
        self.assertIn("https://clawd-ws.fly.dev/health", html)
        self.assertIn("stonkfun.xyz/api/public/v1/tokens?sort=newest", html)
        self.assertIn("stonkfun.xyz/api/public/v1/pairs?launchable=true", html)
        self.assertIn("perp-api.phoenix.trade/v1/market/SOL/mark-price", html)
        tape = read("web", "tape.html")
        self.assertIn("wss://clawd-ws.fly.dev/ws", tape)
        self.assertIn("orbmarkets.io/token/", tape)


if __name__ == "__main__":
    unittest.main()
