"""Uptime checks REST API and the public status page.

Routes (under /api/v1):
  GET    /uptime/checks                   list, with uptime and latency stats
  POST   /uptime/checks                   create (operator)
  GET    /uptime/checks/{id}              one check
  PUT    /uptime/checks/{id}              edit (operator)
  DELETE /uptime/checks/{id}              delete with its history (operator)
  POST   /uptime/checks/{id}/run          probe now (operator)
  GET    /uptime/checks/{id}/results      results of the last ``hours`` (default 24)
  GET    /uptime/incidents                down periods, newest first (?check=)
  GET    /uptime/status-page              status page settings (admin)
  PUT    /uptime/status-page              (admin)
  GET    /public/status                   PUBLIC: the status page data, when enabled

The runner (kowan/uptime/runner.py) starts with the app.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from aiohttp import web

from kowan.auth import require_role
from kowan.auth.middleware import user_from
from kowan.errors import Conflict, NotFound, Validation
from kowan.redact import strip_userinfo
from kowan.uptime import probe
from kowan.uptime.runner import UptimeRunner
from kowan.web import add_public_route, api_route, json_response, read_json, reject_unknown_fields
from kowan.web.app import API_PREFIX, SERVICES_KEY

_RUNNER_KEY = web.AppKey("uptime_runner", object)

_FIELDS = {
    "name", "kind", "target", "method", "expectStatus", "keyword", "verifyTls", "intervalSec",
    "timeoutSec", "failThreshold", "deploymentId", "channelId", "level", "enabled", "public",
}
_LEVELS = ("info", "warning", "critical")

STATUS_PAGE_KEYS = ("status_page.enabled", "status_page.title", "status_page.description")


def register(app: web.Application, svc) -> None:
    runner = UptimeRunner(svc)
    svc.attach("uptime", runner)

    op = require_role("operator")
    admin = require_role("admin")
    api_route(app, "GET", "/uptime/checks", _list)
    api_route(app, "POST", "/uptime/checks", op(_create))
    api_route(app, "GET", "/uptime/checks/{id}", _get)
    api_route(app, "PUT", "/uptime/checks/{id}", op(_update))
    api_route(app, "DELETE", "/uptime/checks/{id}", op(_delete))
    api_route(app, "POST", "/uptime/checks/{id}/run", op(_run))
    api_route(app, "GET", "/uptime/checks/{id}/results", _results)
    api_route(app, "GET", "/uptime/incidents", _incidents)
    api_route(app, "GET", "/uptime/status-page", admin(_get_page))
    api_route(app, "PUT", "/uptime/status-page", admin(_put_page))
    add_public_route(app, "GET", API_PREFIX + "/public/status", _public_status)

    async def _start(_app: web.Application) -> None:
        runner.start()
        _app[_RUNNER_KEY] = runner

    async def _stop(_app: web.Application) -> None:
        await runner.stop()

    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)


def _svc(request: web.Request):
    return request.app[SERVICES_KEY]


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _can_operate(request: web.Request) -> bool:
    u = user_from(request)
    return u is not None and u.role in ("admin", "operator")


def _out(store, c: Dict[str, Any], *, operator: bool, recent: bool = True) -> Dict[str, Any]:
    day = store.uptime_stats(c["id"], _ago(hours=24))
    out: Dict[str, Any] = {
        "id": c["id"],
        "name": c["name"],
        "kind": c["kind"],
        "target": c["target"] if operator else strip_userinfo(c["target"]),
        "method": c["method"],
        "expectStatus": c["expect_status"],
        "keyword": c["keyword"],
        "verifyTls": c["verify_tls"],
        "intervalSec": c["interval_sec"],
        "timeoutSec": c["timeout_sec"],
        "failThreshold": c["fail_threshold"],
        "deploymentId": c["deployment_id"],
        "channelId": c["channel_id"],
        "level": c["level"],
        "enabled": c["enabled"],
        "public": c["public"],
        "createdAt": c["created_at"],
        "updatedAt": c["updated_at"],
        "status": c["status"] if c["enabled"] else "paused",
        "statusSince": c["status_since"],
        "lastCheckedAt": c["last_checked_at"],
        "lastLatencyMs": c["last_latency_ms"],
        "lastCode": c["last_code"],
        "lastError": c["last_error"],
        "certExpiresAt": c["cert_expires_at"],
        "uptime24h": day["uptime"],
        "uptime7d": store.uptime_stats(c["id"], _ago(days=7))["uptime"],
        "uptime30d": store.uptime_stats(c["id"], _ago(days=30))["uptime"],
        "latency24h": day["latencyMs"],
    }
    if recent:
        out["recent"] = store.list_uptime_results(c["id"], _ago(days=30), limit=40)
    return out


def _load(store, check_id: str) -> Dict[str, Any]:
    try:
        return store.get_uptime_check(check_id)
    except Exception as err:  # store.NotFoundError
        raise NotFound("uptime check not found") from err


async def _list(request: web.Request) -> web.Response:
    store = _svc(request).store
    op = _can_operate(request)
    return json_response([_out(store, c, operator=op) for c in store.list_uptime_checks()])


async def _get(request: web.Request) -> web.Response:
    store = _svc(request).store
    return json_response(_out(store, _load(store, request.match_info["id"]), operator=_can_operate(request)))


def _int(body: Dict[str, Any], key: str, default: int, lo: int, hi: int, label: str) -> int:
    v = body.get(key, default)
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise Validation(f"{label} must be a number") from None
    if not lo <= n <= hi:
        raise Validation(f"{label} must be between {lo} and {hi}")
    return n


def _parse(svc, body: Any, cur: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    reject_unknown_fields(body, _FIELDS)
    base = cur or {}
    name = str(body.get("name", base.get("name", "")) or "").strip()
    if not name:
        raise Validation("name is required")
    kind = str(body.get("kind", base.get("kind", "http")) or "http")
    if kind not in ("http", "tcp"):
        raise Validation("kind must be http or tcp")
    target = str(body.get("target", base.get("target", "")) or "").strip()
    try:
        if kind == "http":
            probe.validate_http_target(target)
        else:
            probe.parse_tcp_target(target)
    except ValueError as err:
        raise Validation(str(err)) from None
    method = str(body.get("method", base.get("method", "GET")) or "GET").upper()
    if method not in ("GET", "HEAD"):
        raise Validation("method must be GET or HEAD")
    expect = str(body.get("expectStatus", base.get("expect_status", "200-399")) or "200-399").replace(" ", "")
    try:
        probe.parse_expect(expect)
    except ValueError as err:
        raise Validation(str(err)) from None
    interval = _int(body, "intervalSec", base.get("interval_sec", 60), 10, 86400, "interval")
    timeout = _int(body, "timeoutSec", base.get("timeout_sec", 10), 1, 60, "timeout")
    if timeout >= interval:
        raise Validation("timeout must be shorter than the interval")
    level = str(body.get("level", base.get("level", "critical")) or "critical")
    if level not in _LEVELS:
        raise Validation("level must be info, warning or critical")
    channel = str(body.get("channelId", base.get("channel_id", "")) or "")
    if channel:
        try:
            cred = svc.store.get_credential(channel)
        except Exception:  # noqa: BLE001
            cred = None
        if cred is None or cred.kind != "channel":
            raise Validation("unknown notification channel")
    dep = str(body.get("deploymentId", base.get("deployment_id", "")) or "")
    if dep:
        try:
            svc.store.get_deployment(dep)
        except Exception:  # noqa: BLE001
            raise Validation("unknown deployment") from None
    return {
        "name": name, "kind": kind, "target": target, "method": method, "expect_status": expect,
        "keyword": str(body.get("keyword", base.get("keyword", "")) or "") if kind == "http" else "",
        "verify_tls": bool(body.get("verifyTls", base.get("verify_tls", True))),
        "interval_sec": interval, "timeout_sec": timeout,
        "fail_threshold": _int(body, "failThreshold", base.get("fail_threshold", 2), 1, 10, "failure threshold"),
        "deployment_id": dep, "channel_id": channel, "level": level,
        "enabled": bool(body.get("enabled", base.get("enabled", True))),
        "public": bool(body.get("public", base.get("public", False))),
    }


def _name_taken(store, name: str, exclude: str) -> bool:
    return any(c["name"] == name and c["id"] != exclude for c in store.list_uptime_checks())


async def _create(request: web.Request) -> web.Response:
    svc = _svc(request)
    c = _parse(svc, await read_json(request), None)
    if _name_taken(svc.store, c["name"], ""):
        raise Conflict("a check with that name already exists")
    now = probe.now_rfc3339()
    c.update(id=str(uuid.uuid4()), created_at=now, updated_at=now)
    svc.store.create_uptime_check(c)
    svc.uptime.reschedule(c["id"])
    return json_response(_out(svc.store, svc.store.get_uptime_check(c["id"]), operator=True), status=201)


async def _update(request: web.Request) -> web.Response:
    svc = _svc(request)
    cur = _load(svc.store, request.match_info["id"])
    c = _parse(svc, await read_json(request), cur)
    if _name_taken(svc.store, c["name"], cur["id"]):
        raise Conflict("a check with that name already exists")
    c.update(id=cur["id"], updated_at=probe.now_rfc3339())
    svc.store.update_uptime_check(c)
    if not c["enabled"] and cur["status"] == "down":
        # Paused while down: close the outage instead of leaving it open forever.
        at = probe.now_rfc3339()
        svc.store.close_uptime_incident(cur["id"], at)
        if cur["alert_id"]:
            svc.store.resolve_alert(cur["alert_id"])
        svc.store.save_uptime_state(cur["id"], {"status": "unknown", "status_since": at, "fails": 0, "alert_id": ""})
    svc.uptime.reschedule(cur["id"])
    return json_response(_out(svc.store, svc.store.get_uptime_check(cur["id"]), operator=True))


async def _delete(request: web.Request) -> web.Response:
    svc = _svc(request)
    cur = _load(svc.store, request.match_info["id"])
    if cur["alert_id"]:
        svc.store.resolve_alert(cur["alert_id"])
    svc.store.delete_uptime_check(cur["id"])
    return web.Response(status=204)


async def _run(request: web.Request) -> web.Response:
    svc = _svc(request)
    cur = _load(svc.store, request.match_info["id"])
    res = await svc.uptime.run(cur["id"])
    return json_response({
        "ok": res.ok, "latencyMs": round(res.latency_ms, 1), "code": res.code, "error": res.error,
        "check": _out(svc.store, svc.store.get_uptime_check(cur["id"]), operator=True),
    })


async def _results(request: web.Request) -> web.Response:
    store = _svc(request).store
    cur = _load(store, request.match_info["id"])
    try:
        hours = int(request.query.get("hours", "24"))
    except ValueError:
        raise Validation("hours must be a number") from None
    hours = max(1, min(hours, 24 * 90))
    return json_response(store.list_uptime_results(cur["id"], _ago(hours=hours), limit=5000))


async def _incidents(request: web.Request) -> web.Response:
    store = _svc(request).store
    check = request.query.get("check", "")
    if check:
        _load(store, check)
    return json_response(store.list_uptime_incidents(check or None, limit=100))


def _page(store) -> Dict[str, Any]:
    return {
        "enabled": store.get_setting("status_page.enabled") == "1",
        "title": store.get_setting("status_page.title") or "Service status",
        "description": store.get_setting("status_page.description"),
    }


async def _get_page(request: web.Request) -> web.Response:
    return json_response(_page(_svc(request).store))


async def _put_page(request: web.Request) -> web.Response:
    store = _svc(request).store
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    reject_unknown_fields(body, {"enabled", "title", "description"})
    title = str(body.get("title", "") or "").strip()
    if len(title) > 80:
        raise Validation("title is too long (80 characters at most)")
    desc = str(body.get("description", "") or "").strip()
    if len(desc) > 500:
        raise Validation("description is too long (500 characters at most)")
    store.set_setting("status_page.enabled", "1" if body.get("enabled") else "0")
    store.set_setting("status_page.title", title)
    store.set_setting("status_page.description", desc)
    return json_response(_page(store))


async def _public_status(request: web.Request) -> web.Response:
    """The status page data: public checks only, without their targets."""
    store = _svc(request).store
    page = _page(store)
    if not page["enabled"]:
        raise NotFound("status page is not enabled")
    since30 = _ago(days=30)
    checks = []
    for c in store.list_uptime_checks():
        if not c["public"] or not c["enabled"]:
            continue
        checks.append({
            "id": c["id"],
            "name": c["name"],
            "status": c["status"],
            "statusSince": c["status_since"],
            "uptime30d": store.uptime_stats(c["id"], since30)["uptime"],
            "latencyMs": c["last_latency_ms"] if c["status"] == "up" else None,
            "days": store.uptime_daily(c["id"], since30),
        })
    ids = {c["id"] for c in checks}
    names = {c["id"]: c["name"] for c in checks}
    incidents = [
        {**i, "name": names[i["checkId"]]} for i in store.list_uptime_incidents(limit=200) if i["checkId"] in ids
    ][:20]
    for i in incidents:
        i.pop("error", None)  # error text can name internal hosts
    down = sum(1 for c in checks if c["status"] == "down")
    resp = json_response({
        "title": page["title"],
        "description": page["description"],
        "overall": "down" if down and down == len(checks) else "degraded" if down else "up" if checks else "unknown",
        "checks": checks,
        "incidents": incidents,
        "generatedAt": probe.now_rfc3339(),
    })
    resp.headers["Cache-Control"] = "public, max-age=30"
    return resp
