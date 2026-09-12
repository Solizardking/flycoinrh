"""
The fly's Solana wallet.

Robinhood Chain is EVM; this is a separate ed25519 keypair in a separate .env
entry (FLY_SOL_SECRET), and the two can never be confused for each other.

Same rule as rhwallet.py: the secret is generated here, written straight into
.env (gitignored), and never printed. Only the address is shown. Signing for
Stonkfun and Phoenix happens in-process from this secret.

  py solwallet.py new       create the fly's Solana wallet
  py solwallet.py show      address, SOL balance, rpc
"""
import json
import os
import sys
from pathlib import Path

import requests
from nacl.signing import SigningKey

from envcfg import load_env

ROOT = Path(__file__).parent
ENV = ROOT / ".env"

RPC = "https://api.mainnet-beta.solana.com"
EXPLORER = "https://solscan.io"
LAMPORTS = 1_000_000_000
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class LiveDisarmed(RuntimeError):
    """Raised when a broadcast is asked for with FLY_SOL_LIVE != 1."""


def b58encode(data):
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(B58[r])
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    body = "".join(reversed(out)) if out else ("" if pad else B58[0])
    return B58[0] * pad + body


def b58decode(s):
    n = 0
    for c in s:
        i = B58.find(c)
        if i < 0:
            raise ValueError("not base58")
        n = n * 58 + i
    pad = 0
    for c in s:
        if c == B58[0]:
            pad += 1
        else:
            break
    h = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + h


def compact_u16(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return bytes([(n & 0x7F) | 0x80, n >> 7])
    return bytes([(n & 0x7F) | 0x80, ((n >> 7) & 0x7F) | 0x80, n >> 14])


def read_compact_u16(buf, i=0):
    n = buf[i] & 0x7F
    if buf[i] < 0x80:
        return n, i + 1
    n |= (buf[i + 1] & 0x7F) << 7
    if buf[i + 1] < 0x80:
        return n, i + 2
    n |= buf[i + 2] << 14
    return n, i + 3


def live_armed(env=None):
    env = env if env is not None else load_env()
    return env.get("FLY_SOL_LIVE") == "1"


def _write_env_key(key, value):
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    out, done = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    ENV.write_text("\n".join(out) + "\n")


class Keypair:
    """Ed25519 keypair. Repr is the address only — the secret is not in it."""

    def __init__(self, seed):
        if len(seed) != 32:
            raise ValueError("seed must be 32 bytes")
        self.seed = bytes(seed)
        sk = SigningKey(self.seed)
        self.pubkey = bytes(sk.verify_key)
        self.secret64 = self.seed + self.pubkey
        self.address = b58encode(self.pubkey)
        self._sk = sk

    def sign(self, message):
        return self._sk.sign(message).signature

    def __repr__(self):
        return f"<solwallet {self.address}>"


def generate():
    """A fresh keypair. Does not touch the filesystem."""
    return Keypair(os.urandom(32))


def parse_secret(sec):
    """Accept base58 of 64 or 32 bytes, hex of 64 or 32 bytes, or a JSON array."""
    if not sec:
        raise ValueError("empty secret")
    sec = sec.strip()
    if sec.startswith("["):
        raw = bytes(json.loads(sec))
    else:
        try:
            raw = bytes.fromhex(sec[2:] if sec.startswith("0x") else sec)
        except ValueError:
            raw = b58decode(sec)
    if len(raw) == 64:
        return Keypair(raw[:32])
    if len(raw) == 32:
        return Keypair(raw)
    raise ValueError(f"secret is {len(raw)} bytes, want 32 or 64")


def account(env=None):
    env = env if env is not None else load_env()
    sec = env.get("FLY_SOL_SECRET")
    if not sec:
        print("no Solana wallet yet - run: py solwallet.py new")
        sys.exit(1)
    return parse_secret(sec)


def rpc_call(method, params=None, rpc=None, session=None):
    """JSON-RPC against the project's Solana endpoint (FLY_SOL_RPC), never a platform key."""
    sess = session or requests
    r = sess.post(rpc or RPC, json={"jsonrpc": "2.0", "id": 1,
                                    "method": method,
                                    "params": params or []}, timeout=25)
    return r.json()


def split_tx(tx):
    """sig_start, nsigs, message_bytes for a legacy or versioned Solana transaction."""
    if tx and tx[0] & 0x80:
        n, i = read_compact_u16(tx, 1)
        return i, n, tx[i + n * 64:]
    n, i = read_compact_u16(tx, 0)
    return i, n, tx[i + n * 64:]


def sign_transaction(tx, keypair):
    """
    Sign a serialized Solana transaction (legacy or versioned) in-process.
    Returns the signed bytes. The key never leaves this function.
    """
    if isinstance(tx, str):
        import base64
        tx = base64.b64decode(tx)
    sig_start, nsigs, msg = split_tx(tx)
    if nsigs < 1:
        raise ValueError("transaction has no signature slots")
    sig = keypair.sign(msg)
    out = bytearray(tx)
    out[sig_start:sig_start + 64] = sig
    return bytes(out)


def dummy_legacy_tx(pubkey):
    """A one-signer, zero-instruction transaction, for tests that must sign something."""
    if len(pubkey) != 32:
        raise ValueError("pubkey must be 32 bytes")
    msg = bytes([1, 0, 0]) + compact_u16(1) + pubkey + bytes(32) + compact_u16(0)
    return compact_u16(1) + bytes(64) + msg


def send_raw(tx, env=None, session=None, live=None):
    """Broadcast via FLY_SOL_RPC. Refused unless FLY_SOL_LIVE=1."""
    env = env if env is not None else load_env()
    if live is None:
        live = live_armed(env)
    if not live:
        raise LiveDisarmed("FLY_SOL_LIVE=0 - Solana send refused")
    import base64
    raw = tx if isinstance(tx, (bytes, bytearray)) else base64.b64decode(tx)
    rpc = env.get("FLY_SOL_RPC", RPC)
    return rpc_call("sendTransaction",
                    [base64.b64encode(raw).decode("ascii"),
                     {"encoding": "base64"}],
                    rpc=rpc, session=session)


def confirm(signature, env=None, session=None):
    env = env if env is not None else load_env()
    rpc = env.get("FLY_SOL_RPC", RPC)
    return rpc_call("getSignatureStatuses", [[signature]], rpc=rpc, session=session)


def new():
    env = load_env()
    if env.get("FLY_SOL_SECRET"):
        print("FLY_SOL_SECRET is already set in .env - refusing to overwrite.")
        print("Delete that line by hand first if you really want a new wallet.")
        return
    kp = generate()
    _write_env_key("FLY_SOL_SECRET", b58encode(kp.secret64))
    _write_env_key("FLY_SOL_RPC", RPC)
    _write_env_key("FLY_SOL_LIVE", "0")
    print("created the fly's Solana wallet\n")
    print(f"  address   {kp.address}")
    print("  chain     Solana")
    print("  secret    written to .env (gitignored), not shown here")
    print("\nFund it with SOL, then:  py solwallet.py show")
    print(f"Explorer: {EXPLORER}/account/{kp.address}")


def show_text(kp, lamports=None, rpc=None, live=False, rpc_error=None):
    """What `show` prints. The secret is never in this string."""
    lines = [f"  address   {kp.address}",
             f"  rpc       {rpc or RPC}",
             f"  live      {'ARMED' if live else 'no (FLY_SOL_LIVE=0)'}"]
    if rpc_error:
        lines.append(f"  rpc error: {rpc_error[:120]}")
    elif lamports is not None:
        lines.append(f"  balance   {lamports / LAMPORTS:.9f} SOL")
    lines.append(f"\n  {EXPLORER}/account/{kp.address}")
    return "\n".join(lines)


def balance(env=None, quiet=False, session=None):
    env = env if env is not None else load_env()
    kp = account(env)
    rpc = env.get("FLY_SOL_RPC", RPC)
    lamports = None
    err = None
    try:
        r = rpc_call("getBalance", [kp.address], rpc=rpc, session=session)
        if r.get("error"):
            err = json.dumps(r["error"])
        else:
            lamports = int((r.get("result") or {}).get("value") or 0)
    except Exception as e:
        err = str(e)
    text = show_text(kp, lamports=lamports, rpc=rpc,
                     live=live_armed(env), rpc_error=err)
    if not quiet:
        print(text)
    return lamports


def main(argv=None):
    argv = argv if argv is not None else sys.argv
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "new":
        new()
    elif cmd in ("show", "balance"):
        balance()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
