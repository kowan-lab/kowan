"""Tests for the capability manifest (Phase 0 of the connection model)."""

import unittest

from kowan import capabilities as cap


class TestCapabilities(unittest.TestCase):
    def test_registry_types_declare_docker_auth(self):
        for t in ("generic", "ghcr", "ecr", "gcp", "acr"):
            self.assertTrue(cap.implements(t, cap.CAP_DOCKER_REGISTRY_AUTH), t)

    def test_secret_backends_declare_secret_fetch(self):
        for t in ("vault", "infisical", "phase"):
            self.assertTrue(cap.implements(t, cap.CAP_SECRET_FETCH), t)

    def test_channels_declare_message_send(self):
        for t in ("smtp", "webhook", "ntfy", "telegram", "slack", "discord"):
            self.assertTrue(cap.implements(t, cap.CAP_MESSAGE_SEND), t)

    def test_native_git_types_declare_git_fetch(self):
        for t in ("git-http", "github", "gitlab", "bitbucket"):
            self.assertTrue(cap.implements(t, cap.CAP_GIT_FETCH), t)
            self.assertTrue(cap.implements(t, cap.CAP_HTTP_BEARER), t)
            # No gitApi builder/consumer yet -> must NOT be advertised.
            self.assertFalse(cap.implements(t, cap.CAP_GIT_API), t)

    def test_generic_http_auth_types(self):
        self.assertEqual(cap.capabilities_of("api-token"), frozenset({cap.CAP_HTTP_BEARER}))
        self.assertEqual(cap.capabilities_of("basic-auth"), frozenset({cap.CAP_HTTP_BEARER}))

    def test_static_secret_types_are_not_connections(self):
        # generic/git/env/token static secrets implement nothing (opaque values).
        for t in ("git", "env", "token"):
            self.assertEqual(cap.capabilities_of(t), frozenset(), t)

    def test_cloud_identity_derives_registry_auth(self):
        # A cloud identity (aws/azure/gcp) derives docker registry auth today
        # (ECR/ACR/GCR).
        self.assertTrue(cap.implements("aws", cap.CAP_DOCKER_REGISTRY_AUTH))
        # secretFetch / messageSend / objectStore are future cloud facets: not yet
        # wired, so NOT advertised — keeps the capability pickers honest.
        self.assertFalse(cap.implements("aws", cap.CAP_SECRET_FETCH))
        self.assertFalse(cap.implements("aws", cap.CAP_OBJECT_STORE))
        self.assertFalse(cap.implements("aws", cap.CAP_SSH_ACCESS))

    def test_effective_capabilities_includes_cloud_derivation(self):
        # The catalog/filter view of a cloud type includes its derived leaves.
        eff = cap.effective_capabilities("aws")
        self.assertIn(cap.CAP_CLOUD_IDENTITY, eff)
        self.assertIn(cap.CAP_DOCKER_REGISTRY_AUTH, eff)
        self.assertNotIn(cap.CAP_SECRET_FETCH, eff)  # not wired yet
        # A non-cloud type's effective set equals its manifest set.
        self.assertEqual(
            cap.effective_capabilities("vault"), cap.capabilities_of("vault")
        )


if __name__ == "__main__":
    unittest.main()
