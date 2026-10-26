"""Network and dependency map: a graph of ingress (published ports, proxy
domains), containers grouped by compose stack / deployment, and networks, with
a deterministic layered layout computed here so the browser only draws SVG.

  GET /hosts/{hostID}/netmap?stack=&network=   one host   (any role)
  GET /netmap?stack=&network=                  all hosts, one band per host

Layout (pure, see :func:`layout`): three columns per host band — ingress |
containers (boxed per stack, ``depends_on`` order inside a stack) | networks.
Networks and ingress nodes are ordered by the barycentre of the containers they
touch (ties broken by name) and pushed down to avoid overlap, so the same input
always yields the same coordinates.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from aiohttp import web

from kowan import errors
from kowan.exposure import classify
from kowan.exposure import labels as lbl
from kowan.exposure.service import _dep_index, owner_of
from kowan.store import NotFoundError
from kowan.web import api_route, bind_svc, json_response, ops_error_response

if TYPE_CHECKING:  # pragma: no cover
    from kowan.web import Services

log = logging.getLogger("kowan.api.netmap")

DEPENDS_LABEL = "com.docker.compose.depends_on"
NO_STACK = ""  # containers outside any stack

# Geometry (px).
COL_INGRESS_X = 0
COL_INGRESS_W = 210
COL_CONTAINER_X = 300
COL_CONTAINER_W = 250
COL_NETWORK_X = 640
COL_NETWORK_W = 200
NODE_H = 26
ROW_GAP = 8
STACK_HEAD = 22
STACK_PAD = 6
STACK_GAP = 14
HOST_HEAD = 34
HOST_GAP = 36
WIDTH = COL_NETWORK_X + COL_NETWORK_W


# ---------------------------------------------------------------------------
# graph model (pure)
# ---------------------------------------------------------------------------


def parse_depends_on(value: str) -> List[str]:
    """Service names from compose's ``depends_on`` label
    (``db:service_started:false,redis:service_healthy:true``)."""
    out: List[str] = []
    for part in (value or "").split(","):
        name = part.strip().split(":", 1)[0].strip()
        if name and name not in out:
            out.append(name)
    return out


def build_host_graph(host_id: str, host_name: str, containers: List[Dict[str, Any]],
                     networks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalise one host's raw data into a graph (no coordinates yet).

    ``containers``: ``{id, name, state, image, labels, stack, service,
    deploymentId, ports: [{ip, publicPort, privatePort, protocol}]}``.
    ``networks``: ``{id, name, driver, containers: [containerId]}``.
    """
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    hp = f"{host_id}:"
    by_stack_service: Dict[Tuple[str, str], str] = {}
    for c in containers:
        cid = hp + "c:" + c["id"]
        nodes[cid] = {"id": cid, "kind": "container", "label": c["name"], "sub": c.get("image", ""),
                      "state": c.get("state", ""), "hostId": host_id, "stack": c.get("stack", ""),
                      "ref": {"containerId": c["id"], "deploymentId": c.get("deploymentId", "")}}
        if c.get("stack") and c.get("service"):
            by_stack_service[(c["stack"], c["service"])] = cid
    for c in containers:
        cid = hp + "c:" + c["id"]
        for p in c.get("ports") or []:
            if not p.get("publicPort"):
                continue
            ip = p.get("ip") or "0.0.0.0"
            proto = p.get("protocol") or "tcp"
            pid = f"{hp}p:{ip}:{p['publicPort']}/{proto}"
            cl = classify.classify_port(ip, int(p.get("privatePort") or 0), c.get("image", ""))
            host_ip = f"[{ip}]" if ":" in ip else ip
            nodes.setdefault(pid, {"id": pid, "kind": "port", "label": f"{host_ip}:{p['publicPort']}/{proto}",
                                   "sub": f"→ {p.get('privatePort')}", "state": cl["scope"],
                                   "warn": cl["warn"], "hostId": host_id, "stack": c.get("stack", ""),
                                   "ref": {"containerId": c["id"]}})
            edges.append({"from": pid, "to": cid, "kind": "port"})
        for r in lbl.routes(c.get("labels") or {}):
            did = f"{hp}d:{r['domain']}"
            nodes.setdefault(did, {"id": did, "kind": "domain", "label": r["domain"],
                                   "sub": r["proxy"] + (" · tls" if r.get("tls") else ""),
                                   "state": "tls" if r.get("tls") else "http", "hostId": host_id,
                                   "stack": c.get("stack", ""), "ref": {"containerId": c["id"]}})
            edges.append({"from": did, "to": cid, "kind": "domain"})
        for dep in parse_depends_on((c.get("labels") or {}).get(DEPENDS_LABEL, "")):
            target = by_stack_service.get((c.get("stack", ""), dep))
            if target and target != cid:
                edges.append({"from": cid, "to": target, "kind": "depends"})
    for n in networks:
        nid = f"{hp}n:{n['name']}"
        nodes[nid] = {"id": nid, "kind": "network", "label": n["name"], "sub": n.get("driver", ""),
                      "state": "internal" if n.get("internal") else "", "hostId": host_id, "stack": "",
                      "ref": {"networkId": n.get("id", ""), "networkName": n["name"]}}
        for cid_raw in n.get("containers") or []:
            cid = hp + "c:" + cid_raw
            if cid in nodes:
                edges.append({"from": cid, "to": nid, "kind": "network"})
    # de-duplicate edges deterministically
    seen = set()
    uniq = []
    for e in edges:
        key = (e["from"], e["to"], e["kind"])
        if key in seen:
            continue
        seen.add(key)
        e["id"] = f"{e['kind']}:{e['from']}->{e['to']}"
        uniq.append(e)
    return {"hostId": host_id, "hostName": host_name, "nodes": nodes, "edges": uniq}


def filter_graph(g: Dict[str, Any], *, stack: str = "", network: str = "") -> Dict[str, Any]:
    """Keep the containers of ``stack`` / attached to ``network`` (and what they touch)."""
    if not stack and not network:
        return g
    nodes = g["nodes"]
    edges = g["edges"]
    keep_c = {nid for nid, n in nodes.items() if n["kind"] == "container"}
    if stack:
        want = "" if stack == "-" else stack
        keep_c = {nid for nid in keep_c if nodes[nid]["stack"] == want}
    if network:
        attached = {e["from"] for e in edges if e["kind"] == "network" and nodes[e["to"]]["label"] == network}
        keep_c &= attached
    keep = set(keep_c)
    for e in edges:
        if e["kind"] in ("port", "domain") and e["to"] in keep_c:
            keep.add(e["from"])
        if e["kind"] == "network" and e["from"] in keep_c and (not network or nodes[e["to"]]["label"] == network):
            keep.add(e["to"])
    if network:
        keep |= {nid for nid, n in nodes.items() if n["kind"] == "network" and n["label"] == network}
    return {**g, "nodes": {k: v for k, v in nodes.items() if k in keep},
            "edges": [e for e in edges if e["from"] in keep and e["to"] in keep]}


def _topo(ids: List[str], deps: Dict[str, List[str]], label: Dict[str, str]) -> List[str]:
    """Order ``ids`` so dependencies come first; ties (and cycles) by label then id."""
    key = lambda i: (label[i].lower(), i)  # noqa: E731
    pending = {i: {d for d in deps.get(i, []) if d in ids} for i in ids}
    out: List[str] = []
    while pending:
        ready = sorted([i for i, d in pending.items() if not d], key=key)
        if not ready:  # cycle: break it at the smallest remaining node
            ready = [sorted(pending, key=key)[0]]
        nxt = ready[0]
        out.append(nxt)
        pending.pop(nxt)
        for d in pending.values():
            d.discard(nxt)
    return out


def _place_column(items: List[Tuple[float, str]], start_y: float) -> Dict[str, float]:
    """Place nodes at their wanted centre y, pushed down to avoid overlap."""
    out: Dict[str, float] = {}
    cursor = start_y
    for want, nid in sorted(items, key=lambda t: (t[0], t[1])):
        y = max(cursor, want - NODE_H / 2)
        out[nid] = y
        cursor = y + NODE_H + ROW_GAP
    return out


def layout_host(g: Dict[str, Any], y0: float) -> Dict[str, Any]:
    """Coordinates for one host band starting at ``y0``."""
    nodes = {k: dict(v) for k, v in g["nodes"].items()}
    edges = g["edges"]
    label = {k: v["label"] for k, v in nodes.items()}
    containers = [k for k, v in nodes.items() if v["kind"] == "container"]
    stacks: Dict[str, List[str]] = {}
    for cid in containers:
        stacks.setdefault(nodes[cid]["stack"], []).append(cid)
    deps: Dict[str, List[str]] = {}
    for e in edges:
        if e["kind"] == "depends":
            deps.setdefault(e["from"], []).append(e["to"])
    groups: List[Dict[str, Any]] = []
    y = y0 + HOST_HEAD
    order = sorted(stacks, key=lambda s: (s == NO_STACK, s.lower(), s))
    for s in order:
        members = _topo(stacks[s], deps, label)
        top = y
        y += STACK_HEAD
        for cid in members:
            n = nodes[cid]
            n.update({"x": COL_CONTAINER_X + STACK_PAD, "y": y, "w": COL_CONTAINER_W - 2 * STACK_PAD, "h": NODE_H})
            y += NODE_H + ROW_GAP
        y += STACK_PAD - ROW_GAP
        groups.append({"id": f"{g['hostId']}:s:{s or '-'}", "kind": "stack", "label": s or "standalone",
                       "stack": s, "hostId": g["hostId"], "x": COL_CONTAINER_X, "y": top,
                       "w": COL_CONTAINER_W, "h": y - top,
                       "deploymentId": next((nodes[c]["ref"].get("deploymentId") for c in members
                                             if nodes[c]["ref"].get("deploymentId")), "")})
        y += STACK_GAP
    content_bottom = y
    centre = {cid: nodes[cid]["y"] + NODE_H / 2 for cid in containers}

    def bary(nid: str, side: str) -> float:
        ys = [centre[e["to"] if side == "to" else e["from"]] for e in edges
              if (e["from"] == nid if side == "to" else e["to"] == nid)
              and (e["to"] if side == "to" else e["from"]) in centre]
        return sum(ys) / len(ys) if ys else content_bottom

    ingress = [k for k, v in nodes.items() if v["kind"] in ("port", "domain")]
    nets = [k for k, v in nodes.items() if v["kind"] == "network"]
    ing_y = _place_column([(bary(n, "to"), label[n] + "\x00" + n) for n in ingress], y0 + HOST_HEAD + STACK_HEAD)
    net_y = _place_column([(bary(n, "from"), label[n] + "\x00" + n) for n in nets], y0 + HOST_HEAD + STACK_HEAD)
    bottom = content_bottom
    for key, yy in ing_y.items():
        nid = key.split("\x00", 1)[1]
        nodes[nid].update({"x": COL_INGRESS_X, "y": yy, "w": COL_INGRESS_W, "h": NODE_H})
        bottom = max(bottom, yy + NODE_H + ROW_GAP)
    for key, yy in net_y.items():
        nid = key.split("\x00", 1)[1]
        nodes[nid].update({"x": COL_NETWORK_X, "y": yy, "w": COL_NETWORK_W, "h": NODE_H})
        bottom = max(bottom, yy + NODE_H + ROW_GAP)
    groups.insert(0, {"id": f"{g['hostId']}:h", "kind": "host", "label": g["hostName"], "hostId": g["hostId"],
                      "x": COL_INGRESS_X, "y": y0, "w": WIDTH, "h": bottom - y0, "stack": "", "deploymentId": ""})
    return {"nodes": nodes, "edges": edges, "groups": groups, "bottom": bottom}


def layout(graphs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Lay out host graphs as stacked bands; deterministic for identical input."""
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    groups: List[Dict[str, Any]] = []
    y = 0.0
    for g in sorted(graphs, key=lambda g: (g["hostName"].lower(), g["hostId"])):
        band = layout_host(g, y)
        nodes.extend(sorted(band["nodes"].values(), key=lambda n: n["id"]))
        edges.extend(sorted(band["edges"], key=lambda e: e["id"]))
        groups.extend(band["groups"])
        y = band["bottom"] + HOST_GAP
    return {"nodes": nodes, "edges": edges, "groups": groups, "width": WIDTH,
            "height": max(0.0, y - HOST_GAP)}


# ---------------------------------------------------------------------------
# data collection
# ---------------------------------------------------------------------------


async def collect_host(svc: "Services", host: Any) -> Dict[str, Any]:
    ops = svc.docker.ops(host)
    containers, nets = await asyncio.gather(ops.list_containers(True), ops.list_networks())
    deps = _dep_index(svc.store)
    sem = asyncio.Semaphore(8)

    async def inspect(n: Any) -> Dict[str, Any]:
        async with sem:
            try:
                full = await ops.inspect_network(n.id or n.name)
                members = [c.id for c in (full.containers or [])]
            except Exception:  # noqa: BLE001
                members = []
        return {"id": n.id, "name": n.name, "driver": n.driver, "internal": n.internal, "containers": members}

    raw_nets = [n for n in nets if n.name not in ("none",)]
    net_rows = list(await asyncio.gather(*(inspect(n) for n in raw_nets)))
    # Network inspect reports short or full ids depending on the daemon: map to full.
    full_ids = {c.id: c.id for c in containers}
    for c in containers:
        full_ids.setdefault(c.id[:12], c.id)
    for n in net_rows:
        n["containers"] = [full_ids.get(i, full_ids.get(i[:12], i)) for i in n["containers"]]
    # Only networks with something attached, plus user-defined ones.
    net_rows = [n for n in net_rows if n["containers"] or n["name"] not in ("bridge", "host")]
    crow = []
    for c in containers:
        own = owner_of(c.labels, deps)
        crow.append({
            "id": c.id, "name": (c.names[0] if c.names else c.id[:12]).lstrip("/"), "state": c.state,
            "image": c.image, "labels": c.labels or {}, "stack": own["stack"], "service": own["service"],
            "deploymentId": own["deploymentId"],
            "ports": [{"ip": p.ip, "publicPort": p.public_port, "privatePort": p.private_port,
                       "protocol": p.type} for p in (c.ports or [])],
        })
    return build_host_graph(host.id, host.name or host.id, crow, net_rows)


def _facets(graphs: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    stacks, nets = set(), set()
    for g in graphs:
        for n in g["nodes"].values():
            if n["kind"] == "container" and n["stack"]:
                stacks.add(n["stack"])
            if n["kind"] == "network":
                nets.add(n["label"])
    return {"stacks": sorted(stacks, key=str.lower), "networks": sorted(nets, key=str.lower)}


def _respond(graphs: List[Dict[str, Any]], hosts: List[Dict[str, Any]], request: web.Request) -> web.Response:
    facets = _facets(graphs)
    stack = request.query.get("stack", "")
    network = request.query.get("network", "")
    filtered = [filter_graph(g, stack=stack, network=network) for g in graphs]
    out = layout(filtered)
    out.update(facets)
    out["hosts"] = hosts
    out["counts"] = {
        "containers": sum(1 for n in out["nodes"] if n["kind"] == "container"),
        "networks": sum(1 for n in out["nodes"] if n["kind"] == "network"),
        "ingress": sum(1 for n in out["nodes"] if n["kind"] in ("port", "domain")),
    }
    return json_response(out)


async def _one(svc: "Services", request: web.Request) -> web.StreamResponse:
    try:
        host = svc.store.get_host(request.match_info.get("hostID", ""))
    except NotFoundError:
        return ops_error_response(errors.NotFound("host not found"))
    try:
        g = await asyncio.wait_for(collect_host(svc, host), 20.0)
    except asyncio.TimeoutError:
        return ops_error_response(errors.BadGateway("host did not answer in time"))
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return _respond([g], [{"id": host.id, "name": host.name or host.id, "error": ""}], request)


async def _all(svc: "Services", request: web.Request) -> web.StreamResponse:
    hosts = svc.store.list_hosts()

    async def one(h: Any) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        try:
            return await asyncio.wait_for(collect_host(svc, h), 20.0), {"id": h.id, "name": h.name or h.id, "error": ""}
        except Exception as err:  # noqa: BLE001
            return None, {"id": h.id, "name": h.name or h.id, "error": str(err) or "unreachable"}

    res = await asyncio.gather(*(one(h) for h in hosts))
    return _respond([g for g, _ in res if g is not None], [info for _, info in res], request)


def register(app: web.Application, svc: "Services") -> None:
    h = bind_svc(svc)
    api_route(app, "GET", "/netmap", h(_all))
    api_route(app, "GET", "/hosts/{hostID}/netmap", h(_one))
