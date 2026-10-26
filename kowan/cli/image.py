"""image command group."""

from __future__ import annotations

import typer

from . import main as _main
from .container import DEFAULT_HOST_ID
from .output import first_tag, human_bytes, print_table, short_id, truncate, unix_time

app = typer.Typer(help="Manage images on a host", no_args_is_help=True)


def _ls(
    host: str = typer.Option(DEFAULT_HOST_ID, "--host", help="host id to target"),
) -> None:
    """List images."""
    _main.require_token()
    with _main.new_client() as c:
        images = c.list_images(host)

    rows = [
        [
            short_id(img.get("id", "")),
            truncate(first_tag(img.get("repoTags")), 40),
            human_bytes(int(img.get("size", 0) or 0)),
            unix_time(int(img.get("created", 0) or 0)),
        ]
        for img in images
    ]
    print_table(["ID", "REPOSITORY:TAG", "SIZE", "CREATED"], rows)


app.command("ls", help="List images")(_ls)
app.command("list", hidden=True)(_ls)
