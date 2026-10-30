"""Volume backup (restic) HTTP API (see docs/volume-backups.md).

Routes (under /api/v1). Reads are open to any authenticated principal except
where noted; everything that runs restic or changes state needs ``operator``;
repositories hold credentials, so creating/editing/deleting them is ``admin``.

    GET    /volbackups/repos                         list (secrets masked)
    POST   /volbackups/repos                         create (admin)
    PUT    /volbackups/repos/{id}                    update (admin; blank secret = keep)
    DELETE /volbackups/repos/{id}                    delete (admin; refused while jobs use it)
    POST   /volbackups/repos/{id}/test               open/init the repository (operator)
    GET    /volbackups/repos/{id}/snapshots          ?hostId&volume&fresh (operator)
    GET    /volbackups/repos/{id}/snapshots/{snap}/ls ?path&hostId (operator)
    GET    /volbackups/jobs                          list (+ nextRun, lastRun, running)
    POST   /volbackups/jobs                          create (operator)
    POST   /volbackups/jobs/preview                  resolve a selector to volume names (operator)
    GET    /volbackups/jobs/{id}                     one job
    PUT    /volbackups/jobs/{id}                     update (operator)
    DELETE /volbackups/jobs/{id}                     delete (operator)
    POST   /volbackups/jobs/{id}/run                 back up now -> 202 + run (operator)
    GET    /volbackups/runs                          ?jobId&volume&hostId&limit (no logs)
    GET    /volbackups/runs/{id}                     one run with its log
    GET    /volbackups/volumes/{hostID}/{name}/snapshots   snapshots of one volume (operator)
    POST   /volbackups/volumes/{hostID}/{name}/backup      ad-hoc backup {repoId, tags} (operator)
    POST   /volbackups/restore                       {hostId, repoId, snapshotId, target, newVolume,
                                                      stopContainers, exact} -> 202 + run (operator)
"""

from __future__ import annotations

import logging

from aiohttp import web

from kowan import errors
from kowan.auth import authorize as authz
from kowan.store import NotFoundError, StoreError
from kowan.volbackup.restic import RESTIC_IMAGE
from kowan.volbackup.service import LABEL_BACKUP, VolumeBackupService, validate_job, validate_repo
from kowan.web import api_route, bind_svc, json_response, read_json, require_role

log = logging.getLogger("kowan.api.volbackups")


def _s(svc) -> VolumeBackupService:
    return svc.volbackups


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def job_targets(job) -> list:
    """What a job backs up, for team/grant scoping (kowan.auth.authorize)."""
    host = job.get("hostId", "")
    sel = job.get("selector") or {}
    mode = sel.get("mode", "names")
    if mode == "names":
        targets = [(authz.KIND_VOLUME, host, v) for v in sel.get("volumes") or []]
    elif mode == "deployment":
        targets = [(authz.KIND_DEPLOYMENT, host, sel.get("deploymentId", ""))]
    elif mode == "stack":
        targets = [(authz.KIND_STACK, host, sel.get("stack", ""))]
    elif mode == "label":
        targets = [(authz.KIND_SELECTOR, host, LABEL_BACKUP + "=true")]
    else:
        targets = []
    return targets or [("", host, "")]  # nothing to match: host-wide access only


def run_targets(run) -> list:
    """The volumes a run touched; a run that resolved none needs host-wide access."""
    host = run.get("hostId", "")
    return [(authz.KIND_VOLUME, host, v) for v in run.get("volumes") or []] or [("", host, "")]


def _access(svc) -> authz.TargetAccess:
    return authz.TargetAccess(svc.store, svc.docker)


async def _visible_job(svc, jid: str) -> dict:
    try:
        job = svc.store.get_backup_job(jid)
    except NotFoundError:
        raise errors.NotFound("backup job not found")
    if not await _access(svc).allowed_all(job_targets(job)):
        raise errors.NotFound("backup job not found")
    return job


# -- repositories ------------------------------------------------------------


async def _list_repos(svc, request):
    return json_response(svc.store.list_backup_repos())


async def _create_repo(svc, request):
    body = await read_json(request)
    repo, secrets = validate_repo(body)
    if not secrets.get("password"):
        raise errors.Validation("secrets.password (the restic repository password) is required")
    _s(svc).check_repo_refs(repo, secrets)
    repo.pop("id", None)
    try:
        saved = svc.store.save_backup_repo(repo, secrets)
    except StoreError as err:
        raise errors.Conflict(str(err))
    return json_response(saved, status=201)


async def _update_repo(svc, request):
    rid = request.match_info["id"]
    try:
        existing = svc.store.get_backup_repo(rid)
    except NotFoundError:
        raise errors.NotFound("repository not found")
    body = await read_json(request)
    repo, secrets = validate_repo(body, existing)
    repo["id"] = rid
    _s(svc).check_repo_refs(repo, secrets, existing_id=rid)
    try:
        saved = svc.store.save_backup_repo(repo, secrets)
    except StoreError as err:
        raise errors.Conflict(str(err))
    return json_response(saved)


async def _delete_repo(svc, request):
    rid = request.match_info["id"]
    users = svc.store.jobs_using_repo(rid)
    if users:
        raise errors.Conflict("repository is used by backup job(s): " + ", ".join(users))
    try:
        svc.store.delete_backup_repo(rid)
    except NotFoundError:
        raise errors.NotFound("repository not found")
    return json_response({"status": "deleted"})


async def _test_repo(svc, request):
    body = {}
    if request.can_read_body:
        try:
            body = await read_json(request)
        except Exception:  # noqa: BLE001
            body = {}
    host = str((body or {}).get("hostId", "") or request.query.get("hostId", ""))
    return json_response(await _s(svc).test_repo(request.match_info["id"], host))


async def _repo_snapshots(svc, request):
    q = request.query
    snaps = await _s(svc).list_snapshots(
        request.match_info["id"], q.get("hostId", ""), q.get("volume", ""), fresh=_truthy(q.get("fresh", ""))
    )
    return json_response(snaps)


async def _ls(svc, request):
    q = request.query
    out = await _s(svc).browse(request.match_info["id"], request.match_info["snap"], q.get("path", "/"), q.get("hostId", ""))
    return json_response(out)


# -- jobs ----------------------------------------------------------------------


async def _list_jobs(svc, request):
    jobs = svc.store.list_backup_jobs()
    q = request.query
    if q.get("hostId"):
        jobs = [j for j in jobs if j["hostId"] == q["hostId"]]
    if q.get("deploymentId"):
        jobs = [j for j in jobs if (j.get("selector") or {}).get("deploymentId") == q["deploymentId"]]
    if q.get("volume"):
        v = q["volume"]
        jobs = [j for j in jobs if v in ((j.get("selector") or {}).get("volumes") or []) or (j.get("selector") or {}).get("mode") != "names"]
    jobs = await _access(svc).filter(jobs, job_targets)
    return json_response([_s(svc).job_view(j) for j in jobs])


async def _get_job(svc, request):
    return json_response(_s(svc).job_view(await _visible_job(svc, request.match_info["id"])))


async def _create_job(svc, request):
    body = await read_json(request)
    job = validate_job(body)
    job.pop("id", None)
    # Scheduled runs have no principal: a job may only cover what the caller
    # can operate (reading a volume's files is operate-level).
    await _access(svc).check(job_targets(job), authz.LEVEL_OPERATE)
    _s(svc).check_job_refs(job)
    saved = svc.store.save_backup_job(job)
    _s(svc).reload()
    return json_response(_s(svc).job_view(saved), status=201)


async def _update_job(svc, request):
    jid = request.match_info["id"]
    existing = await _visible_job(svc, jid)
    body = await read_json(request)
    job = validate_job(body, existing)
    job["id"] = jid
    await _access(svc).check(job_targets(existing) + job_targets(job), authz.LEVEL_OPERATE)
    _s(svc).check_job_refs(job)
    if (job["schedule"], job["timezone"]) != (existing["schedule"], existing["timezone"]) or (job["enabled"] and not existing["enabled"]):
        job["lastRunAt"] = ""
    saved = svc.store.save_backup_job(job)
    _s(svc).reload()
    return json_response(_s(svc).job_view(saved))


async def _delete_job(svc, request):
    job = await _visible_job(svc, request.match_info["id"])
    await _access(svc).check(job_targets(job), authz.LEVEL_OPERATE)
    try:
        svc.store.delete_backup_job(job["id"])
    except NotFoundError:
        raise errors.NotFound("backup job not found")
    _s(svc).reload()
    return json_response({"status": "deleted"})


async def _preview_job(svc, request):
    body = await read_json(request)
    if not isinstance(body, dict):
        raise errors.Validation("invalid request body")
    probe = {"name": "preview", "hostId": body.get("hostId", ""), "repoId": "preview", "selector": body.get("selector") or {}}
    job = validate_job(probe)
    vols = await _s(svc).preview_volumes(job)
    return json_response({"volumes": vols})


async def _run_job(svc, request):
    job = await _visible_job(svc, request.match_info["id"])
    await _access(svc).check(job_targets(job), authz.LEVEL_OPERATE)
    run = _s(svc).start_backup(job, trigger="manual")
    return json_response(run, status=202)


# -- runs ------------------------------------------------------------------------


async def _list_runs(svc, request):
    q = request.query
    try:
        limit = int(q.get("limit", "100"))
    except ValueError:
        raise errors.Validation("limit must be an integer")
    runs = svc.store.list_backup_runs(job_id=q.get("jobId", ""), volume=q.get("volume", ""), host_id=q.get("hostId", ""), limit=limit)
    return json_response(await _access(svc).filter(runs, run_targets))


async def _get_run(svc, request):
    try:
        run = svc.store.get_backup_run(request.match_info["id"])
    except NotFoundError:
        raise errors.NotFound("backup run not found")
    if not await _access(svc).allowed_all(run_targets(run)):
        raise errors.NotFound("backup run not found")
    return json_response(run)


# -- volume-centric -------------------------------------------------------------


async def _volume_snapshots(svc, request):
    host, name = request.match_info["hostID"], request.match_info["name"]
    await _access(svc).check([(authz.KIND_VOLUME, host, name)], authz.LEVEL_OPERATE)
    return json_response(await _s(svc).volume_snapshots(host, name))


async def _volume_backup(svc, request):
    body = await read_json(request)
    if not isinstance(body, dict) or not body.get("repoId"):
        raise errors.Validation("repoId is required")
    await _access(svc).check([(authz.KIND_VOLUME, request.match_info["hostID"], request.match_info["name"])],
                             authz.LEVEL_OPERATE)
    run = _s(svc).start_volume_backup(
        request.match_info["hostID"], request.match_info["name"], str(body["repoId"]),
        [str(t) for t in (body.get("tags") or [])],
    )
    return json_response(run, status=202)


async def _restore(svc, request):
    body = await read_json(request)
    if not isinstance(body, dict):
        raise errors.Validation("invalid request body")
    allowed = {"hostId", "repoId", "snapshotId", "target", "newVolume", "stopContainers", "exact"}
    bad = set(body) - allowed
    if bad:
        raise errors.Validation("unknown field(s): " + ", ".join(sorted(bad)))
    for k in ("hostId", "repoId", "snapshotId", "target"):
        if not str(body.get(k) or "").strip():
            raise errors.Validation(f"{k} is required")
    # Restoring writes the volume (a new one needs host-wide manage: it has no labels yet).
    await _access(svc).check([(authz.KIND_VOLUME, str(body["hostId"]), str(body["target"]).strip())],
                             authz.LEVEL_MANAGE)
    run = _s(svc).start_restore(
        host_id=str(body["hostId"]), repo_id=str(body["repoId"]), snapshot_id=str(body["snapshotId"]),
        target=str(body["target"]).strip(), new_volume=bool(body.get("newVolume", False)),
        stop_containers=bool(body.get("stopContainers", True)), exact=bool(body.get("exact", True)),
    )
    return json_response(run, status=202)


async def _info(svc, request):
    return json_response({"image": RESTIC_IMAGE})


def register(app: web.Application, svc) -> None:
    service = VolumeBackupService(svc)
    svc.attach("volbackups", service)
    h = bind_svc(svc)
    op = require_role("operator")
    admin = require_role("admin")
    b = "/volbackups"

    api_route(app, "GET", b + "/info", h(_info))
    api_route(app, "GET", b + "/repos", h(_list_repos))
    api_route(app, "POST", b + "/repos", admin(h(_create_repo)))
    api_route(app, "PUT", b + "/repos/{id}", admin(h(_update_repo)))
    api_route(app, "DELETE", b + "/repos/{id}", admin(h(_delete_repo)))
    api_route(app, "POST", b + "/repos/{id}/test", op(h(_test_repo)))
    api_route(app, "GET", b + "/repos/{id}/snapshots", op(h(_repo_snapshots)))
    api_route(app, "GET", b + "/repos/{id}/snapshots/{snap}/ls", op(h(_ls)))

    api_route(app, "GET", b + "/jobs", h(_list_jobs))
    api_route(app, "POST", b + "/jobs", op(h(_create_job)))
    api_route(app, "POST", b + "/jobs/preview", op(h(_preview_job)))
    api_route(app, "GET", b + "/jobs/{id}", h(_get_job))
    api_route(app, "PUT", b + "/jobs/{id}", op(h(_update_job)))
    api_route(app, "DELETE", b + "/jobs/{id}", op(h(_delete_job)))
    api_route(app, "POST", b + "/jobs/{id}/run", op(h(_run_job)))

    api_route(app, "GET", b + "/runs", h(_list_runs))
    api_route(app, "GET", b + "/runs/{id}", h(_get_run))

    api_route(app, "GET", b + "/volumes/{hostID}/{name}/snapshots", op(h(_volume_snapshots)))
    api_route(app, "POST", b + "/volumes/{hostID}/{name}/backup", op(h(_volume_backup)))
    api_route(app, "POST", b + "/restore", op(h(_restore)))

    async def _on_startup(_app: web.Application) -> None:
        try:
            await service.start()
        except Exception as err:  # noqa: BLE001 - never block startup
            log.error("volbackups: not started: %s", err)

    async def _on_cleanup(_app: web.Application) -> None:
        await service.stop()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
