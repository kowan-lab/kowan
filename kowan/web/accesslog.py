"""Access log that never writes credentials.

Browsers cannot set headers on WebSocket upgrades, so log/stats/events/exec
streams authenticate with ``?token=<jwt>``. aiohttp's default access log prints
the full request line, which put live session tokens into the logs. This logger
keeps the same information with sensitive query values replaced by ``***``,
and so are the tokens that public webhooks carry in their path.
"""

from __future__ import annotations

import re
from typing import Any

from aiohttp.abc import AbstractAccessLogger

_SENSITIVE = re.compile(r"(?i)([?&](?:token|access_token|api_key|apikey|key|secret|password|code)=)[^&#\s]*")


# Public webhooks carry their secret in the path: /hooks/{token},
# /hooks/deploy/{token}, /hooks/workflow/{token}, /hooks/gitops/{token}.
_HOOK_TOKEN = re.compile(r"^(/hooks/(?:[a-z]+/)?)[^/?#]+")


def redact(target: str) -> str:
    """Replace sensitive query values and webhook path tokens with ``***``."""
    return _SENSITIVE.sub(r"\1***", _HOOK_TOKEN.sub(r"\1***", target))


class RedactingAccessLogger(AbstractAccessLogger):
    def log(self, request: Any, response: Any, time: float) -> None:
        remote = request.remote or "-"
        self.logger.info(
            '%s "%s %s HTTP/%d.%d" %s %s %.3fs',
            remote,
            request.method,
            redact(request.path_qs),
            request.version.major,
            request.version.minor,
            response.status,
            response.body_length,
            time,
        )
