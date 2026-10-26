"""Wire frames for the Kowan stream multiplexer.

The mux is deliberately tiny — a symmetric, credit-flow-controlled stream
multiplexer inspired by hashicorp/yamux. One frame is carried per transport
*message* (see ``kowan.transport``), so we do not need a length-delimited
framer: the transport gives us message boundaries for free (a WebSocket binary message == one frame).

Frame layout (big-endian, 10-byte header + optional payload)::

    0        1        2                   6                   10
    +--------+--------+-------------------+-------------------+
    |  type  | flags  |     stream_id     |      length       |  payload...
    +--------+--------+-------------------+-------------------+

  type      DATA | WINDOW_UPDATE | GO_AWAY
  flags     bitmask of SYN | FIN | RST
  stream_id uint32, 0 = session-level (GO_AWAY)
  length    DATA: payload byte count; WINDOW_UPDATE: credit delta
"""

from __future__ import annotations

import struct

_HEADER = struct.Struct("!BBII")
HEADER_LEN = _HEADER.size  # 10 bytes

# ---- frame types ----
TYPE_DATA = 0
TYPE_WINDOW_UPDATE = 1
TYPE_GO_AWAY = 3

# ---- flags (bitmask) ----
FLAG_SYN = 0x1  # open a new stream
FLAG_FIN = 0x4  # half-close: no more data will be sent in this direction
FLAG_RST = 0x8  # abort the stream


def encode(typ: int, flags: int, stream_id: int, length: int = 0, payload: bytes = b"") -> bytes:
    """Encode one frame. For DATA, ``length`` is derived from ``payload``."""
    if payload:
        length = len(payload)
    return _HEADER.pack(typ, flags, stream_id, length) + payload


def decode(data: bytes):
    """Decode one framed message into ``(type, flags, stream_id, length, payload)``."""
    if len(data) < HEADER_LEN:
        raise ValueError(f"short frame: {len(data)} bytes")
    typ, flags, stream_id, length = _HEADER.unpack_from(data, 0)
    payload = data[HEADER_LEN:]
    return typ, flags, stream_id, length, payload
