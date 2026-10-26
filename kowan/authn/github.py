"""GitHub sign-in (OAuth2 + the user API; GitHub is not an OIDC provider).

GitHub issues no ID token, so the identity comes from the REST API with the
access token and is shaped into OIDC-like claims so the same mapping and
provisioning code applies:

    sub                 numeric GitHub user id (stable across renames)
    preferred_username  login
    name, picture       profile
    email               primary verified address (``/user/emails``)
    email_verified      True only for a verified address
    groups              org logins and ``org/team-slug`` (needs ``read:org``)

GitHub Enterprise Server works by setting ``baseUrl`` (web) and ``apiUrl``.
PKCE (S256) is sent too; GitHub accepts it for OAuth apps.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from .oidc import HttpFactory, OIDCError, default_http, get_json, post_form

DEFAULT_BASE = "https://github.com"
DEFAULT_API = "https://api.github.com"
DEFAULT_SCOPES = ["read:user", "user:email", "read:org"]


class GitHubClient:
    def __init__(self, client_id: str, client_secret: str, *, base_url: str = "", api_url: str = "",
                 http: Optional[HttpFactory] = None) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.api_url = (api_url or DEFAULT_API).rstrip("/")
        self._http = http or default_http

    def authorization_url(self, redirect_uri: str, scopes: List[str], state: str, challenge: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "allow_signup": "false",
        }
        return f"{self.base_url}/login/oauth/authorize?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> str:
        body = await post_form(self._http, f"{self.base_url}/login/oauth/access_token", {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        })
        token = body.get("access_token")
        if not token:
            raise OIDCError("github: no access token in response", "token_exchange_failed")
        return str(token)

    def _headers(self, token: str) -> Dict[str, str]:
        return {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    async def identity(self, token: str, want_groups: bool = True) -> Dict[str, Any]:
        h = self._headers(token)
        user = await get_json(self._http, f"{self.api_url}/user", h)
        if not isinstance(user, dict) or not user.get("id"):
            raise OIDCError("github: unexpected /user response", "provider_error")
        claims: Dict[str, Any] = {
            "sub": str(user["id"]),
            "preferred_username": user.get("login") or "",
            "name": user.get("name") or user.get("login") or "",
            "picture": user.get("avatar_url") or "",
            "email": "",
            "email_verified": False,
            "groups": [],
        }
        try:
            emails = await get_json(self._http, f"{self.api_url}/user/emails", h)
        except OIDCError:
            emails = []
        if isinstance(emails, list):
            verified = [e for e in emails if isinstance(e, dict) and e.get("verified")]
            primary = next((e for e in verified if e.get("primary")), verified[0] if verified else None)
            if primary:
                claims["email"] = primary.get("email") or ""
                claims["email_verified"] = True
        if want_groups:
            groups: List[str] = []
            try:
                orgs = await get_json(self._http, f"{self.api_url}/user/orgs?per_page=100", h)
                groups += [o.get("login") for o in orgs if isinstance(o, dict) and o.get("login")]
            except OIDCError:
                pass
            try:
                teams = await get_json(self._http, f"{self.api_url}/user/teams?per_page=100", h)
                for t in teams if isinstance(teams, list) else []:
                    org = (t.get("organization") or {}).get("login") if isinstance(t, dict) else None
                    if org and t.get("slug"):
                        groups.append(f"{org}/{t['slug']}")
            except OIDCError:
                pass
            claims["groups"] = sorted(set(groups))
        return claims
