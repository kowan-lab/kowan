"""Server settings an admin changes from the UI, which used to be variables.

Each :class:`Tunable` names one value (a retention window, an interval, an
image, ...), its default and how to check it. Values live in the ``settings``
table under ``tunable.<key>`` and are read where they are used, through
:func:`value`, so a change applies at once, with no restart.

The old environment variable is only a first value: :func:`load` copies it
into the settings while nothing is saved for that key yet, so an install that
set it keeps its behaviour after the upgrade. From then on the setting wins and
the variable can be removed. A few seeds come from :class:`ServerConfig`
instead of the raw environment, so tests that build a config keep control of
them (the monitor interval, retention windows, the release repository).

Bootstrap values stay environment-only on purpose: listen address, data dir,
TLS, keys, trusted proxies, the outer address allowlists and server scripts.
Changing those from a browser would let a stolen admin session widen what the
server trusts.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

log = logging.getLogger("kowan.tunables")

PREFIX = "tunable."

DURATION, INT, BOOL, TEXT, ORIGINS = "duration", "int", "bool", "text", "origins"


@dataclass(frozen=True)
class Tunable:
    key: str
    env: str
    kind: str
    default: str
    group: str
    label: str
    help: str
    #: Smallest value (seconds for durations); 0 disables when ``zero_off``.
    minimum: float = 0
    zero_off: bool = False
    #: Seed from this ServerConfig attribute instead of the raw environment.
    config_attr: str = ""


REGISTRY: Tuple[Tunable, ...] = (
    # --- retention -----------------------------------------------------------
    Tunable("metrics_retention", "KOWAN_METRICS_RETENTION", DURATION, "7d", "retention",
            "Resource history", "How long host and container CPU and memory samples are kept.",
            minimum=3600, config_attr="metrics_retention"),
    Tunable("audit_retention", "KOWAN_AUDIT_RETENTION", DURATION, "90d", "retention",
            "Audit log", "How long audit entries are kept. 0 keeps them forever.",
            minimum=86400, zero_off=True, config_attr="audit_retention"),
    Tunable("alert_retention", "KOWAN_ALERT_RETENTION", DURATION, "30d", "retention",
            "Resolved alerts", "How long alerts are kept once resolved. 0 keeps them forever.",
            minimum=86400, zero_off=True, config_attr="alert_retention"),
    Tunable("workflow_runs_keep", "KOWAN_WORKFLOW_RUNS_KEEP", INT, "100", "retention",
            "Workflow runs", "Runs kept per workflow, newest first. 0 keeps them all.",
            minimum=1, zero_off=True, config_attr="workflow_runs_keep"),
    # --- monitoring ----------------------------------------------------------
    Tunable("monitor_interval", "KOWAN_MONITOR_INTERVAL", DURATION, "15s", "monitoring",
            "Monitoring interval", "How often resources are sampled and alert rules checked.",
            minimum=5, config_attr="monitor_interval"),
    # --- MCP -----------------------------------------------------------------
    Tunable("mcp_allowed_origins", "KOWAN_MCP_ALLOWED_ORIGINS", ORIGINS, "", "mcp",
            "Allowed web origins",
            "Other web pages allowed to call the MCP endpoint from a browser, one origin per line "
            "(https://app.example.com). Kowan's own address is always allowed; desktop clients are not affected."),
    # --- advanced ------------------------------------------------------------
    Tunable("reconcile_interval", "KOWAN_RECONCILE_INTERVAL", DURATION, "5s", "advanced",
            "Reconcile interval", "How often deployments are compared with what runs.", minimum=1),
    Tunable("reconcile_cooldown", "KOWAN_RECONCILE_COOLDOWN", DURATION, "2m", "advanced",
            "Reconcile cooldown", "Wait after a failed automatic redeploy before trying again.", minimum=10),
    Tunable("gitops_tick", "KOWAN_GITOPS_TICK", DURATION, "15s", "advanced",
            "Git sync check", "How often Git-backed deployments look for due polls.", minimum=5),
    Tunable("git_timeout", "KOWAN_GIT_TIMEOUT", DURATION, "2m", "advanced",
            "Git timeout", "Longest a clone or fetch may take.", minimum=10),
    Tunable("compose_validate", "KOWAN_COMPOSE_VALIDATE", BOOL, "true", "advanced",
            "Validate compose files", "Check a compose file before it is deployed or saved."),
    Tunable("compose_validate_timeout", "KOWAN_COMPOSE_VALIDATE_TIMEOUT", DURATION, "10s", "advanced",
            "Validation timeout", "Longest the structural compose check may take.", minimum=1),
    Tunable("compose_host_timeout", "KOWAN_COMPOSE_HOST_TIMEOUT", DURATION, "5s", "advanced",
            "Host check timeout", "Longest the checks against the target host (ports, networks) may take.",
            minimum=1),
    Tunable("trivy_image", "KOWAN_TRIVY_IMAGE", TEXT, "", "advanced",
            "Scanner image", "Trivy image used for vulnerability scans, for example a mirror. Empty uses the pinned one."),
    Tunable("trivy_cache_volume", "KOWAN_TRIVY_CACHE_VOLUME", TEXT, "", "advanced",
            "Scanner cache volume", "Docker volume that keeps the vulnerability database. Empty uses the default."),
    Tunable("trivy_container_prefix", "KOWAN_TRIVY_CONTAINER_PREFIX", TEXT, "", "advanced",
            "Scanner container prefix", "Name prefix of the throwaway scan containers. Empty uses the default."),
    Tunable("agent_image", "KOWAN_AGENT_IMAGE", TEXT, "", "advanced",
            "Agent image", "Image in the join commands and agent updates, for example from a private mirror. "
            "Empty uses ghcr.io/kowan-lab/kowan-agent."),
    Tunable("release_repository", "KOWAN_RELEASE_REPOSITORY", TEXT, "ghcr.io/kowan-lab/kowan", "advanced",
            "Release repository", "Where Kowan looks for new versions of itself. Empty turns the check off.",
            config_attr="release_repository"),
)

BY_KEY: Dict[str, Tunable] = {t.key: t for t in REGISTRY}

_lock = threading.Lock()
# Saved values (as text) for this process; a key absent here uses its default.
_saved: Dict[str, str] = {}


# --- parsing ----------------------------------------------------------------

def _seconds_text(secs: float) -> str:
    """Shortest exact text for a duration: 90d, 2h, 1h30m, 15s, 0."""
    if secs <= 0:
        return "0"
    if secs != int(secs):
        return f"{secs:g}s"
    n, out = int(secs), ""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if n >= size:
            out += f"{n // size}{unit}"
            n %= size
    return out


def parse(t: Tunable, raw: str) -> Tuple[Any, Optional[str]]:
    """``(parsed, error)`` for a value as people write it."""
    from kowan.config import _parse_go_duration

    text = (raw or "").strip()
    if t.kind == DURATION:
        if text == "":
            return None, "is required"
        if text.replace(".", "", 1).isdigit():  # plain seconds, as the variables accepted
            text += "s"
        d = _parse_go_duration(text)
        if d is None:
            return None, "is not a duration such as 30s, 15m, 2h or 90d"
        secs = d.total_seconds()
        if secs == 0 and t.zero_off:
            return 0.0, None
        if secs < t.minimum:
            return None, f"must be at least {_seconds_text(t.minimum)}" + (" (or 0 to turn it off)" if t.zero_off else "")
        return secs, None
    if t.kind == INT:
        try:
            n = int(text)
        except ValueError:
            return None, "must be a whole number"
        if n == 0 and t.zero_off:
            return 0, None
        if n < t.minimum:
            return None, f"must be at least {int(t.minimum)}"
        return n, None
    if t.kind == BOOL:
        low = text.lower()
        if low in ("1", "true", "yes", "on"):
            return True, None
        if low in ("0", "false", "no", "off"):
            return False, None
        return None, "must be true or false"
    if t.kind == ORIGINS:
        out: List[str] = []
        for part in text.replace(",", "\n").split("\n"):
            part = part.strip().rstrip("/")
            if not part:
                continue
            try:
                u = urlsplit(part)
            except ValueError:
                return None, f"{part} is not a valid origin"
            if u.scheme not in ("http", "https") or not u.hostname or u.path or u.query or u.fragment:
                return None, f"{part} is not an origin such as https://app.example.com"
            out.append(f"{u.scheme}://{u.netloc.lower()}")
        return out, None
    if any(c.isspace() for c in text):
        return None, "must not contain spaces"
    return text, None


def canonical(t: Tunable, parsed: Any) -> str:
    """The text a parsed value is saved and shown as."""
    if t.kind == DURATION:
        return _seconds_text(float(parsed))
    if t.kind == BOOL:
        return "true" if parsed else "false"
    if t.kind == ORIGINS:
        return "\n".join(parsed)
    return str(parsed)


def _seed_text(t: Tunable, config: Any) -> Optional[str]:
    """The old variable's value, if it was set to something other than the default."""
    if t.config_attr and config is not None and hasattr(config, t.config_attr):
        v = getattr(config, t.config_attr)
        if hasattr(v, "total_seconds"):
            v = _seconds_text(v.total_seconds())
        text = str(v)
    else:
        text = os.environ.get(t.env, "")
        if text.strip() == "":
            return None
    parsed, err = parse(t, text)
    if err:
        log.error("%s %s: ignored, using the default", t.env, err)
        return None
    out = canonical(t, parsed)
    return None if out == canonical(t, parse(t, t.default)[0]) else out


# --- runtime ----------------------------------------------------------------

def load(store: Any, config: Any = None) -> None:
    """Read the saved values; seed the unsaved ones from the old variables."""
    saved: Dict[str, str] = {}
    for t in REGISTRY:
        key = PREFIX + t.key
        if store.setting_exists(key):
            saved[t.key] = store.get_setting(key)
            continue
        seed = _seed_text(t, config)
        if seed is not None:
            store.set_setting(key, seed)
            saved[t.key] = seed
            log.info("%s copied into the settings; it is now changed from the web UI", t.env)
    with _lock:
        _saved.clear()
        _saved.update(saved)


def text(key: str) -> str:
    """The current value as text (saved, else the default)."""
    t = BY_KEY[key]
    with _lock:
        return _saved.get(key, t.default)


def value(key: str) -> Any:
    """The current parsed value: seconds (float), int, bool, str or list of origins."""
    t = BY_KEY[key]
    parsed, err = parse(t, text(key))
    if err:  # a saved value only gets here after validation; be safe anyway
        parsed, _ = parse(t, t.default)
    return parsed


def is_saved(key: str) -> bool:
    with _lock:
        return key in _saved


def save(store: Any, values: Dict[str, Optional[str]]) -> Dict[str, str]:
    """Validate and apply ``{key: text or None}`` (None = back to the default).

    All or nothing: returns ``{key: error}`` and saves nothing when any value
    is wrong."""
    errors: Dict[str, str] = {}
    clean: Dict[str, Optional[str]] = {}
    for key, raw in values.items():
        t = BY_KEY.get(key)
        if t is None:
            errors[key] = "unknown setting"
            continue
        if raw is None:
            clean[key] = None
            continue
        if not isinstance(raw, str):
            errors[key] = "must be text"
            continue
        parsed, err = parse(t, raw)
        if err:
            errors[key] = f"{t.label} {err}"
            continue
        clean[key] = canonical(t, parsed)
    if errors:
        return errors
    for key, val in clean.items():
        if val is None:
            store.delete_setting(PREFIX + key)
        else:
            store.set_setting(PREFIX + key, val)
    with _lock:
        for key, val in clean.items():
            if val is None:
                _saved.pop(key, None)
            else:
                _saved[key] = val
    return {}


def env_still_set(t: Tunable) -> bool:
    return os.environ.get(t.env, "").strip() != ""


@contextmanager
def override(**values: str) -> Iterator[None]:
    """Tests: set values for the duration of a block (text, as typed)."""
    with _lock:
        before = dict(_saved)
        for k, v in values.items():
            t = BY_KEY[k]
            parsed, err = parse(t, v)
            if err:
                raise ValueError(f"{k}: {err}")
            _saved[k] = canonical(t, parsed)
    try:
        yield
    finally:
        with _lock:
            _saved.clear()
            _saved.update(before)


def describe(t: Tunable) -> Dict[str, Any]:
    return {
        "key": t.key,
        "group": t.group,
        "label": t.label,
        "help": t.help,
        "kind": t.kind,
        "value": text(t.key),
        "default": t.default,
        "custom": is_saved(t.key),
        "env": t.env,
        "envSet": env_still_set(t),
    }


Getter = Callable[[], Any]
