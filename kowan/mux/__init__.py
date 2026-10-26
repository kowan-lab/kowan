"""In-house symmetric stream multiplexer.

Both endpoints can open independent, flow-controlled streams over a single
connection — the property the reverse-tunnel needs (control-plane opens
streams for host ops; the agent opens streams to push events).
"""

from .session import Session
from .stream import Stream, StreamReset, StreamClosed, DEFAULT_WINDOW

__all__ = ["Session", "Stream", "StreamReset", "StreamClosed", "DEFAULT_WINDOW"]
