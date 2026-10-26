"""network command group."""

from __future__ import annotations

import typer

from . import main as _main
from .container import DEFAULT_HOST_ID
from .output import dash, print_table, short_id, truncate

app = typer.Typer(help="Manage networks on a host", no_args_is_help=True)


def _ls(
    host: str = typer.Option(DEFAULT_HOST_ID, "--host", help="host id to target"),
) -> None:
    """List networks."""
    _main.require_token()
    with _main.new_client() as c:
        networks = c.list_networks(host)

    rows = [
        [
            short_id(n.get("id", "")),
            truncate(n.get("name", ""), 30),
            dash(n.get("driver", "")),
            dash(n.get("scope", "")),
        ]
        for n in networks
    ]
    print_table(["ID", "NAME", "DRIVER", "SCOPE"], rows)


app.command("ls", help="List networks")(_ls)
app.command("list", hidden=True)(_ls)
