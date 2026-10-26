"""workflow command group."""

from __future__ import annotations

import typer

from . import main as _main
from .output import bool_str, dash, print_table, truncate

app = typer.Typer(help="Manage automation workflows", no_args_is_help=True)


def _ls() -> None:
    """List workflows."""
    _main.require_token()
    with _main.new_client() as c:
        workflows = c.list_workflows()

    rows = [
        [
            w.get("id", ""),
            truncate(w.get("name", ""), 30),
            str(len(w.get("steps") or [])),
            str(len(w.get("triggers") or [])),
            bool_str(bool(w.get("enabled", False))),
            dash(w.get("updatedAt", "")),
        ]
        for w in workflows
    ]
    print_table(["ID", "NAME", "STEPS", "TRIGGERS", "ENABLED", "UPDATED"], rows)


app.command("ls", help="List workflows")(_ls)
app.command("list", hidden=True)(_ls)


def _run(
    workflow_id: str = typer.Argument(..., help="workflow id"),
) -> None:
    """Trigger a manual run of a workflow."""
    _main.require_token()
    with _main.new_client() as c:
        run_id = c.run_workflow(workflow_id)
    print(f"Started run {run_id}")


app.command("run", help="Trigger a manual run of a workflow")(_run)


def _runs(
    workflow_id: str = typer.Argument(..., help="workflow id"),
) -> None:
    """Show a workflow's run history."""
    _main.require_token()
    with _main.new_client() as c:
        runs = c.list_workflow_runs(workflow_id)

    rows = [
        [
            run.get("id", ""),
            dash(run.get("trigger", "")),
            dash(run.get("status", "")),
            str(len(run.get("steps") or [])),
            dash(run.get("startedAt", "")),
            dash(run.get("finishedAt", "")),
        ]
        for run in runs
    ]
    print_table(
        ["RUN ID", "TRIGGER", "STATUS", "STEPS", "STARTED", "FINISHED"], rows
    )


app.command("runs", help="Show a workflow's run history")(_runs)
