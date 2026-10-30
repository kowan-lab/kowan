"""Containers stopped on purpose are not crashes: one that exited with an error
code although its restart policy (always/unless-stopped) would have restarted
a crash was stopped (docker stop); ``npm start`` exits 1 on SIGTERM. The list
marks them ``manuallyStopped`` and the problem checks leave them alone."""

import asyncio
import unittest

from kowan.docker.local import LocalClient
from kowan.mcp.tools.workloads import _problem
from kowan.models import Container


def _row(cid, state, status):
    return {"Id": cid, "Names": [f"/{cid}"], "Image": "x", "State": state, "Status": status, "Labels": {}}


class _StubLocal(LocalClient):
    """LocalClient with the Engine stubbed: a fixed list, policies per id."""

    def __init__(self, rows, policies):
        super().__init__("/nonexistent.sock")
        self.rows, self.policies, self.inspected = rows, policies, []

    async def _request_json(self, method, path, *, params=None, json_body=None):
        return self.rows

    async def inspect_container(self, id):
        self.inspected.append(id)
        if id not in self.policies:
            raise RuntimeError("gone")
        return {"HostConfig": {"RestartPolicy": {"Name": self.policies[id]}}}


class TestManualStops(unittest.TestCase):
    def test_list_marks_stops_and_inspects_each_once(self):
        rows = [
            _row("npm", "exited", "Exited (1) 3 hours ago"),        # unless-stopped: a stop
            _row("always", "exited", "Exited (143) 1 hour ago"),     # always: a stop
            _row("crash", "exited", "Exited (1) 2 minutes ago"),     # no policy: a crash
            _row("onfail", "exited", "Exited (2) 5 minutes ago"),    # on-failure gave up: a crash
            _row("clean", "exited", "Exited (0) 1 day ago"),         # exit 0: never inspected
            _row("up", "running", "Up 2 hours"),
            _row("gone", "exited", "Exited (1) 1 minute ago"),       # inspect fails: stays a problem
        ]
        c = _StubLocal(rows, {"npm": "unless-stopped", "always": "always", "crash": "no", "onfail": "on-failure"})
        got = {x.id: x.manually_stopped for x in asyncio.run(c.list_containers(True))}
        self.assertEqual(got, {"npm": True, "always": True, "crash": False, "onfail": False,
                               "clean": False, "up": False, "gone": False})
        self.assertEqual(sorted(c.inspected), ["always", "crash", "gone", "npm", "onfail"])

        # The next list reuses the answers (only the failed inspect is retried);
        # a container that started again drops out, and is asked again later.
        c.inspected.clear()
        asyncio.run(c.list_containers(True))
        self.assertEqual(c.inspected, ["gone"])
        c.rows = [_row("npm", "running", "Up 1 second")]
        asyncio.run(c.list_containers(True))
        c.rows = [_row("npm", "exited", "Exited (1) 1 second ago")]
        c.policies["npm"] = "no"
        c.inspected.clear()
        [npm] = asyncio.run(c.list_containers(True))
        self.assertEqual((c.inspected, npm.manually_stopped), (["npm"], False))

    def test_problem_checks_skip_stops(self):
        stopped = Container(id="a", state="exited", status="Exited (1) 1 hour ago", manuallyStopped=True)
        crashed = Container(id="b", state="exited", status="Exited (1) 1 hour ago")
        self.assertFalse(_problem(stopped))
        self.assertTrue(_problem(crashed))
        self.assertTrue(Container.model_validate(stopped.model_dump(by_alias=True)).manually_stopped)  # over the tunnel


if __name__ == "__main__":
    unittest.main()
