"""``Provider`` resolves the appropriate :class:`Ops` for a given host.

- The local host is served by the single local ``Ops`` passed at construction.
- Every other host is served by a ``RemoteClient`` dialing through the
  :class:`EdgeDialer`'s reverse tunnel: the agent dials out and the control
  plane opens per-op mux streams via the hub.

``RemoteClient`` lives in remote.py; it is imported lazily inside
:meth:`Provider._edge_ops` so this module imports and compiles on its own.
Tunnel clients are created lazily and cached per host id; the cached client's
dialer re-fetches the live tunnel session on each call, so the cache stays valid
across reconnects and needs no external invalidation.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Protocol

from .base import Ops
from .imagecache import CachingOps, ImageListCache

from kowan.auth.authorize import wrap_ops as _authz_wrap

# Host type constant (same value as in kowan.models). Duplicated here so the
# selection logic reads clearly without a hard dependency on kowan.models; the
# Host object passed to ops() is duck-typed on .id / .type / .address / .token.
HOST_TYPE_LOCAL = "local"


class EdgeDialer(Protocol):
    """Control-plane side of the edge reverse tunnel, as seen by this package.

    Satisfied by ``kowan.tunnel.Hub``. Defining the protocol here lets
    :class:`Provider` serve edge hosts without importing ``kowan.tunnel``.
    """

    def connected(self, host_id: str) -> bool:
        """Report whether a live tunnel session exists for ``host_id``."""
        ...

    async def dial(self, host_id: str) -> Any:
        """Open a new multiplexed connection to the connected edge host."""
        ...


class Provider:
    """Resolves the appropriate :class:`Ops` implementation for a given host.

    Safe for concurrent use (guarded by an internal lock).
    """

    def __init__(
        self,
        local: Optional[Ops],
        local_host_id: str,
        edge: Optional[EdgeDialer] = None,
    ) -> None:
        """Create a Provider.

        ``local`` is the ``Ops`` for the host whose id equals ``local_host_id``
        (typically the fixed ``"local"`` host). ``edge`` serves edge-mode hosts
        over the reverse tunnel and may be ``None`` (edge hosts are then reported
        as offline).
        """
        self._local = local
        self._local_host_id = local_host_id
        self._edge = edge
        self._lock = threading.Lock()
        self._tunnels: Dict[str, Ops] = {}
        # Per-host image list cache shared by every Ops handed out (see
        # imagecache.py). The local client is wrapped once, lazily.
        self.image_cache = ImageListCache()
        self._local_cached: Optional[Ops] = None

    def ops(self, host: Any) -> Ops:
        """Return the :class:`Ops` implementation for ``host``.

        ``host`` is a ``models.Host`` (duck-typed on ``id``/``type``/``token``).
        For local hosts the shared local ``Ops`` is returned. For every other
        host a cached tunnel-backed client is returned, or a ``RuntimeError`` is
        raised if no tunnel hub is configured / no tunnel is connected.
        """
        host_id = host.id
        host_type = getattr(host, "type", "")
        token = getattr(host, "token", "") or ""

        if host_type == HOST_TYPE_LOCAL or host_id == self._local_host_id:
            if self._local is None:
                raise RuntimeError(
                    f"docker: no local Ops configured for host {host_id!r}"
                )
            if self._local_cached is None:
                self._local_cached = CachingOps(  # type: ignore[assignment]
                    self._local, self._local_host_id, self.image_cache
                )
            # Scoped principals get an authorizing proxy (kowan.auth.authorize).
            return _authz_wrap(host, self._local_cached)  # type: ignore[return-value]

        # Both edge- and agent-mode hosts are reached over the reverse-tunnel
        # mux: the agent dials out and the control plane opens per-op streams
        # via the hub. There is no direct-TCP RemoteClient, so every non-local
        # host goes through _edge_ops.
        if self._edge is None:
            raise RuntimeError(
                f"docker: no tunnel hub configured for remote host {host_id!r}"
            )
        return _authz_wrap(host, self._edge_ops(host_id, token))

    def forget(self, host_id: str) -> None:
        """Drop the cached client and image list of a deleted host."""
        with self._lock:
            self._tunnels.pop(host_id, None)
        self.image_cache.forget(host_id)

    def _edge_ops(self, host_id: str, token: str) -> Ops:
        """Return the tunnel-backed client for an edge host.

        Raises ``RuntimeError`` when the edge dialer is unavailable or the host is
        not currently connected. The cached client's dialer re-fetches the live
        tunnel session on each request, so caching by host id is safe across
        reconnects.
        """
        if self._edge is None or not self._edge.connected(host_id):
            raise RuntimeError(
                f"docker: edge host {host_id!r} is offline (no tunnel connected)"
            )

        with self._lock:
            client = self._tunnels.get(host_id)
            if client is not None:
                return client

            from .remote import make_remote_client  # lazy: avoids import cycle

            edge = self._edge

            async def _dial() -> Any:
                # Re-fetch the live session on every call so the client keeps
                # working across tunnel reconnects; returns a fresh mux Stream.
                return await edge.dial(host_id)

            client = CachingOps(make_remote_client(_dial), host_id, self.image_cache)
            self._tunnels[host_id] = client  # type: ignore[assignment]
            return client  # type: ignore[return-value]
