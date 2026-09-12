"""
PumpFun live launches from the clawd-ws relay, as JSON frames.

The public page at https://clawd-ws.fly.dev/ is a viewer. The process talks
to the same relay the page does — `wss://clawd-ws.fly.dev/ws` — and parses
its JSON. Nothing here scrapes the HTML.

Frames the relay actually sends (from the page's own handler):

  {"type": "token-launch", "mint", "name", "symbol", ...}
  {"type": "status", "connected", "uptime", "totalLaunches", ...}

  py clawdws.py parse '<json>'
  py clawdws.py listen          one frame, then exit (evidence / smoke)
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
from urllib.parse import urlparse

WS_URL = "wss://clawd-ws.fly.dev/ws"


def parse_frame(raw):
    """
    Turn one relay frame into a dict with a `type` of token-launch or status.

    Unknown types return None. Invalid JSON raises. A token-launch always
    carries mint, name and symbol (empty string if the relay omitted them);
    a status always carries connected and uptime.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        msg = json.loads(raw)
    else:
        msg = raw
    if not isinstance(msg, dict):
        raise ValueError("frame is not an object")
    t = msg.get("type")
    if t == "token-launch":
        return {
            "type": "token-launch",
            "mint": msg.get("mint") or "",
            "name": msg.get("name") or "",
            "symbol": msg.get("symbol") or "",
            "signature": msg.get("signature") or "",
            "creator": msg.get("creator") or "",
            "description": msg.get("description") or "",
            "marketCapSol": msg.get("marketCapSol"),
            "time": msg.get("time"),
            "imageUri": msg.get("imageUri") or "",
            "website": msg.get("website") or "",
            "twitter": msg.get("twitter") or "",
            "telegram": msg.get("telegram") or "",
            "hasGithub": bool(msg.get("hasGithub")),
            "githubUrls": list(msg.get("githubUrls") or []),
        }
    if t == "status":
        return {
            "type": "status",
            "connected": bool(msg.get("connected")),
            "uptime": int(msg.get("uptime") or 0),
            "totalLaunches": int(msg.get("totalLaunches") or 0),
            "githubLaunches": int(msg.get("githubLaunches") or 0),
            "clients": int(msg.get("clients") or 0),
        }
    return None


def http_headers(head):
    """Parse HTTP header bytes from a websocket handshake response."""
    if isinstance(head, str):
        head = head.encode("ascii", "replace")
    out = {}
    for p in head.split(b"\r\n")[1:]:
        if b":" not in p:
            continue
        k, v = p.split(b":", 1)
        out[k.strip().decode("ascii", "replace").lower()] = v.strip().decode("ascii", "replace")
    return out


class _Buf:
    """Byte stream over a socket, seeded with handshake leftover."""

    def __init__(self, sock, leftover=b""):
        self.sock = sock
        self.buf = leftover if isinstance(leftover, (bytes, bytearray)) else leftover.encode("utf-8")

    def read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("socket closed")
            if not isinstance(chunk, (bytes, bytearray)):
                chunk = chunk.encode("utf-8")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def _read_ws_frame(src):
    hdr = src.read(2)
    opcode = hdr[0] & 0x0F
    masked = hdr[1] & 0x80
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", src.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", src.read(8))[0]
    mask = src.read(4) if masked else b""
    payload = src.read(length)
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if opcode == 0x8:
        return "close", payload
    if opcode == 0x9:
        return "ping", payload
    if opcode == 0x1:
        return "text", payload.decode("utf-8")
    if opcode == 0x2:
        return "binary", payload
    return "other", payload


def _send_ws_frame(sock, opcode, payload=b""):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    hdr = bytes([0x80 | opcode])
    n = len(payload)
    mask = os.urandom(4)
    if n < 126:
        hdr += bytes([0x80 | n])
    elif n < 65536:
        hdr += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        hdr += bytes([0x80 | 127]) + struct.pack("!Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(hdr + mask + masked)


def ws_url():
    return os.environ.get("FLY_CLAWD_WS") or WS_URL


def connect(url=None, timeout=20):
    """Open a client websocket to the relay. Caller must close the socket."""
    url = url or ws_url()
    u = urlparse(url)
    host = u.hostname
    port = u.port or (443 if u.scheme == "wss" else 80)
    path = u.path or "/"
    raw = socket.create_connection((host, port), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if u.scheme == "wss" else raw
    sock.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (f"GET {path} HTTP/1.1\r\n"
           f"Host: {host}\r\n"
           f"Origin: https://{host}\r\n"
           "Upgrade: websocket\r\n"
           "Connection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\n"
           "Sec-WebSocket-Version: 13\r\n"
           "\r\n")
    sock.sendall(req.encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise ConnectionError("no websocket handshake")
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    status = head.split(b"\r\n", 1)[0]
    if b"101" not in status:
        sock.close()
        raise ConnectionError(status.decode("ascii", "replace")[:200])
    expect = base64.b64encode(hashlib.sha1(
        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
    headers = http_headers(head)
    accept = headers.get("sec-websocket-accept", "")
    if accept != expect:
        sock.close()
        raise ConnectionError("bad websocket accept")
    return sock, rest


def listen(url=None, max_frames=1, timeout=20):
    """
    Connect to the relay and yield parsed frames (skipping unknown types).
    Stops after max_frames parsed events or when the socket closes.
    """
    url = url or ws_url()
    sock, leftover = connect(url, timeout=timeout)
    src = _Buf(sock, leftover)
    got = 0
    try:
        while got < max_frames:
            while True:
                kind, payload = _read_ws_frame(src)
                if kind == "ping":
                    _send_ws_frame(sock, 0xA, payload)
                    continue
                if kind == "close":
                    return
                if kind == "text":
                    ev = parse_frame(payload)
                    if ev is not None:
                        got += 1
                        yield ev
                    break
    finally:
        try:
            sock.close()
        except Exception:
            pass


def main(argv=None):
    argv = argv if argv is not None else sys.argv
    cmd = argv[1] if len(argv) > 1 else "listen"
    if cmd == "parse":
        raw = argv[2] if len(argv) > 2 else sys.stdin.read()
        print(json.dumps(parse_frame(raw), indent=2))
        return
    if cmd == "listen":
        n = int(argv[2]) if len(argv) > 2 else 1
        for ev in listen(max_frames=n):
            print(json.dumps(ev))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
