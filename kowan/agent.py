"""kowan-agent — runs on a Docker host and exposes that host's Docker ops.

Two transports, selected by
``KOWAN_AGENT_MODE`` (see :func:`kowan.config.load_agent_config`):

  * ``edge`` / atos (agent -> server): the agent dials OUT to the control
    plane's ``/edge/connect`` WebSocket, runs the tunnel handshake as the
    dialer (SENDING its edge token), and then serves inbound op streams the
    control plane opens over the reverse tunnel. It reconnects forever with
    exponential backoff, resetting the backoff after a session that stayed up
    long enough to count as "connected".

  * ``agent`` / stoa (server -> agent, the default): the agent runs an aiohttp
    WebSocket server on ``KOWAN_AGENT_ADDR`` (default ``:9001``). When the
    control plane dials ``GET /connect``, the agent runs the handshake as the
    ACCEPTOR — it VERIFIES the server presents the agent's configured token —
    and then serves inbound op streams. Optional TLS from
    ``KOWAN_AGENT_TLS_CERT`` / ``KOWAN_AGENT_TLS_KEY``.

In both modes the agent always has a token configured: it SENDS the token in
edge mode and VERIFIES it in direct mode. The Docker ops are served by a
:class:`kowan.docker.LocalClient` over the local Docker Engine socket.

The mux consumes whole WebSocket *messages* (via ``MessageTransport``), so both
the edge dial-out and each accepted direct-mode WebSocket are aiohttp
WebSockets wrapped by :class:`AiohttpWSTransport`. ``asyncio`` signal handlers
drive graceful shutdown.

Runnable as ``python3 -m kowan.agent``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import ssl
import sys
from typing import Optional

import aiohttp

from . import __version__, agent_ops_server
from .config import AgentConfig, load_agent_config
from .docker import LocalClient
from .transport.aiohttp_ws import HANDSHAKE_TIMEOUT, MAX_MESSAGE_SIZE, AiohttpWSTransport
from .tunnel import establish_agent_side
from .tunnel.handshake import preauth_ok, tls_binding
from .web.netpolicy import address_within, parse_cidrs

log = logging.getLogger("kowan.agent")

# Edge reconnect backoff bounds (seconds).
_MIN_BACKOFF = 1.0
_MAX_BACKOFF = 30.0

_USAGE = """\
kowan-agent runs on a Docker host and exposes that host's Docker operations to
the Kowan control plane.

Usage:
  python3 -m kowan.agent [flags]

Flags:
  -h, --help      print this help and exit
  -v, --version   print the version and exit

Modes (KOWAN_AGENT_MODE):
  agent  (default)  WebSocket listener the control plane dials directly
  edge              dials OUT to the control plane over a reverse tunnel

Environment variables:
  KOWAN_AGENT_MODE      "agent" (default) or "edge"
  KOWAN_AGENT_ADDR      listen address for agent mode (default ":9001")
  KOWAN_AGENT_TOKEN     bearer token / edge enrollment token
  KOWAN_AGENT_TLS_CERT  path to a PEM TLS certificate (agent mode, optional)
  KOWAN_AGENT_TLS_KEY   path to a PEM TLS private key (agent mode, optional)
  KOWAN_SERVER_URL      control-plane base URL the edge agent dials (edge mode)
"""

VERSION = __version__


# Tokens the control plane generates are 64 hex characters; anything this
# short was typed by hand and is guessable.
MIN_TOKEN_LENGTH = 32


def config_problem(cfg: AgentConfig) -> str:
    """Why this configuration must not start, or "" when it is safe.

    The token is the only thing standing between the network and the Docker
    socket (root on the host): an agent without a strong one would accept anyone.
    """
    if cfg.mode == "edge" and not cfg.edge_token:
        return "KOWAN_AGENT_TOKEN is not set: copy the join command from Kowan (Hosts > Add host)."
    if cfg.mode == "edge":
        return ""
    try:
        parse_cidrs(cfg.allow_cidrs, "KOWAN_AGENT_ALLOW_CIDRS")
    except ValueError as err:
        return str(err)
    if len(cfg.token) < MIN_TOKEN_LENGTH:
        return (
            f"KOWAN_AGENT_TOKEN must be set to the token Kowan generated for this host "
            f"(at least {MIN_TOKEN_LENGTH} characters); refusing to expose the Docker socket without it."
        )
    return ""


def _token_fingerprint(token: str) -> str:
    """Short, non-reversible identifier for a bearer token (safe to log)."""
    if not token:
        return "(none)"
    return hashlib.sha256(token.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# edge / atos mode: dial OUT to the control plane.
# --------------------------------------------------------------------------- #


def _edge_connect_url(server_url: str) -> str:
    """Build the ``ws(s)://.../edge/connect`` URL from the server base URL.

    The server URL is usually http/https; aiohttp's ``ws_connect`` requires an
    explicit ws/wss scheme, so we rewrite it here.
    """
    base = server_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif not (base.startswith("ws://") or base.startswith("wss://")):
        # No scheme given; assume plain ws.
        base = "ws://" + base
    return base + "/edge/connect"


async def _serve_edge_session(cfg: AgentConfig, connect_url: str, ops) -> None:
    """Dial the control plane once and serve op streams until the session dies.

    Returns when the session ends (clean disconnect) and raises on a dial or
    transport failure so the reconnect loop can log and back off.
    """
    headers = {"Authorization": "Bearer " + cfg.edge_token}

    # wss:// verifies TLS by default (aiohttp verifies certs unless told not to);
    # that's the desired behavior when dialing a real control plane over https.
    # Messages are capped at one mux frame's worth (MAX_MESSAGE_SIZE).
    # heartbeat: without pings a NAT-dropped tunnel left the agent blocked on
    # receive() forever and it never redialed.
    async with aiohttp.ClientSession() as sess, sess.ws_connect(
        connect_url, headers=headers, max_msg_size=MAX_MESSAGE_SIZE, heartbeat=30.0
    ) as ws:
        log.info("edge tunnel connected: %s", connect_url)
        transport = AiohttpWSTransport(ws)
        # dialed=True: the agent dialed out, so it SENDS its edge token.
        session = await asyncio.wait_for(
            establish_agent_side(transport, dialed=True, token=cfg.edge_token),
            HANDSHAKE_TIMEOUT,
        )
        # Serve inbound op streams the control plane opens over the tunnel until
        # the session ends. While the session is up, keep the heartbeat file
        # fresh: it is what `kowan.agent healthcheck` (the image HEALTHCHECK, and
        # the self-update verify step) reads. It is removed when the session ends.
        from .updater.preflight import heartbeat_loop

        hb = asyncio.ensure_future(heartbeat_loop())
        try:
            await agent_ops_server.serve_agent_ops(session, ops)
        finally:
            hb.cancel()
            try:
                await hb
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _run_edge(cfg: AgentConfig, ops, stop: asyncio.Event) -> None:
    """Reconnect loop for edge mode with exponential-backoff-with-reset.

    A session that stayed connected longer
    than the max backoff resets the backoff so a long-lived session followed by
    a disconnect reconnects promptly; a quick failure keeps the backoff growing
    so we don't hammer an unavailable control plane.
    """
    if not cfg.server_url:
        raise ValueError("edge mode requires KOWAN_SERVER_URL")

    connect_url = _edge_connect_url(cfg.server_url)
    backoff = _MIN_BACKOFF

    log.info(
        "edge agent starting: serverURL=%s tokenFingerprint=%s",
        cfg.server_url,
        _token_fingerprint(cfg.edge_token),
    )

    loop = asyncio.get_running_loop()
    while not stop.is_set():
        session_start = loop.time()
        err: Optional[BaseException] = None
        # Race the session against the stop signal: a live tunnel never ends on
        # its own, so without this SIGTERM would wait for Docker's kill timeout.
        session = asyncio.ensure_future(_serve_edge_session(cfg, connect_url, ops))
        stopper = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({session, stopper}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            session.cancel()
            stopper.cancel()
            raise
        if not session.done():
            session.cancel()
            try:
                await asyncio.wait_for(session, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            stopper.cancel()
            break
        stopper.cancel()
        exc = session.exception() if not session.cancelled() else None
        if exc is not None:
            err = exc
        session_duration = loop.time() - session_start

        if stop.is_set():
            break

        # If the session stayed connected for a while, treat it as a successful
        # connection and reset the backoff.
        reconnected_after_success = session_duration > _MAX_BACKOFF
        if reconnected_after_success:
            backoff = _MIN_BACKOFF

        if err is not None:
            log.warning("edge tunnel disconnected, will retry: %r (backoff %.0fs)", err, backoff)
        else:
            log.info("edge tunnel closed, will reconnect (backoff %.0fs)", backoff)

        # Wait with backoff, honoring the stop signal so shutdown aborts promptly.
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break

        # Only grow the backoff when the previous session did not stabilize.
        if not reconnected_after_success:
            backoff = min(backoff * 2, _MAX_BACKOFF)

    log.info("shutdown signal received, stopping edge agent")


# --------------------------------------------------------------------------- #
# agent / stoa mode: accept the control plane's inbound dial.
# --------------------------------------------------------------------------- #


async def _handle_connect(request, cfg: AgentConfig, ops, binding: str = ""):
    """aiohttp handler for ``GET /connect`` in direct mode.

    Accepts the control plane's WebSocket dial, runs the challenge-response
    handshake bound to this agent's TLS certificate (``binding``; the token
    never crosses the wire, see kowan.tunnel.handshake), then serves inbound op
    streams until the session ends.
    """
    from aiohttp import web

    # Refuse strangers before upgrading: only the allowed addresses, and only
    # with a fresh HMAC of the token in the Authorization header.
    allow = parse_cidrs(cfg.allow_cidrs)
    if allow and not address_within(request.remote or "", allow):
        raise web.HTTPForbidden(text="address not allowed")
    if not preauth_ok(cfg.token, request.headers.get("Authorization", "")):
        raise web.HTTPUnauthorized(text="invalid token")

    ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=MAX_MESSAGE_SIZE)  # see server.py
    await ws.prepare(request)
    from kowan.web.wsregistry import track as _track_ws

    _track_ws(request, ws)  # closed on shutdown (no SIGTERM hang)

    transport = AiohttpWSTransport(ws)
    try:
        # dialed=False: the server dialed us, so the agent VERIFIES the server
        # presents our configured token (default verifier in establish_agent_side).
        session = await asyncio.wait_for(
            establish_agent_side(transport, dialed=False, token=cfg.token, binding=binding),
            HANDSHAKE_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - reject bad handshakes cleanly
        log.warning("agent: rejected inbound connection: %r", exc)
        await transport.close()
        return ws

    log.info("agent: control plane connected")
    try:
        await agent_ops_server.serve_agent_ops(session, ops)
    finally:
        await transport.close()
    return ws


def _build_ssl_context(cfg: AgentConfig) -> tuple[ssl.SSLContext, bytes]:
    """Build a server ``SSLContext`` for direct/stoa mode, and return the DER
    of the certificate it serves (the handshake binds its proofs to it).

    With KOWAN_AGENT_TLS_CERT / KOWAN_AGENT_TLS_KEY, load that pair. Otherwise
    generate an ephemeral self-signed certificate so the direct-mode listener
    always serves wss://. No CA is needed: the challenge-response handshake
    covers the certificate's fingerprint, so an interceptor's certificate makes
    it fail. Only the direct/agent mode calls this; edge mode dials OUT and
    never needs a server cert.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if cfg.tls_cert and cfg.tls_key:
        ctx.load_cert_chain(certfile=cfg.tls_cert, keyfile=cfg.tls_key)
        log.info("agent TLS using provided certificate: %s", cfg.tls_cert)
        with open(cfg.tls_cert, encoding="ascii") as f:
            return ctx, ssl.PEM_cert_to_DER_cert(f.read())
    cert_pem, key_pem = _generate_self_signed_cert()
    _load_cert_chain_from_mem(ctx, cert_pem, key_pem)
    log.info("agent TLS using a generated self-signed certificate")
    return ctx, ssl.PEM_cert_to_DER_cert(cert_pem.decode("ascii"))


def _generate_self_signed_cert() -> tuple[bytes, bytes]:
    """Return (certPEM, keyPEM) for an ephemeral P-256 self-signed cert.

    ECDSA P-256, valid ~1 year, CN
    ``kowan-agent`` / O ``kowan``, SANs localhost + loopback IPs. ``cryptography``
    is imported lazily so the edge-mode agent (which never serves TLS) doesn't
    need it installed.
    """
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "kowan-agent"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "kowan"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.IPAddress(ipaddress.ip_address("::1")),
            ]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _load_cert_chain_from_mem(ctx: ssl.SSLContext, cert_pem: bytes, key_pem: bytes) -> None:
    """Load an in-memory PEM cert/key into ``ctx``.

    ``SSLContext.load_cert_chain`` only reads from disk, so write the PEMs to a
    private temp dir with 0600 perms, load, then remove them — the loaded key
    material lives in the context, not the files.
    """
    import os
    import tempfile

    d = tempfile.mkdtemp(prefix="kowan-agent-tls-")
    cert_path = os.path.join(d, "cert.pem")
    key_path = os.path.join(d, "key.pem")
    try:
        for path, data in ((cert_path, cert_pem), (key_path, key_pem)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    finally:
        for path in (cert_path, key_path):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass


def _split_addr(addr: str) -> tuple[Optional[str], int]:
    """Split a ``host:port`` listen address (":9001", "0.0.0.0:9001") into (host, port).

    An empty host (from a leading ":") means "all interfaces" — aiohttp wants
    ``None`` for that.
    """
    if ":" not in addr:
        # Bare port or bare host? Ambiguous; assume a port.
        try:
            return None, int(addr)
        except ValueError:
            return addr, 9001
    host, _, port_s = addr.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        port = 9001
    return (host or None), port


async def _run_agent(cfg: AgentConfig, ops, stop: asyncio.Event) -> None:
    """Run the direct-mode aiohttp WebSocket server until the stop signal."""
    from aiohttp import web

    app = web.Application()
    from kowan.web.wsregistry import install as _install_ws_registry

    _install_ws_registry(app)

    ssl_ctx, cert_der = _build_ssl_context(cfg)
    binding = tls_binding(cert_der)

    async def handler(request):
        return await _handle_connect(request, cfg, ops, binding)

    app.router.add_get("/connect", handler)
    host, port = _split_addr(cfg.addr)

    runner = web.AppRunner(app, shutdown_timeout=5.0)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port, ssl_context=ssl_ctx)
    await site.start()

    log.info(
        "agent listening: addr=%s tokenFingerprint=%s tls=%s",
        cfg.addr,
        _token_fingerprint(cfg.token),
        bool(ssl_ctx),
    )

    # Direct mode: healthy while the listener is serving (see kowan.updater.preflight).
    from .updater.preflight import heartbeat_loop

    hb = asyncio.ensure_future(heartbeat_loop())
    try:
        await stop.wait()
    finally:
        log.info("shutdown signal received, stopping agent")
        hb.cancel()
        try:
            await hb
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await runner.cleanup()


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #


async def _keep_running_after_reboot(ops) -> None:
    """Give this agent's own container the ``unless-stopped`` restart policy
    when it has none, so the agent comes back after the host reboots.

    Agents installed before the join command carried ``--restart`` fix
    themselves on their next start; a policy someone chose (``always``,
    ``on-failure``, ``unless-stopped``) is left alone. The container is the
    one named by ``HOSTNAME`` (Docker's default hostname is the container
    id), confirmed by this agent's token in its environment, so a previous
    agent container kept for rollback (same token) is never touched. Best
    effort: outside a container, or when it cannot be identified, nothing
    happens.
    """
    ref = os.environ.get("HOSTNAME", "").strip()
    token = os.environ.get("KOWAN_AGENT_TOKEN", "")
    if not ref or not token:
        return
    try:
        insp = await asyncio.wait_for(ops.inspect_container(ref), timeout=15)
    except Exception:  # noqa: BLE001 - not a container, or a custom hostname
        return
    if f"KOWAN_AGENT_TOKEN={token}" not in ((insp.get("Config") or {}).get("Env") or []):
        return
    policy = ((insp.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
    if policy != "no":
        return
    try:
        await asyncio.wait_for(
            ops.update_container_config(insp["Id"], {"RestartPolicy": {"Name": "unless-stopped"}}), timeout=15)
        log.info("agent: restart policy set to unless-stopped, so the agent comes back after a reboot")
    except Exception as err:  # noqa: BLE001 - never block the agent on this
        log.warning("agent: could not set the restart policy: %s", err)


async def _run(cfg: AgentConfig) -> None:
    """Construct the local Docker client and run the configured mode."""
    ops = LocalClient(cfg.docker_socket) if cfg.docker_socket else LocalClient()
    stop = asyncio.Event()
    await _keep_running_after_reboot(ops)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Signal handlers may be unavailable (e.g. non-main thread); ignore.
            pass

    try:
        # Edge mode is selected ONLY by KOWAN_AGENT_MODE=edge. KOWAN_SERVER_URL is consumed in
        # edge mode but never selects it — an operator can set it while still
        # running the direct-mode listener.
        if cfg.mode == "edge":
            await _run_edge(cfg, ops, stop)
        else:
            await _run_agent(cfg, ops, stop)
    finally:
        await ops.close()


def main(argv: Optional[list[str]] = None) -> None:
    """CLI entrypoint. Handles --help/--version, then runs the configured mode."""
    args = sys.argv[1:] if argv is None else argv

    # `healthcheck` is the image HEALTHCHECK (heartbeat file freshness).
    if args and args[0] == "healthcheck":
        from kowan.updater.preflight import agent_healthcheck

        raise SystemExit(agent_healthcheck(args[1:]))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # One-shot sub-commands of the self-update system (docs/updates-design.md),
    # dispatched before the help/version scan and before config load:
    #   self-update --plan <b64>  the transactional swap helper container
    #   preflight                 smoke test of a NEW agent image
    if args and args[0] in ("self-update", "preflight"):
        from kowan.updater.preflight import dispatch

        raise SystemExit(dispatch("agent", args))

    for arg in args:
        if arg in ("-h", "--help", "help"):
            sys.stdout.write(_USAGE)
            return
        if arg in ("-v", "--version", "version"):
            sys.stdout.write(f"kowan-agent {VERSION}\n")
            return

    cfg = load_agent_config()

    problem = config_problem(cfg)
    if problem:
        log.error("%s", problem)
        sys.exit(2)

    from .eventloop import run as run_loop

    try:
        # uvloop when installed (optional dep, see kowan/eventloop.py).
        run_loop(_run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
