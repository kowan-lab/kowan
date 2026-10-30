"""``kowanctl mcp`` — a stdio <-> Streamable HTTP bridge for MCP clients.

Desktop MCP clients (Claude Desktop, some IDEs) launch servers as local
processes speaking newline-delimited JSON-RPC on stdin/stdout. This command is
that process: every stdin line is POSTed to the Kowan server's ``/mcp`` endpoint
with the configured API key, and every reply is written to stdout as one line.
It keeps the ``Mcp-Session-Id`` issued on initialize, sends the negotiated
``MCP-Protocol-Version`` header, transparently re-initializes when the server
forgets the session (restart -> 404), and ends the session on EOF.

Configuration (first non-empty wins):

  server:  --url, $KOWAN_URL, then kowanctl's usual chain
           (kowanctl --server, $KOWAN_SERVER, ~/.kowan/config.yaml)
  api key: --api-key, $KOWAN_API_KEY, then kowanctl --api-key / config file
  (a login token from ``kowanctl login`` is used when no API key is set)

Nothing but JSON-RPC is ever written to stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, List, Optional, TextIO

import typer

from .config import first_non_empty
from .httpclient import HTTPError, Response, Session

SESSION_HEADER = "Mcp-Session-Id"
VERSION_HEADER = "MCP-Protocol-Version"


def _err(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class Bridge:
    """Forward JSON-RPC lines to ``<server>/mcp`` and return the reply lines."""

    def __init__(self, server: str, api_key: str = "", token: str = "",
                 http: Optional[Session] = None, log: Optional[TextIO] = None) -> None:
        self.url = server.strip().rstrip("/") + "/mcp"
        self.credential = (api_key or token or "").strip()
        # Unverified TLS like the rest of kowanctl: servers often use self-signed
        # certs. A tool call may take minutes; connecting may not.
        self.http = http or Session(timeout=900.0, connect_timeout=10.0)
        self.log = log or sys.stderr
        self.session_id = ""
        self.protocol_version = ""
        self._init_msg: Optional[dict] = None

    # -- HTTP ---------------------------------------------------------------

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.credential:
            h["Authorization"] = "Bearer " + self.credential
        if self.session_id:
            h[SESSION_HEADER] = self.session_id
        if self.protocol_version:
            h[VERSION_HEADER] = self.protocol_version
        return h

    def _post(self, msg: dict) -> Response:
        return self.http.request("POST", self.url, content=_dump(msg).encode("utf-8"), headers=self._headers())

    @staticmethod
    def _parse(resp: Response) -> List[dict]:
        ctype = resp.headers.get("content-type", "")
        if not resp.content:
            return []
        if ctype.startswith("text/event-stream"):
            out, data = [], []
            for line in resp.text.splitlines() + [""]:
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif line == "" and data:
                    try:
                        out.append(json.loads("\n".join(data)))
                    except ValueError:
                        pass
                    data = []
            return out
        try:
            body = resp.json()
        except ValueError:
            return []
        return body if isinstance(body, list) else [body]

    def _reinitialize(self) -> bool:
        """Replay the client's initialize after the server dropped the session."""
        if self._init_msg is None:
            return False
        self.session_id = ""
        try:
            resp = self._post(self._init_msg)
        except HTTPError:
            return False
        if resp.status_code != 200 or not resp.headers.get(SESSION_HEADER):
            return False
        self.session_id = resp.headers[SESSION_HEADER]
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except HTTPError:
            pass
        print("kowanctl mcp: session expired; re-initialized", file=self.log)
        return True

    # -- one message ----------------------------------------------------------

    def handle_line(self, line: str) -> List[str]:
        line = line.strip()
        if not line:
            return []
        try:
            msg = json.loads(line)
        except ValueError:
            return [_dump(_err(None, -32700, "parse error: stdin line is not valid JSON"))]
        msg_id = msg.get("id") if isinstance(msg, dict) and "method" in msg else None
        is_init = isinstance(msg, dict) and msg.get("method") == "initialize" and "id" in msg
        if is_init:
            self.session_id = ""  # a fresh initialize starts a fresh session
            self.protocol_version = ""
            self._init_msg = msg
        try:
            resp = self._post(msg)
            if resp.status_code == 404 and self.session_id and not is_init and self._reinitialize():
                resp = self._post(msg)
        except HTTPError as err:
            print(f"kowanctl mcp: {self.url}: {err}", file=self.log)
            return [_dump(_err(msg_id, -32603, f"Kowan server unreachable: {err}"))] if msg_id is not None else []

        if is_init and resp.status_code == 200:
            self.session_id = resp.headers.get(SESSION_HEADER, "")
        replies = self._parse(resp)
        if is_init:
            for r in replies:
                v = (r.get("result") or {}).get("protocolVersion") if isinstance(r, dict) else None
                if isinstance(v, str):
                    self.protocol_version = v
        if resp.status_code >= 400:
            message = ""
            if replies and isinstance(replies[0], dict):
                message = (replies[0].get("error") or {}).get("message", "")
            if not message:
                message = resp.text.strip()[:300] or resp.reason_phrase
            print(f"kowanctl mcp: HTTP {resp.status_code}: {message}", file=self.log)
            if msg_id is None:
                return []
            code = -32001 if resp.status_code in (401, 403) else -32603
            return [_dump(_err(msg_id, code, f"HTTP {resp.status_code}: {message}"))]
        return [_dump(r) for r in replies]

    def close(self) -> None:
        if self.session_id:
            try:
                self.http.request("DELETE", self.url, headers=self._headers())
            except HTTPError:
                pass
            self.session_id = ""
        self.http.close()

    def run(self, stdin: TextIO, stdout: TextIO) -> None:
        try:
            for line in stdin:
                for out in self.handle_line(line):
                    stdout.write(out + "\n")
                    stdout.flush()
        finally:
            self.close()


def resolve_bridge_config(flag_url: str = "", flag_api_key: str = "") -> tuple[str, str, str]:
    """(server, api_key, token): --url/--api-key, then $KOWAN_URL/$KOWAN_API_KEY,
    then the usual kowanctl resolution (--server/--api-key, env, config file)."""
    from .main import resolved

    r = resolved()
    server = first_non_empty(flag_url, os.getenv("KOWAN_URL", ""), r.server)
    api_key = first_non_empty(flag_api_key, os.getenv("KOWAN_API_KEY", ""), r.api_key)
    return server, api_key, r.token


def mcp(
    url: str = typer.Option("", "--url", help="Kowan server base URL (default: $KOWAN_URL or the kowanctl config)"),
    api_key: str = typer.Option("", "--api-key", help="API key (default: $KOWAN_API_KEY or the kowanctl config)"),
) -> None:
    """Run a stdio MCP server that proxies to the Kowan server's /mcp endpoint."""
    server, key, token = resolve_bridge_config(url, api_key)
    if not server:
        raise typer.BadParameter("no server configured: pass --url or set KOWAN_URL")
    if not key and not token:
        raise typer.BadParameter("no credential: pass --api-key or set KOWAN_API_KEY")
    Bridge(server, key, token).run(sys.stdin, sys.stdout)


def register(app: typer.Typer) -> None:
    app.command("mcp", help="Run a stdio MCP server bridged to the Kowan server (for Claude Desktop & co.)")(mcp)
