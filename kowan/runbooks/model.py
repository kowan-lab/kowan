"""What a runbook is, how it is validated, and where else runbooks come from.

A runbook is a named list of steps. Each step runs a shell command on a
workload (``dep~…``, ``stack~…``, ``ctr~…``, see :mod:`kowan.workloads`),
optionally on one of its services:

* ``exec``: inside a running container of the service (one replica or all),
  like ``docker exec``;
* ``run``: in a throwaway container made from the service's own image,
  networks, volumes and environment, like ``docker compose run``, removed
  when it ends.

Steps run in order and the run stops at the first one that fails, unless
that step says ``continueOnError``. Steps may target different workloads.

Parameters are named values the user fills in when running it. They reach the
command as environment variables (``$TABLE``), never pasted into it, so a
value is data and cannot become a command.

Runbooks also come from container labels, next to the service they act on::

    kowan.runbook.migrate.command: python manage.py migrate
    kowan.runbook.migrate.title: Migrate the database
    kowan.runbook.migrate.mode: run
    kowan.runbook.migrate.confirm: "true"
    kowan.runbook.migrate.params: "APP, VERBOSITY=1"

Those are read-only in Kowan (the compose file is their source).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from kowan.workloads import LABEL_SWARM_SERVICE, LABEL_SWARM_STACK, parse_workload

MODE_EXEC = "exec"
MODE_RUN = "run"
MODES = (MODE_EXEC, MODE_RUN)
REPLICAS = ("one", "all")
PARAM_TYPES = ("text", "number", "choice")

MAX_STEPS = 20
MAX_PARAMS = 20
MAX_COMMAND = 20000
MAX_VALUE = 4096
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 3600

LABEL_PREFIX = "kowan.runbook."
LABEL_COMPOSE_SERVICE = "com.docker.compose.service"
LABEL_SUPPORT = "kowan.support"
# Put on the throwaway containers of ``run`` steps (with ``kowan.support``).
LABEL_RUN = "kowan.runbook-run"

_PARAM_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_LABEL_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_RUNBOOK_FIELDS = {"name", "description", "confirm", "params", "steps"}
_PARAM_FIELDS = {"name", "label", "type", "choices", "default", "required"}
_STEP_FIELDS = {"title", "workload", "service", "mode", "replicas", "command", "continueOnError", "timeoutSeconds"}


class RunbookError(ValueError):
    """A runbook or a run request that is not valid (a 400)."""


def service_of(labels: Dict[str, str]) -> str:
    """The service a container is a replica of: the compose service, or the
    swarm service without its stack prefix; "" for a lone container."""
    labels = labels or {}
    if labels.get(LABEL_COMPOSE_SERVICE):
        return labels[LABEL_COMPOSE_SERVICE]
    svc = labels.get(LABEL_SWARM_SERVICE, "")
    stack = labels.get(LABEL_SWARM_STACK, "")
    if svc and stack and svc.startswith(stack + "_"):
        return svc[len(stack) + 1:]
    return svc


def _text(v: Any, field: str, limit: int, required: bool = False) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise RunbookError(f"{field} must be text")
    v = v.strip()
    if required and not v:
        raise RunbookError(f"{field} is required")
    if len(v) > limit:
        raise RunbookError(f"{field} is longer than {limit} characters")
    return v


def _unknown(body: Dict[str, Any], allowed: set, where: str) -> None:
    extra = sorted(set(body) - allowed)
    if extra:
        raise RunbookError(f"{where}: unknown field {extra[0]!r}")


def validate_param(p: Any, i: int) -> Dict[str, Any]:
    if not isinstance(p, dict):
        raise RunbookError(f"params[{i}] must be an object")
    _unknown(p, _PARAM_FIELDS, f"params[{i}]")
    name = _text(p.get("name"), f"params[{i}].name", 64, True)
    if not _PARAM_NAME.match(name):
        raise RunbookError(f"params[{i}].name must be an environment variable name such as TABLE or DAYS")
    ptype = p.get("type") or "text"
    if ptype not in PARAM_TYPES:
        raise RunbookError(f"params[{i}].type must be one of {', '.join(PARAM_TYPES)}")
    choices = p.get("choices") or []
    if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
        raise RunbookError(f"params[{i}].choices must be a list of texts")
    choices = [c.strip() for c in choices if c.strip()]
    if ptype == "choice" and not choices:
        raise RunbookError(f"params[{i}]: a choice needs choices")
    out = {
        "name": name,
        "label": _text(p.get("label"), f"params[{i}].label", 120),
        "type": ptype,
        "choices": choices if ptype == "choice" else [],
        "default": _text(p.get("default"), f"params[{i}].default", MAX_VALUE),
        "required": bool(p.get("required", False)),
    }
    if out["default"]:
        check_value(out, out["default"])
    return out


def validate_step(s: Any, i: int) -> Dict[str, Any]:
    if not isinstance(s, dict):
        raise RunbookError(f"steps[{i}] must be an object")
    _unknown(s, _STEP_FIELDS, f"steps[{i}]")
    workload = _text(s.get("workload"), f"steps[{i}].workload", 300, True)
    group = group_of(workload)
    if group is not None:
        if not _GROUP.match(group):
            raise RunbookError(f"steps[{i}].workload: not a host group")
        if not str(s.get("service") or "").strip():
            raise RunbookError(f"steps[{i}].service: a step on a host group names the service or container it runs in")
    else:
        try:
            parse_workload(workload)
        except ValueError as err:
            raise RunbookError(f"steps[{i}].workload: {err}") from None
    mode = s.get("mode") or MODE_EXEC
    if mode not in MODES:
        raise RunbookError(f"steps[{i}].mode must be exec or run")
    replicas = s.get("replicas") or "one"
    if replicas not in REPLICAS:
        raise RunbookError(f"steps[{i}].replicas must be one or all")
    timeout = s.get("timeoutSeconds", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
        raise RunbookError(f"steps[{i}].timeoutSeconds must be between 1 and {MAX_TIMEOUT}")
    return {
        "title": _text(s.get("title"), f"steps[{i}].title", 120),
        "workload": workload,
        "service": _text(s.get("service"), f"steps[{i}].service", 200),
        "mode": mode,
        "replicas": replicas,
        "command": _text(s.get("command"), f"steps[{i}].command", MAX_COMMAND, True),
        "continueOnError": bool(s.get("continueOnError", False)),
        "timeoutSeconds": timeout,
    }


def validate_runbook(body: Any) -> Dict[str, Any]:
    """The stored shape of a runbook from an API body (raises RunbookError)."""
    if not isinstance(body, dict):
        raise RunbookError("invalid request body")
    _unknown(body, _RUNBOOK_FIELDS, "runbook")
    params = body.get("params") or []
    steps = body.get("steps") or []
    if not isinstance(params, list) or len(params) > MAX_PARAMS:
        raise RunbookError(f"params must be a list of at most {MAX_PARAMS}")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise RunbookError(f"a runbook has between 1 and {MAX_STEPS} steps")
    out_params = [validate_param(p, i) for i, p in enumerate(params)]
    names = [p["name"] for p in out_params]
    if len(set(names)) != len(names):
        raise RunbookError("two parameters have the same name")
    return {
        "name": _text(body.get("name"), "name", 80, True),
        "description": _text(body.get("description"), "description", 500),
        "confirm": bool(body.get("confirm", False)),
        "params": out_params,
        "steps": [validate_step(s, i) for i, s in enumerate(steps)],
    }


def check_value(param: Dict[str, Any], value: str) -> str:
    if len(value) > MAX_VALUE or "\x00" in value:
        raise RunbookError(f"{param['name']}: value too long or not text")
    if param["type"] == "number":
        try:
            float(value)
        except ValueError:
            raise RunbookError(f"{param['name']} must be a number") from None
    if param["type"] == "choice" and value not in param["choices"]:
        raise RunbookError(f"{param['name']} must be one of {', '.join(param['choices'])}")
    return value


def resolve_params(runbook: Dict[str, Any], given: Any) -> Dict[str, str]:
    """The values a run uses: the given ones, else the defaults; checked."""
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise RunbookError("params must be an object of name: value")
    known = {p["name"]: p for p in runbook.get("params") or []}
    extra = sorted(set(given) - set(known))
    if extra:
        raise RunbookError(f"unknown parameter {extra[0]!r}")
    out: Dict[str, str] = {}
    for name, p in known.items():
        raw = given.get(name)
        value = p.get("default", "") if raw is None or raw == "" else str(raw)
        if not value:
            if p.get("required"):
                raise RunbookError(f"{name} is required")
            out[name] = ""
            continue
        out[name] = check_value(p, value)
    return out


def workloads_of(runbook: Dict[str, Any]) -> List[str]:
    """The workloads a runbook acts on, in step order, once each."""
    seen: List[str] = []
    for s in runbook.get("steps") or []:
        if s["workload"] not in seen:
            seen.append(s["workload"])
    return seen


def access_target(workload: str) -> Tuple[str, str, str]:
    """The authorization target (``TargetAccess``) of a workload id."""
    kind, host, rest = parse_workload(workload)
    return {"dep": ("deployment", "", rest), "stack": ("stack", host, rest), "ctr": ("container", host, rest)}[kind]


GROUP_PREFIX = "group~"
_GROUP = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


def group_of(workload: str) -> Optional[str]:
    """The tag of a ``group~<tag>`` target (every host in a group), else None."""
    return workload[len(GROUP_PREFIX):] if workload.startswith(GROUP_PREFIX) else None


def access_targets(workload: str, store: Any) -> List[Tuple[str, str, str]]:
    """The authorization targets of a step's target: the workload, or, for a
    host group, each of its hosts (host-wide access; an empty group needs
    fleet-wide access)."""
    group = group_of(workload)
    if group is None:
        return [access_target(workload)]
    hosts = store.hosts_with_tag(group)
    return [("host", h, "") for h in hosts] or [("deployment", "", "")]


def runbook_targets(rb: Dict[str, Any], store: Any) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    for w in workloads_of(rb):
        out += access_targets(w, store)
    return out


# ---------------------------------------------------------------------------
# Runbooks declared with labels
# ---------------------------------------------------------------------------

def label_id(workload: str, service: str, key: str) -> str:
    return f"label:{workload}:{service}:{key}"


def parse_label_id(rid: str) -> Optional[Tuple[str, str, str]]:
    if not rid.startswith("label:"):
        return None
    parts = rid[len("label:"):].split(":")
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _label_params(raw: str) -> List[Dict[str, Any]]:
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, default = item.partition("=")
        out.append({"name": name.strip(), "default": default.strip(), "type": "text"})
    return out


def from_labels(labels: Dict[str, str], workload: str) -> List[Dict[str, Any]]:
    """The runbooks a container's labels declare for its own service.
    Malformed ones are skipped (a container label cannot answer a 400)."""
    keys = sorted({k[len(LABEL_PREFIX):].split(".", 1)[0] for k in (labels or {}) if k.startswith(LABEL_PREFIX)})
    service = service_of(labels)
    out = []
    for key in keys:
        if not _LABEL_KEY.match(key):
            continue
        get = lambda f: labels.get(f"{LABEL_PREFIX}{key}.{f}", "")  # noqa: E731
        try:
            timeout = int(get("timeout") or DEFAULT_TIMEOUT)
        except ValueError:
            timeout = DEFAULT_TIMEOUT
        body = {
            "name": get("title") or key,
            "description": get("description"),
            "confirm": get("confirm").lower() in ("1", "true", "yes"),
            "params": _label_params(get("params")),
            "steps": [{"workload": workload, "service": service, "command": get("command"),
                       "mode": get("mode") or MODE_EXEC, "replicas": get("replicas") or "one",
                       "timeoutSeconds": timeout}],
        }
        try:
            rb = validate_runbook(body)
        except RunbookError:
            continue
        rb.update({"id": label_id(workload, service, key), "source": "labels",
                   "createdBy": "", "createdAt": "", "updatedAt": ""})
        out.append(rb)
    return out


# ---------------------------------------------------------------------------
# Suggestions by image
# ---------------------------------------------------------------------------

# (image name fragment, name, description, command, mode, confirm, params)
_SUGGESTIONS = [
    ("redis", "Redis memory", "How much memory Redis uses and for what.", "redis-cli INFO memory", MODE_EXEC, False, []),
    ("redis", "Empty the Redis cache", "Delete every key of the current database.", "redis-cli FLUSHDB", MODE_EXEC, True, []),
    ("postgres", "Vacuum and analyze", "Reclaim space and refresh the planner statistics.",
     'psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" -c "VACUUM (ANALYZE, VERBOSE);"', MODE_EXEC, False, []),
    ("postgres", "Database sizes", "The size of every database.",
     'psql -U "${POSTGRES_USER:-postgres}" -c "SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database ORDER BY 2 DESC;"',
     MODE_EXEC, False, []),
    ("mysql", "Check and optimize tables", "mysqlcheck on every database.",
     'mysqlcheck -u root -p"$MYSQL_ROOT_PASSWORD" --all-databases --optimize', MODE_EXEC, False, []),
    ("mariadb", "Check and optimize tables", "mariadb-check on every database.",
     'mariadb-check -u root -p"$MARIADB_ROOT_PASSWORD" --all-databases --optimize', MODE_EXEC, False, []),
    ("mongo", "Database stats", "Size and objects of a database.",
     'mongosh --quiet --eval "printjson(db.getSiblingDB(process.env.DB).stats())"', MODE_EXEC, False,
     [{"name": "DB", "label": "Database", "default": "admin"}]),
    ("nginx", "Reload nginx", "Check the configuration, then reload it without dropping connections.",
     "nginx -t && nginx -s reload", MODE_EXEC, False, []),
    ("traefik", "Traefik health", "Ask Traefik whether it is healthy.", "traefik healthcheck", MODE_EXEC, False, []),
    ("rabbitmq", "Queues", "Every queue with its messages and consumers.",
     "rabbitmqctl list_queues name messages consumers", MODE_EXEC, False, []),
]

_GENERIC = [
    ("Disk usage", "The biggest folders in the container.", "du -sh /* 2>/dev/null | sort -h | tail -n 15", MODE_EXEC, False, []),
    ("Processes", "What runs in the container.", "ps aux 2>/dev/null || top -b -n 1", MODE_EXEC, False, []),
]


def suggestions(services: Dict[str, str], workload: str) -> List[Dict[str, Any]]:
    """Runbooks worth adding for a workload, from the images of its services
    (``{service: image}``), then a few that suit any container."""
    out = []
    for service, image in sorted(services.items()):
        name = (image or "").split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0].lower()
        for frag, title, desc, cmd, mode, confirm, params in _SUGGESTIONS:
            if frag in name:
                out.append({
                    "name": f"{title} ({service})" if service and len(services) > 1 else title,
                    "description": desc, "confirm": confirm,
                    "params": [validate_param(p, i) for i, p in enumerate(params)],
                    "steps": [{"title": "", "workload": workload, "service": service, "mode": mode, "replicas": "one",
                               "command": cmd, "continueOnError": False, "timeoutSeconds": DEFAULT_TIMEOUT}],
                })
    first = sorted(services)[0] if services else ""
    for title, desc, cmd, mode, confirm, params in _GENERIC:
        out.append({"name": title, "description": desc, "confirm": confirm, "params": params,
                    "steps": [{"title": "", "workload": workload, "service": first, "mode": mode, "replicas": "one",
                               "command": cmd, "continueOnError": False, "timeoutSeconds": DEFAULT_TIMEOUT}]})
    return out
