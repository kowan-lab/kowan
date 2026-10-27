"""Kowan's automation engine.

A workflow is a named, ordered list of
typed steps (script, http, deploy, container, run-container, prune, notify,
backup) executed sequentially with per-step output captured into a run record.
Runs can be
triggered manually, via a public webhook token, or on a cron schedule (see
``scheduler.py``). The Engine executes runs as detached asyncio tasks and
persists the run after each step so the UI can poll progress.

Learner notes:

  - :meth:`Engine.run` schedules an ``asyncio.Task`` and keeps a
    strong reference to it (asyncio only weakly references tasks, so without the
    ``_background_tasks`` set the task could be GC'd mid-flight). The per-run
    timeout is applied with ``asyncio.wait_for``.
  - The store is SYNCHRONOUS, so store calls are plain method
    calls (no await). Docker ``Ops`` methods ARE async and awaited.
  - A ``NotifyFunc`` is injected (``notify_fn``) to avoid a hard dependency on
    the notify package; it defaults to a small sender built from ``svc.store`` +
    ``kowan.notify.build``.
  - The bash "script" step shells out with ``asyncio.create_subprocess_exec``,
    passing the webhook body on stdin and KOWAN_WEBHOOK_* env vars.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, Optional

from kowan import httpcap

# Response bytes an HTTP step keeps (shown in the run output).
_HTTP_STEP_MAX_BODY = 64 << 10

from kowan import backup
from kowan.models import (
    ContainerActionRestart,
    ContainerActionStart,
    ContainerActionStop,
    ContainerSpec,
    KeyValue,
    StepResult,
    StepStatusFailed,
    StepStatusSkipped,
    StepStatusSuccess,
    Workflow,
    WorkflowPayload,
    WorkflowRun,
    WorkflowRunFailed,
    WorkflowRunRunning,
    WorkflowRunSuccess,
    WorkflowStep,
    WorkflowStepBackup,
    WorkflowStepContainer,
    WorkflowStepDeploy,
    WorkflowStepHTTP,
    WorkflowStepNotify,
    WorkflowStepPrune,
    WorkflowStepRunContainer,
    WorkflowStepScript,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids import cycles
    from kowan.web import Services
    from kowan.deployment.service import DeploymentService

log = logging.getLogger("kowan.workflow")

# scriptTimeout bounds how long a single "script" step may run before it is
# killed and the step recorded as a failure.
_SCRIPT_TIMEOUT = 5 * 60.0

# runTimeout bounds the total wall-clock time of an asynchronous workflow run.
_RUN_TIMEOUT = 60 * 60.0

# httpStepTimeout bounds a single outbound http step.
_HTTP_STEP_TIMEOUT = 30.0

# runContainerTimeout bounds a single "run-container" step: how long we poll the
# container's inspect for it to exit before giving up (and force-removing it if
# asked to). Mirrors the per-step timeouts above.
_RUN_CONTAINER_TIMEOUT = 5 * 60.0

# runContainerPollInterval is how often "run-container" re-inspects the container
# while waiting for it to exit.
_RUN_CONTAINER_POLL = 0.5

# runContainerMaxOutput caps the captured stdout of a "run-container" step, in
# bytes, the same way _run_http caps its response body.
_RUN_CONTAINER_MAX_OUTPUT = 64 << 10

# Allowed actions for the (generalized) "deploy" step, mapped to the
# DeploymentService method of the same name. Empty/missing -> redeploy.
_DEPLOY_ACTIONS = ("redeploy", "deploy", "start", "stop", "destroy")

# NotifyFunc sends a message through a notification channel identified by id.
# It is injected so the engine has no hard dependency on notify. The Message is a
# ``kowan.notify.Message``; we type it loosely to avoid a hard import here.
NotifyFunc = Callable[[str, "object"], Awaitable[None]]


def _now_rfc3339() -> str:
    """Current UTC time formatted as RFC3339 (second precision, ``Z`` suffix)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# A NAME token in a dollar-var reference: a leading letter/underscore then
# letters/digits/underscores. Two forms are recognised in a step string:
#   $NAME       — bare, greedy up to the first non-NAME char
#   ${NAME}     — braced, so it can abut following NAME characters
_ENV_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _expand_env(s: str, env: Dict[str, str]) -> str:
    """Expand CI-style ``$NAME`` / ``${NAME}`` references in ``s`` from ``env``.

    This is env-style substitution (deliberately NOT brace templating): a literal
    dollar sign directly followed by a NAME, or a dollar sign followed by a
    braced NAME, is replaced by ``env[NAME]``. NAME is ``[A-Za-z_][A-Za-z0-9_]*``.
    Unknown names are left LITERAL (the whole ``$NAME`` / ``${NAME}`` token is
    kept verbatim) rather than blanked, so an unset reference is visible instead
    of silently vanishing. A bare ``$`` not followed by a NAME is left as-is.
    """
    if "$" not in s:
        return s

    def repl(m: "re.Match[str]") -> str:
        name = m.group(1) if m.group(1) is not None else m.group(2)
        if name in env:
            return env[name]
        return m.group(0)  # unknown -> leave the literal token in place

    return _ENV_VAR_RE.sub(repl, s)


# What a script step inherits from the server's own environment: enough to run
# bash, never the server's configuration or secrets (KOWAN_*, cloud credentials).
_SCRIPT_BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR")

# Variables a step may not export to later steps: they change how bash or the
# dynamic loader behave rather than carrying data.
_UNSAFE_EXPORTS = frozenset({
    "PATH", "BASH_ENV", "ENV", "IFS", "PS4", "SHELLOPTS", "BASHOPTS",
    "PROMPT_COMMAND", "HOME", "KOWAN_OUTPUT",
})
_EXPORT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_export_name(key: str) -> bool:
    if not _EXPORT_NAME.match(key) or key in _UNSAFE_EXPORTS:
        return False
    return not key.startswith(("LD_", "DYLD_", "BASH_FUNC_"))


def _script_env(env: Dict[str, str]) -> Dict[str, str]:
    proc_env = {k: os.environ[k] for k in _SCRIPT_BASE_ENV if k in os.environ}
    proc_env.update({k: v for k, v in env.items() if _safe_export_name(k)})
    return proc_env


def _parse_export_file(text: str) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` lines exported by a script step into a dict.

    Each line is split on the FIRST ``=`` (values may contain ``=``); the key is
    trimmed. Blank lines and ``#`` comment lines are skipped. A line without an
    ``=`` is ignored. Mirrors a CI job's ``$KOWAN_OUTPUT`` env-export convention.
    """
    out: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _safe_export_name(key):
            continue
        out[key] = value
    return out


@dataclass
class Deps:
    """The collaborators the Engine needs to dispatch steps.

    ``deployments`` is the DeploymentService;
    ``docker`` is the provider (``docker.Provider``); ``notify_fn`` is the
    injected notify sender (None -> notify steps fail); ``data_dir`` is the
    control-plane data directory used by backup steps.
    """

    store: object
    deployments: Optional["DeploymentService"] = None
    docker: object = None
    notify_fn: Optional[NotifyFunc] = None
    data_dir: str = ""
    # False (KOWAN_SERVER_SCRIPTS=off) refuses script steps: they run bash on
    # the control plane, which a hosted (SaaS) instance must never allow.
    server_scripts: bool = True


class Engine:
    """Loads workflows and executes their steps, recording runs in the store."""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        # Strong references to in-flight detached runs so they are not GC'd.
        self._background_tasks: set[asyncio.Task] = set()

    # -- construction from the DI container -----------------------------------

    @classmethod
    def from_services(
        cls,
        svc: "Services",
        *,
        deployments: Optional["DeploymentService"] = None,
        notify_fn: Optional[NotifyFunc] = None,
    ) -> "Engine":
        """Build an Engine wired from the ``Services`` container.

        Falls back to ``svc.deployments`` for the deployment service and, when
        no ``notify_fn`` is given, to a SendVia-equivalent default built from
        the store. ``data_dir`` comes from ``svc.config``.
        """
        deps = Deps(
            store=svc.store,
            deployments=deployments or getattr(svc, "deployments", None),
            docker=svc.docker,
            notify_fn=notify_fn or _default_notify_fn(svc),
            data_dir=getattr(svc.config, "data_dir", ""),
            server_scripts=getattr(svc.config, "server_scripts", True),
        )
        return cls(deps)

    # -- run lifecycle --------------------------------------------------------

    async def run(
        self,
        workflow_id: str,
        trigger: str,
        payload: Optional[WorkflowPayload],
    ) -> str:
        """Load the workflow, create a "running" run record, and execute its
        steps as a DETACHED asyncio task so the caller returns immediately with
        the new run id. The run is persisted after every step so the UI can poll
        progress. ``trigger`` records how the run started (manual/webhook/cron/
        or a trigger type).
        """
        wf = self._deps.store.get_workflow(workflow_id)

        run = WorkflowRun(
            id=str(uuid.uuid4()),
            workflow_id=wf.id,
            trigger=trigger,
            status=WorkflowRunRunning,
            steps=[],
            started_at=_now_rfc3339(),
        )
        self._deps.store.create_run(run)

        async def _detached() -> None:
            try:
                await asyncio.wait_for(
                    self._execute(wf, run, payload), timeout=_RUN_TIMEOUT
                )
            except asyncio.TimeoutError:
                log.error(
                    "workflow run timed out workflow=%s run=%s", wf.id, run.id
                )
            except Exception:  # noqa: BLE001 - never leak a stack from a task
                log.exception(
                    "workflow run crashed workflow=%s run=%s", wf.id, run.id
                )

        task = asyncio.ensure_future(_detached())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return run.id

    async def _execute(
        self,
        wf: Workflow,
        run: WorkflowRun,
        payload: Optional[WorkflowPayload],
    ) -> WorkflowRun:
        """Run each step in order, capturing its result into the run and
        persisting after every step. When a step fails and is not marked
        ``continue_on_error``, the remaining steps are recorded as "skipped" and
        the run is marked failed.

        The run environment (CI-style variables) is built once from the workflow
        variables plus the trigger payload, then THREADED through the loop: each
        step's string config is expanded from the current env before it runs, and
        any variables a step exports are merged back in for later steps."""
        failed = False
        stopped = False

        # Off the event loop: an external secret (Vault, ...) is a network fetch.
        env = await asyncio.to_thread(self._build_run_env, wf, payload)

        for step in wf.steps:
            if stopped:
                run.steps.append(
                    StepResult(name=step.name, type=step.type, status=StepStatusSkipped)
                )
                self._persist(run)
                continue

            start = _monotonic_ms()
            output = ""
            step_err: Optional[Exception] = None
            try:
                output = await self._dispatch(step, payload, env)
            except _StepError as e:
                # A step that failed but still produced partial output (e.g. an
                # http step with a bad status carries the response body).
                output = e.output
                step_err = e
            except Exception as e:  # noqa: BLE001 - recorded as the step failure
                step_err = e

            res = StepResult(
                name=step.name,
                type=step.type,
                output=output,
                duration_ms=_monotonic_ms() - start,
            )
            if step_err is not None:
                res.status = StepStatusFailed
                res.error = str(step_err)
                failed = True
                if not step.continue_on_error:
                    stopped = True
            else:
                res.status = StepStatusSuccess
            run.steps.append(res)
            self._persist(run)

        run.status = WorkflowRunFailed if failed else WorkflowRunSuccess
        run.finished_at = _now_rfc3339()
        self._persist(run)

        log.info(
            "workflow run finished workflow=%s run=%s trigger=%s status=%s",
            run.workflow_id, run.id, run.trigger, run.status,
        )
        return run

    def _build_run_env(
        self, wf: Workflow, payload: Optional[WorkflowPayload]
    ) -> Dict[str, str]:
        """Build the initial run environment for a workflow run.

        Seeded from ``wf.variables`` (each value passed through
        ``store.resolve_secrets`` so ``[[secret]]`` references resolve; resolve
        errors are tolerated, leaving the raw value, matching the http render
        path). When a trigger payload is present, the standard webhook vars are
        added (the same names the script step already exports), so steps can
        reference the inbound request via ``$KOWAN_WEBHOOK_*``.
        """
        env: Dict[str, str] = {}
        for k, v in (wf.variables or {}).items():
            try:
                env[k] = self._deps.store.resolve_secrets(v)
            except Exception as err:  # noqa: BLE001 - proceed with the raw value
                log.error("workflow: resolve secrets in variable %r err=%s", k, err)
                env[k] = v

        if payload is not None:
            env["KOWAN_WEBHOOK_METHOD"] = payload.method
            env["KOWAN_WEBHOOK_BODY"] = payload.body
            for k, v in payload.headers.items():
                env["KOWAN_WEBHOOK_HEADER_" + _header_env_name(k)] = v
            for k, v in payload.query.items():
                env["KOWAN_WEBHOOK_QUERY_" + _header_env_name(k)] = v

        return env

    def _persist(self, run: WorkflowRun) -> None:
        """Save the current run state. Errors are logged (the run continues)."""
        try:
            self._deps.store.update_run(run)
        except Exception as err:  # noqa: BLE001
            log.error(
                "workflow: persist run run=%s workflow=%s err=%s",
                run.id, run.workflow_id, err,
            )

    async def _dispatch(
        self,
        step: WorkflowStep,
        payload: Optional[WorkflowPayload],
        env: Dict[str, str],
    ) -> str:
        """Route a step to its type-specific handler.

        ``env`` is the threaded run environment: the script/http steps expand
        their string config from it (and the script step may mutate it in place
        by exporting variables); deploy/container/notify/backup ignore it but
        still receive it for a uniform signature."""
        if step.type == WorkflowStepScript:
            if not self._deps.server_scripts:
                raise ValueError("script steps are disabled on this server (KOWAN_SERVER_SCRIPTS=off)")
            return await self._run_script(step, payload, env)
        if step.type == WorkflowStepHTTP:
            return await self._run_http(step, payload, env)
        if step.type == WorkflowStepDeploy:
            return await self._run_deploy(step)
        if step.type == WorkflowStepContainer:
            return await self._run_container(step)
        if step.type == WorkflowStepRunContainer:
            return await self._run_run_container(step, env)
        if step.type == WorkflowStepPrune:
            return await self._run_prune(step)
        if step.type == WorkflowStepNotify:
            return await self._run_notify(step)
        if step.type == WorkflowStepBackup:
            return await self._run_backup(step)
        raise ValueError(f"unknown step type {step.type!r}")

    # -- step handlers --------------------------------------------------------

    async def _run_script(
        self,
        step: WorkflowStep,
        payload: Optional[WorkflowPayload],
        env: Dict[str, str],
    ) -> str:
        """Execute the step's bash script after resolving secret references in
        it. The combined stdout/stderr is returned; a non-zero exit (or timeout)
        raises.

        The subprocess environment is a minimal base plus the threaded run env (so
        ``$NAME`` variables and ``KOWAN_WEBHOOK_*`` are visible as real env vars),
        plus ``KOWAN_OUTPUT`` pointing at a temp file. After the script exits, any
        ``KEY=VALUE`` lines it wrote there are merged back into ``env`` for later
        steps (CI-style exports). The script text still gets ``[[secret]]``
        resolution as before."""
        script = step.config.get("script", "")
        if script == "":
            raise ValueError("script step requires a non-empty script")
        try:
            resolved = await asyncio.to_thread(self._deps.store.resolve_secrets, script)
        except Exception as err:  # noqa: BLE001 - proceed with original text
            log.error("workflow: resolve secrets in script err=%s", err)
            resolved = script

        fd, script_path = tempfile.mkstemp(prefix="kowan-workflow-", suffix=".sh")
        out_fd, output_path = tempfile.mkstemp(prefix="kowan-workflow-out-", suffix=".env")
        os.close(out_fd)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(resolved)

            # The subprocess env: a minimal base (never the server's own env,
            # which holds its configuration and keys) + the threaded run env
            # (variables + webhook vars) + KOWAN_OUTPUT for exports. stdin
            # carries the webhook body when a payload triggered the run.
            proc_env = _script_env(env)
            proc_env["KOWAN_OUTPUT"] = output_path

            stdin_bytes: Optional[bytes] = None
            if payload is not None:
                stdin_bytes = payload.body.encode("utf-8")

            proc = await asyncio.create_subprocess_exec(
                "bash",
                script_path,
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=proc_env,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes), timeout=_SCRIPT_TIMEOUT
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise ValueError(f"script timed out after {int(_SCRIPT_TIMEOUT)}s")

            # Merge exported KEY=VALUE lines into the threaded run env (best
            # effort: a missing/unreadable file just means no exports).
            try:
                with open(output_path, "r", encoding="utf-8", errors="replace") as ef:
                    exported = _parse_export_file(ef.read())
                env.update(exported)
            except OSError:
                pass

            out = stdout.decode("utf-8", "replace") if stdout else ""
            if proc.returncode != 0:
                raise ValueError(
                    f"script execution failed: exit status {proc.returncode}\n{out}"
                )
            return out
        finally:
            for p in (script_path, output_path):
                try:
                    os.remove(p)
                except OSError:
                    pass

    async def _run_deploy(self, step: WorkflowStep) -> str:
        """Resolve the deployment by id or name and run a lifecycle action on it.

        The step's ``action`` config selects which DeploymentService method to
        call: one of redeploy, deploy, start, stop, destroy. An empty/missing
        action defaults to redeploy.
        """
        if self._deps.deployments is None:
            raise ValueError("deploy step: deployment service not configured")

        action = step.config.get("action", "").strip()
        if action == "":
            action = "redeploy"
        if action not in _DEPLOY_ACTIONS:
            raise ValueError(
                f"deploy step: unknown action {action!r} "
                f"(want {' | '.join(_DEPLOY_ACTIONS)})"
            )

        ref = step.config.get("deployment", "")
        if ref == "":
            raise ValueError("deploy step requires a deployment id or name")

        try:
            dep = self._deps.store.get_deployment(ref)
        except Exception:
            try:
                dep = self._deps.store.get_deployment_by_name(ref)
            except Exception:
                raise ValueError(f"deploy step: deployment {ref!r} not found")

        op = getattr(self._deps.deployments, action)
        try:
            await op(dep.id)
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"{action} {dep.name!r}: {err}")
        return f"{action} deployment {dep.name!r}"

    def _ops_for_host(self, step_kind: str, host_id: str):
        """Resolve the ``Ops`` client + host record for a host id.

        Shared by the docker-touching steps (container / run-container / prune).
        Raises a clear ``ValueError`` prefixed with ``step_kind`` when the docker
        provider is unconfigured, the host is unknown, or the client cannot be
        built — following the resolution pattern the container step used inline.
        Returns ``(ops, host)``.
        """
        if self._deps.docker is None:
            raise ValueError(f"{step_kind} step: docker provider not configured")
        if host_id == "":
            raise ValueError(f"{step_kind} step requires a host")
        try:
            host = self._deps.store.get_host(host_id)
        except Exception as err:
            raise ValueError(f"{step_kind} step: host {host_id!r}: {err}")
        try:
            ops = self._deps.docker.ops(host)
        except Exception as err:
            raise ValueError(f"{step_kind} step: resolve host {host_id!r}: {err}")
        return ops, host

    async def _run_container(self, step: WorkflowStep) -> str:
        """Perform a start/stop/restart action on a container on a host."""
        host_id = step.config.get("host", "")
        container_id = step.config.get("container", "")
        action = step.config.get("action", "")
        if host_id == "" or container_id == "" or action == "":
            raise ValueError("container step requires host, container and action")

        ops, host = self._ops_for_host("container", host_id)

        try:
            if action == ContainerActionStart:
                await ops.start_container(container_id)
            elif action == ContainerActionStop:
                await ops.stop_container(container_id)
            elif action == ContainerActionRestart:
                await ops.restart_container(container_id)
            else:
                raise ValueError(
                    f"container step: unknown action {action!r} (want start|stop|restart)"
                )
        except ValueError:
            raise
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"container {action} on host {host.name!r}: {err}")
        return f"{action} container {container_id} on {host.name}"

    async def _run_prune(self, step: WorkflowStep) -> str:
        """Prune Docker resources on a host and summarise what was reclaimed.

        Config: ``host`` (host id) and ``target`` (one of containers, images,
        images-all, volumes, networks, all). Each target maps to the matching
        prune op — ``images-all`` calls ``prune_images(all=True)`` and ``all``
        runs every prune op — and the summary reports the bytes reclaimed (or,
        for networks, that unused networks were removed).
        """
        host_id = step.config.get("host", "")
        target = step.config.get("target", "").strip()
        if target == "":
            raise ValueError("prune step requires a target")

        ops, host = self._ops_for_host("prune", host_id)

        # Each entry: (label, coroutine factory). "all" runs them in order.
        async def _prune_containers() -> str:
            n = await ops.prune_containers()
            return f"containers: reclaimed {int(n)} bytes"

        async def _prune_images(all_images: bool) -> str:
            n = await ops.prune_images(all_images)
            what = "all unused images" if all_images else "dangling images"
            return f"{what}: reclaimed {int(n)} bytes"

        async def _prune_volumes() -> str:
            n = await ops.prune_volumes()
            return f"volumes: reclaimed {int(n)} bytes"

        async def _prune_networks() -> str:
            await ops.prune_networks()
            return "networks: removed unused networks"

        try:
            if target == "containers":
                lines = [await _prune_containers()]
            elif target == "images":
                lines = [await _prune_images(False)]
            elif target == "images-all":
                lines = [await _prune_images(True)]
            elif target == "volumes":
                lines = [await _prune_volumes()]
            elif target == "networks":
                lines = [await _prune_networks()]
            elif target == "all":
                lines = [
                    await _prune_containers(),
                    await _prune_images(True),
                    await _prune_volumes(),
                    await _prune_networks(),
                ]
            else:
                raise ValueError(
                    f"prune step: unknown target {target!r} (want containers | "
                    "images | images-all | volumes | networks | all)"
                )
        except ValueError:
            raise
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"prune {target} on host {host.name!r}: {err}")

        return f"pruned {target} on {host.name}\n" + "\n".join(lines)

    async def _run_run_container(
        self, step: WorkflowStep, env: Dict[str, str]
    ) -> str:
        """Run a one-shot container to completion and capture its stdout.

        Config: ``host`` (host id), ``image`` (image ref), ``command`` (optional,
        shell-splittable), ``env`` (optional newline ``KEY=VALUE`` lines),
        ``outputVar`` (optional variable name), ``remove`` (optional truthy —
        auto-remove after). Creates a container from the image + command + env,
        starts it, POLLS ``inspect_container`` until it is no longer running
        (bounded by the per-step timeout), reads the exit code, fetches stdout via
        ``container_logs``, and — if ``remove`` is truthy — force-removes it.

        The step OUTPUT is the captured stdout (trimmed, size-capped like the http
        step). When ``outputVar`` is set the trimmed stdout is exported into the
        run env as ``env[outputVar]`` and the exit code (as a string) as
        ``env[outputVar + "_EXIT"]`` so later steps read them as ``$NAME``. A
        non-zero exit fails the step, but the captured output is attached to the
        failure (via ``_StepError``) exactly like the http step does on a bad
        status, so the logs are preserved on the failed StepResult.
        """
        image = step.config.get("image", "").strip()
        if image == "":
            raise ValueError("run-container step requires an image")
        host_id = step.config.get("host", "")

        ops, host = self._ops_for_host("run-container", host_id)

        # Build the container spec from image + optional command + optional env.
        command_raw = step.config.get("command", "").strip()
        try:
            command = shlex.split(command_raw) if command_raw else []
        except ValueError as err:
            raise ValueError(f"run-container step: parse command: {err}")

        spec_env = []
        for line in step.config.get("env", "").split("\n"):
            line = line.strip()
            if line == "" or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k == "":
                continue
            spec_env.append(KeyValue(key=k, value=v))

        spec = ContainerSpec(image=image, command=command, env=spec_env)

        remove = _truthy(step.config.get("remove", ""))
        name = "kowan-wf-run-" + uuid.uuid4().hex[:12]

        # Create WITHOUT starting so a create failure never leaves us polling a
        # container that does not exist; then start explicitly.
        try:
            container_id = await ops.create_container(name, spec, False)
        except Exception as err:  # noqa: BLE001
            raise ValueError(
                f"run-container: create {image!r} on host {host.name!r}: {err}"
            )

        removed = False

        async def _cleanup() -> None:
            nonlocal removed
            if remove and not removed:
                removed = True
                try:
                    await ops.remove_container(container_id, True)
                except Exception as rerr:  # noqa: BLE001 - best effort
                    log.error(
                        "run-container: remove %s on host %s err=%s",
                        container_id, host.name, rerr,
                    )

        try:
            try:
                await ops.start_container(container_id)
            except Exception as err:  # noqa: BLE001
                raise ValueError(
                    f"run-container: start {image!r} on host {host.name!r}: {err}"
                )

            exit_code = await self._wait_container_exit(ops, container_id)
            output = await self._collect_container_stdout(ops, container_id)
        finally:
            await _cleanup()

        if step.config.get("outputVar", "").strip():
            var = step.config["outputVar"].strip()
            env[var] = output
            env[var + "_EXIT"] = str(exit_code)

        if exit_code != 0:
            # Preserve the captured output on the failed StepResult, like _run_http.
            raise _StepError(
                output,
                f"run-container: {image!r} exited with status {exit_code}",
            )
        return output

    async def _wait_container_exit(self, ops, container_id: str) -> int:
        """Poll ``inspect_container`` until the container is no longer running,
        returning its exit code. Bounded by ``_RUN_CONTAINER_TIMEOUT``.

        Reads Docker's inspect ``State`` map: ``Running`` (bool) tells us when it
        has exited and ``ExitCode`` (int) is the status. A timeout raises so the
        step fails rather than hanging the run.
        """
        async def _poll() -> int:
            while True:
                insp = await ops.inspect_container(container_id)
                state = (insp or {}).get("State") or {}
                if not state.get("Running", False):
                    code = state.get("ExitCode", 0)
                    try:
                        return int(code)
                    except (TypeError, ValueError):
                        return 0
                await asyncio.sleep(_RUN_CONTAINER_POLL)

        try:
            return await asyncio.wait_for(_poll(), timeout=_RUN_CONTAINER_TIMEOUT)
        except asyncio.TimeoutError:
            raise ValueError(
                f"run-container: container did not exit within "
                f"{int(_RUN_CONTAINER_TIMEOUT)}s"
            )

    async def _collect_container_stdout(self, ops, container_id: str) -> str:
        """Read the container's logs into a trimmed, size-capped string.

        Fetches the whole (non-following) log history via ``container_logs`` and
        drains the returned async byte stream up to ``_RUN_CONTAINER_MAX_OUTPUT``
        bytes, closing it afterwards. Decodes UTF-8 leniently and trims, mirroring
        how ``_run_http`` caps and strips its response body.
        """
        stream = await ops.container_logs(container_id, False, 0)
        buf = bytearray()
        try:
            while len(buf) < _RUN_CONTAINER_MAX_OUTPUT:
                chunk = await stream.read(_RUN_CONTAINER_MAX_OUTPUT - len(buf))
                if not chunk:
                    break
                buf.extend(chunk)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
        return bytes(buf[:_RUN_CONTAINER_MAX_OUTPUT]).decode("utf-8", "replace").strip()

    async def _run_notify(self, step: WorkflowStep) -> str:
        """Send the step's message via the configured notification channel."""
        if self._deps.notify_fn is None:
            raise ValueError("notify step: notifier not configured")
        channel_id = step.config.get("channel", "")
        if channel_id == "":
            raise ValueError("notify step requires a channel id")
        # Lazy import keeps this module free of a hard notify dependency (the
        # sender itself is the injected NotifyFunc).
        from kowan.notify import Message

        msg = Message(
            title=step.config.get("title", ""),
            body=step.config.get("body", ""),
            level=step.config.get("level", ""),
        )
        try:
            await self._deps.notify_fn(channel_id, msg)
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"notify step: {err}")
        return f"sent notification via channel {channel_id}"

    async def _run_backup(self, step: WorkflowStep) -> str:
        """Write a gzip+tar backup of the control-plane state (the SQLite
        database plus the stacks data subtree) to a timestamped file under the
        engine's data directory and report the written path and size.

        The SQLite snapshot is taken via ``store.snapshot`` into a temp file, then a
        manifest, ``kowan.db`` and the ``stacks/`` subtree are streamed into the
        archive. The master secret key is deliberately NOT included.
        """
        if self._deps.store is None:
            raise ValueError("backup step: store not configured")
        if self._deps.data_dir == "":
            raise ValueError("backup step: data directory not configured")

        # Resolve the destination directory: default {data_dir}/backups,
        # overridable via the "path" config (relative paths under data_dir).
        dest_dir = os.path.join(self._deps.data_dir, "backups")
        override = step.config.get("path", "")
        if override:
            if os.path.isabs(override):
                dest_dir = override
            else:
                dest_dir = os.path.join(self._deps.data_dir, override)
        os.makedirs(dest_dir, exist_ok=True)

        filename = "kowan-backup-" + datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H%M%SZ"
        ) + ".tar.gz"
        dest_path = os.path.join(dest_dir, filename)

        def _write() -> None:
            with open(dest_path, "wb") as w:
                backup.create_backup(self._deps.store, self._deps.data_dir, w)

        try:
            await asyncio.to_thread(_write)
        except Exception as err:  # noqa: BLE001
            try:
                os.remove(dest_path)
            except OSError:
                pass
            raise ValueError(f"backup step: {err}")

        size = os.path.getsize(dest_path)
        return f"wrote backup {dest_path} ({size} bytes)"

    async def _run_http(
        self,
        step: WorkflowStep,
        payload: Optional[WorkflowPayload],
        env: Dict[str, str],
    ) -> str:
        """Perform an outbound HTTP request with a configurable method. The url,
        headers and body may template the inbound webhook payload via {{body}},
        {{method}}, {{header.NAME}} and {{query.NAME}}, then reference run
        variables via $NAME/${NAME}, and finally reference secrets via [[name]].
        The transform order is: resolve secrets in the step's own text, then
        env-expand dollar-vars, then payload braces. Values that came from the
        caller (webhook payload, headers, query) are inserted last and literally,
        so a caller cannot smuggle a ``[[secret]]`` or ``$VAR`` reference in.
        """
        method = step.config.get("method", "").strip().upper()
        if method == "":
            method = "POST"
        raw_url = step.config.get("url", "").strip()
        if raw_url == "":
            raise ValueError("http step requires a url")

        async def render(s: str) -> str:
            try:
                s = await asyncio.to_thread(self._deps.store.resolve_secrets, s)
            except Exception as err:  # noqa: BLE001
                log.error("workflow: resolve secrets in http step err=%s", err)
            s = _expand_env(s, env)
            return _apply_payload_template(s, payload)

        url = await render(raw_url)
        body = await render(step.config.get("body", ""))

        headers: Dict[str, str] = {}
        for line in step.config.get("headers", "").split("\n"):
            line = line.strip()
            if line == "":
                continue
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            headers[k.strip()] = await render(v.strip())

        # Auth comes from a referenced connection (httpBearer), not a hand-typed
        # Authorization header. Inject its headers AFTER the manual ones so the
        # connection-provided auth wins over a stray typed header. Optional: a
        # public endpoint has no connectionRef and this is a no-op.
        conn_ref = step.config.get("connectionRef", "").strip()
        if conn_ref:
            from kowan import capabilities as caps
            from kowan import connections
            try:
                bearer = await connections.resolve(
                    self._deps.store, conn_ref, caps.CAP_HTTP_BEARER
                )
                for hk, hv in bearer.auth_headers().items():
                    headers[hk] = hv
            except Exception as err:  # noqa: BLE001
                raise ValueError(f"http step: resolve credential {conn_ref!r}: {err}")

        content = body.encode("utf-8") if body != "" else None
        try:
            resp = await httpcap.request(method, url, limit=_HTTP_STEP_MAX_BODY, timeout=_HTTP_STEP_TIMEOUT,
                                         follow_redirects=True, headers=headers, content=content)
        except httpcap.HTTPError as err:
            raise ValueError(f"http step: {method} {url}: {err}")

        resp_body = resp.content.decode("utf-8", "replace").strip()
        out = f"{method} {url} -> {resp.status_code}\n{resp_body}"

        expect = step.config.get("expectStatus", "").strip()
        if expect != "":
            if not _status_matches(resp.status_code, expect):
                raise _StepError(
                    out, f"http step: status {resp.status_code} does not match expected {expect!r}"
                )
        elif resp.status_code < 200 or resp.status_code >= 300:
            raise _StepError(out, f"http step: unexpected status {resp.status_code}")
        return out


class _StepError(ValueError):
    """A step failure that still carries partial output.

    On a bad HTTP status the captured response body must still be recorded on
    the failed StepResult. Raising would drop the output, so it is stashed on
    the exception and the executor reads it back.
    """

    def __init__(self, output: str, message: str) -> None:
        super().__init__(message)
        self.output = output


def _monotonic_ms() -> int:
    return int(asyncio.get_event_loop().time() * 1000)


def _truthy(s: str) -> bool:
    """Whether a config string denotes a truthy flag (case-insensitive).

    Accepts the usual "yes" spellings a checkbox/toggle serialises to; anything
    else (including "", "0", "false") is falsey.
    """
    return s.strip().lower() in ("1", "true", "yes", "on")


def _default_notify_fn(svc: "Services") -> NotifyFunc:
    """Build a NotifyFunc bound to the store: load the channel + its decrypted config, build the provider, and
    send. Does NOT gate on the channel's MinLevel (the caller chose it)."""

    async def send(channel_id: str, msg: "object") -> None:
        from kowan import capabilities as caps
        from kowan import connections

        store = svc.store
        try:
            provider = await connections.resolve_service(
                store, channel_id, caps.CAP_MESSAGE_SEND
            )
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"notify: resolve channel {channel_id!r}: {err}")
        await provider.send(msg)

    return send


def _apply_payload_template(s: str, payload: Optional[WorkflowPayload]) -> str:
    """Substitute {{body}}, {{method}}, {{header.NAME}} and {{query.NAME}} in
    ``s`` with the inbound webhook payload values. Unknown or no-payload
    placeholders become the empty string."""
    if "{{" not in s:
        return s

    def get(expr: str) -> str:
        expr = expr.strip()
        if payload is None:
            return ""
        if expr == "body":
            return payload.body
        if expr == "method":
            return payload.method
        if expr.startswith("header."):
            return payload.headers.get(_canonical_header(expr[len("header.") :]), "")
        if expr.startswith("query."):
            return payload.query.get(expr[len("query.") :], "")
        return ""

    parts: list[str] = []
    while True:
        i = s.find("{{")
        if i < 0:
            parts.append(s)
            break
        j = s[i:].find("}}")
        if j < 0:
            parts.append(s)
            break
        parts.append(s[:i])
        parts.append(get(s[i + 2 : i + j]))
        s = s[i + j + 2 :]
    return "".join(parts)


def _canonical_header(name: str) -> str:
    """Canonicalise a header name to the canonical HTTP form (Title-Case per
    hyphen segment), so ``{{header.x-hub-signature}}`` matches
    the payload's canonicalised key."""
    return "-".join(part[:1].upper() + part[1:].lower() for part in name.split("-"))


def _status_matches(code: int, expect: str) -> bool:
    """Whether ``code`` matches an expectStatus expression: an exact code
    ("204") or a class like "2xx"."""
    expect = expect.strip().lower()
    if expect.endswith("xx") and len(expect) == 3:
        return f"{code // 100}xx" == expect
    return str(code) == expect


def _header_env_name(k: str) -> str:
    """Convert a header/query key to an env-var-safe upper-snake name, e.g.
    "X-Hub-Signature" -> "X_HUB_SIGNATURE"."""
    out = []
    for r in k.upper():
        if ("A" <= r <= "Z") or ("0" <= r <= "9"):
            out.append(r)
        else:
            out.append("_")
    return "".join(out)
