"""user command group."""

from __future__ import annotations

import typer

from . import main as _main
from .output import dash, print_table, truncate

app = typer.Typer(help="Manage users", no_args_is_help=True)


def _ls() -> None:
    """List users (admin only on the server side)."""
    _main.require_token()
    with _main.new_client() as c:
        users = c.list_users()

    rows = [
        [
            u.get("id", ""),
            truncate(u.get("username", ""), 30),
            u.get("role", ""),
            dash(u.get("createdAt", "")),
        ]
        for u in users
    ]
    print_table(["ID", "USERNAME", "ROLE", "CREATED"], rows)


app.command("ls", help="List users")(_ls)
app.command("list", hidden=True)(_ls)
