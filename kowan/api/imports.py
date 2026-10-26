"""Import existing stacks as deployments.

Routes (under /api/v1):
  POST /import/portainer/scan  {connectionId}                               (admin)
  POST /import/compose/scan    {files: {relative path: text}}                (operator)
  POST /import/apply           {items: [{name, host, strategy, compose | git, variables?}]} (operator)

A scan returns candidates (nothing is stored); apply creates one deployment
per item through the same checks as POST /deployments, without deploying.
Portainer is reached through a saved ``portainer`` connection (Credentials),
so its address and token stay encrypted and are picked, not typed, on each
import. Scanning Portainer is admin-only because it makes the server call a
URL of the caller's choosing.

A stack's variables (a Portainer stack's environment, a folder's ``.env``)
become the deployment's stack variables: the compose stays as written, with
its ``${VAR}``s. The scan proposes each as config or secret (secret when the
name or value looks sensitive); a secret one is kept encrypted in the
deployment's own secret (kowan/deployment/envsecrets.py), as from the form.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from aiohttp import web

from kowan.api.deployments import create_from_body
from kowan.auth import require_role
from kowan.auth.middleware import user_from
from kowan.deployment import gitsource
from kowan.errors import AppError, NotFound, Validation
from kowan.importer import compose_dir, portainer
from kowan.models import Deployment
from kowan.models.credential import CredentialKindConnection
from kowan.web import api_route, json_response, read_json, reject_unknown_fields
from kowan.web.app import SERVICES_KEY

MAX_FILES = 500
MAX_ITEMS = 100

_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_NAME = re.compile(r"PASS|PWD|SECRET|TOKEN|KEY|CREDENTIAL|AUTH|SALT|PRIVATE|DSN|CONNECTION_STRING", re.I)
_URL_WITH_CREDENTIALS = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.I)


def looks_secret(name: str, value: str) -> bool:
    """Whether a variable is proposed as a secret: by its name, or a URL
    carrying a user and password."""
    return bool(_SENSITIVE_NAME.search(name) or _URL_WITH_CREDENTIALS.match(value))


def register(app: web.Application, svc) -> None:
    api_route(app, "POST", "/import/portainer/scan", require_role("admin")(_scan_portainer))
    api_route(app, "POST", "/import/compose/scan", require_role("operator")(_scan_compose))
    api_route(app, "POST", "/import/apply", require_role("operator")(_apply))


def _env_text(env: Dict[str, str]) -> str:
    return "".join(f"{k}={v}\n" for k, v in env.items())


def _split_env(compose: str, env: Dict[str, str]) -> "tuple[str, List[Dict[str, Any]]]":
    """The compose (with only names a variable cannot carry written in) and
    the stack variables, each proposed as config or secret."""
    odd = {k: v for k, v in env.items() if not _VAR_NAME.match(k)}
    if odd:
        compose = gitsource.render_compose(compose, _env_text(odd))
    variables = [{"key": k, "value": v, "secret": looks_secret(k, v)} for k, v in env.items() if k not in odd]
    return compose, variables


def _portainer_connection(svc, conn_id: Any) -> Dict[str, str]:
    """The decrypted fields of a saved ``portainer`` connection."""
    if not isinstance(conn_id, str) or not conn_id:
        raise Validation("connectionId is required: pick a Portainer connection")
    try:
        cred = svc.store.get_credential(conn_id)
    except Exception:  # noqa: BLE001 - store.NotFoundError
        raise NotFound("connection not found") from None
    if cred.kind != CredentialKindConnection or cred.type != "portainer":
        raise Validation("the connection is not a Portainer connection")
    return {k: str(v) for k, v in svc.store.credential_config(conn_id).items()}


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
    reject_unknown_fields(body, {"connectionId"})
    conn = _portainer_connection(svc, body.get("connectionId"))
    try:
        client = portainer.Client(conn.get("url", ""), api_key=conn.get("apiKey", ""),
                                  username=conn.get("username", ""), password=conn.get("password", ""),
                                  verify=conn.get("insecure", "").strip().lower() not in ("true", "yes", "1"))
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
            item["compose"], item["variables"] = _split_env(s.compose, s.env)
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
        env = gitsource.parse_env_file(g["env"]) if g["env"].strip() else {}
        compose, variables = _split_env(g["compose"], env)
        out.append({
            "key": f"folder-{g['folder']}", "source": "folder", "name": g["name"], "kind": "compose",
            "endpoint": f"{g['folder'] or '.'}/{g['file']}", "active": True, "envCount": len(env),
            "host": _suggest_host(hosts, ""), "strategy": "standalone",
            "compose": compose, "variables": variables, "git": None, "importable": True,
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
        variables = it.get("variables")
        if variables is not None and not req.get("git"):
            if not isinstance(variables, list):
                results.append({"name": name, "ok": False, "error": "variables must be a list"})
                continue
            req["variables"] = variables
        try:
            out = await create_from_body(svc, req, author=author, source="import")
        except AppError as err:
            out = web.json_response({"error": str(err)})
        if isinstance(out, web.Response):  # refused: by a check or by the compose gate
            results.append({"name": name, "ok": False, "error": json.loads(out.body)["error"]})
            continue
        results.append({"name": name, "ok": True, "id": out.id})
    return json_response(results)
