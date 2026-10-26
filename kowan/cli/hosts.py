"""hosts command group."""

from __future__ import annotations

import typer

from . import main as _main
from .output import bool_str, dash, print_table

app = typer.Typer(help="Manage and inspect hosts", no_args_is_help=True)


def _ls() -> None:
    """List hosts."""
    _main.require_token()
    with _main.new_client() as c:
        hosts = c.list_hosts()

    rows = [
        [
            h.get("id", ""),
            dash(h.get("name", "")),
            h.get("type", ""),
            dash(h.get("address", "")),
            bool_str(bool(h.get("online", False))),
            dash(h.get("lastSeen", "")),
        ]
        for h in hosts
    ]
    print_table(["ID", "NAME", "TYPE", "ADDRESS", "ONLINE", "LAST SEEN"], rows)


app.command("ls", help="List hosts")(_ls)
app.command("list", hidden=True)(_ls)
