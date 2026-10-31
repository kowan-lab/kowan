"""Auto-update policy routes + the background updater's lifecycle.

  GET    /autoupdate/settings                     global settings
  PUT    /autoupdate/settings                     (admin) {enabled, intervalSeconds,
                                                  defaultScanBlock, verifyTimeoutSeconds, stableSeconds}
  GET    /autoupdate/overview                     every object with a policy (stored
                                                  or label) + state, next window, last result
  GET    /autoupdate/history                      ?kind&hostId&target&outcome&limit&offset
  POST   /autoupdate/check                        (operator) {kind?, hostId?, target?}: run a
                                                  check now (background), honouring policies
  GET    /hosts/{hostID}/containers/{id}/autoupdate   effective policy (with label sources),
                                                  stored policy, state, recent history
  PUT    /hosts/{hostID}/containers/{id}/autoupdate   (operator) store {mode, window, minAge, scanBlock}
  DELETE /hosts/{hostID}/containers/{id}/autoupdate   (operator) drop the stored policy
  GET|PUT|DELETE /deployments/{id}/autoupdate     same for a deployment

Container policies are stored by container NAME (ids change on every recreate).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from aiohttp import web

from kowan.autoupdate.policy import (
    LABEL_MIN_AGE,
    LABEL_NOTIFY,
    LABEL_SCAN_BLOCK,
    LABEL_UPDATE,
    LABEL_WINDOW,
    is_default_policy,
    parse_window,
    resolve_policy,
    validate_policy_body,
)
from kowan.autoupdate.runner import DEFAULT_SETTINGS, MIN_INTERVAL, SETTINGS_KEY, AutoUpdater, load_settings, rfc3339, now_utc
from kowan.auth import authorize as authz
from kowan.scan.gate import parse_block_policy
from kowan.web import SERVICES_KEY, api_route, error_response, json_response, ops_error_response, read_json, require_role

log = logging.getLogger("kowan.api.autoupdate")

KOWAN_LABELS = (LABEL_UPDATE, LABEL_WINDOW, LABEL_MIN_AGE, LABEL_SCAN_BLOCK, LABEL_NOTIFY)


def register(app: web.Application, svc: Any) -> None:
    if getattr(svc, "autoupdater", None) is None:
        svc.attach("autoupdater", AutoUpdater(svc))
    api_route(app, "GET", "/autoupdate/settings", _get_settings)
    api_route(app, "PUT", "/autoupdate/settings", require_role("admin")(_put_settings))
    api_route(app, "GET", "/autoupdate/overview", _overview)
    api_route(app, "GET", "/autoupdate/groups", _list_groups)
    api_route(app, "PUT", "/autoupdate/groups/{tag}", require_role("operator")(_put_group))
    api_route(app, "DELETE", "/autoupdate/groups/{tag}", require_role("operator")(_delete_group))
    api_route(app, "GET", "/autoupdate/history", _history)
    api_route(app, "POST", "/autoupdate/check", require_role("operator")(_check))
    base = "/hosts/{hostID}/containers/{id}/autoupdate"
    api_route(app, "GET", base, _get_container)
    api_route(app, "PUT", base, require_role("operator")(_put_container))
    api_route(app, "DELETE", base, require_role("operator")(_delete_container))
    dbase = "/deployments/{id}/autoupdate"
    api_route(app, "GET", dbase, _get_deployment)
    api_route(app, "PUT", dbase, require_role("operator")(_put_deployment))
    api_route(app, "DELETE", dbase, require_role("operator")(_delete_deployment))

    async def _start(_app: web.Application) -> None:
        svc.autoupdater.start()

    async def _stop(_app: web.Application) -> None:
        await svc.autoupdater.stop()

    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)


def _svc(request: web.Request) -> Any:
    return request.app[SERVICES_KEY]


def _targets(o: Dict[str, Any]) -> List[tuple]:
    """A policy / state / history row's target, for team/grant scoping."""
    if o.get("kind") == "deployment":
        return [(authz.KIND_DEPLOYMENT, "", o.get("target", ""))]
    return [(authz.KIND_CONTAINER, o.get("hostId", ""), o.get("target", ""))]


KIND_GROUP = "group"


def _group_targets(store: Any, tag: str) -> List[tuple]:
    """A host group's policy acts on every host in it: host-wide access to
    each (an empty group: fleet-wide only)."""
    hosts = store.hosts_with_tag(tag)
    return [(authz.KIND_HOST, h, "") for h in hosts] or [(authz.KIND_DEPLOYMENT, "", "")]


async def _list_groups(request: web.Request) -> web.StreamResponse:
    """Every host group with its hosts and its policy (null: none)."""
    svc = _svc(request)
    tags: Dict[str, List[str]] = {}
    for h in authz.filter_hosts(svc.store.list_hosts()):
        for t in h.tags:
            tags.setdefault(t, []).append(h.id)
    out = []
    for tag in sorted(tags):
        p = svc.store.get_autoupdate_policy(KIND_GROUP, "", tag)
        out.append({"tag": tag, "hosts": tags[tag], "stored": p, "effective": resolve_policy({}, p, svc.autoupdater.defaults()).to_dict()})
    return json_response(out)


async def _put_group(request: web.Request) -> web.StreamResponse:
    """The policy of a host group: the default of every container and
    deployment on its hosts that has no policy of its own."""
    svc = _svc(request)
    from kowan.api.hosts import check_tags

    try:
        [tag] = check_tags([request.match_info["tag"]])
    except Exception as err:  # noqa: BLE001
        return error_response(400, str(err))
    await authz.TargetAccess(svc.store, svc.docker).check(_group_targets(svc.store, tag), authz.LEVEL_MANAGE)
    try:
        p = validate_policy_body(await _body(request))
    except ValueError as err:
        return error_response(400, str(err))
    if is_default_policy(p):
        svc.store.delete_autoupdate_policy(KIND_GROUP, "", tag)
    else:
        svc.store.upsert_autoupdate_policy({**p, "kind": KIND_GROUP, "hostId": "", "target": tag,
                                            "updatedAt": rfc3339(now_utc()), "updatedBy": _user(request)})
    return json_response(svc.store.get_autoupdate_policy(KIND_GROUP, "", tag))


async def _delete_group(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    tag = request.match_info["tag"]
    await authz.TargetAccess(svc.store, svc.docker).check(_group_targets(svc.store, tag), authz.LEVEL_MANAGE)
    svc.store.delete_autoupdate_policy(KIND_GROUP, "", tag)
    return json_response({"status": "deleted"})


def _user(request: web.Request) -> str:
    try:
        from kowan.auth.middleware import user_from

        u = user_from(request)
        return getattr(u, "username", "") or ""
    except Exception:  # noqa: BLE001
        return ""


async def _body(request: web.Request) -> Dict[str, Any]:
    data = await read_json(request)
    if not isinstance(data, dict):
        raise ValueError("body must be an object")
    return data


def _window_info(window: str) -> Dict[str, Any]:
    try:
        w = parse_window(window)
    except ValueError as err:
        return {"valid": False, "error": str(err)}
    if w is None:
        return {"valid": True, "always": True}
    now = datetime.now()
    nxt = w.next_open(now)
    return {"valid": True, "always": False, "open": w.is_open(now), "describe": w.describe(),
            "nextOpen": nxt.astimezone().isoformat(timespec="minutes") if nxt else ""}


# -- settings -------------------------------------------------------------------

async def _get_settings(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    cfg = load_settings(svc.store)
    return json_response({**cfg, "lastTickAt": svc.autoupdater.last_tick_at, "minIntervalSeconds": MIN_INTERVAL})


async def _put_settings(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    try:
        body = await _body(request)
        cfg = load_settings(svc.store)
        for k in DEFAULT_SETTINGS:
            if k in body:
                cfg[k] = body[k]
        cfg["enabled"] = bool(cfg["enabled"])
        cfg["intervalSeconds"] = int(cfg["intervalSeconds"])
        if cfg["intervalSeconds"] < MIN_INTERVAL:
            raise ValueError(f"intervalSeconds must be at least {MIN_INTERVAL}")
        cfg["defaultScanBlock"] = parse_block_policy(cfg["defaultScanBlock"])
        cfg["verifyTimeoutSeconds"] = max(15, min(1800, int(cfg["verifyTimeoutSeconds"])))
        cfg["stableSeconds"] = max(0, min(600, int(cfg["stableSeconds"])))
    except (ValueError, TypeError) as err:
        return error_response(400, str(err))
    svc.store.set_setting(SETTINGS_KEY, json.dumps(cfg))
    return json_response(load_settings(svc.store))


# -- overview / history ------------------------------------------------------------

async def _overview(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    store = svc.store
    au: AutoUpdater = svc.autoupdater
    defaults = au.defaults()
    hosts = {h.id: h.name for h in store.list_hosts()}
    deps = {}
    try:
        deps = {d.id: d for d in store.list_deployments()}
    except Exception:  # noqa: BLE001
        pass
    objs: Dict[tuple, Dict[str, Any]] = {}
    for p in store.list_autoupdate_policies():
        # A row equal to the defaults (left by an older knob save) shows like no policy.
        # Host-group policies are listed by /autoupdate/groups.
        if p["kind"] != KIND_GROUP and not is_default_policy(p):
            objs[(p["kind"], p["hostId"], p["target"])] = {"stored": p}
    for st in store.list_autoupdate_states():
        objs.setdefault((st["kind"], st["hostId"], st["target"]), {})["state"] = st["data"]
    out: List[Dict[str, Any]] = []
    for (kind, host_id, target), v in objs.items():
        state = v.get("state") or {}
        stored = v.get("stored")
        eff = state.get("policy")
        if not eff or stored:
            # The state's policy carries label sources; refresh from stored when present.
            if eff and stored:
                eff = dict(eff)
                for k in ("mode", "window", "minAge", "scanBlock"):
                    if (eff.get("sources") or {}).get(k) != "label":
                        merged = resolve_policy({}, stored, defaults).to_dict()
                        eff[k] = merged[k]
                        eff.setdefault("sources", {})[k] = merged["sources"][k]
            else:
                eff = resolve_policy({}, stored, defaults).to_dict()
        if (eff.get("mode") == "off" and not stored
                and state.get("lastResult") not in ("blocked", "rolled_back", "failed")):
            continue  # e.g. a one-off manual update of a container without policy
        hid = host_id or state.get("hostId", "")
        name = target
        if kind == "deployment":
            d = deps.get(target)
            if d is None and not state:
                continue
            name = d.name if d is not None else state.get("name", target)
            if d is not None and not hid and d.targets:
                hid = d.targets[0]
        last = store.last_autoupdate_history(kind, host_id, target)
        out.append({
            "kind": kind, "hostId": hid, "hostName": hosts.get(hid, hid), "target": target, "name": name,
            "stored": stored, "effective": eff, "window": _window_info(eff.get("window", "")),
            "lastCheckAt": state.get("lastCheckAt", ""), "lastResult": state.get("lastResult", ""),
            "lastMessage": state.get("lastMessage", ""), "available": bool(state.get("available")),
            "pending": bool(state.get("pending")), "image": state.get("image", ""),
            "nextCheckAt": au.next_check_at(hid) if hid else None, "last": last,
        })
    out = await authz.TargetAccess(store, svc.docker).filter(out, _targets)
    out.sort(key=lambda o: (o["hostName"] or "", o["kind"], o["name"]))
    cfg = load_settings(store)
    return json_response({"settings": cfg, "lastTickAt": au.last_tick_at, "objects": out})


async def _history(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    q = request.query
    try:
        limit = int(q.get("limit", "50"))
        offset = int(q.get("offset", "0"))
    except ValueError:
        return error_response(400, "limit/offset must be integers")
    where = dict(kind=q.get("kind", ""), host_id=q.get("hostId", ""), target=q.get("target", ""),
                 outcome=q.get("outcome", ""))
    access = authz.TargetAccess(svc.store, svc.docker)
    if access.unrestricted:
        return json_response(svc.store.list_autoupdate_history(**where, limit=limit, offset=offset))
    # Scoped: filter the whole (pruned, so bounded) history, then page it.
    rows: List[Dict[str, Any]] = []
    page_offset = 0
    while True:
        page = svc.store.list_autoupdate_history(**where, limit=500, offset=page_offset)["items"]
        rows += await access.filter(page, _targets)
        if len(page) < 500:
            break
        page_offset += 500
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    return json_response({"items": rows[offset:offset + limit], "total": len(rows)})


_CHECKS: set = set()


async def _check(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    body: Dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await _body(request)
        except Exception:  # noqa: BLE001
            body = {}
    kind = str(body.get("kind") or "")
    host_id = str(body.get("hostId") or "")
    target = str(body.get("target") or "")
    au: AutoUpdater = svc.autoupdater
    hosts = authz.filter_hosts(svc.store.list_hosts())
    if kind and target:
        # An update replaces containers: operate on a deployment (a redeploy),
        # manage on a lone container (remove + create).
        level = authz.LEVEL_OPERATE if kind == "deployment" else authz.LEVEL_MANAGE
        await authz.TargetAccess(svc.store, svc.docker).check(
            _targets({"kind": kind, "hostId": host_id, "target": target}), level)
    if kind == "deployment" and target and not host_id:
        try:
            d = svc.store.get_deployment(target)
            host_id = (d.targets or [""])[0]
        except Exception:  # noqa: BLE001
            return error_response(404, "deployment not found")
    if host_id:
        hosts = [h for h in hosts if h.id == host_id]
        if not hosts:
            return error_response(404, "host not found")
    only = (kind, target) if kind and target else None

    async def run() -> None:
        for h in hosts:
            try:
                await au.process_host(h, check_registry=True, only=only, trigger="manual")
            except Exception as err:  # noqa: BLE001
                log.warning("autoupdate check %s: %s", h.id, err)

    task = asyncio.ensure_future(run())
    _CHECKS.add(task)
    task.add_done_callback(_CHECKS.discard)
    return json_response({"started": True, "hosts": [h.id for h in hosts]}, status=202)


# -- container policy -----------------------------------------------------------

async def _container_ctx(svc: Any, host_id: str, cid: str):
    host = svc.store.get_host(host_id)
    ops = svc.docker.ops(host)
    insp = await ops.inspect_container(cid) or {}
    name = str(insp.get("Name") or "").lstrip("/")
    labels = (insp.get("Config") or {}).get("Labels") or {}
    return ops, insp, name, labels


async def _get_container(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    host_id = request.match_info["hostID"]
    try:
        _, insp, name, labels = await _container_ctx(svc, host_id, request.match_info["id"])
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    stored = svc.store.get_autoupdate_policy("container", host_id, name)
    eff = resolve_policy(labels, stored, svc.autoupdater.defaults())
    hist = svc.store.list_autoupdate_history(kind="container", host_id=host_id, target=name, limit=20)
    owner = labels.get("kowan.deployment.id", "")
    return json_response({
        "kind": "container", "hostId": host_id, "target": name,
        "labels": {k: v for k, v in labels.items() if k in KOWAN_LABELS},
        "stored": stored, "effective": eff.to_dict(), "window": _window_info(eff.window),
        "state": svc.store.get_autoupdate_state("container", host_id, name),
        "history": hist["items"], "deploymentId": owner,
        "managedByDeployment": bool(owner and svc.store.get_autoupdate_policy("deployment", "", owner)
                                    and LABEL_UPDATE not in labels),
    })


async def _put_container(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    host_id = request.match_info["hostID"]
    try:
        _, _, name, labels = await _container_ctx(svc, host_id, request.match_info["id"])
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    if not name:
        return error_response(404, "container not found")
    # Auto-update recreates the container from the background, unrestricted.
    await authz.TargetAccess(svc.store, svc.docker).check(
        [(authz.KIND_CONTAINER, host_id, name)], authz.LEVEL_MANAGE)
    try:
        p = validate_policy_body(await _body(request))
    except ValueError as err:
        return error_response(400, str(err))
    if is_default_policy(p):
        svc.store.delete_autoupdate_policy("container", host_id, name)
    else:
        svc.store.upsert_autoupdate_policy({**p, "kind": "container", "hostId": host_id, "target": name,
                                            "updatedAt": rfc3339(now_utc()), "updatedBy": _user(request)})
    return await _get_container(request)


async def _delete_container(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    host_id = request.match_info["hostID"]
    try:
        _, _, name, _ = await _container_ctx(svc, host_id, request.match_info["id"])
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    await authz.TargetAccess(svc.store, svc.docker).check(
        [(authz.KIND_CONTAINER, host_id, name)], authz.LEVEL_MANAGE)
    svc.store.delete_autoupdate_policy("container", host_id, name)
    return await _get_container(request)


# -- deployment policy ------------------------------------------------------------

async def _get_deployment(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    dep_id = request.match_info["id"]
    try:
        dep = svc.store.get_deployment(dep_id)
    except Exception:  # noqa: BLE001
        return error_response(404, "deployment not found")
    authz.check_deployment(dep, authz.LEVEL_READ)
    stored = svc.store.get_autoupdate_policy("deployment", "", dep_id)
    eff = resolve_policy({}, stored, svc.autoupdater.defaults())
    hist = svc.store.list_autoupdate_history(kind="deployment", target=dep_id, limit=20)
    return json_response({
        "kind": "deployment", "hostId": (dep.targets or [""])[0], "target": dep_id, "name": dep.name,
        "labels": {}, "stored": stored, "effective": eff.to_dict(), "window": _window_info(eff.window),
        "state": svc.store.get_autoupdate_state("deployment", "", dep_id), "history": hist["items"],
        "swarm": dep.strategy == "swarm",
    })


async def _put_deployment(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    dep_id = request.match_info["id"]
    try:
        dep = svc.store.get_deployment(dep_id)
    except Exception:  # noqa: BLE001
        return error_response(404, "deployment not found")
    authz.check_deployment(dep, authz.LEVEL_OPERATE)  # an update is a redeploy
    try:
        p = validate_policy_body(await _body(request))
    except ValueError as err:
        return error_response(400, str(err))
    if is_default_policy(p):
        svc.store.delete_autoupdate_policy("deployment", "", dep_id)
    else:
        svc.store.upsert_autoupdate_policy({**p, "kind": "deployment", "hostId": "", "target": dep_id,
                                            "updatedAt": rfc3339(now_utc()), "updatedBy": _user(request)})
    return await _get_deployment(request)


async def _delete_deployment(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    dep_id = request.match_info["id"]
    try:
        dep = svc.store.get_deployment(dep_id)
    except Exception:  # noqa: BLE001
        return error_response(404, "deployment not found")
    authz.check_deployment(dep, authz.LEVEL_OPERATE)
    svc.store.delete_autoupdate_policy("deployment", "", dep_id)
    return await _get_deployment(request)
