"""Deploy an image registry (``registry:3``) on a host and add it to the
registry list.

The registry speaks plain HTTP on the host's Traefik network; Traefik
publishes it on a domain with a Let's Encrypt certificate, the same way the
Exposure page publishes an app. Docker trusts that certificate everywhere, so
nothing is installed on any machine. The domain's DNS must point at the host
and ports 80 and 443 be open to the internet; with no Traefik on the host the
deploy is refused (the UI offers to deploy one first).

The login is optional: with it, an htpasswd user (bcrypt, see bcrypt.py)
guards pushes and pulls; the file goes into a named volume Kowan fills before
the deploy, so the hash is not in the compose file. Without it anyone who
reaches the registry can push and pull.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Set

import yaml

from kowan import errors
from kowan.models import Credential, CredentialKindRegistry, Deployment
from kowan.models.container import VolumeCreateOptions

from . import bcrypt

log = logging.getLogger("kowan.hostedregistry")

IMAGE = "registry:3"
SERVICE = "registry"
CONTAINER_PORT = 5000
ROLE_LABEL = "kowan.registry.role"
DEFAULT_USER = "kowan"
READY_TIMEOUT = 300.0

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_tasks: Set[asyncio.Task] = set()


class _Bytes:
    """An in-memory :class:`~kowan.docker.base.AsyncByteStream`."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        if n < 0 or n >= len(self._data):
            out, self._data = self._data, b""
        else:
            out, self._data = self._data[:n], self._data[n:]
        return out

    def __aiter__(self):
        return self._chunks()

    async def _chunks(self):
        while self._data:
            yield await self.read(64 << 10)

    async def close(self) -> None:
        self._data = b""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def config_volume(name: str) -> str:
    return f"kowan-registry-{name}-config"


def compose_for(*, auth: bool, domain: str, volume: str) -> Dict[str, Any]:
    env: Dict[str, str] = {"REGISTRY_STORAGE_DELETE_ENABLED": "true", "REGISTRY_HTTP_HOST": f"https://{domain}"}
    svc: Dict[str, Any] = {
        "image": IMAGE,
        "restart": "unless-stopped",
        "environment": env,
        "volumes": ["data:/var/lib/registry"],
        "labels": {ROLE_LABEL: "registry"},
    }
    doc: Dict[str, Any] = {"services": {SERVICE: svc}, "volumes": {"data": {}}}
    if auth:
        env.update({"REGISTRY_AUTH": "htpasswd", "REGISTRY_AUTH_HTPASSWD_REALM": "Kowan registry",
                    "REGISTRY_AUTH_HTPASSWD_PATH": "/config/htpasswd"})
        svc["volumes"].append("config:/config:ro")
        doc["volumes"]["config"] = {"name": volume, "external": True}
    return doc


def _validate(body: Dict[str, Any]) -> Dict[str, Any]:
    from kowan.exposure import labels as lbl

    name = str(body.get("name") or "registry").strip().lower()
    if not _NAME_RE.match(name):
        raise errors.Validation("name: lowercase letters, digits, - and _ (up to 41 characters)")
    domain = lbl.normalize_domain(str(body.get("domain") or ""))
    if not lbl.valid_domain(domain):
        raise errors.Validation("a valid domain is required, e.g. registry.example.com")
    out: Dict[str, Any] = {"name": name, "domain": domain, "proxyId": str(body.get("proxyId") or ""),
                           "auth": body.get("auth", True) is not False}
    if out["auth"]:
        user = str(body.get("username") or DEFAULT_USER).strip()
        if not _USER_RE.match(user):
            raise errors.Validation("username: letters, digits, . _ - (up to 64)")
        password = str(body.get("password") or "")
        out.update(username=user, password=password or secrets.token_urlsafe(24), generated=not password)
        if len(out["password"]) < 8:
            raise errors.Validation("password: at least 8 characters")
    return out


def _check_free(svc: Any, name: str, url_host: str) -> None:
    try:
        svc.store.get_deployment_by_name(name)
        raise errors.Conflict(f"a deployment named {name!r} already exists")
    except errors.Conflict:
        raise
    except Exception:  # noqa: BLE001 - name is free
        pass
    for c in svc.store.list_credentials(CredentialKindRegistry):
        if c.name == name:
            raise errors.Conflict(f"a registry named {name!r} already exists")
        url = str((c.meta or {}).get("url") or "")
        if re.sub(r"^https?://", "", url).rstrip("/") == url_host:
            raise errors.Conflict(f"the registry list already has {url_host} ({c.name})")


async def _upload(ops: Any, volume: str, files: Dict[str, str]) -> None:
    for fname, text in files.items():
        data = text.encode("utf-8")
        await ops.upload_volume_file(volume, "/", fname, len(data), _Bytes(data))


async def deploy(svc: Any, host: Any, body: Dict[str, Any], author: str) -> Dict[str, Any]:
    from kowan.exposure import compose as composemod
    from kowan.exposure import service as exposure

    if not isinstance(body, dict):
        raise errors.Validation("invalid request body")
    req = _validate(body)
    name, domain = req["name"], req["domain"]
    _check_free(svc, name, domain)

    view = await exposure.host_exposure(svc, host)
    proxy = exposure._pick_proxy(view, req["proxyId"])
    labels, net = exposure.plan_labels(proxy, domain=domain, port=CONTAINER_PORT, router_base=name,
                                       target_networks=[])
    if not net["network"]:
        raise errors.Conflict("the proxy has no network a registry can join")
    volume = config_volume(name)
    compose = yaml.safe_dump(compose_for(auth=req["auth"], domain=domain, volume=volume),
                             sort_keys=False, default_flow_style=False)
    compose = composemod.expose(compose, SERVICE, labels, net["network"])

    if req["auth"]:
        line = await asyncio.to_thread(bcrypt.htpasswd_line, req["username"], req["password"])
        data = (line + "\n").encode("utf-8")
        ops = svc.docker.ops(host)
        await ops.create_volume(VolumeCreateOptions(name=volume, labels={ROLE_LABEL: "config"}))
        await ops.upload_volume_file(volume, "/", "htpasswd", len(data), _Bytes(data))

    now = _now()
    dep = Deployment(id=str(uuid.uuid4()), name=name, type="compose", strategy="standalone",
                     targets=[host.id], compose=compose, status="stopped", createdAt=now, updatedAt=now)
    svc.store.create_deployment(dep, source="registry", author=author)
    cred = Credential(
        id=str(uuid.uuid4()), kind=CredentialKindRegistry, type="generic", name=name,
        meta={"url": f"https://{domain}", "username": req.get("username", ""), "insecure": False,
              "verified": False, "lastError": "", "hasPassword": req["auth"], "deploymentId": dep.id},
        createdAt=now, updatedAt=now,
    )
    svc.store.create_credential(cred, {"password": req["password"]} if req["auth"] else {})

    await svc.deployments.dispatch_deploy(dep.id)
    task = asyncio.create_task(_verify_when_ready(svc, dep.id, cred.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    out: Dict[str, Any] = {"deploymentId": dep.id, "registryId": cred.id, "name": name, "url": f"https://{domain}",
                           "address": domain, "auth": req["auth"], "proxy": proxy["name"]}
    if req["auth"]:
        out["username"] = req["username"]
        if req["generated"]:
            out["password"] = req["password"]  # shown once; stored encrypted with the registry
    return out


async def _verify_when_ready(svc: Any, dep_id: str, cred_id: str) -> None:
    """Once the deploy ends, check the registry answers and record it."""
    from kowan.api.credentials import _registry_validate_and_persist

    loop = asyncio.get_running_loop()
    deadline = loop.time() + READY_TIMEOUT
    while loop.time() < deadline:
        try:
            status = svc.store.get_deployment(dep_id).status
        except Exception:  # noqa: BLE001 - deleted meanwhile
            return
        if status in ("deployed", "error", "failed"):
            break
        await asyncio.sleep(2)
    # The container starts listening a moment after compose returns, and
    # Let's Encrypt takes a few seconds to issue the certificate.
    for _ in range(10):
        try:
            res = await _registry_validate_and_persist(svc, cred_id)
        except Exception as err:  # noqa: BLE001
            log.warning("verify hosted registry %s: %s", cred_id, err)
            return
        if res.get("verified"):
            return
        await asyncio.sleep(6)
