"""Token-authenticated, bidirectional host tunnel built on the mux.

Two connection modes, one unified transport above them:

  * ``atos`` ("agent-to-server", edge / behind NAT): the AGENT dials out to
    the control-plane.
  * ``stoa`` ("server-to-agent", direct): the control-plane dials the
    agent's ip.

Only the establishment differs (who dials, who reconnects, and — for stoa —
the agent ip to reach). Once a session exists, the control-plane drives
every host identically via ``Hub.dial(host_id)``, and either side may open
streams (server -> agent host-ops, agent -> server event push).
"""

from kowan._lazy import lazy_exports

_EXPORTS = {
    ".endpoint": (
        "establish_server_side",
        "establish_agent_side",
    ),
    ".handshake": (
        "HandshakeError",
    ),
    ".hub": (
        "Hub",
    ),
}

__getattr__, __dir__ = lazy_exports(__name__, _EXPORTS)

__all__ = [
    "establish_server_side",
    "establish_agent_side",
    "HandshakeError",
    "Hub",
]
