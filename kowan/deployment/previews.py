"""Preview environments: a copy of a git compose deployment running another
branch or a pull request, side by side with it on the same host.

A preview is an ordinary deployment with ``preview`` set (parent, branch,
pull request). It is created and updated by the parent's push webhook (a push
to a branch matching ``git.previews.branches``, or a pull/merge request being
opened or updated) or by hand, and removed when its branch is deleted, its
pull request closed, or nobody pushed to it for ``ttlHours``.

Two previews (and the parent) must not fight over the host: before deploying,
a preview's compose file loses its fixed host ports (Docker picks free ones)
and its ``container_name`` keys; the project name already differs. The file
can tell it runs as a preview from ``KOWAN_PREVIEW_*`` variables.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

import yaml

from kowan.errors import Conflict, Validation
from kowan.models import Deployment, GitSource, PreviewInfo, PreviewSettings
from kowan.models.deployment import sanitize_resource_name

from . import gitsource

log = logging.getLogger("kowan.deployment")

MAX_TTL_HOURS = 24 * 30
MAX_PREVIEWS = 20
_EXPIRE_EVERY = 600.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def branch_of(ref: str) -> str:
    """``refs/heads/x`` -> ``x``; other refs (tags) -> ""."""
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    return "" if ref.startswith("refs/") else ref


def branch_matches(patterns: str, branch: str) -> bool:
    pats = [p.strip() for p in patterns.split(",") if p.strip()]
    return any(fnmatch.fnmatchcase(branch, p) for p in pats)


def validate_settings(p: PreviewSettings) -> str:
    if not 0 <= p.ttl_hours <= MAX_TTL_HOURS:
        return f"previews.ttlHours must be between 0 and {MAX_TTL_HOURS}"
    if not 1 <= p.max <= MAX_PREVIEWS:
        return f"previews.max must be between 1 and {MAX_PREVIEWS}"
    if p.enabled and not p.branches.strip() and not p.pull_requests:
        return "previews need branch patterns, pull requests, or both"
    return ""


def preview_name(parent: Deployment, branch: str, pr: int) -> str:
    suffix = f"pr-{pr}" if pr else (sanitize_resource_name(branch)[:30].strip("-_") or "branch")
    return f"{parent.name}-{suffix}"


# ---------------------------------------------------------------------------
# compose isolation
# ---------------------------------------------------------------------------

_PORT = re.compile(r"^(?:(?:\[[^\]]*\]|[^:]*):)?(?:[^:]*:)?([^:]+)$")


def _container_port(entry: str) -> str:
    """``"127.0.0.1:8080:80/tcp"`` -> ``"80/tcp"``, ``"8080:80"`` -> ``"80"``."""
    m = _PORT.match(entry.strip())
    return m.group(1) if m else entry


def isolate_compose(text: str) -> str:
    """The compose file without fixed host ports and container names, so it
    can run next to the deployment it copies."""
    doc = yaml.safe_load(text) or {}
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict):
        return text
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        svc.pop("container_name", None)
        ports = svc.get("ports")
        if isinstance(ports, list):
            out: List[Any] = []
            for p in ports:
                if isinstance(p, dict):
                    p = {k: v for k, v in p.items() if k not in ("published", "host_ip")}
                    out.append(p)
                else:
                    out.append(_container_port(str(p)))
            svc["ports"] = out
    return yaml.safe_dump(doc, sort_keys=False)


def preview_env(dep: Deployment) -> str:
    """``.env`` lines that tell the compose file it runs as a preview."""
    if dep.preview is None:
        return ""
    return (
        f"KOWAN_PREVIEW=1\nKOWAN_PREVIEW_NAME={dep.resource_name()}\n"
        f"KOWAN_PREVIEW_BRANCH={dep.preview.ref}\nKOWAN_PREVIEW_PR={dep.preview.pull_request or ''}\n"
    )


# ---------------------------------------------------------------------------
# webhook events
# ---------------------------------------------------------------------------


@dataclass
class PreviewEvent:
    action: str  # "upsert" | "delete" | "ignore"
    branch: str = ""
    pr: int = 0
    title: str = ""
    url: str = ""
    reason: str = ""


def evaluate_event(settings: Optional[PreviewSettings], main_ref: str, headers: Mapping[str, str], body: bytes) -> PreviewEvent:
    """What a verified push / pull request webhook means for previews."""
    if settings is None or not settings.enabled:
        return PreviewEvent("ignore", reason="previews are off")
    event = (
        gitsource._h(headers, "X-GitHub-Event") or gitsource._h(headers, "X-Gitea-Event")
        or gitsource._h(headers, "X-Gogs-Event") or gitsource._h(headers, "X-Gitlab-Event")
    ).strip().lower()
    try:
        payload = gitsource._decode_payload(body)
    except ValueError:
        return PreviewEvent("ignore", reason="payload is not JSON")
    if not isinstance(payload, dict):
        return PreviewEvent("ignore", reason="payload is not an object")

    if event in ("pull_request", "merge request hook"):
        if not settings.pull_requests:
            return PreviewEvent("ignore", reason="pull request previews are off")
        if event == "pull_request":
            pr = payload.get("pull_request") or {}
            action = str(payload.get("action") or "")
            number = int(payload.get("number") or pr.get("number") or 0)
            branch = str((pr.get("head") or {}).get("ref") or "")
            title, url = str(pr.get("title") or ""), str(pr.get("html_url") or "")
            closed = action == "closed"
            live = action in ("opened", "reopened", "synchronize", "synchronized", "edited", "ready_for_review")
        else:
            attrs = payload.get("object_attributes") or {}
            action = str(attrs.get("action") or "")
            number = int(attrs.get("iid") or 0)
            branch = str(attrs.get("source_branch") or "")
            title, url = str(attrs.get("title") or ""), str(attrs.get("url") or "")
            closed = action in ("close", "merge")
            live = action in ("open", "reopen", "update")
        if not number or not branch:
            return PreviewEvent("ignore", reason="pull request without number or branch")
        if closed:
            return PreviewEvent("delete", branch, number, title, url)
        if live:
            return PreviewEvent("upsert", branch, number, title, url)
        return PreviewEvent("ignore", reason=f"pull request action {action!r}")

    if event not in ("", "push", "push hook"):
        return PreviewEvent("ignore", reason=f"event {event!r}")
    branch = branch_of(str(payload.get("ref") or ""))
    if not branch:
        return PreviewEvent("ignore", reason="not a branch")
    if gitsource.ref_matches(main_ref, str(payload.get("ref") or "")):
        return PreviewEvent("ignore", reason="the deployment's own branch")
    after = str(payload.get("after") or "")
    if payload.get("deleted") or (after and set(after) == {"0"}):
        return PreviewEvent("delete", branch)
    # A push updates any preview of the branch (pull request ones too); it only
    # creates one when the branch matches the patterns.
    return PreviewEvent("upsert", branch, reason="" if branch_matches(settings.branches, branch) else "update-only")


# ---------------------------------------------------------------------------
# the manager
# ---------------------------------------------------------------------------


class PreviewManager:
    def __init__(self, svc: Any) -> None:
        self._svc = svc
        self._task: Optional[asyncio.Task] = None

    @property
    def _store(self):
        return self._svc.store

    def list(self, parent_id: str) -> List[Deployment]:
        return [d for d in self._store.list_deployments() if d.preview is not None and d.preview.parent_id == parent_id]

    async def upsert(self, parent: Deployment, branch: str, *, pr: int = 0, title: str = "", url: str = "",
                     author: str = "", create: bool = True) -> Optional[Deployment]:
        """Redeploy the previews of ``branch`` (only pull request ``pr``'s when
        given), or create one and deploy it. None when there is none and
        ``create`` is off."""
        settings = parent.git.previews if parent.git else None
        if settings is None or not settings.enabled:
            raise Validation("previews are not enabled on this deployment")
        previews = self.list(parent.id)
        mine = [d for d in previews if (d.preview.pull_request == pr if pr else d.preview.ref == branch)]
        for d in mine:
            d.preview.last_activity_at = _now()
            d.preview.title = title or d.preview.title
            d.preview.url = url or d.preview.url
            d.updated_at = _now()
            self._store.update_deployment(d, author=author or "previews", source="preview")
            await self._dispatch(d.id)
        if mine:
            return mine[0]
        if not create:
            return None
        if len(previews) >= settings.max:
            raise Conflict(f"this deployment already has {settings.max} previews (its limit)")
        name = preview_name(parent, branch, pr)
        want = Deployment(name=name).resource_name()
        if any(d.name == name or d.resource_name() == want for d in self._store.list_deployments()):
            raise Conflict(f"a deployment named {name!r} already exists")
        now = _now()
        git = GitSource(repoUrl=parent.git.repo_url, ref=branch, path=parent.git.path,
                        credentialId=parent.git.credential_id, autoDeploy=False)
        dep = Deployment(
            id=str(uuid.uuid4()), name=name, type=parent.type, strategy=parent.strategy,
            targets=list(parent.targets), status="stopped", git=git, createdAt=now, updatedAt=now,
            # The parent's stack variables, so the preview runs like it does; its
            # secret rows become plain references to the parent's secret.
            variables=[v.model_copy(update={"secret": False}) for v in parent.variables],
            preview=PreviewInfo(parentId=parent.id, ref=branch, pullRequest=pr, title=title, url=url,
                                createdBy=author or ("pull request" if pr else "push"), lastActivityAt=now),
        )
        self._store.create_deployment(dep, author=author or "previews", source="preview")
        log.info("preview created deployment=%s parent=%s branch=%s pr=%s", dep.id, parent.id, branch, pr)
        await self._dispatch(dep.id)
        return dep

    async def remove(self, dep: Deployment) -> None:
        """Tear a preview down and delete it."""
        try:
            await self._svc.deployments.destroy(dep.id)
        except Exception as err:  # noqa: BLE001 - delete the record anyway
            log.warning("preview teardown failed deployment=%s: %s", dep.id, err)
        self._store.delete_deployment(dep.id)
        self._svc.deployments.remove_git_checkout(dep.id)
        log.info("preview removed deployment=%s branch=%s", dep.id, dep.preview.ref if dep.preview else "")

    async def remove_children(self, parent_id: str) -> int:
        previews = self.list(parent_id)
        for d in previews:
            await self.remove(d)
        return len(previews)

    async def handle_event(self, parent: Deployment, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        """Act on a verified webhook; the answer goes back to the git host."""
        ev = evaluate_event(parent.git.previews if parent.git else None, parent.git.ref if parent.git else "", headers, body)
        if ev.action == "ignore":
            return {"status": "ignored", "reason": ev.reason}
        if ev.action == "delete":
            gone = [d for d in self.list(parent.id)
                    if (ev.pr and d.preview.pull_request == ev.pr) or (not ev.pr and d.preview.ref == ev.branch)]
            for d in gone:
                await self.remove(d)
            return {"status": "preview-removed", "count": len(gone)}
        try:
            dep = await self.upsert(parent, ev.branch, pr=ev.pr, title=ev.title, url=ev.url,
                                    create=ev.reason != "update-only")
        except Conflict as err:
            return {"status": "ignored", "reason": str(err)}
        if dep is None:
            return {"status": "ignored", "reason": f"branch {ev.branch!r} does not match the preview patterns"}
        return {"status": "preview-deploying", "preview": dep.id}

    async def expire(self, now: Optional[datetime] = None) -> int:
        """Remove previews idle for longer than their parent's ttl, and
        previews whose parent is gone or no longer has previews on."""
        now = now or datetime.now(timezone.utc)
        deps = {d.id: d for d in self._store.list_deployments()}
        removed = 0
        for d in list(deps.values()):
            if d.preview is None:
                continue
            parent = deps.get(d.preview.parent_id)
            settings = parent.git.previews if parent is not None and parent.git is not None else None
            if settings is None or not settings.enabled:
                await self.remove(d)
                removed += 1
                continue
            if settings.ttl_hours <= 0 or not d.preview.last_activity_at:
                continue
            last = datetime.strptime(d.preview.last_activity_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if now - last > timedelta(hours=settings.ttl_hours):
                await self.remove(d)
                removed += 1
        return removed

    async def _dispatch(self, dep_id: str) -> None:
        try:
            await self._svc.deployments.dispatch_deploy(dep_id, redeploy=True)
        except Conflict:
            pass  # already deploying: it picks up the new commit next time

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                n = await self.expire()
                if n:
                    log.info("previews: removed %d idle previews", n)
            except Exception as err:  # noqa: BLE001
                log.error("previews: expire: %s", err)
            await asyncio.sleep(_EXPIRE_EVERY)

