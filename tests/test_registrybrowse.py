"""Unit tests for the docker-image trigger's registry-connection rework.

Two surfaces are covered, both offline (a tiny synchronous fake store, no SQLite /
no network / no httpx):

  * ``TriggerPoller._apply_connection`` resolving a ``dockerRegistryAuth`` connection
    into ``config`` username/password + registry host (mirroring the git-commit
    gitFetch injection test in ``test_workflow_connections.py``);
  * the docker-image FieldSpec list now advertising a ``connectionRef`` credential
    picker for ``dockerRegistryAuth`` and NO longer advertising inline
    username/password.
"""

import asyncio
import unittest

from kowan import triggers
from kowan.triggers import TypeDockerImage
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


class TestDockerRegistryConnection(unittest.TestCase):
    """The docker-image trigger resolves a dockerRegistryAuth connection into
    username/password + the registry host before the poller builds the trigger."""

    def test_apply_connection_injects_registry_username_password(self):
        # A generic (basic-auth) registry connection: url + username are non-secret
        # (meta), the password is secret (data). credentials() yields (u, p, url).
        store = _FakeStore(
            creds={
                "r1": _FakeCred(
                    "generic",
                    "ghcr",
                    {"url": "https://ghcr.io", "username": "me"},
                )
            },
            configs={"r1": {"password": "ghp_secret"}},
        )
        poller = TriggerPoller(engine=None, store=store)

        cfg = {"repository": "owner/img", "tag": "latest", "connectionRef": "r1"}
        out = asyncio.run(poller._apply_connection(cfg, TypeDockerImage))

        self.assertEqual(out["username"], "me")
        self.assertEqual(out["password"], "ghp_secret")
        # The connection supplies the registry HOST (scheme stripped).
        self.assertEqual(out["registry"], "ghcr.io")
        # The ref itself is consumed, not passed to the trigger builder.
        self.assertNotIn("connectionRef", out)
        # Non-auth config is preserved.
        self.assertEqual(out["repository"], "owner/img")
        self.assertEqual(out["tag"], "latest")
        # The original cfg is not mutated (a copy is returned).
        self.assertIn("connectionRef", cfg)
        self.assertNotIn("registry", cfg)

    def test_apply_connection_noop_without_ref(self):
        # No connection -> config returned unchanged (Docker Hub / anonymous default
        # is applied later by _new_docker_image, not here).
        store = _FakeStore(creds={}, configs={})
        poller = TriggerPoller(engine=None, store=store)

        cfg = {"repository": "library/nginx", "tag": "latest"}
        out = asyncio.run(poller._apply_connection(cfg, TypeDockerImage))

        self.assertEqual(out, cfg)
        self.assertNotIn("registry", out)
        self.assertNotIn("username", out)
        self.assertNotIn("password", out)

    def test_built_poller_reads_injected_registry_and_creds(self):
        # End-to-end within the process: inject via the connection, then build the
        # poller and confirm it captured the resolved registry/creds.
        store = _FakeStore(
            creds={
                "r1": _FakeCred(
                    "generic", "reg", {"url": "https://reg.example.com", "username": "u"}
                )
            },
            configs={"r1": {"password": "pw"}},
        )
        poller = TriggerPoller(engine=None, store=store)
        cfg = {"repository": "team/api", "tag": "v2", "connectionRef": "r1"}
        resolved = asyncio.run(poller._apply_connection(cfg, TypeDockerImage))

        built = triggers.build(TypeDockerImage, resolved)
        self.assertEqual(built.registry, "reg.example.com")
        self.assertEqual(built.repository, "team/api")
        self.assertEqual(built.reference, "v2")
        self.assertEqual(built.username, "u")
        self.assertEqual(built.password, "pw")


class TestDockerImageFieldSpec(unittest.TestCase):
    """The docker-image trigger advertises a dockerRegistryAuth connection picker and
    no longer advertises inline username/password fields."""

    def _fields_by_key(self):
        return {f.key: f for f in triggers.fields(TypeDockerImage)}

    def test_advertises_connection_ref_with_docker_registry_auth(self):
        fields = self._fields_by_key()
        self.assertIn("connectionRef", fields)
        desc = fields["connectionRef"].connection
        self.assertIsNotNone(desc)
        self.assertEqual(desc.get("capability"), "dockerRegistryAuth")
        self.assertTrue(desc.get("optional"))

    def test_advertises_repository_and_tag_required(self):
        fields = self._fields_by_key()
        self.assertIn("repository", fields)
        self.assertIn("tag", fields)
        self.assertTrue(fields["repository"].required)
        self.assertTrue(fields["tag"].required)

    def test_no_inline_username_password_or_image(self):
        fields = self._fields_by_key()
        self.assertNotIn("username", fields)
        self.assertNotIn("password", fields)
        # The old single free-text "image" field is gone too.
        self.assertNotIn("image", fields)

    def test_no_field_is_marked_sensitive(self):
        # Credentials now live in the connection, so the trigger carries no secret.
        self.assertEqual(triggers.sensitive_keys(TypeDockerImage), {})


if __name__ == "__main__":
    unittest.main()
