"""apikey command group."""

from __future__ import annotations

import typer

from . import main as _main
from .output import dash, print_table, truncate

# Role constants (models.Role*).
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"

app = typer.Typer(
    help=(
        "Manage admin-issued API keys. An API key is a role-scoped credential "
        "that authenticates API requests without a username/password login "
        "(for kowanctl, webhook scripts, and CI). The full secret is shown only "
        "once, at creation."
    ),
    no_args_is_help=True,
)


def _ls() -> None:
    """List API keys (metadata only; secrets are never returned)."""
    _main.require_token()
    with _main.new_client() as c:
        keys = c.list_api_keys()

    rows = [
        [
            k.get("id", ""),
            truncate(k.get("name", ""), 30),
            k.get("prefix", ""),
            k.get("role", ""),
            dash(k.get("createdAt", "")),
            dash(k.get("lastUsed", "")),
        ]
        for k in keys
    ]
    print_table(["ID", "NAME", "PREFIX", "ROLE", "CREATED", "LAST USED"], rows)


app.command("ls", help="List API keys")(_ls)
app.command("list", hidden=True)(_ls)


def _create(
    name: str = typer.Argument(..., help="display name for the key"),
    role: str = typer.Option(
        ROLE_VIEWER, "--role", help="role granted to the key (admin|operator|viewer)"
    ),
) -> None:
    """Create an API key and print its one-time secret."""
    _main.require_token()
    name = name.strip()
    if name == "":
        raise typer.BadParameter("name is required")
    role = role.strip()
    if role != "" and not _valid_role(role):
        raise typer.BadParameter(
            f'invalid role "{role}": must be one of admin, operator, viewer'
        )

    with _main.new_client() as c:
        key = c.create_api_key(name, role)

    ak = key.get("apiKey") or {}
    print(
        f'Created API key "{ak.get("name", "")}" '
        f'(id {ak.get("id", "")}, role {ak.get("role", "")})'
    )
    print(f"Secret: {key.get('secret', '')}")
    print("Store this secret now; it will not be shown again.")


app.command("create", help="Create an API key")(_create)


def _rm(
    id_: str = typer.Argument(..., metavar="ID", help="API key id"),
) -> None:
    """Revoke an API key by id."""
    _main.require_token()
    id_ = id_.strip()
    if id_ == "":
        raise typer.BadParameter("id is required")

    with _main.new_client() as c:
        c.delete_api_key(id_)
    print(f"Revoked API key {id_}")


app.command("rm", help="Revoke an API key")(_rm)
app.command("delete", hidden=True)(_rm)
app.command("revoke", hidden=True)(_rm)


def _valid_role(role: str) -> bool:
    """Report whether role is one of the recognized role constants."""
    return role in (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)
