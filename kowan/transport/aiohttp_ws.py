"""WebSocket transport adapter over an aiohttp WebSocket.

Wraps either end of an aiohttp WebSocket:

  * the *server/acceptor* side — an :class:`aiohttp.web.WebSocketResponse` an
    aiohttp handler produces after ``ws.prepare(request)``;
  * the *client/dialer* side — an :class:`aiohttp.ClientWebSocketResponse` from
    ``ClientSession.ws_connect`` (the server->agent dialer and the edge agent).

Both sides speak the same one-binary-message-per-mux-frame contract, so the mux
and tunnel don't care which end they run over.

There is no byte-stream connection here — the mux consumes whole *messages*,
so we expose ``send``/``recv``/``close`` directly against the WebSocket's
binary frames.
"""

from __future__ import annotations

import aiohttp

from .base import MessageTransport


# Largest WebSocket message a tunnel peer may send. Every message is one mux
# frame: a 10-byte header plus at most MAX_FRAME (16 KiB) of payload, so 64 KiB
# leaves room while stopping a peer from making us buffer an unbounded message.
MAX_MESSAGE_SIZE = 64 * 1024

# How long a peer gets to complete the token handshake after the upgrade.
HANDSHAKE_TIMEOUT = 10.0


class AiohttpWSTransport(MessageTransport):
    """Wraps an aiohttp WebSocket (``web.WebSocketResponse`` or client ``ws``).

    Accepts anything with aiohttp's WebSocket API: ``send_bytes``,
    ``receive`` (yielding ``WSMessage``) and ``close``. That covers both the
    server side (``aiohttp.web.WebSocketResponse``, already prepared) and the
    client side (``aiohttp.ClientWebSocketResponse``).
    """

    def __init__(self, ws) -> None:
        self._ws = ws

    async def send(self, data: bytes) -> None:
        await self._ws.send_bytes(bytes(data))

    async def recv(self) -> bytes | None:
        try:
            msg = await self._ws.receive()
        except Exception:
            return None  # transport error == EOF
        if msg.type == aiohttp.WSMsgType.BINARY:
            return msg.data
        if msg.type == aiohttp.WSMsgType.TEXT:
            # Tolerate text frames by encoding them; the handshake and mux only
            # ever send binary, so this is defensive.
            return msg.data.encode()
        # CLOSE / CLOSING / CLOSED / ERROR / any non-data frame == EOF.
        return None

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass
