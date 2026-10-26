"""Public redeploy webhook handler.

A request whose path token matches a deployment's ``redeploy_token`` triggers an
asynchronous deploy of that deployment (the CI trigger) and responds 202;
unknown tokens return 404.

Authentication is by possession of the secret token in the path, so no JWT is
applied — matching the ``/hooks`` pattern. The route is mounted PUBLIC by
``register`` at ``/hooks/deploy/{token}`` via ``add_public_route``.

For a git-sourced compose deployment the same URL also accepts push webhooks
from GitHub/Gitea (``X-Hub-Signature-256`` HMAC-SHA256 with the deployment's
``webhookSecret``) and GitLab (``X-Gitlab-Token``): the push only redeploys when
it targets the configured ref and, if the payload lists changed files, touches
the compose file's directory (see :func:`gitsource.evaluate_push`). Pushes
to other branches and pull request events feed the deployment's preview
environments when it has them (kowan/deployment/previews.py).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from kowan.deployment import gitsource
from kowan.errors import Conflict
from kowan.web import SERVICES_KEY, error_response, json_response, read_body

if TYPE_CHECKING:  # pragma: no cover
    from .service import DeploymentService

log = logging.getLogger("kowan.deployment")

# Upper bound for a git push payload (GitHub caps deliveries at 25 MB; real
# pushes are far smaller). The whole body is needed to verify the HMAC.
_MAX_PUSH_BODY = 10 << 20


async def handle_redeploy_hook(request: web.Request) -> web.StreamResponse:
    """POST /hooks/deploy/{token} — kick off a background redeploy.

    Delegates to :meth:`DeploymentService.dispatch_deploy`, which queues the deploy
    as a tracked background task (status goes pending -> deploying -> deployed) and
    returns immediately, so the webhook outlives the (already answered) request. A
    redeploy already in flight for this deployment is treated as success (202).
    """
    svc = request.app[SERVICES_KEY]
    dep_svc: "DeploymentService" = svc.deployments

    token = request.match_info.get("token", "")
    if token == "":
        return error_response(404, "redeploy webhook not found")

    try:
        dep = svc.store.get_deployment_by_redeploy_token(token)
    except Exception:
        return error_response(404, "redeploy webhook not found")

    # Git push webhooks (GitHub/Gitea HMAC, GitLab token) on a git-sourced
    # deployment are verified and filtered by ref / changed paths. Any other
    # request is a plain token webhook: the path token alone redeploys.
    git = dep.git if (dep.git is not None and dep.git.repo_url) else None
    if git is not None and gitsource.detect_provider(request.headers):
        body = await read_body(request, _MAX_PUSH_BODY + 1)
        if len(body) > _MAX_PUSH_BODY:
            return error_response(413, "payload too large")
        decision = gitsource.evaluate_push(git, request.headers, body)
        if decision.action == "unauthorized":
            log.warning(
                "deploy hook rejected deployment=%s provider=%s: %s",
                dep.id, decision.provider, decision.reason,
            )
            return error_response(401, "invalid webhook signature")
        if decision.action == "pong":
            return json_response({"status": "pong", "deployment": dep.id})
        if decision.action != "deploy":
            # Not a deploy of this deployment: maybe a preview's branch or
            # pull request (the signature is verified at this point).
            previews = getattr(svc, "previews", None)
            if previews is not None and git.previews is not None and git.previews.enabled:
                out = await previews.handle_event(dep, request.headers, body)
                return json_response({**out, "deployment": dep.id}, status=202 if out["status"] != "ignored" else 200)
            return json_response(
                {"status": "ignored", "reason": decision.reason, "deployment": dep.id}
            )
        log.info(
            "deploy hook push deployment=%s provider=%s ref=%s commit=%s",
            dep.id, decision.provider, decision.ref, decision.commit[:12],
        )

    try:
        await dep_svc.dispatch_deploy(dep.id, redeploy=True)
    except Conflict:
        # Already deploying — idempotent from the caller's perspective.
        return json_response(
            {"status": "already-redeploying", "deployment": dep.id}, status=202
        )
    except Exception as err:  # noqa: BLE001 - failed to even queue it
        log.error(
            "redeploy webhook could not queue deploy deployment=%s name=%s err=%s",
            dep.id, dep.name, err,
        )
        return error_response(500, "could not queue redeploy")

    return json_response({"status": "redeploying", "deployment": dep.id}, status=202)
