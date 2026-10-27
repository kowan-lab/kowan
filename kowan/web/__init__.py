"""HTTP plumbing + application skeleton for the Kowan control plane.

Public surface (the contract every api module depends on):

  from kowan.web import (
      Services, create_app, register helpers,
      api_route, add_public_route, require_role,
      REQUEST_USER_KEY, API_PREFIX,
      json_response, error_response, read_json,
  )
"""

from __future__ import annotations

from .app import (
    API_PREFIX,
    REQUEST_ID_KEY,
    REQUEST_USER_KEY,
    SERVICES_KEY,
    Services,
    add_public_route,
    add_spa,
    add_webhooks,
    api_route,
    bind_svc,
    create_app,
    now_rfc3339,
    require_role,
    user_from,
)
from .audit import audit_middleware, client_ip
from .responses import (
    JSONError,
    error_response,
    ops_error_response,
    json_response,
    read_body,
    read_json,
    reject_unknown_fields,
)

__all__ = [
    "API_PREFIX",
    "REQUEST_ID_KEY",
    "REQUEST_USER_KEY",
    "SERVICES_KEY",
    "Services",
    "add_public_route",
    "add_spa",
    "add_webhooks",
    "api_route",
    "bind_svc",
    "create_app",
    "now_rfc3339",
    "require_role",
    "user_from",
    "audit_middleware",
    "client_ip",
    "JSONError",
    "error_response",
    "ops_error_response",
    "json_response",
    "read_body",
    "read_json",
    "reject_unknown_fields",
]
