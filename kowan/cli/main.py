"""The kowanctl typer application.

Global persistent flags (--server / --token / --api-key) are resolved once in
the root callback into module-level state that subcommands read via :func:`new_client` and :func:`require_token`.
"""

from __future__ import annotations

import sys

import typer

from .client import APIError, Client
from .config import Resolved, resolve_config

# CLI state shared across commands, populated by the root callback after merging
# flags, config file, and environment variables (in that precedence).
_resolved = Resolved()

app = typer.Typer(
    name="kowanctl",
    help=(
        "kowanctl is the command-line client for the Kowan Docker management "
        "platform. It talks to a Kowan server over its REST API."
    ),
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root(
    server: str = typer.Option(
        "", "--server", help="Kowan server base URL (overrides config and KOWAN_SERVER)"
    ),
    token: str = typer.Option(
        "", "--token", help="bearer token (overrides config and KOWAN_TOKEN)"
    ),
    api_key: str = typer.Option(
        "", "--api-key", help="API key secret (overrides config and KOWAN_API_KEY)"
    ),
) -> None:
    """Merge configuration sources before any command runs."""
    global _resolved
    _resolved = resolve_config(server, token, api_key)


def resolved() -> Resolved:
    """The effective server/token/api-key after merging all sources."""
    return _resolved


def new_client() -> Client:
    """Build a REST client from the resolved server/token/api-key.

    Raises if no server is configured.
    """
    if _resolved.server == "":
        raise typer.BadParameter(
            "no server configured: pass --server, set KOWAN_SERVER, or run "
            "'kowanctl login'"
        )
    return Client(_resolved.server, _resolved.token, _resolved.api_key)


def require_token() -> None:
    """Fail if no credential (bearer token or API key) is available."""
    if _resolved.token == "" and _resolved.api_key == "":
        raise typer.BadParameter(
            "not authenticated: run 'kowanctl login', pass --token / --api-key, "
            "or set KOWAN_TOKEN / KOWAN_API_KEY"
        )


# Register the command groups. Imported
# lazily-ish at module load; each module exposes a typer app or a registrar.
from . import (  # noqa: E402
    apikey,
    backup,
    container,
    deployment,
    hosts,
    image,
    login,
    mcp as mcp_cmd,
    network,
    user,
    version,
    volume,
    workflow,
)

# login / version / backup / restore are single commands on the root; the rest
# are command groups added as typer sub-apps.
login.register(app)
mcp_cmd.register(app)
version.register(app)
app.add_typer(hosts.app, name="hosts")
app.add_typer(container.app, name="container")
app.add_typer(image.app, name="image")
app.add_typer(volume.app, name="volume")
app.add_typer(network.app, name="network")
app.add_typer(deployment.app, name="deployment")
app.add_typer(workflow.app, name="workflow")
app.add_typer(user.app, name="user")
app.add_typer(apikey.app, name="apikey")
backup.register(app)


def main() -> None:
    """Run the root command; print errors to stderr and exit non-zero.

    Errors are surfaced as ``error: ...`` on stderr with exit code 1.
    """
    try:
        app(standalone_mode=False)
    except (APIError, typer.BadParameter, RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except typer.Exit as e:
        raise SystemExit(e.exit_code)
    except click_exceptions() as e:  # click Abort / usage errors
        # Click's usage errors already print themselves in standalone mode; here
        # we surface them consistently.
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def click_exceptions():
    """Return the usage-error classes to catch.

    Typer >= 0.22 vendors click (``typer._click``) instead of depending on it,
    so the classes come from typer: ``TyperException`` is the base of the
    vendored ``ClickException``/``UsageError``/``BadParameter``.
    """
    return (typer.TyperException, typer.Abort)
