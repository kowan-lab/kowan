"""Read stacks out of Portainer through its REST API.

Portainer 2.x: ``X-API-Key`` (an access token from the user's account
settings) or ``POST /api/auth`` with a username and password for a JWT;
``GET /api/endpoints`` for its environments, ``GET /api/stacks`` for stacks
(type 1 swarm, 2 compose, 3 kubernetes) and ``GET /api/stacks/{id}/file`` for
a stack's compose file. Credentials are used for this read only, never kept.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from kowan import httpcap

_LIMIT = 8 << 20
_TIMEOUT = 15.0

TYPE_SWARM = 1
TYPE_COMPOSE = 2


class PortainerError(Exception):
    pass


@dataclass
class Stack:
    id: int
    name: str
    kind: str  # "compose" | "swarm" | "kubernetes"
    endpoint_id: int
    endpoint: str
    active: bool
    env: Dict[str, str] = field(default_factory=dict)
    compose: str = ""
    git: Optional[Dict[str, Any]] = None
    git_auth: bool = False


def validate_url(url: str) -> str:
    u = urlsplit(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("url must be the http(s) address of Portainer")
    return url.strip().rstrip("/")


def _branch(ref: str) -> str:
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


class Client:
    def __init__(self, url: str, *, api_key: str = "", username: str = "", password: str = "", verify: bool = True) -> None:
        self.url = validate_url(url)
        self._api_key = api_key
        self._username = username
        self._password = password
        self._verify = verify
        self._headers: Dict[str, str] = {}

    async def _get(self, client: httpcap.Client, path: str) -> Any:
        resp = await client.request("GET", self.url + path, limit=_LIMIT, headers=self._headers)
        if resp.status_code in (401, 403):
            raise PortainerError("Portainer refused the credentials")
        if resp.status_code >= 400:
            raise PortainerError(f"Portainer answered {resp.status_code} on {path}")
        if resp.truncated:
            raise PortainerError(f"Portainer's answer on {path} is too large")
        try:
            return json.loads(resp.content)
        except ValueError:
            raise PortainerError(f"Portainer's answer on {path} is not JSON (is the URL right?)") from None

    async def _login(self, client: httpcap.Client) -> None:
        if self._api_key:
            self._headers = {"X-API-Key": self._api_key}
            return
        if not (self._username and self._password):
            raise PortainerError("an access token, or a username and password, is required")
        resp = await client.request("POST", self.url + "/api/auth", limit=1 << 16,
                                    json={"Username": self._username, "Password": self._password})
        if resp.status_code in (401, 403, 422):
            raise PortainerError("Portainer refused the username or password")
        if resp.status_code >= 400:
            raise PortainerError(f"Portainer answered {resp.status_code} on login")
        try:
            jwt = json.loads(resp.content).get("jwt", "")
        except (ValueError, AttributeError):
            jwt = ""
        if not jwt:
            raise PortainerError("Portainer's login answer has no token (is the URL right?)")
        self._headers = {"Authorization": f"Bearer {jwt}"}

    async def stacks(self) -> List[Stack]:
        """Every stack with its compose file (git stacks: the repository)."""
        try:
            async with httpcap.Client(verify=self._verify, timeout=_TIMEOUT, follow_redirects=False) as client:
                await self._login(client)
                endpoints = {int(e.get("Id", 0)): str(e.get("Name", "")) for e in await self._get(client, "/api/endpoints") or []}
                out: List[Stack] = []
                for s in await self._get(client, "/api/stacks") or []:
                    kind = {TYPE_SWARM: "swarm", TYPE_COMPOSE: "compose"}.get(int(s.get("Type") or 0), "kubernetes")
                    eid = int(s.get("EndpointId") or 0)
                    st = Stack(
                        id=int(s.get("Id") or 0), name=str(s.get("Name") or ""), kind=kind, endpoint_id=eid,
                        endpoint=endpoints.get(eid, str(eid)), active=int(s.get("Status") or 1) == 1,
                        env={str(e.get("name", "")): str(e.get("value", "")) for e in s.get("Env") or [] if e.get("name")},
                    )
                    g = s.get("GitConfig") or None
                    if g and g.get("URL"):
                        st.git = {"repoUrl": str(g["URL"]), "ref": _branch(str(g.get("ReferenceName") or "")) or "main",
                                  "path": str(g.get("ConfigFilePath") or "docker-compose.yml")}
                        st.git_auth = bool((g.get("Authentication") or {}).get("Username"))
                    elif kind != "kubernetes":
                        f = await self._get(client, f"/api/stacks/{st.id}/file")
                        st.compose = str((f or {}).get("StackFileContent") or "")
                    out.append(st)
                return out
        except httpcap.HTTPError as err:
            raise PortainerError(f"cannot reach Portainer: {err.__class__.__name__}: {err}") from None
