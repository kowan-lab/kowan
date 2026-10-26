"""Shared error types used across the Kowan application.

The common failure categories are modelled as typed exceptions so service-layer
code can raise them and the web layer maps them to the right HTTP status:

    NotFound      -> 404
    Conflict      -> 409
    Validation    -> 400
    Unauthorized  -> 401
    Forbidden     -> 403
    BadGateway    -> 502  (agent/upstream failures)
    Internal      -> 500

Error bodies always use the JSON envelope ``{"error": <message>}``.
"""

from __future__ import annotations

from http import HTTPStatus


class AppError(Exception):
    """Base class for application errors that map to an HTTP status code.

    ``status`` is the HTTP status returned to clients. Subclasses set a default;
    the message (``str(err)``) becomes the ``error`` field of the JSON envelope.
    """

    status: int = HTTPStatus.INTERNAL_SERVER_ERROR

    def __init__(self, message: str = "", *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status

    def to_dict(self) -> dict[str, str]:
        """Return the JSON error envelope ``{"error": msg}``."""
        return {"error": self.message or self.__class__.__name__}


class Validation(AppError):
    """Invalid or malformed input. Maps to 400 Bad Request."""

    status = HTTPStatus.BAD_REQUEST


class Unauthorized(AppError):
    """Missing or invalid credentials. Maps to 401 Unauthorized."""

    status = HTTPStatus.UNAUTHORIZED


class Forbidden(AppError):
    """Authenticated but not permitted. Maps to 403 Forbidden."""

    status = HTTPStatus.FORBIDDEN


class NotFound(AppError):
    """Requested resource does not exist. Maps to 404 Not Found."""

    status = HTTPStatus.NOT_FOUND


class Conflict(AppError):
    """Resource state conflict, e.g. duplicate name. Maps to 409 Conflict."""

    status = HTTPStatus.CONFLICT


class BadGateway(AppError):
    """Upstream (agent / Docker daemon) failure. Maps to 502 Bad Gateway."""

    status = HTTPStatus.BAD_GATEWAY


class Internal(AppError):
    """Unexpected server-side failure. Maps to 500 Internal Server Error."""

    status = HTTPStatus.INTERNAL_SERVER_ERROR


def error_response(err: BaseException) -> dict[str, str]:
    """Build the ``{"error": msg}`` JSON envelope for an exception.

    The body is always the error envelope. For unclassified exceptions the raw
    message is used.
    """
    if isinstance(err, AppError):
        return err.to_dict()
    return {"error": str(err) or err.__class__.__name__}
