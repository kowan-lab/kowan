"""Import existing stacks as deployments.

Routes (under /api/v1):
  POST /import/portainer/scan  {url, apiKey | username+password, verifyTls}  (admin)
  POST /import/compose/scan    {files: {relative path: text}}                (operator)
  POST /import/apply           {items: [{name, host, strategy, compose | git}]} (operator)

A scan returns candidates (nothing is stored); apply creates one deployment
per item through the same checks as POST /deployments, without deploying.
Portainer credentials are used for the scan only. Scanning Portainer is
admin-only because it makes the server call a URL of the caller's choosing.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from aiohttp import web

from kowan.api.deployments import create_from_body
from kowan.auth import require_role
from kowan.auth.middleware import user_from
from kowan.deployment import gitsource
from kowan.errors import AppError, Validation
from kowan.importer import compose_dir, portainer
from kowan.models import Deployment
from kowan.web import api_route, json_response, read_json, reject_unknown_fields
from kowan.web.app import SERVICES_KEY

MAX_FILES = 500
MAX_ITEMS = 100


def register(app: web.Application, svc) -> None:
    api_route(app, "POST", "/import/portainer/scan", require_role("admin")(_scan_portainer))
    api_route(app, "POST", "/import/compose/scan", require_role("operator")(_scan_compose))
    api_route(app, "POST", "/import/apply", require_role("operator")(_apply))


def _env_text(env: Dict[str, str]) -> str:
    return "".join(f"{k}={v}\n" for k, v in env.items())


def _suggest_host(hosts: List[Any], wanted: str) -> str:
    for h in hosts:
        if wanted and (h.name.lower() == wanted.lower() or h.id == wanted):
            return h.id
    return hosts[0].id if hosts else ""


def _taken(svc) -> set:
    return {d.resource_name() for d in svc.store.list_deployments()}


async def _body(request: web.Request) -> Dict[str, Any]:
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    return body


async def _scan_portainer(request: web.Request) -> web.Response:
    svc = request.app[SERVICES_KEY]
    body = await _body(request)
    reject_unknown_fields(body, {"url", "apiKey", "username", "password", "verifyTls"})
    try:
        client = portainer.Client(str(body.get("url") or ""), api_key=str(body.get("apiKey") or ""),
                                  username=str(body.get("username") or ""), password=str(body.get("password") or ""),
                                  verify=body.get("verifyTls", True) is not False)
    except ValueError as err:
        raise Validation(str(err)) from None
    try:
        stacks = await client.stacks()
    except portainer.PortainerError as err:
        raise Validation(str(err)) from None
    hosts = svc.store.list_hosts()
    taken = _taken(svc)
    out = []
    for s in stacks:
        warnings: List[str] = []
        item: Dict[str, Any] = {
            "key": f"portainer-{s.id}", "source": "portainer", "name": s.name, "kind": s.kind,
            "endpoint": s.endpoint, "active": s.active, "envCount": len(s.env),
            "host": _suggest_host(hosts, s.endpoint), "strategy": "swarm" if s.kind == "swarm" else "standalone",
            "compose": "", "git": None, "importable": s.kind != "kubernetes",
        }
        if s.kind == "kubernetes":
            warnings.append("Kubernetes stacks are not supported")
        elif s.git:
            item["git"] = s.git
            if s.git_auth:
                warnings.append("private repository: pick a git credential after importing")
            if s.env:
                warnings.append(f"{len(s.env)} environment variables stay in Portainer: add them to the repository's .env")
        else:
            item["compose"] = gitsource.render_compose(s.compose, _env_text(s.env))
        if Deployment(name=s.name).resource_name() in taken:
            warnings.append("a deployment with this name exists already")
        item["warnings"] = warnings
        out.append(item)
    return json_response(out)


async def _scan_compose(request: web.Request) -> web.Response:
    svc = request.app[SERVICES_KEY]
    body = await _body(request)
    reject_unknown_fields(body, {"files"})
    files = body.get("files")
    if not isinstance(files, dict) or not files:
        raise Validation("files must map relative paths to their text")
    if len(files) > MAX_FILES:
        raise Validation(f"at most {MAX_FILES} files")
    texts = {}
    for path, text in files.items():
        if not isinstance(text, str) or len(text) > compose_dir.MAX_FILE:
            raise Validation(f"{path}: not text, or larger than 1 MiB")
        texts[str(path)] = text
    hosts = svc.store.list_hosts()
    taken = _taken(svc)
    out = []
    for g in compose_dir.group(texts):
        warnings = []
        if Deployment(name=g["name"]).resource_name() in taken:
            warnings.append("a deployment with this name exists already")
        out.append({
            "key": f"folder-{g['folder']}", "source": "folder", "name": g["name"], "kind": "compose",
            "endpoint": f"{g['folder'] or '.'}/{g['file']}", "active": True,
            "envCount": len([line for line in g["env"].splitlines() if "=" in line and not line.lstrip().startswith("#")]),
            "host": _suggest_host(hosts, ""), "strategy": "standalone",
            "compose": gitsource.render_compose(g["compose"], g["env"]), "git": None, "importable": True,
            "warnings": warnings,
        })
    return json_response(out)


async def _apply(request: web.Request) -> web.Response:
    svc = request.app[SERVICES_KEY]
    body = await _body(request)
    reject_unknown_fields(body, {"items"})
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise Validation("items must be a non-empty list")
    if len(items) > MAX_ITEMS:
        raise Validation(f"at most {MAX_ITEMS} items at once")
    u = user_from(request)
    author = getattr(u, "username", "") if u else ""
    results = []
    for it in items:
        if not isinstance(it, dict):
            results.append({"name": "", "ok": False, "error": "invalid item"})
            continue
        name = str(it.get("name") or "")
        req: Dict[str, Any] = {"name": name, "type": "compose", "strategy": str(it.get("strategy") or "standalone"),
                               "targets": [str(it.get("host") or "")]}
        git = it.get("git")
        if isinstance(git, dict) and git.get("repoUrl"):
            req["git"] = {"repoUrl": str(git.get("repoUrl")), "ref": str(git.get("ref") or "main"),
                          "path": str(git.get("path") or "docker-compose.yml"), "autoDeploy": False}
        else:
            req["compose"] = str(it.get("compose") or "")
        try:
            out = await create_from_body(svc, req, author=author, source="import")
        except AppError as err:
            results.append({"name": name, "ok": False, "error": str(err)})
            continue
        if isinstance(out, web.Response):  # the compose gate refused it
            results.append({"name": name, "ok": False, "error": json.loads(out.body)["error"]})
            continue
        results.append({"name": name, "ok": True, "id": out.id})
    return json_response(results)
