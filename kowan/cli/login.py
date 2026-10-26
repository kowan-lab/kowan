"""login command.

Authenticate to a Kowan server with a username and password. On success the
issued token and server URL are saved to ~/.kowan/config.yaml for subsequent
commands. The existing apiKey is preserved (loadConfig first, then set only
server + token) so a `login` does not wipe a configured API key.
"""

from __future__ import annotations

import getpass
import sys

import typer

from . import main as _main
from .client import Client
from .config import config_path, load_config, save_config


def register(app: typer.Typer) -> None:
    app.command(
        "login",
        help=(
            "Authenticate to a Kowan server with a username and password. On "
            "success the issued token and server URL are saved to "
            "~/.kowan/config.yaml for subsequent commands."
        ),
    )(login)


def login(
    username: str = typer.Option(
        "", "--username", "-u", help="username (prompted if omitted)"
    ),
    password: str = typer.Option(
        "", "--password", "-p", help="password (prompted securely if omitted)"
    ),
) -> None:
    """Authenticate to a Kowan server and save the token."""
    res = _main.resolved()
    if res.server == "":
        raise typer.BadParameter(
            "no server configured: pass --server or set KOWAN_SERVER"
        )

    username = username.strip()
    if username == "":
        username = _prompt_line("Username: ").strip()
    if username == "":
        raise typer.BadParameter("username is required")

    if password == "":
        password = _prompt_password("Password: ")
    if password == "":
        raise typer.BadParameter("password is required")

    c = Client(res.server, "")
    token = c.login(username, password)

    # loadConfig first so a configured apiKey is preserved across login.
    cfg = load_config()
    cfg.server = res.server
    cfg.token = token
    save_config(cfg)

    path = config_path()
    print(f"Logged in to {res.server} as {username}; token saved to {path}")


def _prompt_line(prompt: str) -> str:
    """Print a prompt and read a single line from stdin."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    return line.rstrip("\r\n")


def _prompt_password(prompt: str) -> str:
    """Read a password without echoing; fall back to a plain read off a pipe."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    # Non-interactive: read a plain line (getpass falls back to this too, but be
    # explicit to avoid a warning).
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return sys.stdin.readline().rstrip("\r\n")
