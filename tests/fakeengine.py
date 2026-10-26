"""An in-memory Docker Engine API served on a unix socket (not a test module).

Tests point ``LocalClient`` (or the whole server, via ``docker_socket``) at it,
so the real HTTP path runs end to end without touching the machine's Docker:
request building, version negotiation, error mapping, stream demuxing, tar
handling. It models just enough state to be coherent: containers with a
filesystem, images, volumes (whose content shows through containers that mount
them), networks, plugins, exec sessions, logs and events.

Every request is recorded in ``engine.calls`` as ``(method, path, params, body)``
so tests can assert on what the client sent. ``engine.fail[(method, regex)] =
(status, message)`` makes matching requests fail like the real daemon would.
"""

from __future__ import annotations

import asyncio
import base64
import io
import itertools
import json
import os
import re
import struct
import tarfile
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from aiohttp import web

API_VERSION = "1.47"
MIN_API_VERSION = "1.24"
_ids = itertools.count(1)


def new_id() -> str:
    return f"{next(_ids):064x}"


def frame(data: bytes, stream: int = 1) -> bytes:
    """One stdcopy frame (8-byte header) as the Engine multiplexes output."""
    return struct.pack(">BxxxL", stream, len(data)) + data


class Fs:
    """A tiny file tree: path -> bytes (file) or None (directory)."""

    def __init__(self, files: Optional[Dict[str, Any]] = None) -> None:
        self.entries: Dict[str, Optional[bytes]] = {"/": None}
        for path, content in (files or {}).items():
            self.put(path, content)

    def put(self, path: str, content: Optional[bytes]) -> None:
        path = "/" + path.strip("/")
        parts = path.strip("/").split("/")
        for i in range(1, len(parts)):
            self.entries.setdefault("/" + "/".join(parts[:i]), None)
        self.entries[path] = content if content is None else bytes(content)

    def get(self, path: str):
        return self.entries.get("/" + path.strip("/") if path.strip("/") else "/", KeyError)

    def remove(self, path: str) -> None:
        path = "/" + path.strip("/")
        for p in [p for p in self.entries if p == path or p.startswith(path + "/")]:
            del self.entries[p]

    def children(self, path: str) -> List[str]:
        base = "/" + path.strip("/") if path.strip("/") else ""
        out = []
        for p in self.entries:
            if p != "/" and p.startswith(base + "/") and "/" not in p[len(base) + 1:]:
                out.append(p)
        return sorted(out)

    def tar(self, path: str, root_name: str = "") -> bytes:
        """The Engine's archive of ``path``: the entry itself, named after the
        basename of the path the caller asked for (``root_name``)."""
        path = "/" + path.strip("/") if path.strip("/") else "/"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            root_name = root_name or os.path.basename(path) or "."
            for p in sorted(self.entries):
                if p != path and not p.startswith(path.rstrip("/") + "/"):
                    continue
                rel = root_name + "/" + p[len(path):].lstrip("/") if p != path else root_name
                content = self.entries[p]
                info = tarfile.TarInfo(rel.lstrip("/") or ".")
                info.mtime = 1_700_000_000
                if content is None:
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tf.addfile(info)
                else:
                    info.size = len(content)
                    info.mode = 0o644
                    tf.addfile(info, io.BytesIO(content))
        return buf.getvalue()

    def stat(self, path: str) -> Dict[str, Any]:
        content = self.get(path)
        is_dir = content is None
        return {"name": os.path.basename(path.rstrip("/")) or "/", "size": 0 if is_dir else len(content),
                "mode": (0o20000000000 | 0o755) if is_dir else 0o644, "mtime": "2023-11-14T22:13:20Z",
                "linkTarget": ""}


class FakeEngine:
    def __init__(self) -> None:
        self.containers: Dict[str, Dict[str, Any]] = {}
        self.fs: Dict[str, Fs] = {}
        self.logs: Dict[str, List[Tuple[int, str]]] = {}
        # Optional per-line timestamps (container id -> one per log line); the
        # default stamps every line 2026-01-01T00:00:00Z.
        self.log_times: Dict[str, List[str]] = {}
        self.build_cache: List[Dict[str, Any]] = [{"ID": "bc1", "Size": 512, "InUse": False, "Shared": False},
                                                  {"ID": "bc2", "Size": 256, "InUse": True, "Shared": False}]
        self.images: Dict[str, Dict[str, Any]] = {}
        self.volumes: Dict[str, Dict[str, Any]] = {}
        self.volume_fs: Dict[str, Fs] = {}
        self.networks: Dict[str, Dict[str, Any]] = {}
        self.plugins: List[Dict[str, Any]] = []
        self.execs: Dict[str, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []
        self.calls: List[Tuple[str, str, Dict[str, str], Any]] = []
        self.fail: Dict[Tuple[str, str], Tuple[int, str]] = {}
        # Like ``fail`` but only for the next matching request.
        self.fail_once: Dict[Tuple[str, str], Tuple[int, str]] = {}
        self.swarm = False
        # exec handlers: (container, argv) -> (exit code, stdout bytes)
        self.exec_handler: Optional[Callable[[str, List[str]], Tuple[int, bytes]]] = None
        # One-shot containers: image prefix -> fn(container) -> (exit code, log lines).
        # Starting such a container runs it to completion at once.
        # Registry state for pulls: "repo:tag" -> "sha256:<digest>" of the newest
        # image; pulling a tag whose local image has another digest moves the tag.
        self.registry: Dict[str, str] = {}
        self.runners: List[Tuple[str, Callable[[Dict[str, Any]], Tuple[int, List[str]]]]] = []
        self._runner: Optional[web.AppRunner] = None
        self._dir: Optional[tempfile.TemporaryDirectory] = None
        self.socket_path = ""
        self.add_network("bridge", driver="bridge")
        self.add_network("host", driver="host")
        self.add_network("none", driver="null")

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> str:
        app = web.Application(client_max_size=64 << 20)
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        # A short path: unix socket paths are limited to ~104 bytes on macOS.
        self._dir = tempfile.TemporaryDirectory(prefix="kfe", dir="/tmp")
        self.socket_path = os.path.join(self._dir.name, "d.sock")
        site = web.UnixSite(self._runner, self.socket_path)
        await site.start()
        return self.socket_path

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        if self._dir is not None:
            self._dir.cleanup()

    # -- state helpers -------------------------------------------------------
    def add_image(self, ref: str, *, env=None, cmd=None, labels=None, exposed=None, entrypoint=None,
                  size: int = 10_000_000, arch: str = "amd64") -> str:
        iid = "sha256:" + new_id()
        repo = ref.split(":")[0] if ":" in ref.rsplit("/", 1)[-1] else ref
        self.images[iid] = {
            "Id": iid, "RepoTags": [ref if ":" in ref.rsplit("/", 1)[-1] else ref + ":latest"],
            "RepoDigests": [f"{repo}@sha256:{new_id()}"], "Created": "2026-01-01T00:00:00Z",
            "Size": size, "Architecture": arch, "Os": "linux",
            "Config": {"Env": env or ["PATH=/usr/bin"], "Cmd": cmd, "Entrypoint": entrypoint,
                       "Labels": labels or {}, "ExposedPorts": exposed or {}},
        }
        return iid

    def image_by_ref(self, ref: str) -> Optional[Dict[str, Any]]:
        ref_l = ref if ":" in ref.rsplit("/", 1)[-1] or ref.startswith("sha256:") else ref + ":latest"
        for img in self.images.values():
            if img["Id"] == ref or img["Id"].startswith(ref) or ref_l in img["RepoTags"] or ref in img["RepoDigests"]:
                return img
        return None

    def add_network(self, name: str, driver: str = "bridge", labels=None) -> str:
        nid = new_id()
        self.networks[nid] = {"Id": nid, "Name": name, "Driver": driver, "Scope": "local",
                              "Internal": False, "Attachable": False, "Labels": labels or {},
                              "IPAM": {"Driver": "default", "Config": [{"Subnet": "172.18.0.0/16"}]},
                              "Containers": {}, "Created": "2026-01-01T00:00:00Z", "Options": {}}
        return nid

    def network(self, ref: str) -> Optional[Dict[str, Any]]:
        for n in self.networks.values():
            if n["Id"] == ref or n["Id"].startswith(ref) or n["Name"] == ref:
                return n
        return None

    def add_volume(self, name: str, files: Optional[Dict[str, Any]] = None, labels=None, driver="local") -> None:
        self.volumes[name] = {"Name": name, "Driver": driver, "Mountpoint": f"/var/lib/docker/volumes/{name}/_data",
                              "Labels": labels or {}, "Scope": "local", "Options": {}, "CreatedAt": "2026-01-01T00:00:00Z"}
        self.volume_fs[name] = Fs(files or {})

    def add_container(self, name: str, image: str = "nginx:1.27", *, state: str = "running", labels=None,
                      env=None, ports=None, binds=None, networks=None, files=None, logs=None,
                      exit_code: int = 0, health: Optional[str] = None, cmd=None) -> str:
        img = self.image_by_ref(image) or self.images[self.add_image(image)]
        cid = new_id()
        port_bindings = {}
        exposed = dict((img.get("Config") or {}).get("ExposedPorts") or {})  # inherited, like Docker
        for p in ports or []:  # (private, public)
            exposed[f"{p[0]}/tcp"] = {}
            if p[1]:
                port_bindings[f"{p[0]}/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": str(p[1])}]
        nets = networks or ["bridge"]
        self.containers[cid] = {
            "Id": cid, "Name": "/" + name, "Image": img["Id"], "Created": "2026-01-01T00:00:00Z",
            "Path": (cmd or ["run"])[0], "Args": (cmd or ["run"])[1:], "RestartCount": 0,
            "State": {"Status": state, "Running": state == "running", "Paused": state == "paused",
                      "Restarting": state == "restarting", "ExitCode": exit_code, "Pid": 4242 if state == "running" else 0,
                      "StartedAt": "2026-01-01T00:00:00Z", "FinishedAt": "0001-01-01T00:00:00Z",
                      **({"Health": {"Status": health, "FailingStreak": 0, "Log": []}} if health else {})},
            "Config": {"Image": image, "Env": env or ["PATH=/usr/bin"], "Labels": labels or {}, "Cmd": cmd,
                       "Entrypoint": None, "ExposedPorts": exposed, "Hostname": cid[:12], "WorkingDir": "",
                       "User": "", "Tty": False},
            "HostConfig": {"Binds": binds or [], "PortBindings": port_bindings,
                           "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                           "NetworkMode": nets[0], "Privileged": False, "Memory": 0, "NanoCpus": 0,
                           "CapAdd": None, "Mounts": None},
            "NetworkSettings": {"Networks": {n: {"IPAddress": f"172.18.0.{len(self.containers) + 2}",
                                                 "Aliases": [name], "NetworkID": (self.network(n) or {}).get("Id", "")}
                                             for n in nets},
                                "Ports": {k: v for k, v in port_bindings.items()}},
            "Mounts": [self._mount(b) for b in binds or []],
        }
        for n in nets:
            net = self.network(n)
            if net:
                net["Containers"][cid] = {"Name": name, "IPv4Address": "172.18.0.2/16"}
        self.fs[cid] = Fs({"/etc/hostname": cid[:12].encode(), **(files or {})})
        self.logs[cid] = [(1, line) for line in (logs or [])]
        return cid

    def _mount(self, bind: str) -> Dict[str, Any]:
        src, _, rest = bind.partition(":")
        dst = rest.split(":")[0]
        if src.startswith("/"):
            return {"Type": "bind", "Source": src, "Destination": dst, "RW": True}
        return {"Type": "volume", "Name": src, "Source": f"/var/lib/docker/volumes/{src}/_data",
                "Destination": dst, "Driver": "local", "RW": True}

    def container(self, ref: str) -> Optional[Dict[str, Any]]:
        ref = ref.lstrip("/")
        for c in self.containers.values():
            if c["Id"] == ref or c["Id"].startswith(ref) or c["Name"] == "/" + ref:
                return c
        return None

    def _fs_for(self, c: Dict[str, Any], path: str) -> Tuple[Fs, str]:
        """The filesystem holding ``path`` inside container ``c`` (a mounted volume or its own)."""
        clean = "/" + path.strip("/")
        for m in c.get("Mounts") or []:
            dst = m["Destination"].rstrip("/")
            if m.get("Type") == "volume" and (clean == dst or clean.startswith(dst + "/")):
                return self.volume_fs.setdefault(m["Name"], Fs()), clean[len(dst):] or "/"
        return self.fs[c["Id"]], clean

    def emit(self, typ: str, action: str, actor_id: str, attrs: Optional[Dict[str, str]] = None) -> None:
        self.events.append({"Type": typ, "Action": action, "Actor": {"ID": actor_id, "Attributes": attrs or {}},
                            "time": int(time.time()), "timeNano": time.time_ns(), "scope": "local"})

    # -- HTTP ----------------------------------------------------------------
    @staticmethod
    def _err(status: int, msg: str) -> web.Response:
        return web.json_response({"message": msg}, status=status)

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        path = re.sub(r"^/v\d+\.\d+", "", request.path)
        body: Any = None
        if request.can_read_body:
            raw = await request.read()
            ctype = request.headers.get("Content-Type", "")
            if raw and "json" in ctype:
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = raw
            else:
                body = raw
        params = dict(request.query)
        self.calls.append((request.method, path, params, body))
        for (method, pattern), (status, msg) in self.fail.items():
            if method == request.method and re.search(pattern, path):
                return self._err(status, msg)
        for key, (status, msg) in list(self.fail_once.items()):
            if key[0] == request.method and re.search(key[1], path):
                del self.fail_once[key]
                return self._err(status, msg)
        for method, pattern, handler in self._routes():
            if method != request.method:
                continue
            m = re.fullmatch(pattern, path)
            if m:
                return await handler(request, body, params, *m.groups())
        return self._err(404, f"page not found: {request.method} {path}")

    def _routes(self):
        C = r"/containers/([^/]+)"
        return [
            ("GET", r"/_ping", self._ping), ("HEAD", r"/_ping", self._ping),
            ("GET", r"/version", self._version), ("GET", r"/info", self._info),
            ("GET", r"/system/df", self._df), ("GET", r"/events", self._events),
            ("GET", r"/containers/json", self._list_containers),
            ("POST", r"/containers/create", self._create_container),
            ("POST", r"/containers/prune", self._prune_containers),
            ("GET", C + r"/json", self._inspect_container),
            ("POST", C + r"/(start|stop|restart|pause|unpause|kill)", self._lifecycle),
            ("DELETE", C, self._remove_container),
            ("POST", C + r"/rename", self._rename), ("POST", C + r"/update", self._update),
            ("GET", C + r"/logs", self._logs), ("GET", C + r"/stats", self._stats),
            ("GET", C + r"/top", self._top),
            ("GET", C + r"/archive", self._get_archive), ("HEAD", C + r"/archive", self._head_archive),
            ("PUT", C + r"/archive", self._put_archive),
            ("POST", C + r"/exec", self._exec_create),
            ("POST", r"/exec/([^/]+)/start", self._exec_start), ("GET", r"/exec/([^/]+)/json", self._exec_inspect),
            ("GET", r"/images/json", self._list_images), ("POST", r"/images/create", self._pull),
            ("POST", r"/images/prune", self._prune_images),
            ("GET", r"/images/(.+)/json", self._inspect_image), ("GET", r"/images/(.+)/history", self._history),
            ("DELETE", r"/images/(.+)", self._remove_image),
            ("GET", r"/volumes", self._list_volumes), ("POST", r"/volumes/create", self._create_volume),
            ("POST", r"/volumes/prune", self._prune_volumes),
            ("GET", r"/volumes/([^/]+)", self._inspect_volume), ("DELETE", r"/volumes/([^/]+)", self._remove_volume),
            ("GET", r"/networks", self._list_networks), ("POST", r"/networks/create", self._create_network),
            ("POST", r"/networks/prune", self._prune_networks),
            ("POST", r"/build/prune", self._prune_build),
            ("GET", r"/networks/([^/]+)", self._inspect_network), ("DELETE", r"/networks/([^/]+)", self._remove_network),
            ("POST", r"/networks/([^/]+)/(connect|disconnect)", self._net_connect),
            ("GET", r"/plugins", self._list_plugins), ("GET", r"/plugins/privileges", self._plugin_privileges),
            ("POST", r"/plugins/pull", self._plugin_pull), ("POST", r"/plugins/([^/]+)/(enable|set)", self._plugin_action),
            ("DELETE", r"/plugins/([^/]+)", self._plugin_remove),
            ("GET", r"/nodes", self._nodes),
        ]

    # -- system --------------------------------------------------------------
    async def _ping(self, request, body, params):
        return web.Response(text="OK", headers={"Api-Version": API_VERSION})

    async def _version(self, request, body, params):
        return web.json_response({"Version": "27.5.1", "ApiVersion": API_VERSION, "MinAPIVersion": MIN_API_VERSION,
                                  "Os": "linux", "Arch": "arm64", "KernelVersion": "6.10.0"})

    async def _info(self, request, body, params):
        running = sum(1 for c in self.containers.values() if c["State"]["Running"])
        return web.json_response({
            "ID": "FAKE:ENGINE", "Name": "fakehost", "ServerVersion": "27.5.1", "OperatingSystem": "FakeOS",
            "OSType": "linux", "Architecture": "aarch64", "NCPU": 8, "MemTotal": 8 << 30, "Driver": "overlay2",
            "Containers": len(self.containers), "ContainersRunning": running,
            "ContainersPaused": 0, "ContainersStopped": len(self.containers) - running, "Images": len(self.images),
            "KernelVersion": "6.10.0", "DockerRootDir": "/var/lib/docker",
            "Swarm": {"LocalNodeState": "active" if self.swarm else "inactive", "ControlAvailable": self.swarm,
                      "NodeID": "node1" if self.swarm else ""},
        })

    async def _df(self, request, body, params):
        """Like the Engine: image and volume use counts from the containers."""
        def uses(volume: str) -> int:
            return sum(1 for c in self.containers.values()
                       if any(b.split(":")[0] == volume for b in c["HostConfig"].get("Binds") or []))

        return web.json_response({
            "LayersSize": sum(i["Size"] for i in self.images.values()),
            "Images": [{"Id": i["Id"], "Size": i["Size"], "SharedSize": 0, "RepoTags": i["RepoTags"], "Created": 1_760_000_000,
                        "Containers": sum(1 for c in self.containers.values() if c["Image"] == i["Id"])}
                       for i in self.images.values()],
            "Containers": [{"Id": c["Id"], "SizeRw": 1024, "SizeRootFs": 2048, "State": c["State"]["Status"],
                            "Status": c["State"]["Status"], "Names": [c["Name"]], "Image": c["Config"]["Image"],
                            "Labels": c["Config"]["Labels"], "Created": 1_760_000_000} for c in self.containers.values()],
            "Volumes": [{"Name": v["Name"], "Labels": v["Labels"], "UsageData": {"Size": 4096, "RefCount": uses(v["Name"])}}
                        for v in self.volumes.values()],
            "BuildCache": [dict(b) for b in self.build_cache],
        })

    async def _prune_build(self, request, body, params):
        freed = sum(b["Size"] for b in self.build_cache if not b["InUse"])
        self.build_cache = [b for b in self.build_cache if b["InUse"]]
        return web.json_response({"CachesDeleted": [], "SpaceReclaimed": freed})

    async def _events(self, request, body, params):
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        sent = 0
        while True:
            while sent < len(self.events):
                await resp.write((json.dumps(self.events[sent]) + "\n").encode())
                sent += 1
            if params.get("until"):
                break
            await asyncio.sleep(0.05)
            if request.transport is None or request.transport.is_closing():
                break
        return resp

    async def _nodes(self, request, body, params):
        if not self.swarm:
            return self._err(503, "This node is not a swarm manager.")
        return web.json_response([{"ID": "node1", "Description": {"Hostname": "fakehost"},
                                   "Status": {"State": "ready", "Addr": "10.0.0.1"},
                                   "Spec": {"Role": "manager", "Availability": "active"},
                                   "ManagerStatus": {"Leader": True}}])

    # -- containers ----------------------------------------------------------
    def _summary(self, c: Dict[str, Any]) -> Dict[str, Any]:
        img = self.images.get(c["Image"], {})
        ports = []
        for k, binds in (c["HostConfig"].get("PortBindings") or {}).items():
            priv, proto = k.split("/")
            for b in binds or []:
                ports.append({"PrivatePort": int(priv), "PublicPort": int(b.get("HostPort") or 0),
                              "Type": proto, "IP": b.get("HostIp", "")})
        return {"Id": c["Id"], "Names": [c["Name"]], "Image": c["Config"]["Image"], "ImageID": img.get("Id", ""),
                "Command": " ".join([c["Path"], *c["Args"]]), "Created": 1_700_000_000,
                "State": c["State"]["Status"], "Status": self._status_text(c["State"]),
                "Ports": ports, "Labels": c["Config"]["Labels"], "Mounts": c["Mounts"],
                "NetworkSettings": {"Networks": c["NetworkSettings"]["Networks"]}}

    @staticmethod
    def _status_text(st):
        if st["Running"]:
            health = (st.get("Health") or {}).get("Status")
            return "Up 3 hours" + (f" ({health})" if health in ("healthy", "unhealthy") else "")
        return f"Exited ({st.get('ExitCode', 0)}) 2 minutes ago"

    async def _list_containers(self, request, body, params):
        everyone = params.get("all") in ("1", "true")
        out = [self._summary(c) for c in self.containers.values() if everyone or c["State"]["Running"]]
        flt = json.loads(params.get("filters") or "{}")
        for lab in flt.get("label", []):
            k, _, v = lab.partition("=")
            out = [s for s in out if k in s["Labels"] and (not v or s["Labels"][k] == v)]
        return web.json_response(out)

    async def _create_container(self, request, body, params):
        body = body or {}
        name = params.get("name") or f"fake_{len(self.containers)}"
        if self.container(name):
            return self._err(409, f'Conflict. The container name "/{name}" is already in use')
        image = body.get("Image", "")
        if not self.image_by_ref(image):
            return self._err(404, f"No such image: {image}")
        hc = body.get("HostConfig") or {}
        ports = [(int(k.split("/")[0]), int((v or [{}])[0].get("HostPort") or 0))
                 for k, v in (hc.get("PortBindings") or {}).items()]
        nets = list(((body.get("NetworkingConfig") or {}).get("EndpointsConfig") or {}).keys()) or \
            [hc.get("NetworkMode") or "bridge"]
        cid = self.add_container(name, image, state="created", labels=body.get("Labels"), env=body.get("Env"),
                                 ports=ports, binds=hc.get("Binds"), networks=nets, cmd=body.get("Cmd"))
        c = self.containers[cid]
        c["HostConfig"].update({k: v for k, v in hc.items() if k not in ("Binds", "PortBindings")})
        c["Config"].update({k: v for k, v in body.items() if k in ("Entrypoint", "WorkingDir", "User", "Tty", "Hostname")})
        self.emit("container", "create", cid, {"name": name, "image": image})
        return web.json_response({"Id": cid, "Warnings": []}, status=201)

    async def _inspect_container(self, request, body, params, ref):
        c = self.container(ref)
        return web.json_response(c) if c else self._err(404, f"No such container: {ref}")

    async def _lifecycle(self, request, body, params, ref, action):
        c = self.container(ref)
        if not c:
            return self._err(404, f"No such container: {ref}")
        st = c["State"]
        if action in ("start", "restart", "unpause"):
            if action == "start" and st["Running"]:
                return web.Response(status=304)
            st.update(Status="running", Running=True, Paused=False, Pid=4242)
            for prefix, run in self.runners:
                if action == "start" and c["Config"]["Image"].startswith(prefix):
                    res = run(c)
                    if res is None:  # this one keeps running
                        continue
                    code, lines = res
                    self.logs[c["Id"]] = [(1, line) for line in lines]
                    st.update(Status="exited", Running=False, ExitCode=code, Pid=0)
                    break
        elif action in ("stop", "kill"):
            if not st["Running"] and action == "stop":
                return web.Response(status=304)
            st.update(Status="exited", Running=False, Paused=False, Pid=0)
        elif action == "pause":
            st.update(Status="paused", Paused=True)
        self.emit("container", action, c["Id"], {"name": c["Name"].lstrip("/")})
        return web.Response(status=204)

    async def _remove_container(self, request, body, params, ref):
        c = self.container(ref)
        if not c:
            return self._err(404, f"No such container: {ref}")
        if c["State"]["Running"] and params.get("force") not in ("1", "true"):
            return self._err(409, "You cannot remove a running container. Stop the container before attempting removal or force remove")
        del self.containers[c["Id"]]
        for n in self.networks.values():
            n["Containers"].pop(c["Id"], None)
        self.emit("container", "destroy", c["Id"], {"name": c["Name"].lstrip("/")})
        return web.Response(status=204)

    async def _prune_containers(self, request, body, params):
        gone = [cid for cid, c in self.containers.items() if not c["State"]["Running"]]
        for cid in gone:
            del self.containers[cid]
        return web.json_response({"ContainersDeleted": gone, "SpaceReclaimed": 1024 * len(gone)})

    async def _rename(self, request, body, params, ref):
        c = self.container(ref)
        if not c:
            return self._err(404, f"No such container: {ref}")
        if self.container(params["name"]):
            return self._err(409, "name in use")
        c["Name"] = "/" + params["name"]
        return web.Response(status=204)

    async def _update(self, request, body, params, ref):
        c = self.container(ref)
        if not c:
            return self._err(404, f"No such container: {ref}")
        c["HostConfig"].update(body or {})
        return web.json_response({"Warnings": []})

    async def _logs(self, request, body, params, ref):
        c = self.container(ref)
        if not c:
            return self._err(404, f"No such container: {ref}")
        lines = self.logs.get(c["Id"], [])
        tail = params.get("tail", "all")
        if tail != "all":
            lines = lines[-int(tail):] if int(tail) > 0 else []
        ts = params.get("timestamps") in ("1", "true")
        times = self.log_times.get(c["Id"])
        if times:
            times = times[-len(lines):] if lines else []
        def stamp(i: int) -> str:
            return ((times[i] if times else "2026-01-01T00:00:00.000000000Z") + " ") if ts else ""

        data = b"".join(frame((stamp(i) + line + "\n").encode(), s) for i, (s, line) in enumerate(lines))
        return web.Response(body=data, content_type="application/vnd.docker.multiplexed-stream")

    async def _stats(self, request, body, params, ref):
        c = self.container(ref)
        if not c:
            return self._err(404, f"No such container: {ref}")
        sample = {
            "read": "2026-01-01T00:00:01Z", "preread": "2026-01-01T00:00:00Z",
            "cpu_stats": {"cpu_usage": {"total_usage": 2_000_000_000, "percpu_usage": [1, 1]},
                          "system_cpu_usage": 20_000_000_000, "online_cpus": 2},
            "precpu_stats": {"cpu_usage": {"total_usage": 1_000_000_000}, "system_cpu_usage": 10_000_000_000,
                             "online_cpus": 2},
            "memory_stats": {"usage": 64 << 20, "limit": 1 << 30, "stats": {"inactive_file": 4 << 20}},
            "networks": {"eth0": {"rx_bytes": 1000, "tx_bytes": 2000}},
            "blkio_stats": {"io_service_bytes_recursive": [{"op": "read", "value": 300}, {"op": "write", "value": 400}]},
            "pids_stats": {"current": 7},
        }
        if params.get("stream") in ("1", "true"):
            # A short stream: three samples (and a blank and a garbled line).
            lines = [json.dumps(sample)] * 2 + ["", "{garbled", json.dumps(sample)]
            return web.Response(body=("\n".join(lines) + "\n").encode(), content_type="application/json")
        return web.json_response(sample)

    async def _top(self, request, body, params, ref):
        return web.json_response({"Titles": ["PID", "CMD"], "Processes": [["1", "run"]]})

    def _archive_target(self, ref: str, path: str):
        c = self.container(ref)
        if not c:
            return None, None, self._err(404, f"No such container: {ref}")
        fs, inner = self._fs_for(c, path)
        if fs.get(inner) is KeyError:
            return None, None, self._err(404, f"Could not find the file {path} in container {ref}")
        return fs, inner, None

    async def _head_archive(self, request, body, params, ref):
        fs, inner, err = self._archive_target(ref, params.get("path", "/"))
        if err:
            return err
        stat = base64.b64encode(json.dumps(fs.stat(inner)).encode()).decode()
        return web.Response(status=200, headers={"X-Docker-Container-Path-Stat": stat})

    async def _get_archive(self, request, body, params, ref):
        fs, inner, err = self._archive_target(ref, params.get("path", "/"))
        if err:
            return err
        stat = base64.b64encode(json.dumps(fs.stat(inner)).encode()).decode()
        asked = params.get("path", "/").rstrip("/")
        return web.Response(body=fs.tar(inner, os.path.basename(asked) or "."), content_type="application/x-tar",
                            headers={"X-Docker-Container-Path-Stat": stat})

    async def _put_archive(self, request, body, params, ref):
        c = self.container(ref)
        if not c:
            return self._err(404, f"No such container: {ref}")
        fs, inner = self._fs_for(c, params.get("path", "/"))
        if fs.get(inner) is KeyError:
            return self._err(404, "Could not find the file")
        with tarfile.open(fileobj=io.BytesIO(body or b""), mode="r") as tf:
            for m in tf.getmembers():
                dest = inner.rstrip("/") + "/" + m.name.lstrip("./")
                if m.isdir():
                    fs.put(dest, None)
                elif m.isfile():
                    fs.put(dest, tf.extractfile(m).read())
        return web.Response(status=200)

    # -- exec ----------------------------------------------------------------
    async def _exec_create(self, request, body, params, ref):
        c = self.container(ref)
        if not c:
            return self._err(404, f"No such container: {ref}")
        if not c["State"]["Running"]:
            return self._err(409, f"Container {ref} is not running")
        eid = new_id()
        self.execs[eid] = {"ID": eid, "ContainerID": c["Id"], "Cmd": (body or {}).get("Cmd") or [],
                           "Running": False, "ExitCode": None}
        return web.json_response({"Id": eid}, status=201)

    def _run_exec(self, cid: str, argv: List[str]) -> Tuple[int, bytes]:
        if self.exec_handler is not None:
            res = self.exec_handler(cid, argv)
            if res is not None:
                return res
        c = self.containers[cid]
        if argv[:2] == ["ls", "-1Ap"] or (argv and argv[0] == "ls"):
            path = argv[-1]
            fs, inner = self._fs_for(c, path)
            if fs.get(inner) is KeyError:
                return 2, f"ls: {path}: No such file or directory\n".encode()
            names = []
            for child in fs.children(inner):
                names.append(os.path.basename(child) + ("/" if fs.entries[child] is None else ""))
            return 0, ("\n".join(names) + ("\n" if names else "")).encode()
        if argv and argv[0] in ("echo",):
            return 0, (" ".join(argv[1:]) + "\n").encode()
        if argv and argv[0] in ("rm", "mv", "mkdir"):
            target = argv[-1]
            fs, inner = self._fs_for(c, target)
            if argv[0] == "mkdir":
                fs.put(inner, None)
            elif argv[0] == "rm":
                fs.remove(inner)
            else:
                src_fs, src = self._fs_for(c, argv[-2])
                content = src_fs.get(src)
                src_fs.remove(src)
                fs.put(inner, None if content is KeyError else content)
            return 0, b""
        return 0, b""

    async def _exec_start(self, request, body, params, eid):
        ex = self.execs.get(eid)
        if not ex:
            return self._err(404, "No such exec instance")
        code, out = self._run_exec(ex["ContainerID"], ex["Cmd"])
        ex["ExitCode"] = code
        return web.Response(body=frame(out, 1 if code == 0 else 2), content_type="application/vnd.docker.raw-stream")

    async def _exec_inspect(self, request, body, params, eid):
        ex = self.execs.get(eid)
        if not ex:
            return self._err(404, "No such exec instance")
        return web.json_response({"ID": eid, "Running": False, "ExitCode": ex["ExitCode"] or 0,
                                  "ContainerID": ex["ContainerID"]})

    # -- images --------------------------------------------------------------
    async def _list_images(self, request, body, params):
        return web.json_response([{"Id": i["Id"], "RepoTags": i["RepoTags"], "RepoDigests": i["RepoDigests"],
                                   "Created": 1_700_000_000, "Size": i["Size"], "Labels": i["Config"]["Labels"],
                                   "Containers": sum(1 for c in self.containers.values() if c["Image"] == i["Id"])}
                                  for i in self.images.values()])

    async def _pull(self, request, body, params):
        ref = params.get("fromImage", "")
        tag = params.get("tag", "")
        full = f"{ref}:{tag}" if tag and "@" not in ref else ref
        if "doesnotexist" in full:
            resp = web.StreamResponse()
            await resp.prepare(request)
            await resp.write(json.dumps({"errorDetail": {"message": "manifest unknown"}, "error": "manifest unknown"}).encode() + b"\n")
            return resp
        img = self.image_by_ref(full)
        newer = self.registry.get(full if ":" in full.rsplit("/", 1)[-1] else full + ":latest")
        if img is None or (newer and not any(d.endswith("@" + newer) for d in img["RepoDigests"])):
            # A first pull, or the registry has a newer image: the tag moves to it.
            tag = full if ":" in full.rsplit("/", 1)[-1] else full + ":latest"
            if img is not None:
                img["RepoTags"] = [t for t in img["RepoTags"] if t != tag]
            new = self.images[self.add_image(full)]
            if newer:
                new["RepoDigests"] = [tag.rsplit(":", 1)[0] + "@" + newer]
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        for line in ({"status": f"Pulling from {ref}"}, {"status": "Download complete", "id": "abc"},
                     {"status": f"Status: Downloaded newer image for {full}"}):
            await resp.write((json.dumps(line) + "\n").encode())
        return resp

    async def _inspect_image(self, request, body, params, ref):
        img = self.image_by_ref(ref)
        return web.json_response(img) if img else self._err(404, f"No such image: {ref}")

    async def _history(self, request, body, params, ref):
        img = self.image_by_ref(ref)
        if not img:
            return self._err(404, f"No such image: {ref}")
        return web.json_response([{"Id": img["Id"], "Created": 1_700_000_000, "CreatedBy": "/bin/sh -c #(nop) CMD",
                                   "Size": 0, "Tags": img["RepoTags"], "Comment": ""},
                                  {"Id": "<missing>", "Created": 1_699_000_000, "CreatedBy": "ADD rootfs /",
                                   "Size": img["Size"], "Tags": None, "Comment": ""}])

    async def _remove_image(self, request, body, params, ref):
        img = self.image_by_ref(ref)
        if not img:
            return self._err(404, f"No such image: {ref}")
        if any(c["Image"] == img["Id"] for c in self.containers.values()) and params.get("force") not in ("1", "true"):
            return self._err(409, "conflict: unable to delete (image is being used by a container)")
        del self.images[img["Id"]]
        return web.json_response([{"Untagged": img["RepoTags"][0]}, {"Deleted": img["Id"]}])

    async def _prune_images(self, request, body, params):
        used = {c["Image"] for c in self.containers.values()}
        gone = [i for i in list(self.images) if i not in used]
        for i in gone:
            del self.images[i]
        return web.json_response({"ImagesDeleted": [{"Deleted": i} for i in gone], "SpaceReclaimed": 100 * len(gone)})

    # -- volumes -------------------------------------------------------------
    async def _list_volumes(self, request, body, params):
        return web.json_response({"Volumes": list(self.volumes.values()), "Warnings": []})

    async def _create_volume(self, request, body, params):
        body = body or {}
        name = body.get("Name") or new_id()[:16]
        if name not in self.volumes:
            self.add_volume(name, labels=body.get("Labels"), driver=body.get("Driver") or "local")
            self.volumes[name]["Options"] = body.get("DriverOpts") or {}
        return web.json_response(self.volumes[name], status=201)

    async def _inspect_volume(self, request, body, params, name):
        v = self.volumes.get(name)
        return web.json_response(v) if v else self._err(404, f"get {name}: no such volume")

    def _volume_in_use(self, name: str) -> bool:
        return any(m.get("Name") == name for c in self.containers.values() for m in c["Mounts"])

    async def _remove_volume(self, request, body, params, name):
        if name not in self.volumes:
            return self._err(404, f"get {name}: no such volume")
        if self._volume_in_use(name) and params.get("force") not in ("1", "true"):
            return self._err(409, f"remove {name}: volume is in use")
        del self.volumes[name]
        self.volume_fs.pop(name, None)
        return web.Response(status=204)

    async def _prune_volumes(self, request, body, params):
        gone = [n for n in list(self.volumes) if not self._volume_in_use(n)]
        for n in gone:
            del self.volumes[n]
        return web.json_response({"VolumesDeleted": gone, "SpaceReclaimed": 4096 * len(gone)})

    # -- networks ------------------------------------------------------------
    async def _list_networks(self, request, body, params):
        return web.json_response(list(self.networks.values()))

    async def _create_network(self, request, body, params):
        body = body or {}
        if self.network(body.get("Name", "")):
            return self._err(409, f"network with name {body.get('Name')} already exists")
        nid = self.add_network(body.get("Name", ""), body.get("Driver") or "bridge", body.get("Labels"))
        n = self.networks[nid]
        n["Internal"] = bool(body.get("Internal"))
        n["Attachable"] = bool(body.get("Attachable"))
        if body.get("IPAM"):
            n["IPAM"] = body["IPAM"]
        return web.json_response({"Id": nid, "Warning": ""}, status=201)

    async def _inspect_network(self, request, body, params, ref):
        n = self.network(ref)
        return web.json_response(n) if n else self._err(404, f"network {ref} not found")

    async def _remove_network(self, request, body, params, ref):
        n = self.network(ref)
        if not n:
            return self._err(404, f"network {ref} not found")
        if n["Containers"]:
            return self._err(403, f"error while removing network: network {n['Name']} has active endpoints")
        del self.networks[n["Id"]]
        return web.Response(status=204)

    async def _prune_networks(self, request, body, params):
        gone = [n["Name"] for nid, n in list(self.networks.items())
                if not n["Containers"] and n["Name"] not in ("bridge", "host", "none")]
        for nid in [nid for nid, n in self.networks.items() if n["Name"] in gone]:
            del self.networks[nid]
        return web.json_response({"NetworksDeleted": gone})

    async def _net_connect(self, request, body, params, ref, action):
        n = self.network(ref)
        if not n:
            return self._err(404, f"network {ref} not found")
        c = self.container((body or {}).get("Container", ""))
        if not c:
            return self._err(404, "No such container")
        if action == "connect":
            n["Containers"][c["Id"]] = {"Name": c["Name"].lstrip("/"), "IPv4Address": "172.18.0.9/16"}
            ep = (body or {}).get("EndpointConfig") or {}
            c["NetworkSettings"]["Networks"][n["Name"]] = {"IPAddress": "172.18.0.9", "Aliases": ep.get("Aliases") or [],
                                                           "NetworkID": n["Id"]}
        else:
            n["Containers"].pop(c["Id"], None)
            c["NetworkSettings"]["Networks"].pop(n["Name"], None)
        return web.Response(status=200)

    # -- plugins -------------------------------------------------------------
    async def _list_plugins(self, request, body, params):
        return web.json_response(self.plugins)

    async def _plugin_privileges(self, request, body, params):
        return web.json_response([{"Name": "network", "Description": "", "Value": ["host"]}])

    async def _plugin_pull(self, request, body, params):
        name = params.get("name") or params.get("remote", "")
        self.plugins.append({"Id": new_id(), "Name": name, "Enabled": False,
                             "Config": {"Description": "fake plugin", "Interface": {"Types": ["docker.volumedriver/1.0"]}},
                             "PluginReference": params.get("remote", name)})
        return web.Response(status=200, text="{}\n")

    async def _plugin_action(self, request, body, params, name, action):
        for p in self.plugins:
            if p["Name"] == name or p["Name"].startswith(name):
                if action == "enable":
                    p["Enabled"] = True
                return web.Response(status=200)
        return self._err(404, f"plugin {name} not found")

    async def _plugin_remove(self, request, body, params, name):
        before = len(self.plugins)
        self.plugins = [p for p in self.plugins if p["Name"] != name]
        return web.Response(status=200) if len(self.plugins) < before else self._err(404, "plugin not found")
