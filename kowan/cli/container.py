"""container command group."""

from __future__ import annotations

import asyncio

import typer

from . import main as _main
from .output import first_name, print_table, short_id, truncate

# The host targeted when --host is not supplied; matches the fixed local host id.
DEFAULT_HOST_ID = "local"

app = typer.Typer(
    help="Manage containers on a host",
    no_args_is_help=True,
)


def _ls(
    host: str = typer.Option(DEFAULT_HOST_ID, "--host", help="host id to target"),
    all_: bool = typer.Option(
        False, "--all", "-a", help="include stopped containers"
    ),
) -> None:
    """List containers."""
    _main.require_token()
    with _main.new_client() as c:
        containers = c.list_containers(host, all_)

    rows = [
        [
            short_id(ct.get("id", "")),
            truncate(first_name(ct.get("names")), 30),
            truncate(ct.get("image", ""), 30),
            ct.get("state", ""),
            truncate(ct.get("status", ""), 30),
            _format_ports(ct.get("ports") or []),
        ]
        for ct in containers
    ]
    print_table(["ID", "NAME", "IMAGE", "STATE", "STATUS", "PORTS"], rows)


app.command("ls", help="List containers")(_ls)
app.command("list", hidden=True)(_ls)


def _make_action(action: str, short: str):
    def action_cmd(
        container_id: str = typer.Argument(..., help="container id"),
        host: str = typer.Option(
            DEFAULT_HOST_ID, "--host", help="host id to target"
        ),
    ) -> None:
        _main.require_token()
        with _main.new_client() as c:
            c.container_action(host, container_id, action)
        # Report the action in clean past tense.
        print(f"{container_id}: {_past_tense(action)}")

    action_cmd.__doc__ = short
    app.command(action, help=short)(action_cmd)


_make_action("start", "Start a container")
_make_action("stop", "Stop a container")
_make_action("restart", "Restart a container")


def _logs(
    container_id: str = typer.Argument(..., help="container id"),
    host: str = typer.Option(DEFAULT_HOST_ID, "--host", help="host id to target"),
) -> None:
    """Stream container logs (follow)."""
    _main.require_token()
    c = _main.new_client()
    url = c.container_logs_ws_url(host, container_id)
    headers = c._auth_headers()  # X-API-Key / Authorization
    try:
        asyncio.run(_stream_logs(url, headers))
    except KeyboardInterrupt:
        pass
    finally:
        c.close()


app.command("logs", help="Stream container logs (follow)")(_logs)


async def _stream_logs(url: str, headers: dict[str, str]) -> None:
    """Open a WebSocket to the log stream and print each received text frame."""
    import ssl

    import aiohttp

    # aiohttp's ws_connect takes ssl= as an SSLContext (wss) or is ignored on ws://.
    # max_msg_size=0 removes the default frame-size cap. Accept self-signed certs on
    # wss:// to match the HTTP client.
    ws_kwargs: dict = {"headers": headers, "max_msg_size": 0}
    if url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        ws_kwargs["ssl"] = ssl_ctx

    async with aiohttp.ClientSession() as sess, sess.ws_connect(url, **ws_kwargs) as ws:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                print(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                print(msg.data.decode("utf-8", "replace"))
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                break


def _past_tense(action: str) -> str:
    """Render a lifecycle action verb in past tense for status output."""
    return {
        "start": "started",
        "stop": "stopped",
        "restart": "restarted",
    }.get(action, action + "ed")


def _format_ports(ports: list) -> str:
    """Render a compact port mapping summary."""
    if not ports:
        return "-"
    parts = []
    for p in ports:
        priv = p.get("privatePort", 0)
        pub = p.get("publicPort", 0)
        typ = p.get("type", "")
        if pub != 0:
            parts.append(f"{pub}->{priv}/{typ}")
        else:
            parts.append(f"{priv}/{typ}")
    return ", ".join(parts)
