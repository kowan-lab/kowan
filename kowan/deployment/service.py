"""The deployment service: deploy, start, stop, destroy and reconcile deployments.

A ``Deployment`` is either
a single container spec or a compose project, deployed with one of two
strategies:

  - ``standalone``: the container/compose project is created on the target
    host.
  - ``swarm``: a stack is deployed on a single manager host, optionally with a
    replica count (for the container type a one-service compose file is
    synthesized from the spec).

Both strategies target exactly ONE host: the API layer (``api/deployments.py``)
constrains ``targets`` to a single entry. Fan-out across many standalone hosts
was intentionally removed — a named volume/bind mount is host-local, so "same
app on N hosts" silently diverges into N independent copies; a single host makes
volumes safe by construction. The lifecycle operations below resolve that single
host via ``_standalone_target`` (mirroring ``_swarm_target``) and act on it
directly; the empty-``targets`` case, unreachable through the API, raises a
descriptive ``Validation`` rather than silently no-opping.

Deployments are persisted in the store; their running state is reconciled on
demand against the Docker hosts via ``svc.docker``. Every managed
container/service carries the management labels declared in ``kowan.models`` so
it can be discovered later.

Notes for the learner:

  - The store is synchronous, so store calls are plain method calls (no
    ``await``). Docker ``Ops`` methods are async and must be awaited.
  - The store raises ``NotFoundError``; we translate those into descriptive
    wrapped errors.
  - Per-target deploy failure messages are kept in-memory (``_deploy_errs``),
    guarded by an ``asyncio.Lock``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets as _secrets
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Optional, Tuple

import yaml

from kowan import registry
from kowan.deployment import gitsource, previews
from kowan.errors import Conflict, Validation
from kowan.models import (
    Container,
    ContainerSpec,
    Deployment,
    DeploymentService as DeploymentServiceModel,
    DeploymentStrategySwarm,
    DeploymentTarget,
    DeploymentTypeCompose,
    DeploymentTypeContainer,
    Host,
    LabelDeploymentID,
    LabelDeploymentName,
    LabelManagedBy,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids import cycle
    from kowan.docker.base import AsyncByteStream, Ops
    from kowan.web import Services

log = logging.getLogger("kowan.deployment")

# Deployment status values persisted on the record.
STATUS_DEPLOYED = "deployed"
STATUS_STOPPED = "stopped"
STATUS_ERROR = "error"
# Transient statuses for the async (background) deploy: PENDING is set the instant
# a deploy is requested (queued, waiting for a worker slot); DEPLOYING is set while
# the pull + create is actually running. The terminal status (deployed/error) is
# written when the background work finishes.
STATUS_PENDING = "pending"
STATUS_DEPLOYING = "deploying"

# How many deployments may actively deploy at once. Extra requests stay PENDING
# until a slot frees, so a burst of deploys queues instead of hammering hosts.
_MAX_CONCURRENT_DEPLOYS = 3

# Desired-state reconcile: how often to check that ENABLED deployments are still
# live, and the minimum gap between two reconcile-driven redeploys of the SAME
# deployment (so a flapping/crash-looping container can't trigger a tight redeploy
# loop — the operator's "don't continuously redeploy" concern).
_RECONCILE_INTERVAL = float(os.environ.get("KOWAN_RECONCILE_INTERVAL", "5"))
_RECONCILE_COOLDOWN = float(os.environ.get("KOWAN_RECONCILE_COOLDOWN", "120"))

# Per-target runtime states reported by status().
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_NOT_DEPLOYED = "not_deployed"
STATE_ACTIVE = "active"
STATE_INACTIVE = "inactive"
# A compose target with some services up and others missing or stopped: its
# message names them, and the reconcile loop redeploys it like a stopped one.
STATE_DEGRADED = "degraded"
# stateError marks a target whose last deploy attempt failed and which has no
# running container to show for it. The DeploymentTarget carries the failure
# Message so the UI can render the reason.
STATE_ERROR = "error"

# Labels Docker Compose stamps onto every container it creates: the project
# name and the service name within the project. Used to discover/group
# compose-managed containers for status reporting.
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"


def _now_rfc3339() -> str:
    """Current UTC time as an RFC3339 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def managed_labels(dep: Deployment) -> Dict[str, str]:
    """The management labels stamped onto every resource a deployment creates."""
    return {
        LabelManagedBy: "true",
        LabelDeploymentID: dep.id,
        LabelDeploymentName: dep.name,
    }


def _spec_with_managed_labels(dep: Deployment, spec: ContainerSpec) -> ContainerSpec:
    """Return a copy of ``spec`` with the deployment's management labels merged.

    The deployment labels take precedence. The spec is deep-copied so the
    caller's (possibly persisted) ContainerSpec is never mutated in place.
    """
    spec = spec.model_copy(deep=True)
    labels: Dict[str, str] = dict(spec.labels or {})
    labels.update(managed_labels(dep))
    spec.labels = labels
    return spec


def _is_running(state: str) -> bool:
    """Whether a Docker container state string denotes a running container."""
    return state == "running"


def _compose_services(text: str) -> Dict[str, str]:
    """``{service: restart policy}`` for the services a compose file starts by
    default: those behind a profile or scaled to 0 are left out. Empty when the
    file does not parse (status then falls back to any container of the project)."""
    try:
        doc = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return {}
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict):
        return {}
    out: Dict[str, str] = {}
    for name, svc in services.items():
        svc = svc if isinstance(svc, dict) else {}
        deploy = svc.get("deploy") if isinstance(svc.get("deploy"), dict) else {}
        if svc.get("profiles") or svc.get("scale") == 0 or deploy.get("replicas") == 0:
            continue
        out[str(name)] = str(svc.get("restart") or "no")
    return out


_EXIT_RE = re.compile(r"exited \((-?\d+)\)", re.IGNORECASE)


def _compose_problems(services: Dict[str, str], containers: List[Container]) -> Tuple[List[str], List[str]]:
    """The services of a compose project that are missing (no container at all)
    and stopped (none running). A one-shot service (restart "no" or
    "on-failure") that exited 0 has done its job and counts as up."""
    missing: List[str] = []
    stopped: List[str] = []
    for name, restart in sorted(services.items()):
        cs = [c for c in containers if (c.labels or {}).get(COMPOSE_SERVICE_LABEL) == name]
        if not cs:
            missing.append(name)
            continue
        if any(_is_running(c.state) for c in cs):
            continue
        codes = [_EXIT_RE.search(c.status or "") for c in cs]
        if restart.split(":")[0] in ("no", "on-failure") and all(m and m.group(1) == "0" for m in codes):
            continue
        stopped.append(name)
    return missing, stopped


def _generate_redeploy_token() -> str:
    """Random 32-char hex token for a deployment's public redeploy webhook."""
    return _secrets.token_hex(16)


def _swarm_target(dep: Deployment) -> str:
    """Return the single manager host id for a swarm deployment."""
    if not dep.targets:
        raise Validation(f"swarm deployment {dep.name!r} has no target host")
    return dep.targets[0]


def _is_git(dep: Deployment) -> bool:
    """Whether ``dep`` is a compose deployment backed by a git repository."""
    return (
        dep.type == DeploymentTypeCompose
        and dep.git is not None
        and bool(dep.git.repo_url)
    )


def _standalone_target(dep: Deployment) -> str:
    """Return the single target host id for a standalone deployment."""
    if not dep.targets:
        raise Validation(f"standalone deployment {dep.name!r} has no target host")
    return dep.targets[0]


class DeploymentService:
    """Manages deployments: persistence via the store and lifecycle operations
    against Docker hosts via the provider.

    Constructed with the DI ``svc``
    container (``svc.store`` and ``svc.docker`` are the ones used here).
    """

    def __init__(self, svc: "Services") -> None:
        self.svc = svc
        self.store = svc.store
        self.provider = svc.docker
        # Last per-target deploy failure message, keyed by deployment id then
        # host id. Guarded by _lock.
        self._deploy_errs: Dict[str, Dict[str, str]] = {}
        self._lock = asyncio.Lock()
        # In-flight background deploy tasks, keyed by deployment id, so a second
        # deploy of the same deployment is rejected instead of overlapping.
        self._tasks: Dict[str, "asyncio.Task"] = {}
        # Caps concurrent background deploys; extras wait (status stays PENDING).
        self._deploy_sem = asyncio.Semaphore(_MAX_CONCURRENT_DEPLOYS)
        # monotonic timestamp of the last reconcile-driven redeploy per deployment,
        # for the per-deployment cooldown.
        self._reconcile_at: Dict[str, float] = {}

    # -- per-target error bookkeeping ----------------------------------------

    async def _record_target_error(self, dep_id: str, host_id: str, msg: str) -> None:
        """Store (or, when ``msg`` is empty, clear) a target's last failure."""
        async with self._lock:
            hosts = self._deploy_errs.get(dep_id)
            if msg == "":
                if hosts is not None:
                    hosts.pop(host_id, None)
                    if not hosts:
                        self._deploy_errs.pop(dep_id, None)
                return
            if hosts is None:
                hosts = {}
                self._deploy_errs[dep_id] = hosts
            hosts[host_id] = msg

    async def _target_error(self, dep_id: str, host_id: str) -> str:
        """Return the last recorded deploy failure message for a target."""
        async with self._lock:
            return self._deploy_errs.get(dep_id, {}).get(host_id, "")

    async def _clear_target_errors(self, dep_id: str) -> None:
        """Drop all recorded deploy failures for a deployment."""
        async with self._lock:
            self._deploy_errs.pop(dep_id, None)

    # -- host / registry helpers ---------------------------------------------

    def _ops_for_host(self, host_id: str) -> Tuple["Ops", Host]:
        """Resolve the ``Ops`` implementation for a host id, with the host record."""
        try:
            host = self.store.get_host(host_id)
        except Exception as err:  # NotFoundError et al.
            raise Validation(f"get host {host_id!r}: {err}") from err
        try:
            ops = self.provider.ops(host)
        except Exception as err:
            raise Validation(f"resolve host {host_id!r} client: {err}") from err
        return ops, host

    def _resolve_spec_secrets(self, spec: ContainerSpec) -> ContainerSpec:
        """Rewrite ``[[name]]``/``[[name.field]]`` refs into decrypted values.

        Resolves each environment variable value plus the entrypoint and command
        arguments (the common places credentials are referenced); keys are left
        untouched. The Env/Entrypoint/Command lists are copied before rewriting
        so the caller's spec is never mutated in place.
        """
        if spec.env:
            env = []
            for kv in spec.env:
                kv = kv.model_copy()
                kv.value = self.store.resolve_secrets(kv.value)
                env.append(kv)
            spec.env = env
        if spec.entrypoint:
            spec.entrypoint = [self.store.resolve_secrets(a) for a in spec.entrypoint]
        if spec.command:
            spec.command = [self.store.resolve_secrets(a) for a in spec.command]
        return spec

    # -- Deploy --------------------------------------------------------------

    async def dispatch_deploy(self, dep_id: str, *, redeploy: bool = False) -> Deployment:
        """Queue a deploy to run in the BACKGROUND and return immediately.

        The deployment is flipped to ``pending`` and persisted, then a background
        task runs the real work (which flips it to ``deploying`` once a worker slot
        is free, then to ``deployed``/``error`` on completion). The HTTP caller
        never blocks on image pulls or container creation.

        A second dispatch while one is already in flight for the same deployment is
        rejected with :class:`Conflict` (409). Returns the ``pending`` record.
        """
        dep = self.store.get_deployment(dep_id)  # NotFoundError -> 404 upstream
        existing = self._tasks.get(dep_id)
        if existing is not None and not existing.done():
            raise Conflict("a deploy is already in progress for this deployment")

        dep.status = STATUS_PENDING
        dep.updated_at = _now_rfc3339()
        self.store.update_deployment(dep)

        task = asyncio.create_task(self._run_deploy_bg(dep_id, redeploy))
        self._tasks[dep_id] = task
        task.add_done_callback(self._on_deploy_done)
        return dep

    def _on_deploy_done(self, task: "asyncio.Task") -> None:
        """Drop the finished task and surface an unexpected crash in the log. The
        terminal deployed/error status is written by :meth:`deploy` itself; here we
        only log if the background task blew up outside that (should not happen)."""
        for dep_id, t in list(self._tasks.items()):
            if t is task:
                self._tasks.pop(dep_id, None)
                break
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                log.error("background deploy task crashed: %s", exc)

    async def _run_deploy_bg(self, dep_id: str, redeploy: bool) -> None:
        """Background worker: wait for a concurrency slot (status stays PENDING),
        mark DEPLOYING, then run the real deploy. ``deploy``/``redeploy`` set the
        terminal status and raise on failure; the raise ends the task and is logged
        by the done callback (the error status is already persisted)."""
        async with self._deploy_sem:
            try:
                await self._set_status(dep_id, STATUS_DEPLOYING)
            except Exception as err:  # noqa: BLE001 - non-fatal; deploy still runs
                log.error("mark deploying dep=%s: %s", dep_id, err)
            if redeploy:
                await self.redeploy(dep_id)
            else:
                await self.deploy(dep_id)

    async def _set_status(self, dep_id: str, status: str) -> None:
        """Persist just the status + updatedAt of a deployment (reloads first so a
        concurrent edit to other fields is not clobbered)."""
        dep = self.store.get_deployment(dep_id)
        dep.status = status
        dep.updated_at = _now_rfc3339()
        self.store.update_deployment(dep)

    # -- desired-state reconcile --------------------------------------------
    #
    # A deployment is a COMMITMENT to keep something live: the reconcile loop
    # redeploys an ENABLED deployment when a target that should be up is missing or
    # down. It is deliberately conservative — it only acts on deployments in the
    # ``deployed`` state (a manual stop -> ``stopped``, a failed deploy -> ``error``
    # are both left alone, so a broken deploy never loops), skips one that already
    # has a deploy in flight, and applies a per-deployment cooldown so a
    # crash-looping container cannot drive a tight redeploy loop.

    async def run_reconcile_loop(self, interval: float = _RECONCILE_INTERVAL) -> None:
        """Background loop: every ``interval`` seconds run one reconcile pass until
        cancelled (server cleanup cancels it). Never dies on a pass error."""
        log.info("deployment reconcile loop started (interval=%ss)", interval)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.reconcile_once()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - keep the loop alive
                    log.error("reconcile pass failed: %s", err)
        except asyncio.CancelledError:
            log.info("deployment reconcile loop stopped")
            raise

    async def reconcile_once(self) -> int:
        """Run one reconcile pass; return how many deployments were redispatched.

        Best-effort: a per-deployment failure is logged and never aborts the pass.
        """
        try:
            deployments = self.store.list_deployments()
        except Exception as err:  # noqa: BLE001
            log.error("reconcile: list deployments failed: %s", err)
            return 0

        now = time.monotonic()
        dispatched = 0
        for dep in deployments:
            # Paused deployments are left entirely alone.
            if not dep.enabled:
                continue
            # Only maintain deployments that are meant to be up right now.
            if dep.status != STATUS_DEPLOYED:
                continue
            # Don't stack onto an in-flight deploy.
            task = self._tasks.get(dep.id)
            if task is not None and not task.done():
                continue
            # Per-deployment cooldown: never reconcile-redeploy the same one more
            # than once per _RECONCILE_COOLDOWN, so a flapping container can't loop.
            # (Never reconciled: no cooldown. A default of 0 would mean "just
            # now" for the first minutes after the host boots, as monotonic
            # time starts there.)
            last = self._reconcile_at.get(dep.id)
            if last is not None and now - last < _RECONCILE_COOLDOWN:
                continue
            try:
                if not await self._needs_reconcile(dep):
                    continue
                log.info("reconcile: %s has a down/missing target; redeploying", dep.name)
                self._reconcile_at[dep.id] = now
                await self.dispatch_deploy(dep.id, redeploy=True)
                dispatched += 1
            except Conflict:
                pass  # already deploying — fine
            except Exception as err:  # noqa: BLE001
                log.error("reconcile %s failed: %s", dep.name, err)
        return dispatched

    async def _needs_reconcile(self, dep: Deployment) -> bool:
        """Whether any target of ``dep`` is not up (missing/stopped/errored)."""
        targets = await self.status(dep.id)
        if not targets:
            return False
        return any(t.state not in (STATE_RUNNING, STATE_ACTIVE) for t in targets)

    async def deploy(self, dep_id: str) -> None:
        """Bring a deployment up on its targets.

        For standalone it creates the container (or compose project) on every
        target host; for swarm it deploys a stack on the (single) manager host.
        The deployment's status and updatedAt are persisted on completion.

        Callers pass the deployment id and the record is loaded here, so callers
        (webhook, api) never race a stale copy.
        """
        dep = self.store.get_deployment(dep_id)
        err: Optional[Exception] = None
        # A git-sourced compose deployment is fetched first: ``run_dep`` carries the
        # rendered compose (with .env applied) while ``dep`` (persisted) keeps the
        # raw file text for display, plus the commit once the deploy succeeds.
        run_dep = dep
        commit = ""
        try:
            if _is_git(dep):
                run_dep, commit = await self._prepare_git(dep, fetch=True)
            if dep.strategy == DeploymentStrategySwarm:
                await self._deploy_swarm(run_dep)
            else:
                await self._deploy_standalone(run_dep)
        except Exception as e:  # noqa: BLE001 - recorded + re-wrapped below
            err = e

        if err is None and commit:
            dep.deployed_commit = commit
        dep.status = STATUS_ERROR if err is not None else STATUS_DEPLOYED
        dep.updated_at = _now_rfc3339()
        try:
            # A git deploy that ran a new commit records it as a revision.
            self.store.update_deployment(dep, source="git", note=f"commit {commit[:12]}" if commit else "")
            if err is None:
                self.store.mark_deployment_revision_deployed(dep.id, dep.updated_at)
        except Exception as uerr:
            if err is not None:
                raise Validation(
                    f"deploy: {err} (and persist status: {uerr})"
                ) from err
            raise Validation(f"persist deployment status: {uerr}") from uerr
        if err is not None:
            raise Validation(f"deploy: {err}") from err

    async def _deploy_standalone(self, dep: Deployment) -> None:
        """Deploy to the single target host, recording its error.

        On failure the per-target error is recorded (and surfaced by status)
        and re-raised; on success the recorded error is cleared.
        """
        host_id = _standalone_target(dep)
        try:
            await self._deploy_standalone_target(dep, host_id)
        except Exception as err:  # noqa: BLE001
            await self._record_target_error(dep.id, host_id, str(err))
            raise
        await self._record_target_error(dep.id, host_id, "")

    async def _deploy_standalone_target(self, dep: Deployment, host_id: str) -> None:
        """Deploy the deployment onto a single standalone host."""
        ops, _ = self._ops_for_host(host_id)
        if dep.type == DeploymentTypeContainer:
            if dep.spec is None:
                raise Validation(f"deploy {dep.name!r}: container spec is required")
            # Best-effort pull; ignore failures so locally present images still
            # deploy (and the create step surfaces a missing-image error). When
            # a stored registry matches the image's host, authenticate the pull.
            auth = await registry.docker_auth_for(self.store, dep.spec.image)
            try:
                rc = await ops.pull_image(dep.spec.image, auth)
                await _drain(rc)
            except Exception as perr:  # noqa: BLE001
                log.debug(
                    "deploy pull image failed deployment=%s host=%s image=%s err=%s",
                    dep.id, host_id, dep.spec.image, perr,
                )
            spec = _spec_with_managed_labels(dep, dep.spec)
            # Resolve [[name]]/[[name.field]] secret refs into decrypted values
            # before create; the resolved secrets become container env values.
            try:
                spec = await asyncio.to_thread(self._resolve_spec_secrets, spec)
            except Exception as err:
                raise Validation(f"resolve secrets for {dep.name!r}: {err}") from err
            try:
                await ops.create_container(dep.resource_name(), spec, True)
            except Exception as err:
                raise Validation(
                    f"create container on host {host_id!r}: {err}"
                ) from err
        elif dep.type == DeploymentTypeCompose:
            # Fill in the stack variables, then resolve secret refs (a variable's
            # value may be one) before bringing it up.
            try:
                compose = await asyncio.to_thread(self.store.resolve_secrets, compose_with_variables(dep))
            except Exception as err:
                raise Validation(f"resolve secrets for {dep.name!r}: {err}") from err
            try:
                await ops.compose_up(dep.resource_name(), compose)
            except Exception as err:
                raise Validation(f"compose up on host {host_id!r}: {err}") from err
        else:
            raise Validation(f"deploy {dep.name!r}: unknown type {dep.type!r}")

    async def _deploy_swarm(self, dep: Deployment) -> None:
        """Deploy a stack on the (single) manager host, recording its error."""
        host_id = _swarm_target(dep)
        try:
            await self._deploy_swarm_target(dep, host_id)
        except Exception as derr:  # noqa: BLE001
            await self._record_target_error(dep.id, host_id, str(derr))
            raise
        await self._record_target_error(dep.id, host_id, "")

    async def _deploy_swarm_target(self, dep: Deployment, host_id: str) -> None:
        """Deploy the swarm stack on the resolved manager host."""
        from .compose import synthesize_swarm_compose

        ops, _ = self._ops_for_host(host_id)

        active = await ops.swarm_active()
        if not active:
            raise Validation(
                f"target host {host_id!r} is not an active swarm manager"
            )

        if dep.type == DeploymentTypeCompose:
            compose = compose_with_variables(dep)
        elif dep.type == DeploymentTypeContainer:
            if dep.spec is None:
                raise Validation(f"deploy {dep.name!r}: container spec is required")
            # Resolve secret refs in the spec before synthesizing the one-service
            # compose file so the resolved values reach the swarm service env.
            spec = dep.spec.model_copy(deep=True)
            try:
                spec = await asyncio.to_thread(self._resolve_spec_secrets, spec)
            except Exception as err:
                raise Validation(f"resolve secrets for {dep.name!r}: {err}") from err
            swarm_dep = dep.model_copy()
            swarm_dep.spec = spec
            compose = synthesize_swarm_compose(swarm_dep)
        else:
            raise Validation(f"deploy {dep.name!r}: unknown type {dep.type!r}")

        # Resolve any secret refs in the (compose-typed) or synthesized YAML
        # before handing it to the swarm.
        try:
            compose = await asyncio.to_thread(self.store.resolve_secrets, compose)
        except Exception as err:
            raise Validation(f"resolve secrets for {dep.name!r}: {err}") from err

        try:
            await ops.swarm_deploy(dep.resource_name(), compose)
        except Exception as err:
            raise Validation(f"swarm deploy on host {host_id!r}: {err}") from err

    async def redeploy(self, dep_id: str) -> None:
        """Redeploy is identical to deploy (recreates all resources)."""
        await self.deploy(dep_id)

    # -- Git source ----------------------------------------------------------

    def git_checkout_dir(self, dep_id: str) -> str:
        """Where a git-sourced deployment's shallow clone lives: <data>/git/<id>."""
        cfg = getattr(self.svc, "config", None)
        data_dir = getattr(cfg, "data_dir", "") or "./data"
        return os.path.join(data_dir, "git", dep_id)

    def remove_git_checkout(self, dep_id: str) -> None:
        """Best-effort removal of a deployment's checkout (on delete / git off)."""
        import shutil

        shutil.rmtree(self.git_checkout_dir(dep_id), ignore_errors=True)

    async def _git_auth(self, dep: Deployment) -> Optional[gitsource.GitAuth]:
        """Resolve the deployment's credential (a ``gitFetch`` connection) into
        HTTP auth; None for a public repo."""
        if dep.git is None or not dep.git.credential_id:
            return None
        from kowan import capabilities, connections

        try:
            prov = await connections.resolve(
                self.store, dep.git.credential_id, capabilities.CAP_GIT_FETCH
            )
            username, token = prov.git_auth(dep.git.repo_url)
        except Exception as err:  # noqa: BLE001
            raise Validation(f"git credential {dep.git.credential_id!r}: {err}") from err
        return gitsource.GitAuth(username=username or "", token=token or "")

    async def _prepare_git(self, dep: Deployment, *, fetch: bool) -> Tuple[Deployment, str]:
        """Fetch (when ``fetch`` or no checkout yet) and read the compose file.

        Mutates ``dep`` (raw ``compose`` text + ``last_sync_at`` when fetched) and
        returns ``(run_dep, commit)`` where ``run_dep`` is a copy whose ``compose``
        has the ``.env`` applied. A failure is recorded as the target's error so
        the UI shows why the deploy failed.
        """
        assert dep.git is not None
        dest = self.git_checkout_dir(dep.id)
        commit = dep.deployed_commit
        try:
            if fetch or not os.path.isdir(dest):
                auth = await self._git_auth(dep)
                commit = await gitsource.clone(dep.git.repo_url, dep.git.ref, dest, auth)
                dep.last_sync_at = _now_rfc3339()
            raw, env_text = await asyncio.to_thread(gitsource.read_compose, dest, dep.git.path)
        except Exception as err:  # noqa: BLE001
            if dep.targets:
                await self._record_target_error(dep.id, dep.targets[0], f"git: {err}")
            raise Validation(f"git source: {err}") from err
        dep.compose = raw
        run_dep = dep.model_copy(deep=True)
        if dep.preview is not None:
            # A preview runs next to its parent: no fixed host ports or names.
            run_dep.compose = previews.isolate_compose(
                gitsource.render_compose(raw, env_text + "\n" + previews.preview_env(dep), stack_variables(dep)))
        else:
            run_dep.compose = gitsource.render_compose(raw, env_text, stack_variables(dep))
        return run_dep, commit

    async def remote_commit(self, dep: Deployment) -> str:
        """The commit the configured ref currently points at on the remote."""
        if dep.git is None:
            raise Validation("deployment has no git source")
        auth = await self._git_auth(dep)
        try:
            return await gitsource.ls_remote(dep.git.repo_url, dep.git.ref, auth)
        except gitsource.GitError as err:
            raise Validation(str(err)) from err

    async def sync(self, dep_id: str, *, force: bool = False) -> Tuple[Deployment, bool, str]:
        """"Pull & redeploy": look up the latest commit of the configured ref and,
        when it differs from the deployed one (or ``force``), queue a redeploy.

        Returns ``(deployment, changed, remote_commit)``.
        """
        dep = self.store.get_deployment(dep_id)
        if not _is_git(dep):
            raise Validation("deployment has no git source")
        commit = await self.remote_commit(dep)
        # Persist the sync time (reload first so we don't clobber a concurrent edit).
        dep = self.store.get_deployment(dep_id)
        dep.last_sync_at = _now_rfc3339()
        self.store.update_deployment(dep)
        changed = force or commit != dep.deployed_commit
        if changed:
            dep = await self.dispatch_deploy(dep_id, redeploy=True)
        return dep, changed, commit

    # -- Destroy -------------------------------------------------------------

    async def destroy(self, dep_id: str) -> None:
        """Remove a deployment's running resources while keeping its record."""
        dep = self.store.get_deployment(dep_id)
        err: Optional[Exception] = None
        try:
            if dep.strategy == DeploymentStrategySwarm:
                await self._swarm_remove(dep)
            else:
                await self._destroy_standalone(dep)
        except Exception as e:  # noqa: BLE001
            err = e

        # The deployment is torn down; recorded per-target failures no longer
        # reflect reality, so drop them on success.
        if err is None:
            await self._clear_target_errors(dep.id)

        dep.status = STATUS_STOPPED
        dep.updated_at = _now_rfc3339()
        try:
            self.store.update_deployment(dep)
        except Exception as uerr:
            if err is not None:
                raise Validation(
                    f"destroy: {err} (and persist status: {uerr})"
                ) from err
            raise Validation(f"persist deployment status: {uerr}") from uerr
        if err is not None:
            raise Validation(f"destroy: {err}") from err

    async def _destroy_standalone(self, dep: Deployment) -> None:
        """Tear down a standalone deployment on its target host."""
        host_id = _standalone_target(dep)
        ops, _ = self._ops_for_host(host_id)
        if dep.type == DeploymentTypeContainer:
            try:
                await self._remove_managed_containers(ops, dep)
            except Exception as err:
                raise Validation(
                    f"remove managed containers on host {host_id!r}: {err}"
                ) from err
        elif dep.type == DeploymentTypeCompose:
            try:
                await ops.compose_down(dep.resource_name())
            except Exception as err:
                raise Validation(
                    f"compose down on host {host_id!r}: {err}"
                ) from err
        else:
            raise Validation(f"destroy {dep.name!r}: unknown type {dep.type!r}")

    async def _swarm_remove(self, dep: Deployment) -> None:
        """Remove a swarm stack from the manager host."""
        host_id = _swarm_target(dep)
        ops, _ = self._ops_for_host(host_id)
        try:
            await ops.swarm_remove(dep.resource_name())
        except Exception as err:
            raise Validation(f"swarm remove on host {host_id!r}: {err}") from err

    async def _remove_managed_containers(self, ops: "Ops", dep: Deployment) -> None:
        """Force-remove every container on the host carrying this deployment's id."""
        try:
            containers = await ops.list_containers(True)
        except Exception as err:
            raise Validation(f"list containers: {err}") from err
        for c in containers:
            if (c.labels or {}).get(LabelDeploymentID) != dep.id:
                continue
            try:
                await ops.remove_container(c.id, True)
            except Exception as err:
                raise Validation(f"remove container {c.id}: {err}") from err

    # -- Start / Stop --------------------------------------------------------

    async def start(self, dep_id: str) -> None:
        """(Re)start a deployment's resources.

        For standalone containers it starts the existing managed containers per
        target; for standalone compose it brings the project up again. For swarm
        it (re)deploys the stack.
        """
        dep = self.store.get_deployment(dep_id)
        if dep.strategy == DeploymentStrategySwarm:
            # Re-deploying the stack is the start operation for swarm.
            await self.deploy(dep_id)
            return

        err: Optional[Exception] = None
        try:
            if dep.type == DeploymentTypeContainer:
                await self._start_standalone_containers(dep)
            elif dep.type == DeploymentTypeCompose:
                await self._start_standalone_compose(dep)
            else:
                raise Validation(f"start {dep.name!r}: unknown type {dep.type!r}")
        except Exception as e:  # noqa: BLE001
            err = e
        await self._persist_after_toggle(dep, STATUS_DEPLOYED, err, "start")

    async def _start_standalone_containers(self, dep: Deployment) -> None:
        host_id = _standalone_target(dep)
        ops, _ = self._ops_for_host(host_id)
        ids = await self._managed_container_ids(ops, dep)
        for cid in ids:
            try:
                await ops.start_container(cid)
            except Exception as err:
                raise Validation(
                    f"start container {cid} on host {host_id!r}: {err}"
                ) from err

    async def _start_standalone_compose(self, dep: Deployment) -> None:
        host_id = _standalone_target(dep)
        ops, _ = self._ops_for_host(host_id)
        if _is_git(dep):
            # Restart from the existing checkout (no fetch): Start brings back what
            # was deployed, it does not pull new commits ("Pull & redeploy" does).
            dep, _ = await self._prepare_git(dep.model_copy(deep=True), fetch=False)
        # A Stop runs 'compose down' (removing containers), so Start must
        # recreate with the variables and resolved secret values, like deploy.
        try:
            compose = await asyncio.to_thread(self.store.resolve_secrets, compose_with_variables(dep))
        except Exception as err:
            raise Validation(f"resolve secrets for {dep.name!r}: {err}") from err
        try:
            await ops.compose_up(dep.resource_name(), compose)
        except Exception as err:
            raise Validation(f"compose up on host {host_id!r}: {err}") from err

    async def stop(self, dep_id: str) -> None:
        """Halt a deployment's resources.

        For standalone containers it stops the managed containers per target;
        for standalone compose it brings the project down. For swarm it removes
        the stack.
        """
        dep = self.store.get_deployment(dep_id)
        if dep.strategy == DeploymentStrategySwarm:
            err: Optional[Exception] = None
            try:
                await self._swarm_remove(dep)
            except Exception as e:  # noqa: BLE001
                err = e
            await self._persist_after_toggle(dep, STATUS_STOPPED, err, "stop")
            return

        err = None
        try:
            if dep.type == DeploymentTypeContainer:
                await self._stop_standalone_containers(dep)
            elif dep.type == DeploymentTypeCompose:
                await self._stop_standalone_compose(dep)
            else:
                raise Validation(f"stop {dep.name!r}: unknown type {dep.type!r}")
        except Exception as e:  # noqa: BLE001
            err = e
        await self._persist_after_toggle(dep, STATUS_STOPPED, err, "stop")

    async def _stop_standalone_containers(self, dep: Deployment) -> None:
        host_id = _standalone_target(dep)
        ops, _ = self._ops_for_host(host_id)
        ids = await self._managed_container_ids(ops, dep)
        for cid in ids:
            try:
                await ops.stop_container(cid)
            except Exception as err:
                raise Validation(
                    f"stop container {cid} on host {host_id!r}: {err}"
                ) from err

    async def _stop_standalone_compose(self, dep: Deployment) -> None:
        host_id = _standalone_target(dep)
        ops, _ = self._ops_for_host(host_id)
        try:
            await ops.compose_down(dep.resource_name())
        except Exception as err:
            raise Validation(f"compose down on host {host_id!r}: {err}") from err

    async def _persist_after_toggle(
        self,
        dep: Deployment,
        ok_status: str,
        op_err: Optional[Exception],
        op: str,
    ) -> None:
        """Record the resulting status of a start/stop, wrapping any op error."""
        dep.status = STATUS_ERROR if op_err is not None else ok_status
        dep.updated_at = _now_rfc3339()
        try:
            self.store.update_deployment(dep)
        except Exception as uerr:
            if op_err is not None:
                raise Validation(
                    f"{op}: {op_err} (and persist status: {uerr})"
                ) from op_err
            raise Validation(f"persist deployment status: {uerr}") from uerr
        if op_err is not None:
            raise Validation(f"{op}: {op_err}") from op_err

    # -- Status --------------------------------------------------------------

    async def status(self, dep_id: str) -> List[DeploymentTarget]:
        """Report the per-target runtime state of a deployment.

        Never raises for an unreachable host; instead the affected target
        carries a descriptive message and a not_deployed/inactive state.
        """
        dep = self.store.get_deployment(dep_id)
        if dep.strategy == DeploymentStrategySwarm:
            return await self._status_swarm(dep)
        return await self._status_standalone(dep)

    async def _status_standalone(self, dep: Deployment) -> List[DeploymentTarget]:
        # No target (unreachable through the API): nothing to report.
        if not dep.targets:
            return []
        host_id = dep.targets[0]

        t = DeploymentTarget(hostId=host_id)
        try:
            ops, host = self._ops_for_host(host_id)
            t.host_name = host.name
        except Exception as err:  # noqa: BLE001
            t.state = STATE_NOT_DEPLOYED
            t.message = str(err)
            return [t]

        try:
            containers = await ops.list_containers(True)
        except Exception as err:  # noqa: BLE001
            t.state = STATE_NOT_DEPLOYED
            t.message = str(err)
            return [t]

        match = self._match_container(dep, containers)
        if match is None:
            # No running container. Surface the last deploy error if any.
            msg = await self._target_error(dep.id, host_id)
            if msg:
                t.state = STATE_ERROR
                t.message = msg
            else:
                t.state = STATE_NOT_DEPLOYED
        else:
            t.container_id = match.id
            t.state = STATE_RUNNING if _is_running(match.state) else STATE_STOPPED
            if dep.type == DeploymentTypeCompose:
                self._compose_state(dep, containers, t)
        return [t]

    def _compose_state(self, dep: Deployment, containers: List[Container], t: DeploymentTarget) -> None:
        """Judge a compose target by every service it declares, not by its first
        container: one service up and the others gone is ``degraded``."""
        services = _compose_services(dep.compose)
        if not services:
            return
        project = [c for c in containers if (c.labels or {}).get(COMPOSE_PROJECT_LABEL) == dep.resource_name()]
        missing, stopped = _compose_problems(services, project)
        if not missing and not stopped:
            t.state = STATE_RUNNING
            return
        if not any(_is_running(c.state) for c in project):
            t.state = STATE_STOPPED
            return
        t.state = STATE_DEGRADED
        t.message = "; ".join(p for p in (
            f"{', '.join(missing)} missing" if missing else "",
            f"{', '.join(stopped)} stopped" if stopped else "",
        ) if p)

    async def _status_swarm(self, dep: Deployment) -> List[DeploymentTarget]:
        try:
            host_id = _swarm_target(dep)
        except Exception as err:  # noqa: BLE001
            return [DeploymentTarget(state=STATE_NOT_DEPLOYED, message=str(err))]

        t = DeploymentTarget(hostId=host_id)
        try:
            ops, host = self._ops_for_host(host_id)
            t.host_name = host.name
        except Exception as err:  # noqa: BLE001
            t.state = STATE_INACTIVE
            t.message = str(err)
            return [t]

        try:
            active = await ops.swarm_active()
        except Exception as err:  # noqa: BLE001
            t.state = STATE_INACTIVE
            t.message = str(err)
            return [t]
        if not active:
            t.state = STATE_INACTIVE
            t.message = "host is not an active swarm manager"
            return [t]

        try:
            containers = await ops.list_containers(True)
        except Exception as err:  # noqa: BLE001
            t.state = STATE_ACTIVE
            t.message = str(err)
            return [t]

        match = self._match_container(dep, containers)
        if match is not None:
            t.container_id = match.id
            t.state = STATE_RUNNING if _is_running(match.state) else STATE_STOPPED
        else:
            msg = await self._target_error(dep.id, host_id)
            if msg:
                t.state = STATE_ERROR
                t.message = msg
            else:
                t.state = STATE_NOT_DEPLOYED
        return [t]

    # -- Services ------------------------------------------------------------

    async def services(
        self, dep_id: str, host_id: str
    ) -> List[DeploymentServiceModel]:
        """Report the per-service runtime status of a deployment on a host.

        For the container type it returns a single entry describing the managed
        container. For compose it lists every container on the host belonging to
        the compose project (project label == resource_name) and returns one
        entry per service (grouped by the compose service label). An unreachable
        host or a not-yet-deployed project yields an empty list (no error).
        """
        dep = self.store.get_deployment(dep_id)
        ops, _ = self._ops_for_host(host_id)
        try:
            containers = await ops.list_containers(True)
        except Exception as err:
            raise Validation(f"list containers: {err}") from err

        if dep.type == DeploymentTypeContainer:
            match = self._match_container(dep, containers)
            if match is None:
                return []
            return [_container_to_service(dep.name, match)]
        if dep.type == DeploymentTypeCompose:
            out: List[DeploymentServiceModel] = []
            seen: Dict[str, bool] = {}
            for c in containers:
                labels = c.labels or {}
                if labels.get(COMPOSE_PROJECT_LABEL) != dep.resource_name():
                    continue
                svc_name = labels.get(COMPOSE_SERVICE_LABEL, "")
                if svc_name == "" or seen.get(svc_name):
                    continue
                seen[svc_name] = True
                out.append(_container_to_service(svc_name, c))
            return out
        raise Validation(f"services {dep.name!r}: unknown type {dep.type!r}")

    # -- container matching --------------------------------------------------

    def _match_container(
        self, dep: Deployment, containers: List[Container]
    ) -> Optional[Container]:
        """First container belonging to the deployment (by id label, or compose
        project label matching the resource name)."""
        for c in containers:
            labels = c.labels or {}
            if labels.get(LabelDeploymentID) == dep.id:
                return c
            if labels.get(COMPOSE_PROJECT_LABEL) == dep.resource_name():
                return c
        return None

    async def _managed_container_ids(
        self, ops: "Ops", dep: Deployment
    ) -> List[str]:
        """Ids of all containers on the host belonging to the deployment."""
        try:
            containers = await ops.list_containers(True)
        except Exception as err:
            raise Validation(f"list containers: {err}") from err
        ids: List[str] = []
        for c in containers:
            labels = c.labels or {}
            if (
                labels.get(LabelDeploymentID) == dep.id
                or labels.get(COMPOSE_PROJECT_LABEL) == dep.resource_name()
            ):
                ids.append(c.id)
        return ids

    def _deployment_container_by_id(
        self, dep: Deployment, containers: List[Container], container_id: str
    ) -> Optional[Container]:
        """Container with the given id (full or short-id prefix) that belongs to
        the deployment; None when no owned container matches."""
        for c in containers:
            if c.id != container_id and not c.id.startswith(container_id):
                continue
            labels = c.labels or {}
            if (
                labels.get(LabelDeploymentID) == dep.id
                or labels.get(COMPOSE_PROJECT_LABEL) == dep.resource_name()
            ):
                return c
        return None

    # -- Logs ----------------------------------------------------------------

    async def logs(
        self,
        dep: Deployment,
        host_id: str,
        container_id: str,
        follow: bool,
        tail: int,
    ) -> "AsyncByteStream":
        """Return a log stream for the deployment's managed container on a host.

        When ``container_id`` is non-empty it streams that specific container's
        logs (after validating it belongs to the deployment on the host). When
        empty the container is discovered via the management labels, defaulting
        to the first matched container.

        A deployment targets exactly one host, so the passed ``host_id`` is
        ignored in favour of that single target (guarded like ``_swarm_target``);
        the callers only ever pass the target host or an empty string.
        """
        host_id = _standalone_target(dep)
        ops, _ = self._ops_for_host(host_id)
        try:
            containers = await ops.list_containers(True)
        except Exception as err:
            raise Validation(f"list containers: {err}") from err

        if container_id != "":
            target = self._deployment_container_by_id(dep, containers, container_id)
            if target is None:
                raise Validation(
                    f"container {container_id!r} is not part of deployment "
                    f"{dep.name!r} on host {host_id!r}"
                )
        else:
            target = self._match_container(dep, containers)
            if target is None:
                raise Validation(
                    f"no managed container for deployment {dep.name!r} "
                    f"on host {host_id!r}"
                )

        try:
            return await ops.container_logs(target.id, follow, tail)
        except Exception as err:
            raise Validation(f"container logs {target.id}: {err}") from err


def _container_to_service(
    service: str, c: Container
) -> DeploymentServiceModel:
    """Map a managed container to its DeploymentService view under ``service``."""
    name = c.names[0].lstrip("/") if c.names else ""
    return DeploymentServiceModel(
        service=service,
        containerId=c.id,
        name=name,
        state=c.state,
        image=c.image,
        ports=list(c.ports),
    )


async def _drain(rc: "AsyncByteStream") -> None:
    """Read and discard a byte stream to completion, then close it (used on
    the best-effort pull stream)."""
    try:
        while True:
            chunk = await rc.read(64 * 1024)
            if not chunk:
                break
    finally:
        close = getattr(rc, "close", None)
        if close is not None:
            res = close()
            if isinstance(res, Awaitable):
                await res


# Public lifecycle op alias for the api layer.
LifecycleFn = Callable[[str], Awaitable[None]]


def stack_variables(dep: Deployment) -> Dict[str, str]:
    """A compose deployment's stack variables as a name -> value map."""
    return {v.key: v.value for v in dep.variables}


def compose_with_variables(dep: Deployment) -> str:
    """The compose text with the stack variables filled in. A git deployment's
    run copy already has them (with its .env); filling again changes nothing."""
    variables = stack_variables(dep)
    return gitsource.interpolate(dep.compose, variables) if variables else dep.compose
