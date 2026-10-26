"""version command."""

from __future__ import annotations

import typer


def register(app: typer.Typer) -> None:
    app.command("version", help="Print the kowanctl version")(version)


def version() -> None:
    """Print the kowanctl version."""
    # Read the build version from the package (overridable, like main.version).
    import kowan.cli as pkg

    print(f"kowanctl {pkg.version}")
