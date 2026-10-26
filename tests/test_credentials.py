"""Unit tests for the unified credential store.

Covers the per-kind round-trips (registry basic + cloud config, secret
interpolation, channel masking).
"""

import os
import tempfile
import unittest

from kowan import registry, secrets
from kowan.models import (
    Credential,
    CredentialKindRegistry,
    CredentialKindSecret,
)
from kowan.secrets import Cipher, load_or_create_key
from kowan.store import Store


class _RecordingExternal:
    """A stand-in external-secret resolver (vault/infisical/phase) that records the
    (type, data, field) it is called with, so a test can assert the merged data and
    the effective type reaching the backend. Returns a deterministic marker."""

    def __init__(self):
        self.calls = []

    def is_external(self, secret_type):
        return secret_type in ("vault", "infisical", "phase")

    def resolve(self, secret_type, data, field):
        self.calls.append((secret_type, dict(data), field))
        return f"RESOLVED:{data.get('path', '')}:{data.get('key', '')}"


def _sensitivity(kind, type_):
    if kind == CredentialKindRegistry:
        return registry.sensitive_keys(type_)
    if kind == CredentialKindSecret:
        return secrets.sensitive_keys(type_)
    # Notification channels: mark a plain webhook url sensitive for the test so we
    # don't need to import the whole notify provider registry here.
    return {"url": True}


class TestCredentialStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "t.db"))
        self.store.use_cipher(Cipher(load_or_create_key(self.dir, "")))
        self.store.set_credential_sensitivity(_sensitivity)

    def _mk(self, **kw):
        kw.setdefault("createdAt", "t")
        kw.setdefault("updatedAt", "t")
        return Credential(**kw)

    def test_registry_cloud_config_masked(self):
        cred = self._mk(
            id="r1", kind="registry", type="ecr", name="ecr",
            meta={"url": "123.ecr", "username": "", "insecure": False,
                  "verified": False, "lastError": "", "hasPassword": False},
        )
        self.store.create_credential(cred, {"region": "eu", "accessKeyId": "AK", "secretAccessKey": "SEK"})
        view = self.store.registry_view("r1")
        self.assertEqual(sorted(view.config), ["accessKeyId", "region", "secretAccessKey"])
        masked = self.store.credential_masked_config("r1")
        self.assertEqual(masked["accessKeyId"], "********")
        self.assertEqual(masked["region"], "eu")  # non-sensitive shown

    def test_registry_basic_password_preserved_on_blank_update(self):
        cred = self._mk(
            id="r2", kind="registry", type="generic", name="ghcr",
            meta={"url": "ghcr.io", "username": "me", "insecure": False,
                  "verified": False, "lastError": "", "hasPassword": True},
        )
        self.store.create_credential(cred, {"password": "plainpw"})
        self.assertEqual(self.store.registry_view("r2").password, "plainpw")
        self.assertEqual(self.store.get_credential("r2").config_keys, [])  # password hidden
        # A blank password on update keeps the stored one.
        cred.name = "ghcr2"
        self.store.update_registry_credential(cred, password="", config=None)
        self.assertEqual(self.store.registry_view("r2").password, "plainpw")
        self.assertEqual(self.store.get_credential("r2").name, "ghcr2")

    def test_registry_view_by_host_matches_canonical_host(self):
        """An image ref's bare host resolves the registry credential regardless of
        how the registry URL was entered (scheme / trailing slash / Hub alias) —
        so an ECR pull authenticates instead of falling back to anonymous."""
        ecr = self._mk(
            id="r-ecr", kind="registry", type="ecr", name="ecr",
            # URL entered WITH a scheme (as a user naturally would).
            meta={"url": "https://123.dkr.ecr.eu-west-1.amazonaws.com",
                  "username": "", "insecure": False, "verified": False,
                  "lastError": "", "hasPassword": False},
        )
        self.store.create_credential(
            ecr, {"region": "eu-west-1", "accessKeyId": "AK", "secretAccessKey": "SEK"}
        )
        # The image ref host is always bare — it must still match.
        view = self.store.registry_view_by_host("123.dkr.ecr.eu-west-1.amazonaws.com")
        self.assertEqual(view.id, "r-ecr")
        self.assertEqual(view.config["region"], "eu-west-1")

        # Docker Hub aliases fold together: a "docker.io" ref matches a registry
        # stored as "registry-1.docker.io" (and vice versa).
        hub = self._mk(
            id="r-hub", kind="registry", type="generic", name="hub",
            meta={"url": "registry-1.docker.io", "username": "me", "insecure": False,
                  "verified": False, "lastError": "", "hasPassword": True},
        )
        self.store.create_credential(hub, {"password": "pw"})
        self.assertEqual(self.store.registry_view_by_host("docker.io").id, "r-hub")

    def test_secret_interpolation(self):
        cred = self._mk(id="s1", kind="secret", type="generic", name="db",
                        meta={"description": "d"})
        self.store.create_credential(cred, {"value": "hunter2", "user": "admin"})
        self.assertEqual(self.store.resolve_secrets("[[db]] / [[db.user]]"), "hunter2 / admin")
        self.assertEqual(self.store.resolve_secrets("[[missing]]"), "[[missing]]")  # left as-is

    def test_secret_delegates_to_connection(self):
        """A secret can delegate its backend to a shared external-secret connection:
        many secret refs -> one Vault. The connection supplies the reusable backend
        (address/token); the secret supplies the ref-specific selectors (path/key);
        the provider is built from the CONNECTION's type."""
        fake = _RecordingExternal()
        self.store.set_external_secrets(fake)

        conn = self._mk(id="c1", kind="connection", type="vault", name="vault-prod", meta={})
        self.store.create_credential(conn, {"address": "https://vault:8200", "token": "s.tok"})
        # Two secrets, each pointing at the SAME connection, differing only in selector.
        db = self._mk(id="s1", kind="secret", type="vault", name="dbpass",
                      meta={"connectionRef": "c1"})
        self.store.create_credential(db, {"path": "app/db", "key": "password"})
        api = self._mk(id="s2", kind="secret", type="vault", name="apikey",
                       meta={"connectionRef": "c1"})
        self.store.create_credential(api, {"path": "app/api", "key": "token"})

        self.assertEqual(self.store.resolve_secrets("[[dbpass]]"), "RESOLVED:app/db:password")
        self.assertEqual(self.store.resolve_secrets("[[apikey]]"), "RESOLVED:app/api:token")

        # The resolver saw the connection's type + merged data (backend from the
        # connection, selectors from the secret).
        t, data, _ = fake.calls[0]
        self.assertEqual(t, "vault")                       # connection's type wins
        self.assertEqual(data["address"], "https://vault:8200")  # from connection
        self.assertEqual(data["token"], "s.tok")                 # from connection
        self.assertEqual(data["path"], "app/db")                 # from secret
        self.assertEqual(data["key"], "password")                # from secret

    def test_secret_delegation_missing_connection_falls_back(self):
        """A dangling connectionRef (connection deleted) degrades to the secret's own
        inline type/data rather than erroring."""
        self.store.set_external_secrets(_RecordingExternal())
        s = self._mk(id="s1", kind="secret", type="generic", name="db",
                     meta={"connectionRef": "gone"})
        self.store.create_credential(s, {"value": "inline-val"})
        self.assertEqual(self.store.resolve_secrets("[[db]]"), "inline-val")



if __name__ == "__main__":
    unittest.main()
