"""Unit tests for credential connections in the Workflows feature.

Two surfaces moved their auth ENTRY from inline typed fields to a referenced
connection (a connection id under ``connectionRef``), exactly like
registries/secrets/channels/volumes:

  * the git-commit TRIGGER (gitFetch)   -> username/token for a private repo;
  * the HTTP ACTION (httpBearer)        -> an Authorization/Bearer header.

Both tests use a tiny synchronous fake store (the ``test_volumeauth.py`` style):
no SQLite, no network, no HTTP client. The git side exercises
``TriggerPoller._apply_connection`` directly; the http side exercises the same
``connections.resolve(...).auth_headers()`` path the engine's ``_run_http`` uses.
"""

import asyncio
import base64
import unittest

from kowan import capabilities as cap
from kowan import connections
from kowan.workflow.poller import TriggerPoller


class _FakeCred:
    """Stands in for a credentials-store row: type/name/meta."""

    def __init__(self, ctype, name, meta):
        self.type = ctype
        self.name = name
        self.meta = meta


class _FakeStore:
    """Minimal store exposing the two methods the connection resolvers need:
    ``get_credential(id) -> _FakeCred`` and ``credential_config(id) -> dict``."""

    def __init__(self, creds, configs):
        self._creds = creds
        self._configs = configs

    def get_credential(self, cid):
        return self._creds[cid]

    def credential_config(self, cid):
        return dict(self._configs.get(cid, {}))


class TestGitCommitConnection(unittest.TestCase):
    """The git-commit trigger resolves a gitFetch connection into username/token
    before the poller builds the trigger."""

    def test_apply_connection_injects_username_and_token(self):
        # A git-http connection: username is non-secret (meta), token is secret (data).
        store = _FakeStore(
            creds={"c1": _FakeCred("git-http", "gh", {"username": "me"})},
            configs={"c1": {"token": "ghp_secret"}},
        )
        poller = TriggerPoller(engine=None, store=store)

        cfg = {"repo": "https://git/x.git", "branch": "main", "connectionRef": "c1"}
        out = asyncio.run(poller._apply_connection(cfg, "git-commit"))

        self.assertEqual(out["username"], "me")
        self.assertEqual(out["token"], "ghp_secret")
        # The ref itself is consumed, not passed to the trigger builder.
        self.assertNotIn("connectionRef", out)
        # Non-auth config is preserved.
        self.assertEqual(out["repo"], "https://git/x.git")
        self.assertEqual(out["branch"], "main")
        # The original cfg is not mutated (a copy is returned).
        self.assertIn("connectionRef", cfg)
        self.assertNotIn("username", cfg)

    def test_apply_connection_noop_without_ref(self):
        # A public repo: no connectionRef -> the config is returned unchanged.
        store = _FakeStore(creds={}, configs={})
        poller = TriggerPoller(engine=None, store=store)

        cfg = {"repo": "https://git/public.git", "branch": ""}
        out = asyncio.run(poller._apply_connection(cfg, "git-commit"))

        self.assertEqual(out, cfg)
        self.assertNotIn("username", out)
        self.assertNotIn("token", out)

    def test_apply_connection_token_only_leaves_username_alone(self):
        # A token-only credential must not clobber an absent/empty username with "".
        store = _FakeStore(
            creds={"c2": _FakeCred("gitlab", "gl", {})},
            configs={"c2": {"token": "glpat"}},
        )
        poller = TriggerPoller(engine=None, store=store)

        out = asyncio.run(
            poller._apply_connection({"repo": "r", "connectionRef": "c2"}, "git-commit")
        )
        self.assertEqual(out["token"], "glpat")
        self.assertNotIn("username", out)
        self.assertNotIn("connectionRef", out)


class TestHttpBearerConnection(unittest.TestCase):
    """The HTTP action resolves an httpBearer connection into an auth header via the
    same ``connections.resolve`` path the engine's _run_http uses (no HTTP)."""

    def test_api_token_connection_yields_bearer_header(self):
        # meta {header, scheme} + data {token} -> a single Authorization: Bearer header.
        store = _FakeStore(
            creds={"api1": _FakeCred("api-token", "svc", {"scheme": "Bearer"})},
            configs={"api1": {"token": "abc123"}},
        )
        bearer = asyncio.run(
            connections.resolve(store, "api1", cap.CAP_HTTP_BEARER)
        )
        self.assertEqual(bearer.auth_headers(), {"Authorization": "Bearer abc123"})

    def test_basic_auth_connection_yields_basic_header(self):
        # username (meta) + password (data) -> a Basic header, custom header name.
        store = _FakeStore(
            creds={"ba1": _FakeCred("basic-auth", "svc", {"username": "u", "header": "X-Auth"})},
            configs={"ba1": {"password": "pw"}},
        )
        bearer = asyncio.run(
            connections.resolve(store, "ba1", cap.CAP_HTTP_BEARER)
        )
        expected = "Basic " + base64.b64encode(b"u:pw").decode()
        self.assertEqual(bearer.auth_headers(), {"X-Auth": expected})


if __name__ == "__main__":
    unittest.main()
