"""In-memory Docker fakes for the self-update tests (not a test module itself).

``FakeEngine`` implements :class:`kowan.updater.engine.Engine` (the helper's
surface); ``FakeOps`` implements the slice of :class:`kowan.docker.base.Ops`
the orchestrator and reconciler use. Both share one ``FakeDaemon`` state so a
test can drive the orchestrator, "run" the helper, then reconcile.

Images carry a ``behavior``: ``healthy`` (runs, healthcheck healthy),
``nohc`` (runs, no healthcheck), ``crash`` (exits 1 on start), ``unhealthy``,
``starting`` (health never leaves ``starting``) or ``restarting``.
"""

from __future__ import annotations

import copy
import itertools
from typing import Any, Callable, Dict, List, Optional

from kowan.errors import BadGateway, NotFound
from kowan.models import Container

_ids = itertools.count(1)


def _hex(n: int) -> str:
    return f"{n:064x}"


class FakeDaemon:
    def __init__(self) -> None:
        self.containers: Dict[str, Dict[str, Any]] = {}
        self.images: Dict[str, Dict[str, Any]] = {}
        self.logs: Dict[str, List[str]] = {}
        self.calls: List[tuple] = []
        self.fail_rules: List[Callable[[str, tuple], Optional[Exception]]] = []
        self.arch = "aarch64"

    # -- setup ------------------------------------------------------------
    def add_image(self, ref: str, behavior: str = "healthy", version: str = "", env=None,
                  arch: str = "arm64", cmd=None) -> str:
        iid = "sha256:" + _hex(next(_ids))
        cfg = {"Env": list(env or ["PATH=/usr/bin"]), "Labels": {}, "Cmd": cmd or ["serve"],
               "Entrypoint": ["python", "-m", "kowan.server"]}
        if version:
            cfg["Labels"]["org.opencontainers.image.version"] = version
        if behavior not in ("nohc",):
            cfg["Healthcheck"] = {"Test": ["CMD", "python", "-m", "kowan.server", "healthcheck"]}
        img = {"Id": iid, "RepoTags": [ref], "RepoDigests": [], "Config": cfg,
               "Architecture": arch, "Os": "linux", "_behavior": behavior}
        self.images[iid] = img
        self.images[ref] = img
        return iid

    def add_container(self, name: str, image_ref: str, *, running: bool = True,
                      policy: str = "unless-stopped", env=None, mounts=None, labels=None) -> str:
        img = self.images[image_ref]
        cid = _hex(next(_ids))
        cfg = copy.deepcopy(img["Config"])
        cfg["Env"] = list(cfg.get("Env") or []) + list(env or [])
        cfg["Image"] = image_ref
        cfg["Labels"] = dict(cfg.get("Labels") or {}, **(labels or {}))
        cfg["Hostname"] = cid[:12]
        c = {
            "Id": cid, "Name": "/" + name, "Image": img["Id"], "Config": cfg,
            "HostConfig": {"RestartPolicy": {"Name": policy}, "NetworkMode": "bridge",
                           "Binds": [f"{m.get('Name') or m.get('Source')}:{m['Destination']}"
                                     for m in (mounts or [])]},
            "Mounts": list(mounts or []), "RestartCount": 0,
            "NetworkSettings": {"Networks": {"bridge": {}}},
            "State": {"Status": "created", "Running": False, "ExitCode": 0},
        }
        self.containers[cid] = c
        if running:
            self._start(c)
        return cid

    # -- helpers ----------------------------------------------------------
    def resolve(self, ref: str) -> Dict[str, Any]:
        if ref in self.containers:
            return self.containers[ref]
        for c in self.containers.values():
            if c["Name"] == "/" + ref:
                return c
        for cid, c in self.containers.items():
            if len(ref) >= 6 and cid.startswith(ref):
                return c
        raise NotFound(f"No such container: {ref}")

    def by_name(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            return self.resolve(name)
        except NotFound:
            return None

    def names(self) -> List[str]:
        return sorted(c["Name"].lstrip("/") for c in self.containers.values())

    def _check_fail(self, method: str, *args: Any) -> None:
        self.calls.append((method,) + args)
        for rule in self.fail_rules:
            err = rule(method, args)
            if err is not None:
                raise err

    def fail(self, method: str, match: Any = None, times: int = 10**9, err: Exception = None) -> None:
        """Make ``method`` fail when its first argument resolves to ``match`` (a name)."""
        state = {"left": times}

        def rule(m: str, args: tuple) -> Optional[Exception]:
            if m != method or state["left"] <= 0:
                return None
            if match is not None:
                ref = args[0] if args else ""
                c = self.by_name(ref) if isinstance(ref, str) else None
                cname = c["Name"].lstrip("/") if c else ref
                if cname != match and ref != match:
                    return None
            state["left"] -= 1
            return err or BadGateway(f"injected {method} failure")

        self.fail_rules.append(rule)

    def _start(self, c: Dict[str, Any]) -> None:
        img = self.images.get(c["Image"]) or {}
        b = img.get("_behavior", "healthy")
        st = c["State"]
        if b == "crash":
            st.update({"Status": "exited", "Running": False, "ExitCode": 1})
            self.logs.setdefault(c["Id"], []).append("Traceback: boom")
            return
        st.update({"Status": "running", "Running": True, "ExitCode": 0})
        if b == "restarting":
            c["RestartCount"] += 1
        hc = (c["Config"].get("Healthcheck") or {}).get("Test")
        if hc and hc[0] != "NONE":
            st["Health"] = {"Status": {"healthy": "healthy", "unhealthy": "unhealthy",
                                       "starting": "starting", "restarting": "healthy"}.get(b, "healthy"),
                            "Log": [{"Output": "connection refused"}] if b == "unhealthy" else []}
        else:
            st.pop("Health", None)


def _mounts_from_binds(binds):
    out = []
    for b in binds:
        parts = b.split(":")
        src, dst = parts[0], parts[1]
        rw = not (len(parts) > 2 and "ro" in parts[2].split(","))
        if src.startswith("/"):
            out.append({"Type": "bind", "Source": src, "Destination": dst, "RW": rw})
        else:
            out.append({"Type": "volume", "Name": src, "Destination": dst, "RW": rw})
    return out


class FakeEngine:
    """kowan.updater.engine.Engine over a FakeDaemon."""

    def __init__(self, d: FakeDaemon) -> None:
        self.d = d

    async def inspect_container(self, ref):
        self.d._check_fail("inspect_container", ref)
        return copy.deepcopy(self.d.resolve(ref))

    async def inspect_image(self, ref):
        self.d._check_fail("inspect_image", ref)
        if ref not in self.d.images:
            raise NotFound(f"No such image: {ref}")
        return copy.deepcopy(self.d.images[ref])

    async def stop(self, ref, timeout):
        self.d._check_fail("stop", ref)
        c = self.d.resolve(ref)
        c["State"].update({"Status": "exited", "Running": False})
        c["State"].pop("Health", None)

    async def start(self, ref):
        self.d._check_fail("start", ref)
        c = self.d.resolve(ref)
        if not c["State"].get("Running"):
            self.d._start(c)

    async def rename(self, ref, new_name):
        self.d._check_fail("rename", ref, new_name)
        c = self.d.resolve(ref)
        if self.d.by_name(new_name) is not None:
            raise BadGateway(f"Conflict. The container name /{new_name} is already in use")
        c["Name"] = "/" + new_name

    async def set_restart_policy(self, ref, policy):
        self.d._check_fail("set_restart_policy", ref, policy)
        self.d.resolve(ref)["HostConfig"]["RestartPolicy"] = dict(policy)

    async def create(self, name, body):
        self.d._check_fail("create", name)
        if self.d.by_name(name) is not None:
            raise BadGateway(f"Conflict. The container name /{name} is already in use")
        ref = body.get("Image")
        if ref not in self.d.images:
            raise NotFound(f"No such image: {ref}")
        img = self.d.images[ref]
        cid = _hex(next(_ids))
        cfg = copy.deepcopy(img["Config"])
        for k, v in body.items():
            if k not in ("HostConfig", "NetworkingConfig", "Labels", "Env"):
                cfg[k] = copy.deepcopy(v)
        cfg["Env"] = list(img["Config"].get("Env") or []) + list(body.get("Env") or [])
        cfg["Labels"] = dict(img["Config"].get("Labels") or {}, **(body.get("Labels") or {}))
        self.d.containers[cid] = {
            "Id": cid, "Name": "/" + name, "Image": img["Id"], "Config": cfg,
            "HostConfig": copy.deepcopy(body.get("HostConfig") or {}),
            "Mounts": _mounts_from_binds((body.get("HostConfig") or {}).get("Binds") or []),
            "RestartCount": 0, "NetworkSettings": {"Networks": {}},
            "State": {"Status": "created", "Running": False, "ExitCode": 0},
            "_body": copy.deepcopy(body),
        }
        return cid

    async def connect(self, network, cid, endpoint):
        self.d._check_fail("connect", cid, network)

    async def remove(self, ref, force):
        self.d._check_fail("remove", ref)
        c = self.d.resolve(ref)
        del self.d.containers[c["Id"]]

    async def logs_tail(self, ref, tail):
        self.d._check_fail("logs_tail", ref)
        c = self.d.resolve(ref)
        return list(self.d.logs.get(c["Id"], []))[-tail:]

    async def close(self):
        pass


class _Stream:
    def __init__(self, chunks: List[bytes]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self):
        pass


class FakeInfo:
    def __init__(self, arch: str) -> None:
        self.arch = arch


class FakeOps:
    """Slice of kowan.docker.base.Ops used by the orchestrator/reconciler."""

    def __init__(self, d: FakeDaemon, *, preflight_exit: int = 0, preflight_out: str = "",
                 pull_error: str = "") -> None:
        self.d = d
        self.preflight_exit = preflight_exit
        self.preflight_out = preflight_out or '{"ok":true,"component":"server","checks":[{"name":"imports","ok":true}]}'
        self.pull_error = pull_error
        self.created: List[tuple] = []
        self.pulled: List[str] = []
        self.removed: List[str] = []
        self.helper_logs: Dict[str, str] = {}

    async def ping(self):
        return None

    async def inspect_container(self, ref):
        return copy.deepcopy(self.d.resolve(ref))

    async def inspect_image(self, ref):
        if ref not in self.d.images:
            raise NotFound(f"No such image: {ref}")
        return copy.deepcopy(self.d.images[ref])

    async def list_containers(self, all):  # noqa: A002
        out = []
        for c in self.d.containers.values():
            out.append(Container(id=c["Id"], names=[c["Name"]], image=c["Config"].get("Image", ""),
                                 state=c["State"].get("Status", ""), labels=c["Config"].get("Labels") or {}))
        return out

    async def pull_image(self, ref, auth):
        self.pulled.append(ref)
        if self.pull_error:
            return _Stream([b'{"status":"Pulling"}\n', ('{"error":"%s"}\n' % self.pull_error).encode()])
        return _Stream([b'{"status":"Downloaded newer image"}\n'])

    async def system_info(self):
        return FakeInfo(self.d.arch)

    async def create_container(self, name, spec, start):
        self.created.append((name, spec, start))
        image = spec.image
        if image not in self.d.images:
            raise NotFound(f"No such image: {image}")
        cid = _hex(next(_ids))
        role = (spec.labels or {}).get("kowan.role", "")
        state = {"Status": "running", "Running": True, "ExitCode": 0}
        if role.endswith("-preflight"):
            state = {"Status": "exited", "Running": False, "ExitCode": self.preflight_exit}
            self.helper_logs[cid] = self.preflight_out
        self.d.containers[cid] = {
            "Id": cid, "Name": "/" + name, "Image": self.d.images[image]["Id"],
            "Config": {"Image": image, "Cmd": list(spec.command or []),
                       "Entrypoint": list(spec.entrypoint or []), "Labels": dict(spec.labels or {})},
            "HostConfig": {"RestartPolicy": {"Name": spec.restart_policy}}, "Mounts": [],
            "RestartCount": 0, "State": state, "_spec": spec,
        }
        return cid

    async def container_logs(self, ref, follow, tail):
        c = self.d.resolve(ref)
        text = self.helper_logs.get(c["Id"], "")
        return _Stream([text.encode()])

    async def remove_container(self, ref, force):
        c = self.d.resolve(ref)
        self.removed.append(c["Name"].lstrip("/"))
        del self.d.containers[c["Id"]]


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    async def sleep(self, s: float) -> None:
        self.t += max(s, 0.01)

    def monotonic(self) -> float:
        return self.t
