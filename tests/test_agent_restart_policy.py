"""The agent gives its own container the unless-stopped restart policy when it
has none (agents installed before the join command carried --restart), and
leaves a chosen policy, another container, and non-container runs alone."""

import os
import unittest
from unittest import mock

from kowan.agent import _keep_running_after_reboot

TOKEN = "t" * 40


class _Ops:
    def __init__(self, containers, fail_update=False):
        self.containers = containers
        self.updates = []
        self.fail_update = fail_update

    async def inspect_container(self, ref):
        if ref not in self.containers:
            raise RuntimeError("no such container")
        return self.containers[ref]

    async def update_container_config(self, cid, body):
        if self.fail_update:
            raise RuntimeError("permission denied")
        self.updates.append((cid, body))


def _ctr(cid, policy, token=TOKEN):
    return {"Id": cid, "Config": {"Env": [f"KOWAN_AGENT_TOKEN={token}", "KOWAN_AGENT_MODE=edge"]},
            "HostConfig": {"RestartPolicy": {"Name": policy}}}


class TestRestartPolicy(unittest.IsolatedAsyncioTestCase):
    def env(self, hostname="abc123"):
        return mock.patch.dict(os.environ, {"HOSTNAME": hostname, "KOWAN_AGENT_TOKEN": TOKEN})

    async def test_sets_unless_stopped_when_none(self):
        for policy in ("no", ""):
            ops = _Ops({"abc123": _ctr("abc123full", policy)})
            with self.env():
                await _keep_running_after_reboot(ops)
            self.assertEqual(ops.updates, [("abc123full", {"RestartPolicy": {"Name": "unless-stopped"}})])

    async def test_keeps_a_chosen_policy(self):
        for policy in ("always", "on-failure", "unless-stopped"):
            ops = _Ops({"abc123": _ctr("abc123full", policy)})
            with self.env():
                await _keep_running_after_reboot(ops)
            self.assertEqual(ops.updates, [], policy)

    async def test_only_its_own_container(self):
        # The hostname names another container (a custom --hostname): its
        # token differs, so it is left alone.
        ops = _Ops({"abc123": _ctr("other", "no", token="x" * 40)})
        with self.env():
            await _keep_running_after_reboot(ops)
        self.assertEqual(ops.updates, [])

    async def test_outside_a_container_or_without_a_token(self):
        ops = _Ops({})
        with self.env():
            await _keep_running_after_reboot(ops)  # inspect fails: nothing happens
        with mock.patch.dict(os.environ, {"HOSTNAME": "abc123", "KOWAN_AGENT_TOKEN": ""}):
            await _keep_running_after_reboot(_Ops({"abc123": _ctr("abc123full", "no")}))
        self.assertEqual(ops.updates, [])

    async def test_a_failed_update_does_not_stop_the_agent(self):
        ops = _Ops({"abc123": _ctr("abc123full", "no")}, fail_update=True)
        with self.env(), self.assertLogs("kowan.agent", "WARNING"):
            await _keep_running_after_reboot(ops)


if __name__ == "__main__":
    unittest.main()
