"""Alert and alert-rule REST API, plus the rule-type catalog and metrics query.

Mounted under ``/api/v1`` via
:func:`register`.

Routes (read routes are open to any authenticated principal; mutating routes
require the ``operator`` role — admins are always allowed):

    GET    /alerts                     list recent alerts (?limit)
    POST   /alerts/{id}/resolve        mark an alert resolved            [operator]
    GET    /alert-rule-types           the rule-type catalog (drives the UI)
    GET    /alert-rules                list alert rules
    GET    /alert-rules/{id}           one alert rule
    POST   /alert-rules                create a rule                     [operator]
    PUT    /alert-rules/{id}           update a rule                     [operator]
    DELETE /alert-rules/{id}           delete a rule                     [operator]

The ``GET /hosts/{hostID}/metrics`` time-series query lives in
:mod:`kowan.api.system`, not here.

All handlers operate on ``svc.store``; the Monitor's in-memory state is never
touched from the API.
"""

from __future__ import annotations

import logging
import math

from kowan.auth import authorize as authz
import uuid
from datetime import datetime, timezone

from aiohttp import web

from kowan.auth import require_role
from kowan.errors import NotFound, Validation
from kowan.models import AlertRule
from kowan.models.alert import (
    rule_type_by_name,
    rule_types,
    valid_level,
    valid_operator,
)
from kowan.web import SERVICES_KEY, api_route, json_response, read_json

log = logging.getLogger("kowan.monitor.api")


def _now() -> str:
    """RFC3339 UTC timestamp (second precision, ``Z`` suffix)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _svc(request: web.Request):
    return request.app[SERVICES_KEY]


def register(app: web.Application, svc) -> None:
    """Mount the alert/alert-rule/metrics routes under ``/api/v1``.

    Read routes are viewer-accessible; mutating routes require ``operator``
    (admins always allowed).
    """
    # Alerts.
    api_route(app, "GET", "/alerts", _list_alerts)
    api_route(app, "POST", "/alerts/{id}/resolve", require_role("operator")(_resolve_alert))

    # Alert rules.
    api_route(app, "GET", "/alert-rule-types", _list_rule_types)
    api_route(app, "GET", "/alert-rules", _list_alert_rules)
    api_route(app, "GET", "/alert-rules/{id}", _get_alert_rule)
    api_route(app, "POST", "/alert-rules", require_role("operator")(_create_alert_rule))
    api_route(app, "PUT", "/alert-rules/{id}", require_role("operator")(_update_alert_rule))
    api_route(app, "DELETE", "/alert-rules/{id}", require_role("operator")(_delete_alert_rule))
    # NOTE: GET /hosts/{hostID}/metrics is NOT registered here; it lives in
    # kowan/api/system.py (with host-existence 404 and RFC3339 ?since 400).


# --- rule types -----------------------------------------------------------


async def _list_rule_types(request: web.Request) -> web.Response:
    """Return the catalog of supported alert rule types with display metadata and
    defaults. The frontend rule builder is driven entirely from this."""
    return json_response(rule_types())


# --- alerts ---------------------------------------------------------------


async def _list_alerts(request: web.Request) -> web.Response:
    """Return recent alerts, newest first. Optional ``?limit`` caps the count."""
    svc = _svc(request)
    limit = _parse_limit(request.query.get("limit", ""))
    alerts = authz.filter_host_items(svc.store.list_alerts(limit))
    return json_response(alerts)


async def _resolve_alert(request: web.Request) -> web.Response:
    """Mark an alert resolved."""
    svc = _svc(request)
    alert_id = request.match_info.get("id", "")
    if alert_id == "":
        raise Validation("missing alert id")
    ctx = authz.current()
    if ctx is not None and not ctx.unrestricted:
        match = [a for a in svc.store.list_alerts(1000) if a.id == alert_id]
        if match and not authz.host_item_visible(match[0].host_id or ""):
            raise authz._forbidden("insufficient permissions for this alert")
    svc.store.resolve_alert(alert_id)
    return json_response({"status": "resolved"})


# --- alert rules ----------------------------------------------------------


async def _list_alert_rules(request: web.Request) -> web.Response:
    """Return all configured alert rules."""
    svc = _svc(request)
    return json_response(authz.filter_host_items(svc.store.list_alert_rules()))


async def _get_alert_rule(request: web.Request) -> web.Response:
    """Return a single alert rule by id (404 if absent)."""
    svc = _svc(request)
    rule_id = request.match_info["id"]
    rule = _load_rule(svc, rule_id)
    return json_response(rule)


async def _create_alert_rule(request: web.Request) -> web.Response:
    """Create a new alert rule. New rules default to auto-resolve + enabled on
    unless the client says otherwise."""
    svc = _svc(request)
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")

    rule = AlertRule(
        id=str(uuid.uuid4()),
        autoResolve=True,
        enabled=True,
        createdAt=_now(),
    )
    _apply(body, rule)  # raises Validation on invalid input
    authz.check_host_item(rule.host_id, authz.LEVEL_MANAGE)
    svc.store.create_alert_rule(rule)
    return json_response(rule, status=201)


async def _update_alert_rule(request: web.Request) -> web.Response:
    """Update the mutable fields of an existing alert rule.

    Absent body fields leave the stored value untouched, so the UI can flip a
    single field (e.g. the enabled toggle) without resubmitting the whole rule."""
    svc = _svc(request)
    rule_id = request.match_info["id"]

    existing = _load_rule(svc, rule_id)

    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")

    _apply(body, existing)  # raises Validation on invalid input
    authz.check_host_item(existing.host_id, authz.LEVEL_MANAGE)
    svc.store.update_alert_rule(existing)
    return json_response(existing)


async def _delete_alert_rule(request: web.Request) -> web.Response:
    """Remove an alert rule by id (404 if absent)."""
    svc = _svc(request)
    rule_id = request.match_info["id"]
    _load_rule(svc, rule_id)  # 404 precheck
    svc.store.delete_alert_rule(rule_id)
    svc.store.delete_rule_runbook(rule_id)
    return json_response({"status": "deleted"})


# --- request body -> rule -------------------------------------------------


def _apply(body: dict, rule: AlertRule) -> None:
    """Mutate ``rule`` in place with whichever request fields were provided, then
    normalize/validate. Raises :class:`Validation` on invalid input.

    Accepts camelCase keys (the wire form). Only keys present in ``body`` are
    applied, so a partial update leaves the rest untouched.
    """
    if "name" in body and body["name"] is not None:
        rule.name = str(body["name"]).strip()
    if "type" in body and body["type"] is not None:
        rule.type = str(body["type"])
    if "operator" in body and body["operator"] is not None:
        rule.operator = str(body["operator"])
    if "threshold" in body and body["threshold"] is not None:
        try:
            rule.threshold = float(body["threshold"])
        except (TypeError, ValueError):
            raise Validation("threshold must be a number")
        # Reject nan/inf: they persist happily but later blow up the monitor's
        # message formatting (int(nan) → ValueError), which would kill the loop.
        if not math.isfinite(rule.threshold):
            raise Validation("threshold must be a finite number")
    if "severity" in body and body["severity"] is not None:
        rule.severity = str(body["severity"])
    if "forDuration" in body and body["forDuration"] is not None:
        try:
            rule.for_duration = int(body["forDuration"])
        except (TypeError, ValueError):
            raise Validation("forDuration must be an integer")
    if "hostId" in body and body["hostId"] is not None:
        rule.host_id = str(body["hostId"])
    if "match" in body and body["match"] is not None:
        rule.match = str(body["match"]).strip()
    if "labelKey" in body and body["labelKey"] is not None:
        rule.label_key = str(body["labelKey"]).strip()
    if "labelValue" in body and body["labelValue"] is not None:
        rule.label_value = str(body["labelValue"]).strip()
    if "channelId" in body and body["channelId"] is not None:
        rule.channel_id = str(body["channelId"])
    if "autoResolve" in body and body["autoResolve"] is not None:
        rule.auto_resolve = bool(body["autoResolve"])
    if "enabled" in body and body["enabled"] is not None:
        rule.enabled = bool(body["enabled"])

    # Normalize and validate.
    if rule.name == "":
        raise Validation("name is required")
    spec, ok = rule_type_by_name(rule.type)
    if not ok or spec is None:
        raise Validation("invalid rule type")
    if rule.operator == "":
        rule.operator = spec.default_operator
    if spec.numeric and not valid_operator(rule.operator):
        raise Validation("invalid operator")
    if rule.severity != "" and not valid_level(rule.severity):
        raise Validation("invalid severity")
    if rule.for_duration < 0:
        raise Validation("duration cannot be negative")


# --- helpers --------------------------------------------------------------


def _load_rule(svc, rule_id: str) -> AlertRule:
    """Fetch an alert rule or raise :class:`NotFound` (=> 404)."""
    try:
        rule = svc.store.get_alert_rule(rule_id)
    except Exception as err:  # store raises store.NotFoundError
        raise NotFound("alert rule not found") from err
    authz.check_host_item(rule.host_id)  # team/grant scoping
    return rule


def _parse_limit(s: str) -> int:
    """Parse an optional ``?limit``; a missing or invalid value yields 0, which
    the store treats as its default."""
    if s == "":
        return 0
    try:
        n = int(s)
    except ValueError:
        return 0
    return n if n >= 0 else 0
