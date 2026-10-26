"""Deployment container-spec persistence round-trip.

Guards the Config-tab editing path end to end at the store layer: a container
deployment's spec (env vars, port mappings, volumes and the advanced fields)
must survive create + update + read-back unchanged. The REST edit flow is a thin
wrapper over ``ContainerSpec.model_validate`` + ``store.update_deployment``, so a
faithful store round-trip is what proves "env etc." actually works.
"""

import os
import tempfile
import unittest

from kowan.models import (
    ContainerSpec,
    Deployment,
    KeyValue,
    PortMapping,
    VolumeMapping,
)
from kowan.models.deployment import DeploymentStrategyStandalone, DeploymentTypeContainer
from kowan.store import Store


def _fresh_store() -> Store:
    d = tempfile.mkdtemp()
    return Store(os.path.join(d, "t.db"))


def _spec(env: list[KeyValue]) -> ContainerSpec:
    return ContainerSpec(
        image="nginx:latest",
        env=env,
        ports=[PortMapping(hostPort=8080, containerPort=80, protocol="tcp")],
        volumes=[VolumeMapping(source="data", target="/var/www", readOnly=True)],
        networks=["frontend"],
        restartPolicy="unless-stopped",
        command=["nginx", "-g", "daemon off;"],
        workingDir="/app",
        user="1000:1000",
        memoryLimitMB=256,
        privileged=True,
        extraArgs=["--cap-add=NET_ADMIN"],
    )


class TestDeploymentSpecRoundTrip(unittest.TestCase):
    def _make(self, store: Store, env: list[KeyValue]) -> Deployment:
        dep = Deployment(
            id="d1",
            name="web",
            type=DeploymentTypeContainer,
            strategy=DeploymentStrategyStandalone,
            targets=["h1"],
            spec=_spec(env),
            status="stopped",
            createdAt="c",
            updatedAt="c",
        )
        store.create_deployment(dep)
        return dep

    def test_create_persists_full_spec(self):
        store = _fresh_store()
        self._make(store, [KeyValue(key="TZ", value="UTC")])

        got = store.get_deployment("d1")
        self.assertIsNotNone(got.spec)
        s = got.spec
        self.assertEqual(s.image, "nginx:latest")
        # Env survives with key + value intact.
        self.assertEqual([(e.key, e.value) for e in s.env], [("TZ", "UTC")])
        # Ports / volumes / advanced fields all round-trip.
        self.assertEqual(s.ports[0].host_port, 8080)
        self.assertEqual(s.ports[0].container_port, 80)
        self.assertEqual(s.volumes[0].source, "data")
        self.assertTrue(s.volumes[0].read_only)
        self.assertEqual(s.networks, ["frontend"])
        self.assertEqual(s.restart_policy, "unless-stopped")
        self.assertEqual(s.working_dir, "/app")
        self.assertEqual(s.user, "1000:1000")
        self.assertEqual(s.memory_limit_mb, 256)
        self.assertTrue(s.privileged)
        self.assertEqual(s.extra_args, ["--cap-add=NET_ADMIN"])

    def test_update_env_persists_the_edit(self):
        store = _fresh_store()
        dep = self._make(store, [KeyValue(key="TZ", value="UTC")])

        # Edit the env the way the Config tab does: replace the whole list.
        dep.spec.env = [
            KeyValue(key="TZ", value="Europe/Rome"),
            KeyValue(key="DEBUG", value="1"),
        ]
        store.update_deployment(dep)

        got = store.get_deployment("d1")
        self.assertEqual(
            [(e.key, e.value) for e in got.spec.env],
            [("TZ", "Europe/Rome"), ("DEBUG", "1")],
        )

    def test_clearing_env_persists_empty(self):
        store = _fresh_store()
        dep = self._make(store, [KeyValue(key="TZ", value="UTC")])

        dep.spec.env = []
        store.update_deployment(dep)

        self.assertEqual(store.get_deployment("d1").spec.env, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
