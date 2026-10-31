"""Inventory: what runs where, as a file (see docs/health.md).

    GET /inventory?format=csv|json&group=<tag>

One row per container on every host the caller can see (only the containers
they can see on it): host and its groups, workload and its kind, service,
container, image and image id, state, published ports, creation time and
the deployment that owns it. ``group`` limits it to one host group. The CSV
comes as a download; JSON also lists the hosts that could not be read.
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone
from typing import Any, Dict, List

from aiohttp import web

from kowan import errors
from kowan.auth import authorize as authz
from kowan.runbooks.model import service_of
from kowan.web import api_route, bind_svc, json_response
from kowan.workloads import compose_deployments, workload_of

COLUMNS = ["host", "hostId", "groups", "workload", "kind", "service", "container", "image", "imageId",
           "state", "status", "ports", "created", "deployment"]
_KINDS = {"dep": "deployment", "stack": "stack", "ctr": "container"}


def _ports(c: Any) -> str:
    out = []
    for p in getattr(c, "ports", None) or []:
        if p.public_port:
            out.append(f"{p.ip + ':' if p.ip and p.ip not in ('0.0.0.0', '::') else ''}{p.public_port}->{p.private_port}/{p.type}")
    return " ".join(sorted(set(out)))


async def inventory_rows(svc: Any, group: str = "") -> Dict[str, Any]:
    hosts = authz.filter_hosts(svc.store.list_hosts())
    if group:
        hosts = [h for h in hosts if group in (h.tags or [])]
    deps = {d.id: d.name for d in svc.store.list_deployments()}
    compose = compose_deployments(svc.store)

    async def one(h: Any) -> Any:
        try:
            ops = svc.docker.ops(h)
            return h, await asyncio.wait_for(ops.list_containers(True), timeout=20), ""
        except Exception as err:  # noqa: BLE001 - offline host: reported, not fatal
            return h, [], str(err) or err.__class__.__name__

    rows: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    for h, cs, err in await asyncio.gather(*(one(h) for h in hosts)):
        if err:
            failed.append({"hostId": h.id, "host": h.name, "error": err})
            continue
        for c in cs:
            labels = c.labels or {}
            if labels.get("kowan.support") == "true":
                continue
            name = ((c.names or [""])[0]).lstrip("/")
            wl, _, dep = workload_of(labels, h.id, name, compose)
            rows.append({
                "host": h.name, "hostId": h.id, "groups": " ".join(h.tags or []), "workload": wl,
                "kind": _KINDS[wl.split("~", 1)[0]], "service": service_of(labels), "container": name,
                "image": c.image, "imageId": (c.image_id or "").replace("sha256:", "")[:12],
                "state": c.state, "status": c.status, "ports": _ports(c),
                "created": datetime.fromtimestamp(c.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if c.created else "",
                "deployment": deps.get(dep, ""),
            })
    rows.sort(key=lambda r: (r["host"], r["workload"], r["container"]))
    return {"items": rows, "errors": failed}


async def _inventory(svc, request: web.Request) -> web.StreamResponse:
    fmt = request.query.get("format", "csv")
    if fmt not in ("csv", "json"):
        raise errors.Validation("format must be csv or json")
    data = await inventory_rows(svc, request.query.get("group", "").strip().lower())
    if fmt == "json":
        return json_response(data)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in data["items"]:
        w.writerow(r)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return web.Response(body=buf.getvalue().encode("utf-8"), content_type="text/csv", charset="utf-8",
                        headers={"Content-Disposition": f'attachment; filename="kowan-inventory-{day}.csv"'})


def register(app: web.Application, svc) -> None:
    api_route(app, "GET", "/inventory", bind_svc(svc)(_inventory))
