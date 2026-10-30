"""backup / restore commands.

These are two single commands mounted on the root (not a group): ``backup``
downloads a tar.gz snapshot of the control-plane state, and ``restore`` uploads
a backup archive which the server stages and applies on its next start.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import typer

from . import main as _main


def register(app: typer.Typer) -> None:
    app.command(
        "backup",
        help=(
            "Download a backup of the control-plane state (database, stacks and "
            "the server's keys: keep it as safe as the keys)"
        ),
    )(backup)
    app.command(
        "restore",
        help=(
            "Upload a backup archive to stage a restore (applied on server "
            "restart)"
        ),
    )(restore)


def backup(
    out: str = typer.Option(
        "", "--out", "-o", help="output file (default ./kowan-backup-<date>.tar.gz)"
    ),
) -> None:
    """Download a backup of the control-plane state (database, stacks and keys).

    The archive holds the server's encryption and signing keys, so the file is
    created readable by its owner only (0600)."""
    _main.require_token()

    dest = out.strip()
    if dest == "":
        dest = "kowan-backup-" + datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".tar.gz"

    with _main.new_client() as c:
        try:
            f = os.fdopen(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
        except OSError as e:
            raise RuntimeError(f"create backup file {dest}: {e}") from e
        try:
            c.download_backup(f)
        except Exception:
            f.close()
            # Remove the partial file so a failed download leaves no truncated
            # archive behind.
            try:
                os.remove(dest)
            except OSError:
                pass
            raise
        finally:
            if not f.closed:
                f.close()
    print(f"Backup written to {dest}")


def restore(
    file: str = typer.Argument(..., metavar="FILE", help="backup archive to upload"),
) -> None:
    """Upload a backup archive to stage a restore (applied on server restart)."""
    _main.require_token()

    try:
        f = open(file, "rb")
    except OSError as e:
        raise RuntimeError(f"open backup file {file}: {e}") from e
    try:
        with _main.new_client() as c:
            c.restore(f)
    finally:
        f.close()
    print("Restore staged; restart the server to apply.")
