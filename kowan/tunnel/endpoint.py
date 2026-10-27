"""High-level tunnel establishment — ties the token handshake to the mux.

There are two independent axes, and this is the whole trick:

  * WHO DIALED  -> sends vs verifies the token          (handshake.py)
  * WHO IS THE CONTROL-PLANE -> mux client (odd ids) vs mux server (even ids)

The server is ALWAYS the control-plane (mux client); the agent is always the
mux server. In production the control-plane opens streams for host ops; the
mux itself stays symmetric (either side *could* open a stream) even though
only the server->agent direction is exercised.
"""

from __future__ import annotations

from ..mux.session import Session
from .handshake import acceptor_handshake, dialer_handshake, stoa_agent_handshake, stoa_server_handshake


async def establish_server_side(transport, *, dialed: bool, token: str | None = None,
                                verify=None, host_id: str | None = None,
                                binding: str = "") -> tuple[str, Session]:
    """Establish the control-plane end of a tunnel.

    ``dialed=True``  -> stoa/direct: the server dialed the agent; both sides
                        prove they hold ``token`` without sending it, bound to
                        the agent's TLS certificate (``binding``, see
                        handshake.py). ``host_id`` comes from the host record.
    ``dialed=False`` -> atos/edge: the agent dialed the server; it VERIFIES
                        the incoming token via ``verify``.

    Returns ``(host_id, session)``. The session is the mux CLIENT (odd ids).
    """
    if dialed:
        if token is None:
            raise ValueError("stoa/server-dial requires the host token")
        await stoa_server_handshake(transport, token, binding)
        resolved = host_id or ""
    else:
        if verify is None:
            raise ValueError("atos/server-accept requires a token verifier")
        resolved = await acceptor_handshake(transport, verify)
    # The control plane only opens streams; it never accepts agent-opened ones.
    session = Session(transport, is_client=True, accept_streams=False).start()
    return resolved, session


async def establish_agent_side(transport, *, dialed: bool, token: str,
                               binding: str = "") -> Session:
    """Establish the agent end of a tunnel.

    ``dialed=True``  -> atos/edge: the agent dialed the server; it SENDS
                        ``token``.
    ``dialed=False`` -> stoa/direct: the server dialed the agent; each side
                        proves it holds ``token`` without sending it, bound to
                        this agent's TLS certificate (``binding``).

    Returns the mux SERVER session (even ids). Callers serve inbound host-ops
    opened by the control-plane via ``session.accept_stream()`` (see
    ``agent_ops_server.serve_agent_ops``).
    """
    if dialed:
        await dialer_handshake(transport, token)
    else:
        await stoa_agent_handshake(transport, token, binding)
    return Session(transport, is_client=False).start()
