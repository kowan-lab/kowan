"""Server-side dialer for ``agent`` (stoa / direct) mode hosts.

Edge (atos) hosts dial IN to ``/edge/connect`` and register themselves on the
:class:`~kowan.tunnel.hub.Hub`. Agent (stoa) hosts are the mirror image: THIS
control plane dials OUT to each agent's address, runs the control-plane side of
the token handshake (``dialed=True`` -> it SENDS the host token), and registers
the resulting mux session on the *same* Hub. Once registered, the provider
serves an agent host exactly like an edge host — ``hub.dial(host_id)`` opens a
stream and the agent protocol flows over it — so no provider change is needed.

:class:`StoaDialer` polls the store on an interval and keeps one reconnect loop
per agent-mode host that has an address, tearing a loop down when the host is
deleted, switched to edge, or has its address/token changed.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Optional

import aiohttp

from .models.host import HostModeAgent, HostTypeLocal
from .transport.aiohttp_ws import HANDSHAKE_TIMEOUT, MAX_MESSAGE_SIZE, AiohttpWSTransport
from .tunnel import establish_server_side
from .tunnel.handshake import preauth_value, tls_binding

log = logging.getLogger("kowan.dialer")

# Reconnect backoff bounds, mirroring the agent's edge reconnect loop.
_MIN_BACKOFF = 1.0
_MAX_BACKOFF = 30.0

# How often the supervisor reconciles the store's agent hosts against the set of
# running dial loops (picks up created/deleted/edited hosts).
_RECONCILE_INTERVAL = 10.0


def _connect_url(address: str) -> str:
    """Build the agent ``/connect`` websocket URL from a host address.

    The address may be a bare ``host:port`` (default: TLS, since a direct-mode
    agent serves wss:// with a self-signed cert) or carry an explicit scheme.
    """
    a = address.strip().rstrip("/")
    if a.startswith(("ws://", "wss://")):
        base = a
    elif a.startswith("https://"):
        base = "wss://" + a[len("https://"):]
    elif a.startswith("http://"):
        base = "ws://" + a[len("http://"):]
    else:
        base = "wss://" + a  # no scheme -> assume TLS (agent self-signs)
    return base + "/connect"


def _insecure_ssl() -> ssl.SSLContext:
    """A client SSL context that skips certificate verification.

    A direct-mode agent serves a self-signed certificate it generates at
    startup, so there is no CA to check it against. Authentication instead comes
    from the handshake, whose proofs are bound to the certificate this
    connection sees (kowan.tunnel.handshake): an interceptor cannot pass it, and
    the token never crosses the wire.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _peer_cert(ws) -> bytes:
    """DER of the certificate the agent presented, or b"" on plain ws://."""
    ssl_object = ws.get_extra_info("ssl_object")
    if ssl_object is None:
        return b""
    return ssl_object.getpeercert(binary_form=True) or b""


async def _serve_session(host, hub) -> None:
    """Dial one agent, register its session on the hub, serve until it ends.

    Raises on a dial/handshake/transport failure so the caller's reconnect loop
    can back off; returns cleanly when the session closes.
    """
    url = _connect_url(host.address)
    # aiohttp's ws_connect takes ssl= as an SSLContext (wss) or False to disable
    # TLS (ws). Messages are capped at one mux frame's worth so a hostile peer
    # cannot make us buffer an unbounded message. The Authorization header
    # carries a timestamped HMAC (never the token) so the agent can refuse
    # strangers before upgrading.
    # heartbeat: detect a silently dead agent link so the reconnect loop runs.
    ws_kwargs = {"max_msg_size": MAX_MESSAGE_SIZE, "heartbeat": 30.0,
                 "headers": {"Authorization": preauth_value(host.token)}}
    if url.startswith("wss://"):
        ws_kwargs["ssl"] = _insecure_ssl()

    async with aiohttp.ClientSession() as sess, sess.ws_connect(url, **ws_kwargs) as ws:
        transport = AiohttpWSTransport(ws)
        # dialed=True: WE dialed out, so we SEND the host token; the server is
        # always the control-plane (mux client).
        # The certificate this connection actually presents: the handshake
        # proofs cover it, so an interceptor's certificate makes them fail.
        binding = tls_binding(_peer_cert(ws))
        host_id, session = await asyncio.wait_for(
            establish_server_side(transport, dialed=True, token=host.token, host_id=host.id,
                                  binding=binding),
            HANDSHAKE_TIMEOUT,
        )
        log.info("agent tunnel connected host=%s addr=%s", host_id, host.address)
        await hub.register(host_id, session)
        try:
            await session.wait_closed()
        finally:
            await hub.unregister(host_id, session)
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
            log.info("agent tunnel disconnected host=%s", host_id)


class StoaDialer:
    """Maintains outbound tunnels to every ``agent``-mode host with an address."""

    def __init__(self, svc, interval: float = _RECONCILE_INTERVAL):
        self._store = svc.store
        self._hub = svc.hub
        self._interval = interval
        self._stop = asyncio.Event()
        self._supervisor: Optional[asyncio.Task] = None
        # host_id -> reconnect task, and host_id -> (address, token) it was
        # started for, so a changed address/token restarts the loop.
        self._tasks: dict[str, asyncio.Task] = {}
        self._specs: dict[str, tuple[str, str]] = {}

    async def start(self) -> None:
        """Spawn the supervisor task and return (does not block)."""
        if self._supervisor is None:
            self._supervisor = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        """Signal shutdown and cancel every dial loop + the supervisor."""
        self._stop.set()
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self._specs.clear()
        if self._supervisor is not None:
            self._supervisor.cancel()
            self._supervisor = None

    async def _run(self) -> None:
        log.info("stoa dialer started")
        while not self._stop.is_set():
            try:
                self._reconcile()
            except Exception as err:  # noqa: BLE001 - never let the loop die
                log.warning("stoa dialer reconcile failed: %r", err)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
        log.info("stoa dialer stopped")

    def _desired(self) -> dict[str, tuple[str, str]]:
        """The agent-mode hosts that should have an outbound tunnel."""
        out: dict[str, tuple[str, str]] = {}
        for h in self._store.list_hosts():
            if getattr(h, "type", "") == HostTypeLocal:
                continue
            if getattr(h, "mode", "") != HostModeAgent:
                continue
            address = (getattr(h, "address", "") or "").strip()
            if not address:
                continue
            out[h.id] = (address, getattr(h, "token", "") or "")
        return out

    def _reconcile(self) -> None:
        desired = self._desired()

        # Start new loops; restart loops whose address/token changed or that died.
        for host_id, spec in desired.items():
            task = self._tasks.get(host_id)
            if task is not None and not task.done() and self._specs.get(host_id) == spec:
                continue
            if task is not None and not task.done():
                task.cancel()
            self._specs[host_id] = spec
            self._tasks[host_id] = asyncio.ensure_future(self._host_loop(host_id))

        # Stop loops for hosts that are gone / no longer agent-mode.
        for host_id in list(self._tasks):
            if host_id not in desired:
                task = self._tasks.pop(host_id, None)
                self._specs.pop(host_id, None)
                if task is not None:
                    task.cancel()

    def _current_host(self, host_id: str):
        """Re-fetch the host so each reconnect uses the latest address/token."""
        try:
            h = self._store.get_host(host_id)
        except Exception:  # noqa: BLE001 - deleted between reconciles
            return None
        if getattr(h, "mode", "") != HostModeAgent:
            return None
        if not (getattr(h, "address", "") or "").strip():
            return None
        return h

    async def _host_loop(self, host_id: str) -> None:
        """Reconnect loop for one agent host (backoff-with-reset, like the agent)."""
        backoff = _MIN_BACKOFF
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            host = self._current_host(host_id)
            if host is None:
                return  # host gone or no longer dialable
            start = loop.time()
            err: Optional[BaseException] = None
            try:
                await _serve_session(host, self._hub)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure -> back off + retry
                err = exc
            duration = loop.time() - start

            if self._stop.is_set():
                return

            # A session that stayed up longer than the max backoff counts as a
            # success and resets the backoff; a quick failure keeps it growing.
            stabilized = duration > _MAX_BACKOFF
            if stabilized:
                backoff = _MIN_BACKOFF
            if err is not None:
                log.warning(
                    "agent tunnel host=%s failed, will retry: %r (backoff %.0fs)",
                    host_id, err, backoff,
                )

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            if not stabilized:
                backoff = min(backoff * 2, _MAX_BACKOFF)
