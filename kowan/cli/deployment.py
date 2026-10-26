"""deployment command group."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import main as _main
from .output import dash, print_table, short_id, truncate

app = typer.Typer(
    help="Manage unified deployments",
    no_args_is_help=True,
)


def _ls() -> None:
    """List deployments."""
    _main.require_token()
    with _main.new_client() as c:
        deployments = c.list_deployments()

    rows = [
        [
            d.get("id", ""),
            truncate(d.get("name", ""), 30),
            dash(d.get("type", "")),
            dash(d.get("strategy", "")),
            str(len(d.get("targets") or [])),
            dash(d.get("status", "")),
            dash(d.get("updatedAt", "")),
        ]
        for d in deployments
    ]
    print_table(
        ["ID", "NAME", "TYPE", "STRATEGY", "TARGETS", "STATUS", "UPDATED"], rows
    )


app.command("ls", help="List deployments")(_ls)
app.command("list", hidden=True)(_ls)


def _create(
    file: str = typer.Option(
        "", "--file", "-f", help="path to a deployment JSON file (required)"
    ),
) -> None:
    """Create a deployment record from a JSON file."""
    _main.require_token()
    if file.strip() == "":
        raise typer.BadParameter("--file is required")

    try:
        data = Path(file).read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"read deployment file {file}: {e}") from e
    try:
        dep = json.loads(data)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"parse deployment file {file}: {e}") from e

    with _main.new_client() as c:
        out = c.create_deployment(dep)
    print(
        f'Created deployment "{out.get("name", "")}" ({out.get("id", "")}) '
        f'status: {dash(out.get("status", ""))}'
    )


app.command("create", help="Create a deployment record from a JSON file")(_create)


def _make_action(action: str, short: str, method_name: str) -> None:
    def action_cmd(
        deployment_id: str = typer.Argument(..., help="deployment id"),
    ) -> None:
        _main.require_token()
        with _main.new_client() as c:
            out = getattr(c, method_name)(deployment_id)
        print(
            f'Deployment "{out.get("name", "")}" ({out.get("id", "")}) '
            f'status: {dash(out.get("status", ""))}'
        )

    action_cmd.__doc__ = short
    app.command(action, help=short)(action_cmd)


_make_action("deploy", "Deploy a deployment to its targets", "deploy_deployment")
_make_action("redeploy", "Redeploy a deployment", "redeploy_deployment")
_make_action("stop", "Stop a deployment's running resources", "stop_deployment")
_make_action("start", "Start a stopped deployment", "start_deployment")
_make_action(
    "destroy",
    "Remove a deployment's running resources (keep record)",
    "destroy_deployment",
)


def _status(
    deployment_id: str = typer.Argument(..., help="deployment id"),
) -> None:
    """Show a deployment's per-target status."""
    _main.require_token()
    with _main.new_client() as c:
        targets = c.deployment_status(deployment_id)

    rows = [
        [
            t.get("hostId", ""),
            truncate(dash(t.get("hostName", "")), 30),
            dash(t.get("state", "")),
            short_id(t.get("containerId", "")),
            truncate(dash(t.get("message", "")), 40),
        ]
        for t in targets
    ]
    print_table(["HOST ID", "HOST NAME", "STATE", "CONTAINER", "MESSAGE"], rows)


app.command("status", help="Show a deployment's per-target status")(_status)


def _rm(
    deployment_id: str = typer.Argument(..., help="deployment id"),
) -> None:
    """Destroy and delete a deployment."""
    _main.require_token()
    with _main.new_client() as c:
        c.delete_deployment(deployment_id)
    print(f"{deployment_id}: deleted")


app.command("rm", help="Destroy and delete a deployment")(_rm)
app.command("delete", hidden=True)(_rm)


def _history(
    deployment_id: str = typer.Argument(..., help="deployment id"),
) -> None:
    """List the saved revisions of a deployment, newest first."""
    _main.require_token()
    with _main.new_client() as c:
        revisions = c.deployment_revisions(deployment_id)
    rows = [
        [
            f"#{r.get('number', '')}" + (" *" if r.get("live") else ""),
            dash(r.get("createdAt", "")),
            dash(r.get("author", "")),
            dash(r.get("source", "")),
            truncate(", ".join(r.get("changes") or []) or "-", 30),
            truncate(dash(r.get("note", "")), 30),
        ]
        for r in revisions
    ]
    print_table(["REV", "SAVED", "BY", "SOURCE", "CHANGED", "NOTE"], rows)
    if any(r.get("live") for r in revisions):
        print("* running now")


app.command("history", help="List a deployment's revisions (newest first)")(_history)


def _diff(
    deployment_id: str = typer.Argument(..., help="deployment id"),
    revision: int = typer.Argument(..., help="revision number"),
    against: int = typer.Option(0, "--against", help="compare with this revision (default: the previous one)"),
) -> None:
    """Show what a revision changed, as a YAML line diff."""
    _main.require_token()
    with _main.new_client() as c:
        out = c.deployment_revision_diff(deployment_id, revision, against)
    print(f"--- #{out.get('from', 0)}\n+++ #{out.get('to', revision)}")
    for line in out.get("lines") or []:
        print(f"{line.get('op', ' ')}{line.get('text', '')}")


app.command("diff", help="Show what a revision changed")(_diff)


def _rollback(
    deployment_id: str = typer.Argument(..., help="deployment id"),
    revision: int = typer.Argument(..., help="revision to restore"),
    no_deploy: bool = typer.Option(False, "--no-deploy", help="save the restored config without deploying it"),
) -> None:
    """Restore a revision as a new one and redeploy it."""
    _main.require_token()
    with _main.new_client() as c:
        out = c.rollback_deployment(deployment_id, revision, deploy=not no_deploy)
    print(
        f'Deployment "{out.get("name", "")}" rolled back to #{revision} '
        f'status: {dash(out.get("status", ""))}'
    )


app.command("rollback", help="Restore a revision and redeploy it")(_rollback)


def _previews(
    deployment_id: str = typer.Argument(..., help="deployment id"),
) -> None:
    """List the preview environments of a git deployment."""
    _main.require_token()
    with _main.new_client() as c:
        items = c.deployment_previews(deployment_id)
    rows = [
        [
            d.get("id", ""),
            truncate(d.get("name", ""), 30),
            truncate((d.get("preview") or {}).get("ref", ""), 30),
            f"#{(d.get('preview') or {}).get('pullRequest')}" if (d.get("preview") or {}).get("pullRequest") else "-",
            dash(d.get("status", "")),
            dash((d.get("preview") or {}).get("lastActivityAt", "")),
        ]
        for d in items
    ]
    print_table(["ID", "NAME", "BRANCH", "PR", "STATUS", "LAST PUSH"], rows)


app.command("previews", help="List a git deployment's preview environments")(_previews)


def _preview(
    deployment_id: str = typer.Argument(..., help="deployment id"),
    branch: str = typer.Argument(..., help="branch to preview"),
) -> None:
    """Create (or redeploy) the preview of a branch."""
    _main.require_token()
    with _main.new_client() as c:
        out = c.create_preview(deployment_id, branch)
    print(f'Preview "{out.get("name", "")}" of {branch} is deploying (id {out.get("id", "")})')


app.command("preview", help="Create or redeploy the preview of a branch")(_preview)


def _promote(
    deployment_id: str = typer.Argument(..., help="deployment to promote (e.g. staging)"),
    to: str = typer.Option("", "--to", help="target deployment id (default: the one it promotes to)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="only show what would change"),
    no_deploy: bool = typer.Option(False, "--no-deploy", help="save the target without deploying it"),
) -> None:
    """Run in the target what this deployment runs (images or git commit)."""
    _main.require_token()
    with _main.new_client() as c:
        plan = c.promotion_plan(deployment_id, to)
        target = plan.get("target") or {}
        changes = plan.get("changes") or []
        for ch in changes:
            print(f"{ch.get('what', '')}: {ch.get('from', '') or '-'} -> {ch.get('to', '')}")
        for note in plan.get("notes") or []:
            print(f"note: {note}")
        if not changes:
            print(f'"{target.get("name", "")}" already runs the same thing')
            return
        if dry_run:
            return
        out = c.promote(deployment_id, target.get("id", ""), deploy=not no_deploy)
    print(f'Promoted into "{out.get("name", "")}" status: {dash(out.get("status", ""))}')


app.command("promote", help="Promote a deployment's images or commit into another (staging -> production)")(_promote)
