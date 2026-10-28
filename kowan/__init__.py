"""kowan — the Kowan Docker-management platform.

Three entrypoints share this package: ``kowan.server`` (the control plane),
``kowan.agent`` (runs on every Docker host) and ``kowan.cli`` (``kowanctl``).
They are built on an in-house stream multiplexer (``kowan.mux``) carried over
a message transport (``kowan.transport``) and the token-authenticated host
tunnel (``kowan.tunnel``).
"""

# The release version, the single source for the package metadata, the CLI,
# the agent and the MCP server.
__version__ = "0.1.11"
