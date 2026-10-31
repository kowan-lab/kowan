"""Running runbooks (see :mod:`kowan.runbooks.model` for what one is).

A run is a background task: the request that starts it returns at once with
the run's id, and anyone allowed to read it follows it live over a WebSocket
(:class:`LiveRun` keeps what was said so far, so a late viewer sees it all) or
reads it later from the store. Access is checked when the run starts, on every
workload it touches; the steps then run with the server's own Docker access,
as schedules do, since the runbook is what was approved, not its commands one
by one.

The exit code of a command is printed by the shell after it, behind a random
marker that is cut from the output: ``exec`` gives no exit code over the
interactive stream, and this works the same on the local daemon and through
an agent.

Python notes: each step reads the stream in chunks with a deadline
(``asyncio.wait_for`` per read, bounded by the step timeout), and the live
viewers are ``asyncio.Queue``s fed by the run task.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import re
import secrets
import shlex
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from kowan.workloads import compose_deployments, parse_workload, workload_of

from . import model
from .model import RunbookError

log = logging.getLogger("kowan.runbooks")

# Output kept per step record (the end of it, which says how it went).
MAX_OUTPUT = 64 * 1024
# What a live run keeps for viewers that join late.
MAX_LIVE = 2 * 1024 * 1024
# A finished run stays live this long, for a viewer opening it just after.
LIVE_LINGER = 120.0
_READ = 65536

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_WARNING = "warning"  # a step allowed to fail failed
STATUS_FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_script(command: str, params: Dict[str, str], marker: str) -> str:
    """The ``sh -c`` script of a step: the parameters exported with their
    values quoted (data, never code), the command in a subshell so its own
    ``exit`` ends only that, then its exit code behind ``marker``."""
    lines = [f"export {name}={shlex.quote(value)}" for name, value in sorted(params.items())]
    lines += ["(", command, ")", f"printf '{marker}%s__\\n' \"$?\""]
    return "\n".join(lines)


class _Output:
    """Turns a step's byte stream into text: cuts the exit marker out (it may
    arrive split across chunks) and keeps the tail for the record."""

    def __init__(self, marker: str, emit) -> None:
        self.marker = marker
        self.emit = emit
        self.exit_code: Optional[int] = None
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""
        self.kept = ""

    def _say(self, text: str) -> None:
        if not text:
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.kept = (self.kept + text)[-MAX_OUTPUT:]
        self.emit(text)

    def feed(self, data: bytes, final: bool = False) -> None:
        text = self._pending + self._dec.decode(data, final=final)
        self._pending = ""
        i = text.find(self.marker)
        if i >= 0:
            end = text.find("__", i + len(self.marker))
            if end >= 0:
                try:
                    self.exit_code = int(text[i + len(self.marker):end])
                except ValueError:
                    pass
                self._say(text[:i])
                rest = text[end + 2:].lstrip("\r\n")
                self._say(rest)
                return
            if not final:
                self._say(text[:i])
                self._pending = text[i:]
                return
        if not final:
            # Hold back what could be the start of the marker.
            for k in range(min(len(self.marker), len(text)), 0, -1):
                if self.marker.startswith(text[-k:]):
                    self._say(text[:-k])
                    self._pending = text[-k:]
                    return
        self._say(text)


class LiveRun:
    """A run as it happens: its events, and the viewers following it."""

    def __init__(self, run: Dict[str, Any]) -> None:
        self.run = run
        self.events: List[Dict[str, Any]] = []
        self.size = 0
        self.subs: List["asyncio.Queue[Optional[Dict[str, Any]]]"] = []
        self.done = asyncio.Event()
        self.ended = 0.0

    def publish(self, ev: Dict[str, Any]) -> None:
        if ev.get("type") == "output":
            self.size += len(ev.get("text", ""))
            if self.size > MAX_LIVE:
                return  # the record still keeps the tail
        self.events.append(ev)
        for q in list(self.subs):
            q.put_nowait(ev)

    def follow(self) -> "asyncio.Queue[Optional[Dict[str, Any]]]":
        q: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()
        for ev in self.events:
            q.put_nowait(ev)
        if self.done.is_set():
            q.put_nowait(None)
        else:
            self.subs.append(q)
        return q

    def unfollow(self, q) -> None:
        if q in self.subs:
            self.subs.remove(q)

    def finish(self) -> None:
        self.done.set()
        self.ended = time.monotonic()
        for q in self.subs:
            q.put_nowait(None)
        self.subs.clear()


class RunbookService:
    def __init__(self, svc: Any) -> None:
        self.svc = svc
        self.store = svc.store
        self._live: Dict[str, LiveRun] = {}
        self._tasks: Dict[str, "asyncio.Task[None]"] = {}
        self._running: Dict[str, str] = {}  # runbook id -> run id

    # -- runbooks ----------------------------------------------------------------
    async def _host_containers(self, host_id: str) -> List[Any]:
        ops = self.svc.docker.ops(self.store.get_host(host_id))
        return list(await ops.list_containers(True) or [])

    async def labelled(self, host_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Runbooks declared with labels, on the given hosts (default: every
        online one), once per service (replicas carry the same labels)."""
        if host_ids is None:
            # An offline host fails its listing below and is skipped.
            host_ids = [h.id for h in self.store.list_hosts()]
        deps = compose_deployments(self.store)

        async def one(h: str) -> List[Dict[str, Any]]:
            try:
                cs = await self._host_containers(h)
            except Exception as err:  # noqa: BLE001 - host down: its labels are unknown
                log.debug("runbooks: list containers on %s: %s", h, err)
                return []
            out = []
            for c in cs:
                labels = getattr(c, "labels", None) or {}
                if not any(k.startswith(model.LABEL_PREFIX) for k in labels):
                    continue
                name = ((getattr(c, "names", None) or [""])[0]).lstrip("/")
                wl, _, _ = workload_of(labels, h, name, deps)
                out += model.from_labels(labels, wl)
            return out

        seen: Dict[str, Dict[str, Any]] = {}
        for batch in await asyncio.gather(*(one(h) for h in host_ids)):
            for rb in batch:
                seen.setdefault(rb["id"], rb)
        return list(seen.values())

    async def get(self, rid: str) -> Dict[str, Any]:
        """A stored or label-declared runbook (NotFoundError if neither)."""
        from kowan.store import NotFoundError

        parsed = model.parse_label_id(rid)
        if parsed is None:
            return self.store.get_runbook(rid)
        workload, _, _ = parsed
        _, host, _ = parse_workload(workload)
        hosts = [host] if host else self._workload_hosts(workload)
        for rb in await self.labelled(hosts):
            if rb["id"] == rid:
                return rb
        raise NotFoundError("runbook not found")

    def _workload_hosts(self, workload: str) -> List[str]:
        group = model.group_of(workload)
        if group is not None:
            return self.store.hosts_with_tag(group)
        kind, host, rest = parse_workload(workload)
        if kind != "dep":
            return [host]
        try:
            return list(self.store.get_deployment(rest).targets or [])
        except Exception:  # noqa: BLE001
            return []

    async def services(self, workload: str) -> Dict[str, str]:
        """``{service: image}`` of a workload's containers."""
        out: Dict[str, str] = {}
        for _, c in await self._containers(workload):
            image = getattr(c, "image", "") or ""
            if image.startswith("sha256:"):
                # Created from an image id: its name tells nothing, the
                # container's own name (redis, postgres-main) might.
                image = ((getattr(c, "names", None) or [""])[0]).lstrip("/")
            out.setdefault(model.service_of(getattr(c, "labels", None) or {}), image)
        return out

    async def _containers(self, workload: str, service: Optional[str] = None) -> List[Tuple[str, Any]]:
        """``(host, container)`` of a workload (and service), running first,
        without the throwaway containers of runs."""
        deps = compose_deployments(self.store)
        group = model.group_of(workload) is not None
        out: List[Tuple[str, Any]] = []
        for h in self._workload_hosts(workload):
            try:
                found = await self._host_containers(h)
            except Exception as err:  # noqa: BLE001 - one offline host of a group
                if not group:
                    raise
                log.warning("runbooks: host %s of %s: %s", h, workload, err)
                continue
            for c in found:
                labels = getattr(c, "labels", None) or {}
                if labels.get(model.LABEL_RUN):
                    continue
                name = ((getattr(c, "names", None) or [""])[0]).lstrip("/")
                if group:
                    # On a group: the service, or a lone container, of that name.
                    if service in (model.service_of(labels), name):
                        out.append((h, c))
                    continue
                if workload_of(labels, h, name, deps)[0] != workload:
                    continue
                if service is not None and model.service_of(labels) != service:
                    continue
                out.append((h, c))
        out.sort(key=lambda hc: (getattr(hc[1], "state", "") != "running", ((getattr(hc[1], "names", None) or [""])[0])))
        return out

    # -- runs ------------------------------------------------------------------------
    def live(self, run_id: str) -> Optional[LiveRun]:
        now = time.monotonic()
        for rid in [r for r, lv in self._live.items() if lv.done.is_set() and now - lv.ended > LIVE_LINGER]:
            del self._live[rid]
        return self._live.get(run_id)

    def start(self, rb: Dict[str, Any], params: Any, user: str, trigger: str = "manual",
              context: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Start a run in the background and return it (status running).
        ``context`` adds environment variables of what started it (an alert's
        ``KOWAN_ALERT_*``), next to the parameters. Raises RunbookError for bad
        parameters or a run already going."""
        values = model.resolve_params(rb, params)
        if rb["id"] in self._running:
            raise RunbookError("this runbook is already running")
        run = {
            "id": str(uuid.uuid4()), "runbookId": rb["id"], "runbookName": rb["name"],
            "workloads": model.workloads_of(rb), "params": values, "user": user, "trigger": trigger,
            "status": STATUS_RUNNING, "steps": [], "startedAt": _now(), "finishedAt": "",
        }
        self.store.save_runbook_run(run)
        live = LiveRun(run)
        self._live[run["id"]] = live
        self._running[rb["id"]] = run["id"]
        env = {**{k: str(v) for k, v in (context or {}).items()}, **values}
        self._tasks[run["id"]] = asyncio.ensure_future(self._run(rb, run, env, live))
        return run

    async def run_and_wait(self, rb: Dict[str, Any], params: Any, user: str, trigger: str,
                           timeout: Optional[float] = None, context: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        run = self.start(rb, params, user, trigger, context)
        live = self._live[run["id"]]
        await asyncio.wait_for(live.done.wait(), timeout=timeout)
        return live.run

    async def stop(self) -> None:
        for t in list(self._tasks.values()):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _run(self, rb: Dict[str, Any], run: Dict[str, Any], values: Dict[str, str], live: LiveRun) -> None:
        from kowan.auth import authorize

        # Access was checked when the run started; the steps use the server's
        # own Docker access (a team member's view would refuse the throwaway
        # container a "run" step needs).
        authorize.bind(None)
        status = STATUS_SUCCEEDED
        try:
            for i, step in enumerate(rb["steps"]):
                ok = await self._step(i, step, values, run, live)
                if not ok:
                    if step.get("continueOnError"):
                        status = STATUS_WARNING
                        continue
                    status = STATUS_FAILED
                    break
        except asyncio.CancelledError:
            status = STATUS_FAILED
            raise
        except Exception as err:  # noqa: BLE001 - a bug must not leave it "running"
            log.exception("runbook %s: %s", rb["name"], err)
            status = STATUS_FAILED
        finally:
            run["status"] = status
            run["finishedAt"] = _now()
            try:
                self.store.save_runbook_run(run)
            except Exception as err:  # noqa: BLE001
                log.error("runbook run %s: save: %s", run["id"], err)
            live.publish({"type": "done", "run": {k: v for k, v in run.items() if k != "steps"} | {"steps": _brief(run["steps"])}})
            live.finish()
            self._running.pop(rb["id"], None)
            self._tasks.pop(run["id"], None)
            log.info("runbook %s (%s) %s", rb["name"], run["trigger"], status)

    async def _step(self, i: int, step: Dict[str, Any], values: Dict[str, str], run: Dict[str, Any],
                    live: LiveRun) -> bool:
        """Run one step (on one replica or on all); True when every part of it
        succeeded."""
        def record(**kw: Any) -> Dict[str, Any]:
            rec = {"step": i, "title": step.get("title") or "", "workload": step["workload"],
                   "service": step.get("service", ""), "mode": step["mode"], "host": "", "container": "",
                   "status": STATUS_RUNNING, "exitCode": None, "error": "", "output": "",
                   "startedAt": _now(), "finishedAt": ""}
            rec.update(kw)
            run["steps"].append(rec)
            live.publish({"type": "step", "record": len(run["steps"]) - 1, "data": _brief([rec])[0]})
            return rec

        def end(rec: Dict[str, Any], ok: bool) -> None:
            rec["status"] = STATUS_SUCCEEDED if ok else STATUS_FAILED
            rec["finishedAt"] = _now()
            live.publish({"type": "step", "record": run["steps"].index(rec), "data": _brief([rec])[0]})
            self.store.save_runbook_run(run)

        try:
            found = await self._containers(step["workload"], step.get("service", ""))
        except Exception as err:  # noqa: BLE001 - host gone, store error
            rec = record(error=f"cannot list the containers: {err}")
            end(rec, False)
            return False
        if step["mode"] == model.MODE_EXEC:
            targets = [(h, c) for h, c in found if getattr(c, "state", "") == "running"]
            # On a group it runs on every host's one, else one replica or all.
            if step.get("replicas") != "all" and model.group_of(step["workload"]) is None:
                targets = targets[:1]
        else:
            targets = found[:1]
        if not targets:
            what = f"service {step['service']}" if step.get("service") else "this workload"
            rec = record(error=f"no {'running ' if step['mode'] == model.MODE_EXEC else ''}container of {what}")
            end(rec, False)
            return False

        all_ok = True
        for host, c in targets:
            name = ((getattr(c, "names", None) or [""])[0]).lstrip("/")
            rec = record(host=host, container=name)
            idx = run["steps"].index(rec)
            marker = f"__KOWAN_EXIT_{secrets.token_hex(8)}_"
            out = _Output(marker, lambda text, idx=idx: live.publish({"type": "output", "record": idx, "text": text}))
            script = build_script(step["command"], values, marker)
            try:
                ops = self.svc.docker.ops(self.store.get_host(host))
                if step["mode"] == model.MODE_EXEC:
                    await self._exec(ops, c.id, script, out, step["timeoutSeconds"])
                else:
                    await self._run_once(ops, c, name, run["id"], script, out, step["timeoutSeconds"])
            except asyncio.TimeoutError:
                rec["error"] = f"timed out after {step['timeoutSeconds']}s"
            except asyncio.CancelledError:
                rec["error"] = "stopped"
                rec["output"] = out.kept
                end(rec, False)
                raise
            except Exception as err:  # noqa: BLE001
                rec["error"] = str(err) or err.__class__.__name__
            rec["output"] = out.kept
            rec["exitCode"] = out.exit_code
            ok = not rec["error"] and out.exit_code == 0
            if not rec["error"] and out.exit_code is None:
                rec["error"] = "the command ended without an exit code (the shell was killed?)"
            end(rec, ok)
            all_ok = all_ok and ok
        return all_ok

    async def _pump(self, stream: Any, out: _Output, deadline: float) -> None:
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise asyncio.TimeoutError
                data = await asyncio.wait_for(stream.read(_READ), timeout=left)
                if not data:
                    break
                out.feed(data)
            out.feed(b"", final=True)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    res = close()
                    if hasattr(res, "__await__"):
                        await res
                except Exception:  # noqa: BLE001
                    pass

    async def _exec(self, ops: Any, cid: str, script: str, out: _Output, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        stream = await asyncio.wait_for(ops.exec_attach(cid, ["sh", "-c", script], True), timeout=60)
        await self._pump(stream, out, deadline)

    async def _run_once(self, ops: Any, source: Any, name: str, run_id: str, script: str,
                        out: _Output, timeout: int) -> None:
        """Run ``script`` in a throwaway copy of ``source``: same image,
        environment, volumes and networks; no ports, no restart; removed at
        the end whatever happens."""
        deadline = time.monotonic() + timeout
        spec = await ops.container_spec(source.id)
        spec.ports = []
        spec.restart_policy = "no"
        spec.pull_image = False
        # The workload's own labels (``container_spec`` leaves the compose
        # ones out) keep it in its stack for permissions; it is hidden from the
        # lists as a support container, and declares no runbook or schedule.
        own = {k: v for k, v in (getattr(source, "labels", None) or {}).items()
               if k.startswith("com.docker.compose.") or k in ("com.docker.stack.namespace", "kowan.deployment.id")}
        spec.labels = {
            k: v for k, v in ((spec.labels or {}) | own).items()
            if not k.startswith((model.LABEL_PREFIX, "kowan.schedule.", "com.docker.swarm."))
        } | {model.LABEL_SUPPORT: "true", model.LABEL_RUN: run_id, "com.docker.compose.oneoff": "True"}
        spec.entrypoint = ["/bin/sh", "-c"]
        spec.command = [script]
        spec.tty = True
        spec.open_stdin = False
        cname = f"{name[:40]}-runbook-{run_id[:8]}"
        cid = await ops.create_container(cname, spec, True)
        try:
            stream = _Unstamped(await ops.container_logs(cid, True, 0))
            await self._pump(stream, out, deadline)
            if out.exit_code is None:
                insp = await ops.inspect_container(cid) or {}
                state = insp.get("State") or {}
                if not state.get("Running"):
                    out.exit_code = int(state.get("ExitCode", -1))
        finally:
            try:
                await ops.remove_container(cid, True)
            except Exception as err:  # noqa: BLE001
                log.warning("runbook: remove %s: %s", cname, err)


class _Unstamped:
    """A log stream without the timestamp Docker puts before each line
    (``container_logs`` asks for them)."""

    _STAMP = re.compile(rb"^\d{4}-\d\d-\d\dT[0-9:.]+(Z|[+-]\d\d:\d\d) ")

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._buf = b""
        self._eof = False

    async def read(self, n: int = -1) -> bytes:
        while True:
            if b"\n" in self._buf:
                head, _, self._buf = self._buf.rpartition(b"\n")
                return b"\n".join(self._STAMP.sub(b"", line) for line in head.split(b"\n")) + b"\n"
            if self._eof:
                rest, self._buf = self._buf, b""
                return self._STAMP.sub(b"", rest)
            data = await self._inner.read(n)
            if not data:
                self._eof = True
            self._buf += data

    async def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            res = close()
            if hasattr(res, "__await__"):
                await res


def _brief(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Step records without their output (which goes out as its own events)."""
    return [{k: v for k, v in r.items() if k != "output"} for r in records]
