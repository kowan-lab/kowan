"""Agent-side Docker ops server: accept inbound mux streams and serve each one.

The agent doesn't run an HTTP server for Docker ops; instead it holds one
end of the reverse-tunnel :class:`kowan.mux.Session` and the control plane opens
a fresh mux stream per Docker operation. This module is the agent's accept loop:
for every stream the control plane opens, it runs
:func:`kowan.agentproto.serve_ops` against the agent's local
:class:`kowan.docker.base.Ops` (typically a ``LocalClient`` over the Docker
Engine socket).

Wire the ``ops`` backend in when constructing the agent; this loop is transport-
agnostic and only needs a mux ``session`` whose ``accept_stream`` yields the
control plane's per-op streams.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import agentproto

log = logging.getLogger("kowan.agent")


async def serve_agent_ops(session: Any, ops: Any) -> None:
    """Accept inbound mux streams on ``session`` and serve each with ``ops``.

    Loops calling ``session.accept_stream()`` (which returns ``None`` when the
    session ends) and handles every accepted stream concurrently with
    :func:`agentproto.serve_ops`. Each handler serves exactly one op and then
    resets its stream, so a slow/hung op can never block others.

    Returns when the session ends (``accept_stream`` yields ``None``).
    """
    tasks: set[asyncio.Task] = set()
    try:
        while True:
            stream = await session.accept_stream()
            if stream is None:
                break  # session closed
            task = asyncio.ensure_future(handle_ops_stream(stream, ops))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()


async def handle_ops_stream(stream: Any, ops: Any) -> None:
    """Serve one op on ``stream`` then tear the stream down.

    The body of :func:`serve_agent_ops`'s accept loop. Exceptions are logged,
    never raised, so one bad request can't kill the accept loop or the session
    reader.
    """
    try:
        await agentproto.serve_ops(stream, ops)
    except Exception as exc:  # noqa: BLE001 - defensive: keep the loop alive
        log.warning("agent ops stream failed: %r", exc)
    finally:
        # Half-close (FIN) so the client's read side sees a clean EOF. We use
        # close() rather than reset(): a RST can arrive before the client has
        # drained buffered response bytes and would surface as StreamReset
        # instead of the data (mux checks the reset flag before the recv buffer).
        # FIN is idempotent and never truncates. serve_ops already FINs
        # streaming responses; a second close() here is a no-op.
        try:
            await stream.close()
        except Exception:  # noqa: BLE001
            pass
