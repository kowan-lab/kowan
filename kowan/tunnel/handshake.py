"""Handshakes that authenticate a tunnel before the mux starts.

* ``atos`` (edge / behind NAT): the AGENT dials the server over wss:// with a
  certificate it verifies against the system CAs, and sends its token; the
  SERVER checks it against the host record (``dialer_handshake`` /
  ``acceptor_handshake``).
* ``stoa`` (direct): the SERVER dials an address users typed, and the agent
  serves a self-signed certificate, so the token must never cross the wire.
  Both sides prove they hold it with HMACs bound to the agent's certificate
  (``stoa_server_handshake`` / ``stoa_agent_handshake``, below).

The agent therefore always has a token, in either mode, and the mode is a
single env var.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

PROTOCOL = "kowan.tunnel/1"


class HandshakeError(Exception):
    pass


async def dialer_handshake(transport, token: str) -> dict:
    """Run on the side that DIALED: send the token, await acceptance."""
    hello = {"proto": PROTOCOL, "token": token}
    await transport.send(json.dumps(hello).encode())
    raw = await transport.recv()
    if raw is None:
        raise HandshakeError("connection closed during handshake")
    reply = json.loads(raw.decode())
    if not reply.get("ok"):
        raise HandshakeError(reply.get("error", "handshake refused"))
    return reply


async def acceptor_handshake(transport, verify) -> str:
    """Run on the side that ACCEPTED: read the token, verify, reply.

    ``verify(token) -> host_id | None`` returns the resolved host id when the
    token is valid, else a falsy value. Returns the resolved host id, or raises
    ``HandshakeError`` (after sending a rejection) when the token is invalid.
    """
    raw = await transport.recv()
    if raw is None:
        raise HandshakeError("connection closed during handshake")
    try:
        hello = json.loads(raw.decode())
    except Exception as e:
        await _reject(transport, "malformed handshake")
        raise HandshakeError(f"malformed handshake: {e}")
    if hello.get("proto") != PROTOCOL:
        await _reject(transport, "protocol mismatch")
        raise HandshakeError("protocol mismatch")
    token = hello.get("token") or ""
    host_id = verify(token)
    if not host_id:
        await _reject(transport, "invalid token")
        raise HandshakeError("invalid token")
    await transport.send(json.dumps({"ok": True, "hostId": host_id}).encode())
    return host_id


async def _reject(transport, reason: str) -> None:
    try:
        await transport.send(json.dumps({"ok": False, "error": reason}).encode())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Direct mode (stoa): mutual challenge-response bound to the TLS channel.
#
# The server dials an address users typed, over TLS it cannot verify (the agent
# serves a self-signed certificate it generates at startup). So the token never
# crosses the wire. Each side proves it holds the token with an HMAC over both
# sides' random challenges and over the fingerprint of the agent's TLS
# certificate. A man in the middle presents its own certificate: the server's
# proof then covers the wrong fingerprint and the agent refuses it, and without
# the token the attacker can forge neither proof. The agent proves itself back,
# so the server never sends secrets to an impostor.
# ---------------------------------------------------------------------------

STOA_PROTOCOL = "kowan.stoa/1"
PREAUTH_SCHEME = "Kowan-Preauth"
PREAUTH_SKEW = 120  # seconds of clock difference tolerated


def tls_binding(cert_der: Optional[bytes]) -> str:
    """Fingerprint of the agent's TLS certificate ("" on a plain connection)."""
    return hashlib.sha256(cert_der).hexdigest() if cert_der else ""


def _mac(token: str, *parts: str) -> str:
    return hmac.new(token.encode(), "|".join(parts).encode(), hashlib.sha256).hexdigest()


def preauth_value(token: str, now: Optional[int] = None) -> str:
    """Authorization header value that lets the agent refuse strangers before
    upgrading, without the token itself on the wire."""
    ts = str(int(time.time() if now is None else now))
    return f"{PREAUTH_SCHEME} {ts}.{_mac(token, 'preauth', ts)}"


def preauth_ok(token: str, header: str, now: Optional[int] = None) -> bool:
    prefix = PREAUTH_SCHEME + " "
    if not token or not header.startswith(prefix):
        return False
    ts, _, sig = header[len(prefix):].strip().partition(".")
    if not ts.isdigit() or abs(int(time.time() if now is None else now) - int(ts)) > PREAUTH_SKEW:
        return False
    return hmac.compare_digest(sig, _mac(token, "preauth", ts))


async def _recv_json(transport) -> dict:
    raw = await transport.recv()
    if raw is None:
        raise HandshakeError("connection closed during handshake")
    try:
        msg = json.loads(raw.decode())
    except Exception as e:
        raise HandshakeError(f"malformed handshake: {e}")
    if not isinstance(msg, dict):
        raise HandshakeError("malformed handshake")
    return msg


async def stoa_agent_handshake(transport, token: str, binding: str) -> None:
    """Agent side (it accepted the connection): challenge, verify, prove back."""
    if not token:
        raise HandshakeError("agent has no token")
    mine = secrets.token_hex(16)
    await transport.send(json.dumps({"proto": STOA_PROTOCOL, "challenge": mine}).encode())
    hello = await _recv_json(transport)
    theirs = str(hello.get("challenge") or "")
    if hello.get("proto") != STOA_PROTOCOL or len(theirs) < 32:
        await _reject(transport, "protocol mismatch")
        raise HandshakeError("protocol mismatch")
    expected = _mac(token, "server", mine, theirs, binding)
    if not hmac.compare_digest(str(hello.get("proof") or ""), expected):
        await _reject(transport, "invalid proof")
        raise HandshakeError("invalid proof (wrong token, or the connection was intercepted)")
    proof = _mac(token, "agent", mine, theirs, binding)
    await transport.send(json.dumps({"ok": True, "proof": proof}).encode())


async def stoa_server_handshake(transport, token: str, binding: str) -> None:
    """Server side (it dialed): answer the agent's challenge, verify its proof."""
    if not token:
        raise HandshakeError("host has no token")
    first = await _recv_json(transport)
    theirs = str(first.get("challenge") or "")
    if first.get("proto") != STOA_PROTOCOL or len(theirs) < 32:
        raise HandshakeError("protocol mismatch")
    mine = secrets.token_hex(16)
    proof = _mac(token, "server", theirs, mine, binding)
    await transport.send(json.dumps({"proto": STOA_PROTOCOL, "challenge": mine, "proof": proof}).encode())
    reply = await _recv_json(transport)
    if not reply.get("ok"):
        raise HandshakeError(str(reply.get("error") or "handshake refused"))
    expected = _mac(token, "agent", theirs, mine, binding)
    if not hmac.compare_digest(str(reply.get("proof") or ""), expected):
        raise HandshakeError("agent proof invalid (wrong token, or the connection was intercepted)")
