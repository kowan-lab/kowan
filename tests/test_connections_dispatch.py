"""Tests for capability dispatch (Phase 2 of the connection model).

Verifies that, given a typed connection's fields, the dispatch layer builds an
implementation exposing the requested capability's method — reusing the existing
registry / secrets / notify providers, without the caller branching on type — and
that a service config can delegate its auth to a shared connection by reference.
"""

import asyncio
import base64
import json
import os
import tempfile
import unittest

from kowan import capabilities as cap
from kowan import connections
from kowan import registry
from kowan.connections import CapabilityError
from kowan.models import Credential
from kowan.secrets import Cipher, load_or_create_key
from kowan.store import Store


class TestCapabilityDispatch(unittest.TestCase):
    def test_docker_registry_auth_from_basic_registry(self):
        impl = connections.build_capability(
            "generic", {"password": "tok"}, cap.CAP_DOCKER_REGISTRY_AUTH,
            {"url": "ghcr.io", "username": "me"},
        )
        self.assertTrue(hasattr(impl, "docker_auth"))

    def test_docker_registry_auth_from_ecr(self):
        impl = connections.build_capability(
            "ecr", {"region": "eu-west-1", "accessKeyId": "AK", "secretAccessKey": "SEK"},
            cap.CAP_DOCKER_REGISTRY_AUTH, {"url": "123.dkr.ecr"},
        )
        self.assertTrue(hasattr(impl, "docker_auth"))

    def test_docker_registry_auth_from_aws_cloud(self):
        # One AWS connection derives ECR docker auth via cloudIdentity (reusing the
        # existing ECR provider) — the user's exact "AWS creds for ECR" case.
        impl = connections.build_capability(
            "aws", {"region": "eu-west-1", "accessKeyId": "AK", "secretAccessKey": "SEK"},
            cap.CAP_DOCKER_REGISTRY_AUTH, {"url": "123.dkr.ecr.eu-west-1.amazonaws.com"},
        )
        self.assertTrue(hasattr(impl, "docker_auth"))
        self.assertEqual(type(impl).__name__, "ECRProvider")

    def test_secret_fetch_from_vault(self):
        impl = connections.build_capability(
            "vault",
            {"address": "https://vault", "token": "t", "mount": "secret", "path": "app", "key": "k"},
            cap.CAP_SECRET_FETCH,
        )
        self.assertTrue(hasattr(impl, "resolve"))

    def test_message_send_from_slack(self):
        impl = connections.build_capability(
            "slack", {"url": "https://hooks.slack/x"}, cap.CAP_MESSAGE_SEND,
        )
        self.assertTrue(hasattr(impl, "send"))

    def test_http_bearer_from_token(self):
        impl = connections.build_capability(
            "vault", {"token": "abc"}, cap.CAP_HTTP_BEARER, {"scheme": "Bearer"},
        )
        self.assertEqual(impl.auth_headers(), {"Authorization": "Bearer abc"})

    def test_git_fetch_from_github(self):
        # A github connection: non-secret username (meta) + secret token (data)
        # -> the (username, token) pair the git-commit trigger uses for basic auth.
        impl = connections.build_capability(
            "github", {"token": "ghp_x"}, cap.CAP_GIT_FETCH, {"username": "me"},
        )
        self.assertEqual(impl.git_auth(), ("me", "ghp_x"))
        # The connection form stores every field in data; username there works too.
        impl2 = connections.build_capability(
            "github", {"username": "me2", "token": "ghp_y"}, cap.CAP_GIT_FETCH,
        )
        self.assertEqual(impl2.git_auth(), ("me2", "ghp_y"))

    def test_git_fetch_token_only(self):
        # gitlab/token-only: no username -> ("", token) still authenticates.
        impl = connections.build_capability(
            "gitlab", {"token": "glpat"}, cap.CAP_GIT_FETCH,
        )
        self.assertEqual(impl.git_auth("https://gitlab/x.git"), ("", "glpat"))

    def test_native_basic_auth_http_bearer(self):
        impl = connections.build_capability(
            "basic-auth", {"password": "pw"}, cap.CAP_HTTP_BEARER, {"username": "u"},
        )
        # username lives in meta for a basic-auth connection -> "Basic u:pw".
        expected = "Basic " + base64.b64encode(b"u:pw").decode()
        self.assertEqual(impl.auth_headers(), {"Authorization": expected})

    def test_wrong_capability_raises(self):
        with self.assertRaises(CapabilityError):
            connections.build_capability("slack", {}, cap.CAP_DOCKER_REGISTRY_AUTH)
        # api-token has no gitFetch builder path (declares only httpBearer).
        with self.assertRaises(CapabilityError):
            connections.build_capability("api-token", {"token": "x"}, cap.CAP_GIT_FETCH)


class TestServiceReference(unittest.TestCase):
    """A registry service that references a shared connection resolves its auth from
    the connection's secret + the service's own (url) config."""

    def setUp(self):
        d = tempfile.mkdtemp()
        self.store = Store(os.path.join(d, "t.db"))
        self.store.use_cipher(Cipher(load_or_create_key(d, "")))
        self.store.set_credential_sensitivity(lambda kind, t: registry.sensitive_keys(t))

    def test_registry_references_shared_connection(self):
        # One reusable connection: a Docker login (secret = password).
        conn = Credential(id="conn1", kind="connection", type="generic",
                          name="ghcr-login", meta={"username": "me"},
                          createdAt="t", updatedAt="t")
        self.store.create_credential(conn, {"password": "s3cret"})
        # Two registry services pointing at the SAME connection, different urls.
        for i, url in enumerate(("ghcr.io", "ghcr.io/team")):
            reg = Credential(id=f"reg{i}", kind="registry", type="generic",
                            name=f"r{i}", meta={"url": url, "connectionRef": "conn1"},
                            createdAt="t", updatedAt="t")
            self.store.create_credential(reg, {})

        impl = asyncio.run(
            connections.resolve_service(self.store, "reg0", cap.CAP_DOCKER_REGISTRY_AUTH)
        )
        self.assertTrue(hasattr(impl, "docker_auth"))
        # The static basic-auth token must carry the CONNECTION's password and the
        # SERVICE's url — proving the two rows were merged at resolve time.
        token = asyncio.run(impl.docker_auth())
        decoded = json.loads(base64.b64decode(token))
        self.assertEqual(decoded.get("username"), "me")
        self.assertEqual(decoded.get("password"), "s3cret")
        self.assertEqual(decoded.get("serveraddress"), "ghcr.io")

    def test_build_push_credentials_resolve_via_connection(self):
        # An image build's push credentials must come from the registry's delegated
        # connection, not just inline registry auth — this pins the migration that
        # wires _fill_registry_credentials through connections.resolve_service.
        from types import SimpleNamespace

        from kowan.api.images import _fill_registry_credentials

        conn = Credential(id="conn2", kind="connection", type="generic",
                          name="push-login", meta={"username": "pusher"},
                          createdAt="t", updatedAt="t")
        self.store.create_credential(conn, {"password": "pushpw"})
        reg = Credential(id="regp", kind="registry", type="generic", name="rp",
                         meta={"url": "reg.example.com", "connectionRef": "conn2"},
                         createdAt="t", updatedAt="t")
        self.store.create_credential(reg, {})

        svc = SimpleNamespace(store=self.store)
        opts = SimpleNamespace(
            registry_id="regp", registry_host="", registry_user="", registry_pass=""
        )
        asyncio.run(_fill_registry_credentials(svc, opts))
        self.assertEqual(opts.registry_user, "pusher")
        self.assertEqual(opts.registry_pass, "pushpw")
        self.assertEqual(opts.registry_host, "reg.example.com")

    def test_channel_references_shared_connection(self):
        # A notification channel delegating its transport to a shared slack connection
        # resolves messageSend from the CONNECTION (so test-send works without inline
        # config).
        conn = Credential(id="ch-conn", kind="connection", type="slack",
                          name="slack-shared", meta={}, createdAt="t", updatedAt="t")
        self.store.create_credential(conn, {"url": "https://hooks.slack/xyz"})
        chan = Credential(id="chan0", kind="channel", type="slack", name="alerts",
                          meta={"connectionRef": "ch-conn"}, createdAt="t", updatedAt="t")
        self.store.create_credential(chan, {})
        impl = asyncio.run(
            connections.resolve_service(self.store, "chan0", cap.CAP_MESSAGE_SEND)
        )
        self.assertTrue(hasattr(impl, "send"))

    def test_inline_service_still_works(self):
        # No connectionRef: the service embeds its own auth (backward compatible).
        reg = Credential(id="reg9", kind="registry", type="generic", name="inline",
                        meta={"url": "reg.example"}, createdAt="t", updatedAt="t")
        self.store.create_credential(reg, {"password": "inlinepw"})
        impl = asyncio.run(
            connections.resolve_service(self.store, "reg9", cap.CAP_DOCKER_REGISTRY_AUTH)
        )
        token = asyncio.run(impl.docker_auth())
        decoded = json.loads(base64.b64decode(token))
        self.assertEqual(decoded.get("password"), "inlinepw")


if __name__ == "__main__":
    unittest.main()
