"""Unit tests for registry repository/tag browsing (offline, mocked I/O).

Covers the browse rework that made cloud registries listable:

  * per-provider dispatch — generic/ghcr/gcp fall back to the Docker Registry v2
    API (``/v2/_catalog`` + ``/v2/{repo}/tags/list``), while ECR and ACR use their
    cloud-native listing APIs (ECR has no ``_catalog``);
  * ECR/ACR pagination and tag extraction;
  * the resolution fix: the browse endpoints resolve a registry through the
    capability dispatch (``resolve_service``, which follows ``connectionRef``), so a
    registry backed by a shared cloud connection browses with THAT connection's
    credentials — the old ``registry_view`` path ignored the connection and built a
    credential-less generic provider that could never see a cloud registry.

No SQLite / no network / no HTTP client: a tiny fake store plus mocked provider I/O.
"""

import asyncio
import unittest
from unittest import mock

from kowan.models import Registry
from kowan.registry.basic import BasicProvider
from kowan.registry.ecr import ECRProvider
from kowan.registry.acr import ACRProvider
from kowan.registry.gcp import GCPProvider


def _run(coro):
    return asyncio.run(coro)


class TestECRBrowse(unittest.TestCase):
    """ECR lists via the signed ECR API (DescribeRepositories / ListImages), not
    the v2 catalog which ECR does not implement."""

    def _provider(self):
        return ECRProvider(
            Registry(
                type="ecr",
                url="https://123.dkr.ecr.eu-west-1.amazonaws.com",
                config={
                    "region": "eu-west-1",
                    "accessKeyId": "AKIA",
                    "secretAccessKey": "shh",
                },
            )
        )

    def test_list_repositories_paginates(self):
        p = self._provider()
        pages = [
            {"repositories": [{"repositoryName": "a"}, {"repositoryName": "b"}], "nextToken": "n2"},
            {"repositories": [{"repositoryName": "c"}]},  # no nextToken -> stop
        ]
        with mock.patch.object(
            ECRProvider, "_ecr_call", new=mock.AsyncMock(side_effect=pages)
        ) as call:
            repos = _run(p.list_repositories())
        self.assertEqual(repos, ["a", "b", "c"])
        # First page has no nextToken; second page forwards it.
        self.assertEqual(call.await_args_list[0].args[0], "DescribeRepositories")
        self.assertNotIn("nextToken", call.await_args_list[0].args[1])
        self.assertEqual(call.await_args_list[1].args[1].get("nextToken"), "n2")

    def test_list_tags_skips_untagged_and_paginates(self):
        p = self._provider()
        pages = [
            {
                "imageIds": [
                    {"imageTag": "v1"},
                    {"imageDigest": "sha256:deadbeef"},  # untagged -> skipped
                    {"imageTag": "v2"},
                ],
                "nextToken": "n2",
            },
            {"imageIds": [{"imageTag": "v3"}]},
        ]
        with mock.patch.object(
            ECRProvider, "_ecr_call", new=mock.AsyncMock(side_effect=pages)
        ) as call:
            tags = _run(p.list_tags("team/app"))
        self.assertEqual(tags, ["v1", "v2", "v3"])
        self.assertEqual(call.await_args_list[0].args[0], "ListImages")
        self.assertEqual(call.await_args_list[0].args[1]["repositoryName"], "team/app")

    def test_list_tags_rejects_empty_repository(self):
        with self.assertRaises(ValueError):
            _run(self._provider().list_tags("   "))


class TestACRBrowse(unittest.TestCase):
    """ACR speaks v2 but with a per-scope Bearer access token minted from the
    refresh-token exchange — not the generic Basic-realm handshake."""

    def _provider(self):
        return ACRProvider(
            Registry(
                type="acr",
                url="https://myreg.azurecr.io",
                config={
                    "tenantId": "t",
                    "clientId": "c",
                    "clientSecret": "s",
                },
            )
        )

    def test_list_repositories_uses_catalog_scope(self):
        p = self._provider()
        with mock.patch.object(
            ACRProvider, "_access_token", new=mock.AsyncMock(return_value="acc")
        ) as at, mock.patch(
            "kowan.registry.acr._bearer_get_json",
            new=mock.AsyncMock(return_value={"repositories": ["x/y", "z"]}),
        ) as get:
            repos = _run(p.list_repositories())
        self.assertEqual(repos, ["x/y", "z"])
        # host + catalog scope, Bearer token, correct v2 URL.
        self.assertEqual(at.await_args.args, ("myreg.azurecr.io", "registry:catalog:*"))
        self.assertEqual(get.await_args.args[0], "https://myreg.azurecr.io/v2/_catalog")
        self.assertEqual(get.await_args.args[1], "acc")

    def test_list_tags_uses_repo_scope(self):
        p = self._provider()
        with mock.patch.object(
            ACRProvider, "_access_token", new=mock.AsyncMock(return_value="acc")
        ) as at, mock.patch(
            "kowan.registry.acr._bearer_get_json",
            new=mock.AsyncMock(return_value={"tags": ["1", "2"]}),
        ) as get:
            tags = _run(p.list_tags("team/app"))
        self.assertEqual(tags, ["1", "2"])
        self.assertEqual(
            at.await_args.args, ("myreg.azurecr.io", "repository:team/app:metadata_read")
        )
        self.assertEqual(
            get.await_args.args[0], "https://myreg.azurecr.io/v2/team/app/tags/list"
        )


class TestV2DefaultDispatch(unittest.TestCase):
    """generic/ghcr/gcp inherit the v2 catalog/tags default; ECR/ACR override it."""

    def test_basic_provider_calls_v2_with_credentials_and_insecure(self):
        p = BasicProvider(
            Registry(type="generic", url="https://reg.local", username="u", password="p", insecure=True)
        )
        with mock.patch(
            "kowan.registry.browse.v2_repositories",
            new=mock.AsyncMock(return_value=["r1"]),
        ) as v2:
            repos = _run(p.list_repositories())
        self.assertEqual(repos, ["r1"])
        # (username, password, server, insecure)
        self.assertEqual(v2.await_args.args, ("u", "p", "https://reg.local", True))

    def test_provider_dispatch_table(self):
        # Documents the intended override/inherit split so a regression is loud.
        self.assertIs(BasicProvider.list_repositories, GCPProvider.list_repositories)
        self.assertIsNot(ECRProvider.list_repositories, BasicProvider.list_repositories)
        self.assertIsNot(ACRProvider.list_repositories, BasicProvider.list_repositories)


class _FakeCred:
    def __init__(self, ctype, name, meta):
        self.type = ctype
        self.name = name
        self.meta = meta


class _FakeStore:
    def __init__(self, creds, configs):
        self._creds = creds
        self._configs = configs

    def get_credential(self, cid):
        return self._creds[cid]

    def credential_config(self, cid):
        return dict(self._configs.get(cid, {}))


class _FakeSvc:
    def __init__(self, store):
        self.store = store


class TestBrowseResolution(unittest.TestCase):
    """The browse path must resolve a registry through the connection dispatch, so a
    cloud-connection-backed registry builds the RIGHT provider with the connection's
    credentials — the historical bug built a generic, credential-less provider."""

    def test_connection_backed_registry_resolves_to_ecr(self):
        from kowan.api import credentials as creds_api

        store = _FakeStore(
            creds={
                # A registry stored as generic + a connectionRef (how the UI creates
                # a cloud registry: auth lives in the referenced connection).
                "reg1": _FakeCred(
                    "generic",
                    "prod-ecr",
                    {
                        "url": "https://123.dkr.ecr.eu-west-1.amazonaws.com",
                        "connectionRef": "conn1",
                        "insecure": False,
                    },
                ),
                "conn1": _FakeCred("aws", "aws-prod", {}),
            },
            configs={
                "conn1": {
                    "region": "eu-west-1",
                    "accessKeyId": "AKIA",
                    "secretAccessKey": "shh",
                }
            },
        )
        provider = _run(creds_api._browse_provider(_FakeSvc(store), "reg1"))
        # Following connectionRef -> aws connection -> ECR provider bound to region.
        self.assertIsInstance(provider, ECRProvider)
        self.assertEqual(provider._region, "eu-west-1")


if __name__ == "__main__":
    unittest.main()
