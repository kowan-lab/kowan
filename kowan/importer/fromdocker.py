"""Migrate: make a workload Kowan only discovered into a Kowan deployment,
from Docker itself (no Portainer needed).

* A compose stack: its original files, when the host still has them. Compose
  labels every container with the project's working folder and compose
  files (``com.docker.compose.project.working_dir`` / ``.config_files``); a
  throwaway container on that host mounts those folders read-only and prints
  the ``.yml``/``.yaml`` files and the folder's ``.env``. Relative bind paths
  (``./data``) are made absolute, since Kowan deploys from elsewhere.
* Otherwise (files gone, a swarm stack, a container started by hand) the
  compose file is rebuilt from what Docker reports for each container
  (``inspect``), leaving out what the image already sets.
* A lone container becomes a container deployment from its own spec.

The deployment takes the project's (or the container's) name, so deploying it
takes over the same containers, networks and volumes instead of starting new
ones; volumes are declared ``external`` under their real names, so their data
stays. Values of secret-looking variables move to stack variables (stored
encrypted), referenced as ``${NAME}``.
"""

from __future__ import annotations

import base64
import posixpath
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from kowan.workloads import compose_deployments, parse_workload, workload_of

LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"
LABEL_WORKDIR = "com.docker.compose.project.working_dir"
LABEL_FILES = "com.docker.compose.project.config_files"
LABEL_ONEOFF = "com.docker.compose.oneoff"
LABEL_DEPENDS = "com.docker.compose.depends_on"
LABEL_STACK = "com.docker.stack.namespace"
LABEL_SWARM_SERVICE = "com.docker.swarm.service.name"

_SECRET = re.compile(r"(PASS|SECRET|TOKEN|KEY|PWD|CREDENTIAL|AUTH|PRIVATE|SALT|DSN)", re.I)
_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_FILE = 1 << 20
_DROP_LABELS = ("com.docker.compose.", "com.docker.swarm.", "com.docker.stack.", "desktop.docker.io")


class MigrateError(ValueError):
    """The workload cannot be migrated (gone, managed already, malformed)."""


# ---------------------------------------------------------------------------
# Rebuilding a compose file from inspect documents
# ---------------------------------------------------------------------------

def _dur(ns: Any) -> Optional[str]:
    try:
        n = int(ns or 0)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    s = n / 1e9
    return f"{int(s)}s" if s == int(s) else f"{s:g}s"


def _escape(v: str) -> str:
    """A literal value in compose: a $ would be interpolated."""
    return v.replace("$", "$$")


def _short(name: str, project: str) -> str:
    return name[len(project) + 1:] if project and name.startswith(project + "_") else name


def service_from_inspect(insp: Dict[str, Any], image_cfg: Dict[str, Any], project: str,
                         volumes: Dict[str, Dict[str, Any]], networks: Dict[str, Dict[str, Any]],
                         variables: List[Dict[str, Any]], warnings: List[str]) -> Dict[str, Any]:
    """The compose service of one container. Adds the top-level volumes and
    networks it needs, and the secret-looking variables it moved out."""
    cfg = insp.get("Config") or {}
    hc = insp.get("HostConfig") or {}
    name = str(insp.get("Name") or "").lstrip("/")
    svc: Dict[str, Any] = {"image": re.sub(r"@sha256:[0-9a-f]+$", "", str(cfg.get("Image") or ""))}
    if cfg.get("Entrypoint") and cfg.get("Entrypoint") != image_cfg.get("Entrypoint"):
        svc["entrypoint"] = cfg["Entrypoint"]
    if cfg.get("Cmd") and cfg.get("Cmd") != image_cfg.get("Cmd"):
        svc["command"] = cfg["Cmd"]
    img_env = set(image_cfg.get("Env") or [])
    env: Dict[str, str] = {}
    for kv in cfg.get("Env") or []:
        if kv in img_env:
            continue
        k, _, v = str(kv).partition("=")
        if _SECRET.search(k) and _VAR.match(k) and v:
            if not any(x["key"] == k for x in variables):
                variables.append({"key": k, "value": v, "secret": True})
            elif next(x for x in variables if x["key"] == k)["value"] != v:
                warnings.append(f"{name}: {k} differs between containers; the first value is used")
            env[k] = "${" + k + "}"
        else:
            env[k] = _escape(v)
    if env:
        svc["environment"] = env
    ports = []
    for key, binds in sorted((hc.get("PortBindings") or {}).items()):
        cport, _, proto = key.partition("/")
        for b in binds or []:
            hp, ip = b.get("HostPort") or "", b.get("HostIp") or ""
            p = f"{hp}:{cport}" if hp else cport
            if ip and ip not in ("0.0.0.0", "::"):
                p = f"{ip}:{p}"
            ports.append(p + ("" if proto in ("", "tcp") else f"/{proto}"))
    if ports:
        svc["ports"] = sorted(set(ports))
    mounts = []
    for m in insp.get("Mounts") or []:
        dest, ro = m.get("Destination", ""), not m.get("RW", True)
        if m.get("Type") == "volume":
            real = m.get("Name", "")
            short = _short(real, project)
            volumes[short] = {"external": True, "name": real}
            mounts.append(f"{short}:{dest}" + (":ro" if ro else ""))
        elif m.get("Type") == "bind":
            mounts.append(f"{m.get('Source', '')}:{dest}" + (":ro" if ro else ""))
        elif m.get("Type") == "tmpfs":
            svc.setdefault("tmpfs", []).append(dest)
    if mounts:
        svc["volumes"] = mounts
    mode = str(hc.get("NetworkMode") or "")
    if mode == "host":
        svc["network_mode"] = "host"
    elif mode.startswith("container:"):
        warnings.append(f"{name}: shares another container's network ({mode}); set network_mode by hand")
    else:
        nets = sorted(((insp.get("NetworkSettings") or {}).get("Networks") or {}).keys())
        own = [n for n in nets if n not in (f"{project}_default", "bridge")]
        if own:
            svc["networks"] = [_short(n, project) for n in own]
            for n in own:
                networks[_short(n, project)] = {"external": True, "name": n}
            if f"{project}_default" in nets:
                svc["networks"].append("default")
    policy = (hc.get("RestartPolicy") or {}).get("Name") or ""
    if policy and policy != "no":
        svc["restart"] = policy
    img_labels = image_cfg.get("Labels") or {}
    labels = {k: v for k, v in (cfg.get("Labels") or {}).items()
              if not k.startswith(_DROP_LABELS) and img_labels.get(k) != v}
    if labels:
        svc["labels"] = labels
    hcheck = cfg.get("Healthcheck") or {}
    if hcheck.get("Test") and hcheck != (image_cfg.get("Healthcheck") or {}):
        h: Dict[str, Any] = {"test": hcheck["Test"]}
        for src, dst in (("Interval", "interval"), ("Timeout", "timeout"), ("StartPeriod", "start_period")):
            if _dur(hcheck.get(src)):
                h[dst] = _dur(hcheck.get(src))
        if hcheck.get("Retries"):
            h["retries"] = hcheck["Retries"]
        svc["healthcheck"] = h
    for src, dst in (("WorkingDir", "working_dir"), ("User", "user")):
        if cfg.get(src) and cfg.get(src) != image_cfg.get(src):
            svc[dst] = cfg[src]
    if cfg.get("Tty"):
        svc["tty"] = True
    if cfg.get("OpenStdin"):
        svc["stdin_open"] = True
    if hc.get("Privileged"):
        svc["privileged"] = True
    for src, dst in (("CapAdd", "cap_add"), ("CapDrop", "cap_drop"), ("ExtraHosts", "extra_hosts")):
        if hc.get(src):
            svc[dst] = list(hc[src])
    if hc.get("Devices"):
        svc["devices"] = [f"{d.get('PathOnHost')}:{d.get('PathInContainer')}" for d in hc["Devices"]]
    if hc.get("Memory"):
        svc["mem_limit"] = int(hc["Memory"])
    if hc.get("NanoCpus"):
        svc["cpus"] = round(int(hc["NanoCpus"]) / 1e9, 3)
    log_cfg = hc.get("LogConfig") or {}
    if log_cfg.get("Type") and (log_cfg["Type"] != "json-file" or log_cfg.get("Config")):
        svc["logging"] = {"driver": log_cfg["Type"], **({"options": log_cfg["Config"]} if log_cfg.get("Config") else {})}
    deps = [d.split(":", 1)[0] for d in str((cfg.get("Labels") or {}).get(LABEL_DEPENDS, "")).split(",") if d]
    if deps:
        svc["depends_on"] = deps
    return svc


def rebuild_compose(inspects: List[Tuple[Dict[str, Any], Dict[str, Any]]], project: str, swarm: bool = False
                    ) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """``(compose YAML, stack variables, warnings)`` from ``(container
    inspect, its image's Config)`` pairs of one stack."""
    services: Dict[str, Dict[str, Any]] = {}
    replicas: Dict[str, int] = {}
    volumes: Dict[str, Dict[str, Any]] = {}
    networks: Dict[str, Dict[str, Any]] = {}
    variables: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for insp, image_cfg in inspects:
        labels = (insp.get("Config") or {}).get("Labels") or {}
        name = str(insp.get("Name") or "").lstrip("/")
        svc_name = labels.get(LABEL_SERVICE) or _short(labels.get(LABEL_SWARM_SERVICE, ""), project) or name
        replicas[svc_name] = replicas.get(svc_name, 0) + 1
        if svc_name in services:
            continue
        svc = service_from_inspect(insp, image_cfg, project, volumes, networks, variables, warnings)
        if not swarm and not re.match(rf"^{re.escape(project)}[-_]{re.escape(svc_name)}[-_]\d+$", name) and name != svc_name:
            svc["container_name"] = name
        services[svc_name] = svc
    for s, n in replicas.items():
        if n > 1:
            if swarm:
                services[s].setdefault("deploy", {})["replicas"] = n
            else:
                services[s]["scale"] = n
    doc: Dict[str, Any] = {"services": services}
    if volumes:
        doc["volumes"] = volumes
    if networks:
        doc["networks"] = networks
    if swarm:
        warnings.append("rebuilt from the running tasks: the services' placement, update and resource settings "
                        "are not visible from a container; review the deploy sections")
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False), variables, warnings


# ---------------------------------------------------------------------------
# The original files, read on the host
# ---------------------------------------------------------------------------

def absolutize(compose: str, workdir: str) -> Tuple[str, List[str]]:
    """Relative bind sources (``./data``) made absolute under ``workdir``;
    warnings for what still depends on the folder (build, env_file)."""
    warnings: List[str] = []
    try:
        doc = yaml.safe_load(compose)
    except yaml.YAMLError:
        return compose, ["the compose file did not parse; check it before deploying"]
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        return compose, []
    changed = False

    def fix(src: str) -> str:
        nonlocal changed
        if src.startswith(("./", "../")) or src in (".", ".."):
            changed = True
            return posixpath.normpath(posixpath.join(workdir, src))
        return src

    for name, svc in doc["services"].items():
        if not isinstance(svc, dict):
            continue
        vols = svc.get("volumes") or []
        for i, v in enumerate(vols):
            if isinstance(v, str) and ":" in v:
                src, rest = v.split(":", 1)
                vols[i] = f"{fix(src)}:{rest}"
            elif isinstance(v, dict) and v.get("type") == "bind" and isinstance(v.get("source"), str):
                v["source"] = fix(v["source"])
        if "build" in svc:
            warnings.append(f"{name}: builds from the folder on the host; Kowan needs an image or a git source")
        if "env_file" in svc:
            warnings.append(f"{name}: reads env_file from the folder on the host; move it into stack variables")
    return (yaml.safe_dump(doc, sort_keys=False, default_flow_style=False) if changed else compose), warnings


async def read_host_files(ops: Any, workdir: str, files: List[str]) -> Dict[str, str]:
    """The compose files and the ``.env`` of a project folder on its host,
    read by a throwaway container that mounts their folders read-only. Only
    ``.yml``/``.yaml`` files and ``.env``; missing ones are left out."""
    from kowan.volbackup.executor import run_helper

    wanted = [f for f in files if f.endswith((".yml", ".yaml"))]
    if workdir:
        wanted.append(posixpath.join(workdir, ".env"))
    dirs = sorted({posixpath.dirname(f) for f in wanted})
    if not dirs:
        return {}
    mount = {d: f"/kowan/h{i}" for i, d in enumerate(dirs)}
    lines = []
    for f in wanted:
        inside = posixpath.join(mount[posixpath.dirname(f)], posixpath.basename(f))
        lines.append(f'if [ -f "{inside}" ] && [ $(wc -c < "{inside}") -le {_MAX_FILE} ]; then '
                     f'echo "@@FILE {f}"; base64 "{inside}" | tr -d "\\n"; echo; fi')
    res = await run_helper(ops, "\n".join(lines), env={}, binds=[(d, m, True) for d, m in mount.items()],
                           timeout=60, label="migrate")
    out: Dict[str, str] = {}
    current = ""
    for line in res.lines:
        if line.startswith("@@FILE "):
            current = line[len("@@FILE "):]
        elif current and line.strip():
            try:
                out[current] = base64.b64decode(line.strip()).decode("utf-8", "replace")
            except ValueError:
                pass
            current = ""
    return out


def _parse_env(text: str) -> Dict[str, str]:
    from kowan.deployment import gitsource

    return gitsource.parse_env_file(text)


# ---------------------------------------------------------------------------
# A workload, end to end
# ---------------------------------------------------------------------------

async def _containers(svc: Any, host_id: str, workload: str) -> List[Any]:
    ops = svc.docker.ops(svc.store.get_host(host_id))
    deps = compose_deployments(svc.store)
    out = []
    for c in await ops.list_containers(True):
        labels = c.labels or {}
        if labels.get("kowan.support") == "true" or labels.get(LABEL_ONEOFF) == "True":
            continue
        name = ((c.names or [""])[0]).lstrip("/")
        if workload_of(labels, host_id, name, deps)[0] == workload:
            out.append(c)
    return out


async def scan(svc: Any, workload: str) -> Dict[str, Any]:
    """What migrating a discovered workload would create: an import candidate
    (the shape ``/import/apply`` takes) with where it came from."""
    try:
        kind, host_id, rest = parse_workload(workload)
    except ValueError as err:
        raise MigrateError(str(err)) from None
    if kind == "dep":
        raise MigrateError("this workload is already a Kowan deployment")
    ops = svc.docker.ops(svc.store.get_host(host_id))
    containers = await _containers(svc, host_id, workload)
    if not containers:
        raise MigrateError("no container of this workload on its host")
    warnings: List[str] = []
    base = {"workload": workload, "host": host_id, "name": rest, "warnings": warnings, "importable": True}

    if kind == "ctr":
        c = containers[0]
        spec = await ops.container_spec(c.id)
        data = spec.model_dump(by_alias=True)
        if not data.get("image"):
            raise MigrateError("the container has no image to run")
        return {**base, "type": "container", "source": "container", "spec": data, "strategy": "standalone"}

    labels = containers[0].labels or {}
    swarm = bool(labels.get(LABEL_STACK)) and not labels.get(LABEL_PROJECT)
    workdir = labels.get(LABEL_WORKDIR, "")
    files = [f for f in (labels.get(LABEL_FILES) or "").split(",") if f]
    if not swarm and files:
        try:
            found = await read_host_files(ops, workdir, files)
        except Exception as err:  # noqa: BLE001 - no helper image, host refused the mount
            found = {}
            warnings.append(f"could not read the compose files on the host: {err}")
        composes = [found[f] for f in files if f in found]
        if len(composes) == 1:
            compose, more = absolutize(composes[0], workdir)
            warnings += more
            env = _parse_env(found.get(posixpath.join(workdir, ".env"), ""))
            variables = [{"key": k, "value": v, "secret": bool(_SECRET.search(k))} for k, v in env.items() if _VAR.match(k)]
            return {**base, "type": "compose", "source": "files", "path": files[0], "strategy": "standalone",
                    "compose": compose, "variables": variables}
        if len(composes) > 1:
            warnings.append(f"the stack was started from {len(files)} compose files; Kowan rebuilt one from the containers")
        elif files:
            warnings.append(f"{files[0]} is no longer on the host; Kowan rebuilt the compose file from the containers")
    pairs = []
    for c in containers:
        insp = await ops.inspect_container(c.id) or {}
        try:
            img = (await ops.inspect_image(insp.get("Image") or "") or {}).get("Config") or {}
        except Exception:  # noqa: BLE001 - image gone: keep everything
            img = {}
        pairs.append((insp, img))
    compose, variables, more = rebuild_compose(pairs, rest, swarm=swarm)
    warnings += more
    return {**base, "type": "compose", "source": "rebuilt", "strategy": "swarm" if swarm else "standalone",
            "compose": compose, "variables": variables}
