"""Trigger poller for the polling trigger types.

On a fixed interval the poller walks
every enabled workflow, evaluates each of its enabled poll-kind triggers (rss,
docker-image, git-commit, git-pr) against its external source, and starts a run
through the engine when a trigger reports a change. The per-trigger "last seen"
state is persisted back so a change fires exactly once.

Two correctness rules:

  1. State advances ONLY AFTER a run actually starts. Advancing state before a
     failed ``engine.run`` would mark the change as seen while no run happened,
     dropping the event forever. On a run-start failure the state is left
     unchanged (so the next cycle retries) and the error is recorded.
  2. ``save_trigger_state`` RE-READS the workflow just before writing and merges
     only the volatile poller state (state/lastFired/lastError) by trigger id,
     so a concurrent operator edit (PUT /workflows) in the poll window is not
     clobbered (RMW race avoidance).

Learner notes: the store is synchronous, so store calls are plain (no await).
``evaluate`` is async because a trigger's ``Poller.evaluate`` does async HTTP.
The loop uses ``asyncio.sleep`` and is cancelled by cancelling the task.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List

from kowan import triggers
from kowan.models import Workflow, WorkflowTrigger
from kowan.store import NotFoundError

log = logging.getLogger("kowan.workflow.poller")

# pollInterval is how often the poller evaluates every enabled poll-kind trigger.
_POLL_INTERVAL = 60.0

# pollEvalTimeout bounds a single trigger evaluation so one slow/unreachable
# source cannot stall the whole poll cycle.
_POLL_EVAL_TIMEOUT = 25.0

# runTimeout matches the engine's per-run wall-clock budget.
_RUN_TIMEOUT = 60 * 60.0


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class TriggerPoller:
    """Drives the polling trigger types."""

    def __init__(self, engine, store) -> None:
        self._engine = engine
        self._store = store

    async def start(self) -> None:
        """Run the poll loop until cancelled. Does a first (seeding) pass shortly
        after startup so freshly added triggers seed their state quickly, then on
        every tick; cancel the task to stop it."""
        try:
            await asyncio.sleep(5.0)
            await self.poll_once()
            while True:
                await asyncio.sleep(_POLL_INTERVAL)
                await self.poll_once()
        except asyncio.CancelledError:
            raise

    async def poll_once(self) -> None:
        """Evaluate every enabled poll-kind trigger across all enabled workflows
        once. Per-workflow trigger state is saved after its triggers are
        processed."""
        try:
            workflows = self._store.list_workflows()
        except Exception as err:  # noqa: BLE001
            log.error("poller: list workflows err=%s", err)
            return

        for wf in workflows:
            if not wf.enabled or not wf.triggers:
                continue
            changed = False
            for t in wf.triggers:
                if not t.enabled or triggers.kind(t.type) != triggers.KindPoll:
                    continue
                if await self._evaluate(wf, t):
                    changed = True
            if changed:
                self._save_trigger_state(wf.id, wf.triggers)

    def _save_trigger_state(
        self, workflow_id: str, evaluated: List[WorkflowTrigger]
    ) -> None:
        """Persist the poller-owned state fields (state/lastFired/lastError) of
        ``evaluated`` onto the workflow's CURRENT triggers. Re-reads the workflow
        just before writing so a concurrent operator edit is preserved — only the
        volatile poller state is merged in, matched by stable trigger id. If the
        workflow was deleted mid-poll the write is skipped."""
        try:
            current = self._store.get_workflow(workflow_id)
        except NotFoundError:
            # The workflow was deleted during the poll: nothing to save.
            return
        except Exception as err:  # noqa: BLE001 - don't clobber on a read failure
            log.error(
                "poller: reload workflow for state save err=%s workflow=%s",
                err, workflow_id,
            )
            return

        by_id: Dict[str, WorkflowTrigger] = {t.id: t for t in evaluated}
        for t in current.triggers:
            src = by_id.get(t.id)
            if src is not None:
                t.state = src.state
                t.last_fired = src.last_fired
                t.last_error = src.last_error

        try:
            self._store.update_workflow_triggers(workflow_id, current.triggers)
        except Exception as err:  # noqa: BLE001
            log.error("poller: save trigger state err=%s workflow=%s", err, workflow_id)

    def _resolve_config(self, config: Dict[str, str]) -> Dict[str, str]:
        """Return a copy of a trigger's config with any [[secret]] references
        resolved to their live values. On a resolve error a value is left as-is."""
        out: Dict[str, str] = {}
        for k, v in config.items():
            try:
                out[k] = self._store.resolve_secrets(v)
            except Exception:  # noqa: BLE001
                out[k] = v
        return out

    def _connection_capability(self, ttype: str) -> str:
        """Return the capability declared by ``ttype``'s connection-picker field, or
        "" if the trigger type has no such field.

        A trigger type advertises a credential picker by setting ``connection`` on one
        of its :class:`FieldSpec`s (see ``triggers.fields``); the descriptor carries the
        capability the picker resolves. This lets ``_apply_connection`` dispatch on the
        trigger's OWN declared capability instead of hard-coding gitFetch."""
        for f in triggers.fields(ttype):
            desc = getattr(f, "connection", None)
            if desc:
                return str(desc.get("capability", "") or "")
        return ""

    async def _apply_connection(self, cfg: Dict[str, str], ttype: str = "") -> Dict[str, str]:
        """Return a copy of ``cfg`` with a referenced ``connectionRef`` resolved into
        the config fields the poller factory reads, and ``connectionRef`` removed.
        No-op when absent.

        A poll-kind trigger can reference credentials as a connection id (like
        registries/secrets/channels/volumes) instead of typing them inline. Which
        capability that connection resolves is declared by the trigger type's
        connection-picker field, so we dispatch on it:

          * ``gitFetch`` (git-commit): ``git_auth() -> (username, token)`` -> set
            ``config["username"]``/``["token"]``.
          * ``dockerRegistryAuth`` (docker-image): the registry Provider's
            ``credentials() -> (username, password, url)`` -> set
            ``config["username"]``/``["password"]`` and set ``config["registry"]``
            to the host part of the connection's url.

        In every case ``connectionRef`` is dropped from the returned config so the
        factory (``_new_git_commit`` / ``_new_docker_image``) reads plain fields, and
        old inline configs keep working. Empty resolved values never overwrite an
        existing half (a public repo / anonymous registry is left untouched).

        The store is synchronous, so we build the capability inline from
        ``get_credential`` (type/name/meta) + ``credential_config`` (decrypted data);
        the method itself is async because a cloud registry Provider's
        ``credentials()`` (ECR/GCR/ACR token exchange) is a coroutine.
        """
        ref = (cfg.get("connectionRef") or "").strip()
        if not ref:
            return cfg

        from kowan import capabilities, connections

        capability = self._connection_capability(ttype) or capabilities.CAP_GIT_FETCH
        cred = self._store.get_credential(ref)
        data = self._store.credential_config(ref)
        meta = dict(cred.meta)
        meta.setdefault("name", cred.name)
        provider = connections.build_capability(cred.type, data, capability, meta)

        out = {k: v for k, v in cfg.items() if k != "connectionRef"}

        if capability == capabilities.CAP_DOCKER_REGISTRY_AUTH:
            # A registry Provider exposes (username, password, url) via the async
            # credentials(). It also carries the registry HOST, so the picker supplies
            # both auth AND the registry to poll — no separate registry field needed.
            from kowan.registry.basic import scheme_and_host

            username, password, url = await provider.credentials()
            if username:
                out["username"] = username
            if password:
                out["password"] = password
            host = scheme_and_host(url)[1] if url else ""
            if host:
                out["registry"] = host
            return out

        # gitFetch (default): (username, token) for a private clone.
        username, token = provider.git_auth()
        # Only overwrite with non-empty resolved values, so a public repo with no
        # connection (or a token-only credential) leaves the other half alone.
        if username:
            out["username"] = username
        if token:
            out["token"] = token
        return out

    async def _evaluate(self, wf: Workflow, t: WorkflowTrigger) -> bool:
        """Run one trigger's Poller, mutating ``t`` in place (state/lastFired/
        lastError) and firing a run when it reports a change. Returns True when
        ``t`` was mutated and should be persisted."""
        # Resolve any [[secret]] references in the config before building the
        # provider (e.g. a git/registry token stored as [[github-token]]).
        cfg = await asyncio.to_thread(self._resolve_config, t.config)

        # Resolve a referenced credential connection into the auth fields the
        # trigger factory reads, dispatched on the capability the trigger type
        # declares (gitFetch -> username/token; dockerRegistryAuth ->
        # username/password + registry host). On failure record the error and skip
        # firing this cycle, exactly like the triggers.build failure branch below.
        try:
            cfg = await self._apply_connection(cfg, t.type)
        except Exception as err:  # noqa: BLE001 - bad ref / connection cannot auth
            msg = str(err)
            if t.last_error == msg:
                return False
            t.last_error = msg
            return True

        try:
            provider = triggers.build(t.type, cfg)
        except Exception as err:  # noqa: BLE001 - disabled plugin or bad config
            msg = str(err)
            if t.last_error == msg:
                return False
            t.last_error = msg
            return True

        try:
            fired, new_state = await asyncio.wait_for(
                provider.evaluate(cfg, t.state), timeout=_POLL_EVAL_TIMEOUT
            )
        except Exception as err:  # noqa: BLE001
            log.warning(
                "poller: trigger evaluate failed err=%s workflow=%s trigger=%s",
                err, wf.id, t.type,
            )
            t.last_error = str(err)
            return True

        mutated = False
        if t.last_error != "":
            t.last_error = ""
            mutated = True

        if fired:
            # A change was observed: start the run FIRST and only advance the
            # persisted state once it succeeds. Advancing state before a failed
            # run would mark the change as seen while no run happened, dropping
            # the event forever. Leaving state unchanged makes the poller retry
            # next cycle; the error is recorded so it surfaces in the UI.
            log.info(
                "poller: trigger fired workflow=%s name=%s trigger=%s",
                wf.id, wf.name, t.type,
            )
            try:
                await self._engine.run(wf.id, t.type, None)
            except Exception as err:  # noqa: BLE001
                log.error(
                    "poller: run failed err=%s workflow=%s trigger=%s",
                    err, wf.id, t.type,
                )
                t.last_error = str(err)
                return True
            t.state = new_state
            t.last_fired = _now_rfc3339()
            return True

        # No change to fire on: still persist any state advance the provider
        # reports (e.g. seeding the initial "last seen" value on the first poll).
        if new_state != t.state:
            t.state = new_state
            mutated = True
        return mutated
