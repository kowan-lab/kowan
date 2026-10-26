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


def rename(
    host: str = typer.Argument(..., help="Host id or current name"),
    name: str = typer.Argument(..., help="New name"),
) -> None:
    """Rename a host (the local one too). Its id does not change."""
    _main.require_token()
    with _main.new_client() as c:
        hosts = c.list_hosts()
        match = [h for h in hosts if h.get("id") == host] or [h for h in hosts if h.get("name") == host]
        if not match:
            raise typer.BadParameter(f"no host with id or name {host!r}")
        h = c.rename_host(match[0]["id"], name)
    print(f"Renamed host {h.get('id', '')} to {h.get('name', '')}")


app.command("ls", help="List hosts")(_ls)
app.command("list", hidden=True)(_ls)
app.command("rename", help="Rename a host")(rename)
