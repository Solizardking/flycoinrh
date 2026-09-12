"""
The clawd-ws relay parser, driven on fixtures copied from the page protocol.

The page at https://clawd-ws.fly.dev/ handles two JSON types off `/ws`:
`token-launch` (mint, name, symbol, ...) and `status` (connected, uptime, ...).
These tests feed those shapes into the shipped parser — they do not reimplement it.

  py -m unittest test_clawdws -v
"""
import json
import unittest

import clawdws

# Recorded from the page's own handleLaunch / handleStatus field list.
TOKEN_LAUNCH = {
    "type": "token-launch",
    "mint": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
    "name": "Fly Coin",
    "symbol": "FLY",
    "signature": "5sigExample111111111111111111111111111111111111111111111111111",
    "creator": "Creator1111111111111111111111111111111111111",
    "description": "a measured brain, not a vibe",
    "marketCapSol": 12.5,
    "time": 1_710_000_000_000,
    "imageUri": "https://example.com/fly.png",
    "website": "https://flybrain.online",
    "twitter": "flycoin",
    "telegram": "flycoin",
    "hasGithub": True,
    "githubUrls": ["https://github.com/ad7584/flycoin"],
}

STATUS = {
    "type": "status",
    "connected": True,
    "uptime": 125,
    "totalLaunches": 42,
    "githubLaunches": 3,
    "clients": 7,
}


class Parse(unittest.TestCase):
    def test_token_launch_carries_mint_name_symbol(self):
        ev = clawdws.parse_frame(json.dumps(TOKEN_LAUNCH))
        self.assertEqual(ev["type"], "token-launch")
        self.assertEqual(ev["mint"], TOKEN_LAUNCH["mint"])
        self.assertEqual(ev["name"], TOKEN_LAUNCH["name"])
        self.assertEqual(ev["symbol"], TOKEN_LAUNCH["symbol"])
        self.assertEqual(ev["creator"], TOKEN_LAUNCH["creator"])
        self.assertEqual(ev["marketCapSol"], 12.5)

    def test_status_carries_connected_and_uptime(self):
        ev = clawdws.parse_frame(json.dumps(STATUS))
        self.assertEqual(ev["type"], "status")
        self.assertTrue(ev["connected"])
        self.assertEqual(ev["uptime"], 125)
        self.assertEqual(ev["totalLaunches"], 42)
        self.assertEqual(ev["clients"], 7)

    def test_bytes_and_dict_take_the_same_path(self):
        a = clawdws.parse_frame(json.dumps(TOKEN_LAUNCH).encode("utf-8"))
        b = clawdws.parse_frame(TOKEN_LAUNCH)
        self.assertEqual(a["mint"], b["mint"])
        self.assertEqual(a["name"], b["name"])
        self.assertEqual(a["symbol"], b["symbol"])

    def test_unknown_type_is_none_invalid_json_raises(self):
        self.assertIsNone(clawdws.parse_frame('{"type": "pong"}'))
        with self.assertRaises(json.JSONDecodeError):
            clawdws.parse_frame("not-json")

    def test_handshake_headers_split_on_bytes(self):
        head = (b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Sec-WebSocket-Accept: abc\r\n")
        h = clawdws.http_headers(head)
        self.assertEqual(h["upgrade"], "websocket")
        self.assertEqual(h["sec-websocket-accept"], "abc")

    def test_cli_parse_prints_the_parsed_event(self):
        import io
        from unittest import mock
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            clawdws.main(["clawdws.py", "parse", json.dumps(STATUS)])
        out = json.loads(buf.getvalue())
        self.assertTrue(out["connected"])
        self.assertEqual(out["uptime"], 125)


if __name__ == "__main__":
    unittest.main()
