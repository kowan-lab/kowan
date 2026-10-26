"""volume command group."""

from __future__ import annotations

import typer

from . import main as _main
from .container import DEFAULT_HOST_ID
from .output import dash, print_table, truncate

app = typer.Typer(help="Manage volumes on a host", no_args_is_help=True)


def _ls(
    host: str = typer.Option(DEFAULT_HOST_ID, "--host", help="host id to target"),
) -> None:
    """List volumes."""
    _main.require_token()
    with _main.new_client() as c:
        volumes = c.list_volumes(host)

    rows = [
        [
            truncate(v.get("name", ""), 40),
            dash(v.get("driver", "")),
            truncate(v.get("mountpoint", ""), 50),
            dash(v.get("createdAt", "")),
        ]
        for v in volumes
    ]
    print_table(["NAME", "DRIVER", "MOUNTPOINT", "CREATED"], rows)


app.command("ls", help="List volumes")(_ls)
app.command("list", hidden=True)(_ls)
