"""Image vulnerability scanning routes (Trivy on the target host).

  GET  /hosts/{hostID}/images/{id}/scan      latest stored result for the image
                                             (with findings) + ``running`` flag;
                                             ``id`` is an image id or a ref.
  POST /hosts/{hostID}/images/{id}/scan      (operator) start a scan in the
                                             background; ``{"force": true}``
                                             re-scans even when a fresh (< 24 h)
                                             result exists; ``{"wait": true}``
                                             waits and returns the result.
  GET  /hosts/{hostID}/scans                 latest results for every image on
                                             the host, a containerId -> imageId
                                             map (for row chips) and the host's
                                             severity totals.
  GET  /scans/summary                        per-host severity totals over the
                                             stored results (dashboard).

Image refs containing ``/`` can be passed as ``?image=`` on the same routes
with ``_`` as the path id.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from aiohttp import web

from kowan.scan.service import ScanService, is_fresh
from kowan.scan.trivy import scanner_version_from_image, sum_counts, trivy_image
from kowan.web import SERVICES_KEY, api_route, error_response, json_response, ops_error_response, read_json, require_role

log = logging.getLogger("kowan.api.scan")


def register(app: web.Application, svc: Any) -> None:
    if getattr(svc, "scans", None) is None:
        svc.attach("scans", ScanService(svc))
    api_route(app, "GET", "/hosts/{hostID}/images/{id}/scan", _get_scan)
    api_route(app, "POST", "/hosts/{hostID}/images/{id}/scan", require_role("operator")(_start_scan))
    api_route(app, "GET", "/hosts/{hostID}/scans", _host_scans)
    api_route(app, "GET", "/scans/summary", _summary)


def _svc(request: web.Request) -> Any:
    return request.app[SERVICES_KEY]


def _image_param(request: web.Request) -> str:
    img = request.match_info.get("id", "")
    if img in ("", "_"):
        img = request.query.get("image", "")
    return img


def _ops(svc: Any, host_id: str) -> Any:
    host = svc.store.get_host(host_id)
    return svc.docker.ops(host)


async def _get_scan(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    host_id = request.match_info["hostID"]
    image = _image_param(request)
    try:
        ops = _ops(svc, host_id)
    except Exception as err:  # noqa: BLE001
        return error_response(404, f"host: {err}")
    try:
        insp = await ops.inspect_image(image)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    image_id = str((insp or {}).get("Id") or image)
    rec = svc.store.get_image_scan(image_id)
    return json_response({
        "imageId": image_id,
        "running": svc.scans.is_running(host_id, image_id),
        "fresh": is_fresh(rec),
        "result": rec,
        "scanner": {"image": trivy_image(), "version": scanner_version_from_image(trivy_image())},
    })


async def _start_scan(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    host_id = request.match_info["hostID"]
    image = _image_param(request)
    body: Dict[str, Any] = {}
    if request.can_read_body:
        try:
            data = await read_json(request)
            body = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 - empty body is fine
            body = {}
    try:
        ops = _ops(svc, host_id)
        insp = await ops.inspect_image(image)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    image_id = str(insp.get("Id") or "")
    force = bool(body.get("force"))
    if body.get("wait"):
        try:
            rec = await svc.scans.scan(host_id, image_id, force=force, ops=ops, image_insp=insp)
        except Exception as err:  # noqa: BLE001
            return error_response(502, str(err))
        return json_response({"imageId": image_id, "running": False, "fresh": True, "result": rec})
    cached = svc.store.get_image_scan(image_id, with_findings=False)
    if not force and is_fresh(cached):
        return json_response({"imageId": image_id, "running": False, "fresh": True, "result": cached})
    svc.scans.start_background(host_id, image_id, force=force)
    return json_response({"imageId": image_id, "running": True, "fresh": False, "result": cached}, status=202)


def _norm_tag(tag: str) -> str:
    from kowan.api.updates import parse_image_ref

    try:
        return parse_image_ref(tag).key
    except ValueError:
        return tag


async def _host_scans(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    host_id = request.match_info["hostID"]
    try:
        ops = _ops(svc, host_id)
        images = await ops.list_images()
        containers = await ops.list_containers(True)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    ids = [i.id for i in images]
    by_id = {r["imageId"]: r for r in svc.store.list_image_scans(ids)}
    tag_to_id: Dict[str, str] = {}
    rows: List[Dict[str, Any]] = []
    for img in images:
        for t in img.repo_tags or []:
            if t and t != "<none>:<none>":
                tag_to_id[_norm_tag(t)] = img.id
        rec = by_id.get(img.id)
        rows.append({
            "imageId": img.id,
            "repoTags": [t for t in (img.repo_tags or []) if t != "<none>:<none>"],
            "running": svc.scans.is_running(host_id, img.id),
            "result": rec,
        })
    cmap: Dict[str, str] = {}
    for c in containers:
        ref = c.image or ""
        iid = ""
        if ref.startswith("sha256:"):
            iid = ref
        else:
            iid = tag_to_id.get(_norm_tag(ref), "")
        if iid:
            cmap[c.id] = iid
    totals = sum_counts(r["result"]["counts"] for r in rows if r["result"] and r["result"].get("status") == "ok")
    scanned = sum(1 for r in rows if r["result"] and r["result"].get("status") == "ok")
    return json_response({"hostId": host_id, "images": rows, "containers": cmap, "totals": totals,
                          "scanned": scanned, "imageCount": len(rows)})


async def _summary(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    per: Dict[str, Dict[str, Any]] = {}
    for r in svc.store.list_image_scans():
        if r.get("status") != "ok":
            continue
        h = per.setdefault(r["hostId"], {"hostId": r["hostId"], "scanned": 0, "lastScanAt": "", "counts": []})
        h["scanned"] += 1
        h["counts"].append(r["counts"])
        if r["scannedAt"] > h["lastScanAt"]:
            h["lastScanAt"] = r["scannedAt"]
    out = []
    for h in per.values():
        out.append({**h, "counts": sum_counts(h["counts"])})
    return json_response(out)
