"""Exposure orchestration against a host's ``Ops`` and the deployment service.

* :func:`host_exposure` builds the per-host view: published ports (scope +
  risk), proxy-routed domains parsed from labels, and the detected proxies.
* :func:`certificates` reads certificate status from the proxy's storage and,
  for Traefik with its API enabled, router status through exec.
* :func:`expose` / :func:`unexpose` publish a container or a deployment
  (container or compose service) on a domain: plain containers are recreated
  with a rollback-safe raw clone (``Ops.container_relabel``); deployments get
  their spec / compose file edited and are redeployed through the normal
  background deploy path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from kowan import errors
from kowan.models import Deployment

from . import certs as certmod
from . import classify
from . import compose as composemod
from . import labels as lbl
from . import proxy as proxymod

log = logging.getLogger("kowan.exposure")

COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"
DEP_ID = "kowan.deployment.id"
DEP_NAME = "kowan.deployment.name"


def _name(c: Any) -> str:
    names = getattr(c, "names", None) or []
    return (names[0] if names else getattr(c, "id", "")[:12]).lstrip("/")


def _dep_index(store: Any) -> Dict[str, Deployment]:
    """resource name / id -> deployment, to attribute containers."""
    out: Dict[str, Deployment] = {}
    try:
        for d in store.list_deployments():
            out["id:" + d.id] = d
            out["rn:" + d.resource_name()] = d
    except Exception:  # noqa: BLE001
        pass
    return out


def owner_of(labels: Dict[str, str], deps: Dict[str, Deployment]) -> Dict[str, str]:
    """Stack / deployment attribution of a container from its labels."""
    labels = labels or {}
    dep = None
    if labels.get(DEP_ID):
        dep = deps.get("id:" + labels[DEP_ID])
    project = labels.get(COMPOSE_PROJECT, "")
    if dep is None and project:
        dep = deps.get("rn:" + project)
    return {
        "stack": project or (dep.name if dep else "") or labels.get(DEP_NAME, ""),
        "service": labels.get(COMPOSE_SERVICE, ""),
        "deploymentId": dep.id if dep else labels.get(DEP_ID, ""),
        "deploymentName": dep.name if dep else labels.get(DEP_NAME, ""),
        "deploymentType": dep.type if dep else "",
    }


async def _inspect_many(ops: Any, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    sem = asyncio.Semaphore(8)

    async def one(cid: str) -> Tuple[str, Dict[str, Any]]:
        async with sem:
            try:
                return cid, await ops.inspect_container(cid) or {}
            except Exception:  # noqa: BLE001
                return cid, {}

    return dict(await asyncio.gather(*(one(i) for i in ids)))


def _networks_of(insp: Dict[str, Any]) -> List[str]:
    return sorted(((insp.get("NetworkSettings") or {}).get("Networks") or {}).keys())


async def describe_proxy(ops: Any, c: Any, insp: Dict[str, Any]) -> Dict[str, Any]:
    """Settings of one detected proxy container."""
    kind = proxymod.proxy_kind(c.image)
    cfg_block = insp.get("Config") or {}
    args = list(insp.get("Args") or [])
    if not args:
        args = list(cfg_block.get("Cmd") or [])
    env = list(cfg_block.get("Env") or [])
    out: Dict[str, Any] = {
        "kind": kind,
        "containerId": c.id,
        "name": _name(c),
        "image": c.image,
        "state": c.state,
        "networks": _networks_of(insp),
        "ports": [{"ip": p.ip, "publicPort": p.public_port, "privatePort": p.private_port,
                   "protocol": p.type} for p in (c.ports or []) if p.public_port],
    }
    flat = proxymod.flags_to_config(args, env)
    if not any(k.startswith("entrypoints.") or k.startswith("providers.") for k in flat):
        path = proxymod.config_file_path(flat) or "/etc/traefik/traefik.yml"
        for candidate in ([path] if proxymod.config_file_path(flat) else
                          ["/etc/traefik/traefik.yml", "/etc/traefik/traefik.yaml", "/traefik.yml"]):
            try:
                raw = await ops.container_read_file(c.id, candidate, 256 * 1024)
            except Exception:  # noqa: BLE001
                continue
            flat = proxymod.merge_file_config(flat, proxymod.parse_file(raw.decode("utf-8", "replace")))
            out["configFile"] = candidate
            break
    out.update(proxymod.traefik_settings(flat))
    return out


async def host_exposure(svc: Any, host: Any) -> Dict[str, Any]:
    """Everything reachable from outside on one host."""
    ops = svc.docker.ops(host)
    containers = await ops.list_containers(True)
    deps = _dep_index(svc.store)
    proxies_raw = [c for c in containers if proxymod.proxy_kind(c.image)]
    insp = await _inspect_many(ops, [c.id for c in proxies_raw])
    proxies = []
    for c in proxies_raw:
        try:
            proxies.append(await describe_proxy(ops, c, insp.get(c.id, {})))
        except Exception as err:  # noqa: BLE001
            log.debug("describe proxy %s: %s", c.id, err)
    running_proxy_kinds = {p["kind"] for p in proxies if p.get("state") == "running"}
    ports: List[Dict[str, Any]] = []
    domains: List[Dict[str, Any]] = []
    for c in sorted(containers, key=lambda x: _name(x)):
        own = owner_of(c.labels, deps)
        base = {"containerId": c.id, "containerName": _name(c), "image": c.image,
                "state": c.state, **own}
        seen = set()
        for p in c.ports or []:
            if not p.public_port:
                continue
            key = (p.ip, p.public_port, p.private_port, p.type)
            if key in seen:
                continue
            seen.add(key)
            cl = classify.classify_port(p.ip, p.private_port, c.image)
            ports.append({**base, "ip": p.ip or "0.0.0.0", "publicPort": p.public_port,
                          "privatePort": p.private_port, "protocol": p.type or "tcp", **cl})
        mark = lbl.exposed_by_port(c.labels)
        for r in lbl.routes(c.labels):
            domains.append({**base, **r,
                            "proxyRunning": r["proxy"] in running_proxy_kinds,
                            "managedByKowan": bool(mark and mark.get("domain") == r["domain"])})
    ports.sort(key=lambda p: (0 if p["warn"] else 1, {"public": 0, "interface": 1, "local": 2}[p["scope"]],
                              p["publicPort"], p["containerName"]))
    domains.sort(key=lambda d: (d["domain"], d["containerName"]))
    return {
        "hostId": host.id,
        "hostName": host.name or host.id,
        "ports": ports,
        "domains": domains,
        "proxies": proxies,
        "summary": {
            "publicPorts": sum(1 for p in ports if p["scope"] == "public"),
            "localPorts": sum(1 for p in ports if p["scope"] == "local"),
            "interfacePorts": sum(1 for p in ports if p["scope"] == "interface"),
            "riskyPublic": sum(1 for p in ports if p["warn"]),
            "domains": len(domains),
        },
    }


# ---------------------------------------------------------------------------
# certificates
# ---------------------------------------------------------------------------


async def _traefik_routers(ops: Any, proxy: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    if not proxy.get("apiEnabled"):
        return None
    addr = proxy.get("apiAddress") or ":8080"
    host, _, port = addr.rpartition(":")
    host = host or "127.0.0.1"
    if host in ("0.0.0.0", "[::]", "::"):
        host = "127.0.0.1"
    url = f"http://{host}:{port or '8080'}/api/http/routers"
    for cmd in (["wget", "-qO-", url], ["curl", "-fsS", url]):
        try:
            res = await ops.container_exec_status(proxy["containerId"], cmd)
        except Exception:  # noqa: BLE001
            return None
        if int(res.get("exitCode", 1)) == 0:
            try:
                doc = json.loads(res.get("output") or "[]")
                return doc if isinstance(doc, list) else None
            except ValueError:
                return None
    return None


async def certificates(svc: Any, host: Any) -> Dict[str, Any]:
    """Certificate + router status per domain, where the proxy lets us read it."""
    view = await host_exposure(svc, host)
    ops = svc.docker.ops(host)
    all_certs: List[Dict[str, Any]] = []
    routers: Dict[str, Dict[str, Any]] = {}
    notes: List[str] = []
    for p in view["proxies"]:
        if p.get("state") != "running":
            continue
        if p["kind"] == lbl.PROXY_TRAEFIK:
            paths = sorted({(r.get("storage") or "acme.json") for r in (p.get("resolvers") or {}).values()})
            for storage in paths:
                path = storage if storage.startswith("/") else "/" + storage
                try:
                    raw = await ops.container_read_file(p["containerId"], path, 8 << 20)
                    all_certs.extend(certmod.parse_acme_json(raw))
                except Exception as err:  # noqa: BLE001
                    notes.append(f"{p['name']}: cannot read {path} ({err})")
            rs = await _traefik_routers(ops, p)
            if rs is None:
                notes.append(f"{p['name']}: router status unavailable (Traefik API not enabled or not reachable from inside the container)")
            else:
                for r in rs:
                    nm = str(r.get("name", "")).split("@", 1)[0]
                    routers[nm] = {"status": r.get("status", ""), "errors": r.get("error") or [],
                                   "tls": r.get("tls") or {}}
    # de-duplicate by (domain, notAfter)
    uniq: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for c in all_certs:
        uniq[(c.get("domain", ""), c.get("notAfter", ""))] = c
    cert_list = sorted(uniq.values(), key=lambda c: c.get("domain", ""))
    rows = []
    for d in view["domains"]:
        c = certmod.match(d["domain"], cert_list) if d.get("tls") else None
        rows.append({
            "domain": d["domain"],
            "proxy": d["proxy"],
            "router": d["router"],
            "containerName": d["containerName"],
            "tls": d.get("tls", False),
            "certResolver": d.get("certResolver", ""),
            "routerStatus": routers.get(d["router"], {}).get("status", "") if d["proxy"] == lbl.PROXY_TRAEFIK else "",
            "routerErrors": routers.get(d["router"], {}).get("errors", []) if d["proxy"] == lbl.PROXY_TRAEFIK else [],
            "certificate": {k: c.get(k) for k in ("issuer", "notAfter", "daysLeft", "status", "staging", "resolver", "source")} if c else None,
        })
    return {"hostId": host.id, "domains": rows, "certificates": cert_list, "notes": notes}


# ---------------------------------------------------------------------------
# expose / unexpose
# ---------------------------------------------------------------------------


def _pick_proxy(view: Dict[str, Any], proxy_id: str) -> Dict[str, Any]:
    running = [p for p in view["proxies"] if p.get("state") == "running"]
    if proxy_id:
        for p in view["proxies"]:
            if p["containerId"] == proxy_id or p["containerId"].startswith(proxy_id) or p["name"] == proxy_id:
                return p
        raise errors.NotFound("proxy container not found on this host")
    if not running:
        raise errors.Conflict("no running Traefik on this host; deploy one first")
    # deterministic default: the first Traefik by name
    running.sort(key=lambda p: p["name"])
    return running[0]


def plan_labels(proxy: Dict[str, Any], *, domain: str, port: int, router_base: str,
                target_networks: List[str], entrypoint: str = "", tls: Optional[bool] = None,
                cert_resolver: Optional[str] = None) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Labels + network decision for exposing a target through ``proxy``."""
    net = proxymod.pick_network(proxy, target_networks)
    kind = proxy["kind"]
    ep = entrypoint or proxy.get("secureEntrypoint") or proxy.get("httpEntrypoint") or ""
    if tls is None:
        tls = bool(ep) and ep == proxy.get("secureEntrypoint")
    resolver = cert_resolver if cert_resolver is not None else (
        (proxy.get("certResolvers") or [""])[0] if tls else "")
    labels = lbl.expose_labels(
        kind, domain=domain, port=port, router=lbl.router_name(router_base),
        network=net["network"], entrypoint=ep, tls=bool(tls), cert_resolver=resolver or "",
        network_added=net["connect"],
    )
    return labels, net


def _exposed_port_guess(insp: Dict[str, Any]) -> int:
    exposed = sorted(int(k.split("/", 1)[0]) for k in ((insp.get("Config") or {}).get("ExposedPorts") or {})
                     if k.split("/", 1)[0].isdigit() and k.endswith("/tcp"))
    for pref in (80, 8080, 3000, 8000, 5000):
        if pref in exposed:
            return pref
    return exposed[0] if exposed else 0


async def expose(svc: Any, host: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    target = body.get("target") or {}
    kind = target.get("kind") or "container"
    domain = lbl.normalize_domain(str(body.get("domain") or ""))
    if not lbl.valid_domain(domain):
        raise errors.Validation("a valid domain is required (e.g. app.example.com)")
    port = int(body.get("port") or 0)
    ops = svc.docker.ops(host)
    view = await host_exposure(svc, host)
    for d in view["domains"]:
        if d["domain"] == domain and d["containerId"] != target.get("id") and not (
                kind == "deployment" and d.get("deploymentId") == target.get("id")):
            raise errors.Conflict(f"{domain} is already routed to {d['containerName']}")
    proxy = _pick_proxy(view, str(body.get("proxyId") or ""))
    tls = body.get("tls")
    tls = None if tls is None else bool(tls)
    resolver = body.get("certResolver")
    entrypoint = str(body.get("entrypoint") or "")

    if kind == "container":
        cid = str(target.get("id") or "")
        insp = await ops.inspect_container(cid)
        labels_now = (insp.get("Config") or {}).get("Labels") or {}
        own = owner_of(labels_now, _dep_index(svc.store))
        if own["deploymentId"] and own["deploymentType"]:
            # Managed by a Kowan deployment: edit the deployment, not the container
            # (a recreate would be reverted by the next deploy).
            return await expose(svc, host, {**body, "target": {
                "kind": "deployment", "id": own["deploymentId"], "service": own["service"]}})
        if proxymod.proxy_kind((insp.get("Config") or {}).get("Image", "")):
            raise errors.Validation("cannot expose the proxy itself")
        port = port or _exposed_port_guess(insp)
        if not port:
            raise errors.Validation("port is required (the container port the app listens on)")
        name = str(insp.get("Name") or cid).lstrip("/")
        prev = lbl.exposed_by_port(labels_now)
        added = (prev or {}).get("network")  # joined by a previous expose: still "added"
        labels, net = plan_labels(proxy, domain=domain, port=port, router_base=name,
                                  target_networks=[n for n in _networks_of(insp) if n != added],
                                  entrypoint=entrypoint, tls=tls, cert_resolver=resolver)
        remove = [k for k in lbl.unexpose_keys(labels_now) if k not in labels] if prev else []
        disconnect = [prev["network"]] if prev and prev.get("network") and prev["network"] != net["network"] else []
        new_id = await ops.container_relabel(cid, labels, remove,
                                             [net["network"]] if net["connect"] and net["network"] else [],
                                             disconnect)
        warn = []
        if own["stack"]:
            warn.append("This container is part of the compose stack "
                        f"'{own['stack']}': a later 'docker compose up' recreates it without these labels.")
        return {"status": "exposed", "kind": "container", "containerId": new_id, "domain": domain,
                "proxy": proxy["kind"], "proxyName": proxy["name"], "network": net["network"],
                "labels": labels, "warnings": warn}

    if kind == "deployment":
        dep = svc.store.get_deployment(str(target.get("id") or ""))
        _check_dep_host(dep, host)
        dsvc = svc.deployments
        if dep.type == "container":
            if dep.spec is None:
                raise errors.Validation("deployment has no container spec")
            spec = dep.spec.model_copy(deep=True)
            port = port or (spec.ports[0].container_port if spec.ports else 0)
            if not port:
                raise errors.Validation("port is required (the container port the app listens on)")
            # A network Kowan added on a previous expose still counts as added, so a
            # re-expose keeps the marker and a later unexpose removes it again.
            prev = lbl.exposed_by_port(spec.labels or {}) or {}
            nets = [n for n in spec.networks or [] if n != prev.get("network")]
            labels, net = plan_labels(proxy, domain=domain, port=port, router_base=dep.resource_name(),
                                      target_networks=nets, entrypoint=entrypoint,
                                      tls=tls, cert_resolver=resolver)
            cur = dict(spec.labels or {})
            if lbl.exposed_by_port(cur):
                for k in lbl.unexpose_keys(cur):
                    cur.pop(k, None)
            cur.update(labels)
            spec.labels = cur
            if net["connect"] and net["network"] and net["network"] not in spec.networks:
                spec.networks = list(spec.networks or []) + [net["network"]]
            dep.spec = spec
        else:
            if dep.git is not None and dep.git.repo_url:
                raise errors.Conflict("this deployment's compose file comes from git; add the labels in the repository")
            if dep.strategy == "swarm":
                raise errors.Conflict("swarm stacks are not supported by expose yet (labels belong under deploy.labels)")
            names = composemod.services(dep.compose)
            service = str(target.get("service") or (names[0] if len(names) == 1 else ""))
            if service not in names:
                raise errors.Validation("service is required: " + ", ".join(names))
            if not port:
                raise errors.Validation("port is required (the container port the service listens on)")
            labels, net = plan_labels(proxy, domain=domain, port=port,
                                      router_base=f"{dep.resource_name()}-{service}",
                                      target_networks=[], entrypoint=entrypoint, tls=tls,
                                      cert_resolver=resolver)
            # compose networks are project-scoped: always join the proxy network
            labels[lbl.MARK_NETWORK] = net["network"]
            cur = composemod.service_labels(dep.compose, service)
            text = dep.compose
            if lbl.exposed_by_port(cur):
                text = composemod.unexpose(text, service, lbl.unexpose_keys(cur), True)
            try:
                dep.compose = composemod.expose(text, service, labels, net["network"] or None)
            except composemod.ComposeEditError as exc:
                raise errors.Validation(str(exc))
        dep.updated_at = _now()
        svc.store.update_deployment(dep, source="exposure", note=f"expose {domain}")
        await dsvc.dispatch_deploy(dep.id, redeploy=True)
        return {"status": "redeploying", "kind": "deployment", "deploymentId": dep.id, "domain": domain,
                "proxy": proxy["kind"], "proxyName": proxy["name"], "network": net["network"],
                "labels": labels,
                "warnings": ["The compose file was re-serialised: YAML comments were dropped."] if dep.type == "compose" else []}
    raise errors.Validation("target.kind must be container or deployment")


async def unexpose(svc: Any, host: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    target = body.get("target") or {}
    kind = target.get("kind") or "container"
    ops = svc.docker.ops(host)
    if kind == "container":
        cid = str(target.get("id") or "")
        insp = await ops.inspect_container(cid)
        labels_now = (insp.get("Config") or {}).get("Labels") or {}
        own = owner_of(labels_now, _dep_index(svc.store))
        if own["deploymentId"] and own["deploymentType"]:
            return await unexpose(svc, host, {**body, "target": {
                "kind": "deployment", "id": own["deploymentId"], "service": own["service"]}})
        mark = lbl.exposed_by_port(labels_now)
        if not mark:
            raise errors.Conflict("this container was not exposed by Kowan; edit its proxy labels by hand")
        new_id = await ops.container_relabel(cid, {}, lbl.unexpose_keys(labels_now), [],
                                             [mark["network"]] if mark.get("network") else [])
        return {"status": "unexposed", "kind": "container", "containerId": new_id, "domain": mark["domain"]}
    if kind == "deployment":
        dep = svc.store.get_deployment(str(target.get("id") or ""))
        _check_dep_host(dep, host)
        if dep.type == "container":
            spec = dep.spec.model_copy(deep=True) if dep.spec else None
            if spec is None or not lbl.exposed_by_port(spec.labels):
                raise errors.Conflict("this deployment was not exposed by Kowan")
            mark = lbl.exposed_by_port(spec.labels) or {}
            for k in lbl.unexpose_keys(spec.labels):
                spec.labels.pop(k, None)
            if mark.get("network"):
                spec.networks = [n for n in spec.networks if n != mark["network"]]
            dep.spec = spec
            domain = mark.get("domain", "")
        else:
            if dep.git is not None and dep.git.repo_url:
                raise errors.Conflict("this deployment's compose file comes from git; remove the labels in the repository")
            names = composemod.services(dep.compose)
            service = str(target.get("service") or "")
            cands = [service] if service else [s for s in names if composemod.exposure_of(dep.compose, s)]
            if not cands or cands[0] not in names:
                raise errors.Conflict("no service of this deployment was exposed by Kowan")
            service = cands[0]
            cur = composemod.service_labels(dep.compose, service)
            mark = lbl.exposed_by_port(cur)
            if not mark:
                raise errors.Conflict(f"service {service!r} was not exposed by Kowan")
            dep.compose = composemod.unexpose(dep.compose, service, lbl.unexpose_keys(cur), True)
            domain = mark["domain"]
        dep.updated_at = _now()
        svc.store.update_deployment(dep, source="exposure", note=f"unexpose {domain}")
        await svc.deployments.dispatch_deploy(dep.id, redeploy=True)
        return {"status": "redeploying", "kind": "deployment", "deploymentId": dep.id, "domain": domain}
    raise errors.Validation("target.kind must be container or deployment")


def _check_dep_host(dep: Deployment, host: Any) -> None:
    if host.id not in (dep.targets or []):
        raise errors.Validation("the deployment does not target this host")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def deploy_proxy(svc: Any, host: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """Create and deploy a Traefik compose deployment with Let's Encrypt (HTTP-01)."""
    kind = str(body.get("kind") or "traefik")
    if kind != "traefik":
        raise errors.Validation("only kind=traefik can be deployed")
    name = str(body.get("name") or "traefik").strip()
    network = str(body.get("network") or "kowan-proxy").strip()
    try:
        compose = proxymod.traefik_compose(
            email=str(body.get("email") or ""),
            http_port=int(body.get("httpPort") or 80),
            https_port=int(body.get("httpsPort") or 443),
            network=network,
            staging=bool(body.get("staging")),
        )
    except ValueError as exc:
        raise errors.Validation(str(exc))
    try:
        svc.store.get_deployment_by_name(name)
        raise errors.Conflict(f"a deployment named {name!r} already exists")
    except errors.Conflict:
        raise
    except Exception:  # noqa: BLE001 - not found: name is free
        pass
    now = _now()
    dep = Deployment(id=str(uuid.uuid4()), name=name, type="compose", strategy="standalone",
                     targets=[host.id], compose=compose, status="stopped",
                     createdAt=now, updatedAt=now)
    svc.store.create_deployment(dep, source="exposure", author="kowan")
    await svc.deployments.dispatch_deploy(dep.id)
    return {"status": "deploying", "deploymentId": dep.id, "name": name, "network": network}
