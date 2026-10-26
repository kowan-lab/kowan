"""Read-only Docker tools: hosts, containers, logs, stats, images, volumes,
networks, events and image-update checks. Viewer role."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List

from ...auth import authorize as authz
from ..registry import ROLE_VIEWER, ToolError, ToolOutput, ToolRegistry, to_plain
from ._common import (
    CONTAINER_PROP,
    HOST_PROP,
    OFFSET_PROP,
    container_name,
    exit_code_from_status,
    human_bytes,
    iso,
    limit_prop,
    page,
    parse_rfc3339,
    parse_since,
    ping_host,
    resolve_host,
    resolve_ops,
    short_id,
    with_timeout,
    close_stream,
    ops_for_host,
)

LOG_DEFAULT_TAIL = 200
LOG_MAX_TAIL = 2000
LOG_MAX_LINE = 2000  # characters per line
LOG_MAX_BYTES = 8 * 1024 * 1024  # raw bytes read from the daemon per call
INSPECT_LIST_CAP = 50
EVENTS_MAX = 500


def container_row(c: Any, host: Any = None) -> Dict[str, Any]:
    row = {
        "id": short_id(c.id),
        "name": container_name(c),
        "image": c.image,
        "state": c.state,
        "status": c.status,
    }
    code = exit_code_from_status(c.status)
    if code is not None:
        row["exitCode"] = code
    ports = []
    for p in c.ports or []:
        if p.public_port:
            ip = p.ip or "0.0.0.0"
            ports.append(f"{'[' + ip + ']' if ':' in ip else ip}:{p.public_port}->{p.private_port}/{p.type}")
        else:
            ports.append(f"{p.private_port}/{p.type}")
    if ports:
        row["ports"] = sorted(set(ports))
    labels = c.labels or {}
    if labels.get("com.docker.compose.project"):
        row["composeProject"] = labels["com.docker.compose.project"]
    if host is not None:
        row["hostId"] = host.id
        row["hostName"] = host.name
    return row


def summarize_inspect(info: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a raw inspect document to what matters for diagnosis.

    Environment VALUES are never returned (they routinely hold secrets), only
    variable names. Command lines are omitted for the same reason. Lists are
    capped and a ``notes`` entry says when something was cut.
    """
    notes: List[str] = []
    cfg = info.get("Config") or {}
    st = info.get("State") or {}
    hc = info.get("HostConfig") or {}
    ns = info.get("NetworkSettings") or {}

    def cap(items: List[Any], what: str) -> List[Any]:
        if len(items) > INSPECT_LIST_CAP:
            notes.append(f"{what}: showing {INSPECT_LIST_CAP} of {len(items)}")
            return items[:INSPECT_LIST_CAP]
        return items

    health = st.get("Health") or None
    health_out = None
    if health:
        logs = health.get("Log") or []
        last = logs[-1] if logs else {}
        health_out = {
            "status": health.get("Status", ""),
            "failingStreak": health.get("FailingStreak", 0),
            "lastExitCode": last.get("ExitCode"),
            "lastOutput": (last.get("Output") or "").strip()[:500],
            "lastCheck": last.get("End", ""),
        }

    ports: List[str] = []
    for cport, binds in (ns.get("Ports") or {}).items():
        if not binds:
            ports.append(cport)
            continue
        for b in binds:
            ports.append(f"{b.get('HostIp') or '0.0.0.0'}:{b.get('HostPort')}->{cport}")

    mounts = [
        {
            "type": m.get("Type", ""),
            "source": m.get("Name") or m.get("Source", ""),
            "destination": m.get("Destination", ""),
            "rw": bool(m.get("RW", False)),
        }
        for m in info.get("Mounts") or []
    ]
    networks = [
        {"name": name, "ip": (n or {}).get("IPAddress", ""), "aliases": ((n or {}).get("Aliases") or [])[:5]}
        for name, n in (ns.get("Networks") or {}).items()
    ]
    env_keys = sorted({str(e).split("=", 1)[0] for e in cfg.get("Env") or []})
    labels = cfg.get("Labels") or {}
    compose = {
        k.rsplit(".", 1)[-1]: labels[k]
        for k in ("com.docker.compose.project", "com.docker.compose.service")
        if labels.get(k)
    }
    restart = hc.get("RestartPolicy") or {}
    out = {
        "id": short_id(info.get("Id", "")),
        "name": (info.get("Name") or "").lstrip("/"),
        "image": cfg.get("Image", ""),
        "imageId": short_id(info.get("Image", "")),
        "created": info.get("Created", ""),
        "state": {
            "status": st.get("Status", ""),
            "running": bool(st.get("Running")),
            "paused": bool(st.get("Paused")),
            "restarting": bool(st.get("Restarting")),
            "oomKilled": bool(st.get("OOMKilled")),
            "exitCode": st.get("ExitCode", 0),
            "error": st.get("Error", ""),
            "startedAt": st.get("StartedAt", ""),
            "finishedAt": st.get("FinishedAt", ""),
        },
        "health": health_out,
        "restartCount": info.get("RestartCount", 0),
        "restartPolicy": restart.get("Name", "") + (f":{restart.get('MaximumRetryCount')}" if restart.get("MaximumRetryCount") else ""),
        "ports": cap(sorted(set(ports)), "ports"),
        "mounts": cap(mounts, "mounts"),
        "networks": cap(networks, "networks"),
        "envKeys": cap(env_keys, "envKeys"),
        "labelKeys": cap(sorted(labels.keys()), "labelKeys"),
        "compose": compose or None,
        "limits": {
            "memoryBytes": hc.get("Memory", 0),
            "nanoCpus": hc.get("NanoCpus", 0),
        },
        "privileged": bool(hc.get("Privileged")),
    }
    if notes:
        out["notes"] = notes
    return out


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+)\s?(.*)$", re.S)


async def read_log_lines(ops: Any, cid: str, tail: int, timeout: float = 20.0) -> List[str]:
    """Fetch the last ``tail`` raw log lines (timestamped) without following."""
    from ...api._dockerws import _lines, strip_docker_log_header

    stream = await with_timeout(ops.container_logs(cid, False, tail), "opening logs")
    lines: List[str] = []
    total = 0

    async def collect() -> None:
        nonlocal total
        async for raw in _lines(stream):
            raw = strip_docker_log_header(raw)
            total += len(raw)
            lines.append(raw.decode("utf-8", "replace"))
            if len(lines) > tail:
                del lines[: len(lines) - tail]
            if total > LOG_MAX_BYTES:
                break

    try:
        await with_timeout(collect(), "reading logs", timeout)
    finally:
        await close_stream(stream)
    return lines


def register(reg: ToolRegistry) -> None:
    # ------------------------------------------------------------------ hosts
    @reg.tool(
        "list_hosts",
        title="List hosts",
        description="List every Docker host managed by Kowan with live online status (local daemon, agents, edge hosts).",
        schema={"properties": {"limit": limit_prop(100, 500), "offset": OFFSET_PROP}},
        role=ROLE_VIEWER,
    )
    async def list_hosts(ctx, args):
        hosts = authz.filter_hosts(ctx.svc.store.list_hosts())
        online = await asyncio.gather(*(ping_host(ctx.svc, h) for h in hosts))
        rows = [
            {"id": h.id, "name": h.name, "type": h.type, "mode": h.mode, "address": h.address,
             "online": ok, "lastSeen": h.last_seen}
            for h, ok in zip(hosts, online)
        ]
        out = page(rows, args, 100, 500)
        out["online"] = sum(1 for r in rows if r["online"])
        return out

    @reg.tool(
        "get_host",
        title="Get host",
        description="Show one host: connection details, online status and Docker engine info (version, OS, CPUs, memory, container counts).",
        schema={"properties": {"host": HOST_PROP}},
    )
    async def get_host(ctx, args):
        host = resolve_host(ctx.svc, args.get("host"))
        ok = await ping_host(ctx.svc, host)
        out: Dict[str, Any] = {
            "id": host.id, "name": host.name, "type": host.type, "mode": host.mode,
            "address": host.address, "online": ok, "lastSeen": host.last_seen, "createdAt": host.created_at,
        }
        if ok:
            try:
                ops = ops_for_host(ctx.svc, host)
                out["system"] = to_plain(await with_timeout(ops.system_info(), "system info", 10))
            except Exception as err:  # noqa: BLE001
                out["systemError"] = str(err)
        return out

    # ------------------------------------------------------------- containers
    @reg.tool(
        "list_containers",
        title="List containers",
        description=(
            "List containers on a host (or on every online host with all_hosts=true). "
            "Filter by state (running, exited, paused, restarting, created, dead) and by a name substring. "
            "Exited containers include their exit code."
        ),
        schema={
            "properties": {
                "host": HOST_PROP,
                "all_hosts": {"type": "boolean", "description": "Query every online host (ignores host)."},
                "state": {"type": "string", "enum": ["running", "exited", "paused", "restarting", "created", "dead", "removing"]},
                "name": {"type": "string", "description": "Case-insensitive substring of the container name."},
                "include_stopped": {"type": "boolean", "description": "Include non-running containers (default true)."},
                "limit": limit_prop(100, 500),
                "offset": OFFSET_PROP,
            }
        },
        output_schema={
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
                "offset": {"type": "integer"},
                "returned": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "nextOffset": {"type": "integer"},
                "errors": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["items", "total", "returned", "truncated"],
        },
    )
    async def list_containers(ctx, args):
        include_stopped = args.get("include_stopped", True) or args.get("state") not in (None, "running")
        if args.get("all_hosts"):
            hosts = authz.filter_hosts(ctx.svc.store.list_hosts())
        else:
            hosts = [resolve_host(ctx.svc, args.get("host"))]
        errors: List[Dict[str, str]] = []

        async def one(h):
            try:
                ops = ops_for_host(ctx.svc, h)
                cs = await with_timeout(ops.list_containers(include_stopped), f"listing containers on {h.name}", 15)
                return [container_row(c, h) for c in cs]
            except Exception as err:  # noqa: BLE001
                errors.append({"host": h.name or h.id, "error": str(err)})
                return []

        rows: List[Dict[str, Any]] = []
        for chunk in await asyncio.gather(*(one(h) for h in hosts)):
            rows.extend(chunk)
        if not args.get("all_hosts") and errors:
            raise ToolError(errors[0]["error"])
        if args.get("state"):
            rows = [r for r in rows if r["state"] == args["state"]]
        if args.get("name"):
            needle = args["name"].lower()
            rows = [r for r in rows if needle in r["name"].lower()]
        rows.sort(key=lambda r: (r.get("hostName", ""), r["state"] != "running", r["name"]))
        out = page(rows, args, 100, 500)
        if errors:
            out["errors"] = errors
        return out

    @reg.tool(
        "inspect_container",
        title="Inspect container",
        description=(
            "Summarized inspect of one container: image, state and exit code, health check, restart count and policy, "
            "ports, mounts, networks and environment variable NAMES (values are never returned)."
        ),
        schema={"properties": {"host": HOST_PROP, "container": CONTAINER_PROP}, "required": ["container"]},
    )
    async def inspect_container(ctx, args):
        host, ops = resolve_ops(ctx.svc, args.get("host"))
        info = await with_timeout(ops.inspect_container(args["container"]), "inspect")
        out = summarize_inspect(info or {})
        out["hostId"] = host.id
        return out

    @reg.tool(
        "container_logs",
        title="Container logs",
        description=(
            f"Recent log lines of a container (stdout+stderr). tail defaults to {LOG_DEFAULT_TAIL}, max {LOG_MAX_TAIL}. "
            "since accepts unix seconds, RFC3339 or a relative age (15m, 2h, 1d). grep keeps only lines containing the "
            f"text (case-insensitive) and searches the last {LOG_MAX_TAIL} lines."
        ),
        schema={
            "properties": {
                "host": HOST_PROP,
                "container": CONTAINER_PROP,
                "tail": {"type": "integer", "minimum": 1, "maximum": LOG_MAX_TAIL},
                "since": {"type": ["string", "integer"], "description": "Only lines at/after this time."},
                "grep": {"type": "string", "minLength": 1, "maxLength": 200},
                "timestamps": {"type": "boolean", "description": "Prefix each line with its timestamp (default false)."},
            },
            "required": ["container"],
        },
    )
    async def container_logs(ctx, args):
        host, ops = resolve_ops(ctx.svc, args.get("host"))
        tail = int(args.get("tail") or LOG_DEFAULT_TAIL)
        since = parse_since(args.get("since"))
        grep = (args.get("grep") or "").lower()
        fetch = LOG_MAX_TAIL if (grep or since is not None) else tail
        raw = await read_log_lines(ops, args["container"], fetch)
        out_lines: List[str] = []
        for line in raw:
            m = _TS_RE.match(line)
            ts_s, msg = (m.group(1), m.group(2)) if m else ("", line)
            if since is not None and ts_s:
                ts = parse_rfc3339(ts_s)
                if ts is not None and ts < since:
                    continue
            if grep and grep not in msg.lower():
                continue
            if len(msg) > LOG_MAX_LINE:
                msg = msg[:LOG_MAX_LINE] + " …[line truncated]"
            out_lines.append(f"{ts_s} {msg}" if args.get("timestamps") and ts_s else msg)
        truncated = len(out_lines) > tail
        out_lines = out_lines[-tail:]
        header = f"# {len(out_lines)} line(s) from {args['container']} on {host.name}"
        if grep:
            header += f" matching {args['grep']!r}"
        if truncated:
            header += f" (older matches omitted; tail={tail})"
        data = {"container": args["container"], "hostId": host.id, "lines": out_lines,
                "returned": len(out_lines), "truncated": truncated}
        return ToolOutput(data, header + "\n" + "\n".join(out_lines))

    @reg.tool(
        "container_stats",
        title="Container stats",
        description="One-shot resource usage of a container: CPU %, memory usage/limit, network and block I/O, PIDs.",
        schema={"properties": {"host": HOST_PROP, "container": CONTAINER_PROP}, "required": ["container"]},
        output_schema={
            "type": "object",
            "properties": {
                "cpuPercent": {"type": "number"}, "memUsage": {"type": "integer"}, "memLimit": {"type": "integer"},
                "memPercent": {"type": "number"}, "netRx": {"type": "integer"}, "netTx": {"type": "integer"},
                "blkRead": {"type": "integer"}, "blkWrite": {"type": "integer"}, "pids": {"type": "integer"},
                "human": {"type": "object"},
            },
            "required": ["cpuPercent", "memUsage", "memLimit"],
        },
    )
    async def container_stats(ctx, args):
        _host, ops = resolve_ops(ctx.svc, args.get("host"))
        st = to_plain(await with_timeout(ops.container_stats(args["container"]), "stats"))
        st["human"] = {
            "cpu": f"{st.get('cpuPercent', 0):.1f}%",
            "memory": f"{human_bytes(st.get('memUsage', 0))} / {human_bytes(st.get('memLimit', 0))} ({st.get('memPercent', 0):.1f}%)",
            "network": f"rx {human_bytes(st.get('netRx', 0))}, tx {human_bytes(st.get('netTx', 0))}",
            "blockIO": f"read {human_bytes(st.get('blkRead', 0))}, write {human_bytes(st.get('blkWrite', 0))}",
        }
        return st

    # ------------------------------------------------------------ images etc.
    @reg.tool(
        "list_images",
        title="List images",
        description="List images on a host, largest first, with tags and size. dangling=true shows only untagged images.",
        schema={"properties": {"host": HOST_PROP, "dangling": {"type": "boolean"}, "name": {"type": "string"},
                               "limit": limit_prop(100, 500), "offset": OFFSET_PROP}},
    )
    async def list_images(ctx, args):
        _host, ops = resolve_ops(ctx.svc, args.get("host"))
        images = await with_timeout(ops.list_images(), "list images")
        rows = []
        for im in images:
            tags = [t for t in (im.repo_tags or []) if t != "<none>:<none>"]
            rows.append({"id": short_id(im.id), "tags": tags, "size": im.size, "sizeHuman": human_bytes(im.size),
                         "created": iso(im.created) if im.created else ""})
        if args.get("dangling"):
            rows = [r for r in rows if not r["tags"]]
        if args.get("name"):
            n = args["name"].lower()
            rows = [r for r in rows if any(n in t.lower() for t in r["tags"])]
        rows.sort(key=lambda r: -r["size"])
        out = page(rows, args, 100, 500)
        out["totalSize"] = human_bytes(sum(r["size"] for r in rows))
        return out

    @reg.tool(
        "list_volumes",
        title="List volumes",
        description="List Docker volumes on a host (name, driver, mountpoint, compose project).",
        schema={"properties": {"host": HOST_PROP, "name": {"type": "string"}, "limit": limit_prop(100, 500), "offset": OFFSET_PROP}},
    )
    async def list_volumes(ctx, args):
        _host, ops = resolve_ops(ctx.svc, args.get("host"))
        vols = await with_timeout(ops.list_volumes(), "list volumes")
        rows = [{"name": v.name, "driver": v.driver, "mountpoint": v.mountpoint, "createdAt": v.created_at,
                 "composeProject": (v.labels or {}).get("com.docker.compose.project", "")} for v in vols]
        if args.get("name"):
            rows = [r for r in rows if args["name"].lower() in r["name"].lower()]
        rows.sort(key=lambda r: r["name"])
        return page(rows, args, 100, 500)

    @reg.tool(
        "list_networks",
        title="List networks",
        description="List Docker networks on a host (name, driver, scope, internal, subnets).",
        schema={"properties": {"host": HOST_PROP, "limit": limit_prop(100, 500), "offset": OFFSET_PROP}},
    )
    async def list_networks(ctx, args):
        _host, ops = resolve_ops(ctx.svc, args.get("host"))
        nets = await with_timeout(ops.list_networks(), "list networks")
        rows = [{"id": short_id(n.id), "name": n.name, "driver": n.driver, "scope": n.scope, "internal": n.internal,
                 "subnets": [i.subnet for i in (n.ipam or []) if i.subnet]} for n in nets]
        rows.sort(key=lambda r: r["name"])
        return page(rows, args, 100, 500)

    # ----------------------------------------------------------------- events
    @reg.tool(
        "recent_events",
        title="Recent Docker events",
        description=(
            "Docker engine events on a host, newest first (container die/start/oom/health_status, image pulls, ...). "
            "since defaults to 1h and is capped at 24h."
        ),
        schema={"properties": {
            "host": HOST_PROP,
            "since": {"type": ["string", "integer"], "description": "Unix seconds, RFC3339 or relative age (default 1h)."},
            "type": {"type": "string", "enum": ["container", "image", "network", "volume"]},
            "action": {"type": "string", "description": "Only this action, e.g. die, oom, health_status, start."},
            "limit": limit_prop(100, EVENTS_MAX),
        }},
    )
    async def recent_events(ctx, args):
        from ...api import events as ev
        from ...api._dockerws import _lines

        _host, ops = resolve_ops(ctx.svc, args.get("host"))
        now = int(time.time())
        since = parse_since(args.get("since"))
        since_i = ev._clamp_since(int(since) if since is not None else None, now, ev.DEFAULT_LOOKBACK)
        filters = {"type": [args["type"]]} if args.get("type") else None
        try:
            stream = await with_timeout(ops.events(since_i, now, filters), "opening events")
        except NotImplementedError as err:
            raise ToolError("events are not supported by this host") from err
        out: List[Dict[str, Any]] = []

        async def collect():
            async for line in _lines(stream):
                e = ev.parse_event_line(line)
                if e is None:
                    continue
                if args.get("action") and e["action"] != args["action"]:
                    continue
                out.append(e)
                if len(out) > EVENTS_MAX * 2:
                    del out[: len(out) - EVENTS_MAX]

        try:
            await asyncio.wait_for(collect(), timeout=ev.HISTORY_TIMEOUT)
        except asyncio.TimeoutError:
            pass
        finally:
            await close_stream(stream)
        out.reverse()
        rows = [{**{k: v for k, v in e.items() if k != "id"}, "actorId": short_id(e.get("actorId", ""))} for e in out]
        res = page(rows, args, 100, EVENTS_MAX)
        res["since"] = iso(since_i)
        return res

    # ---------------------------------------------------------------- updates
    @reg.tool(
        "check_updates",
        title="Check image updates",
        description=(
            "Compare each container's local image digest with its registry to see which containers have an image "
            "update available. Results are cached; refresh=true forces a registry lookup."
        ),
        schema={"properties": {"host": HOST_PROP, "refresh": {"type": "boolean"},
                               "only_available": {"type": "boolean", "description": "Only rows with an update (default false)."},
                               "limit": limit_prop(100, 500), "offset": OFFSET_PROP}},
        open_world=True,
    )
    async def check_updates(ctx, args):
        from ...api.updates import check_host

        _host, ops = resolve_ops(ctx.svc, args.get("host"))
        rows = await with_timeout(check_host(ctx.svc, ops, refresh=bool(args.get("refresh"))), "update check", 120)
        for r in rows:
            r["containerId"] = short_id(r.get("containerId", ""))
            r.pop("localDigest", None)
            r.pop("remoteDigest", None)
        if args.get("only_available"):
            rows = [r for r in rows if r.get("updateAvailable")]
        rows.sort(key=lambda r: (not r.get("updateAvailable"), r.get("containerName", "")))
        out = page(rows, args, 100, 500)
        out["updatesAvailable"] = sum(1 for r in rows if r.get("updateAvailable"))
        return out
