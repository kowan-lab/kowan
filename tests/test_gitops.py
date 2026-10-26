"""GitOps: resource parsing/validation, diff computation (incl. managed-only
deletes, adoption, conflicts), apply ordering, export round trip, and the REST
flow against a local bare git repository served over file:// (tests only)."""

import asyncio
import importlib
import json
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from unittest import mock

from kowan.deployment import gitsource
from kowan.gitops import parse_documents, scan_tree
from kowan.gitops.diff import field_changes, line_diff
from kowan.gitops.resources import read_repo_file

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
HAS_GIT = shutil.which("git") is not None


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, env=GIT_ENV,
                          capture_output=True, text=True).stdout.strip()


class TestParsing(unittest.TestCase):
    def test_documents(self):
        text = """apiVersion: kowan/v1
kind: Deployment
metadata: {name: web, labels: {team: a}}
spec: {host: local}
---
services:            # a compose file: not a Kowan resource, ignored
  x: {image: y}
---
apiVersion: kowan/v2
kind: Deployment
metadata: {name: bad}
---
apiVersion: kowan/v1
kind: Workflow
metadata: {}
---
apiVersion: kowan/v1
kind: Workflow
metadata: {name: w}
spec: [1]
---
apiVersion: kowan/v1
kind: Workflow
metadata: {name: w2}
status: {}
"""
        res, errs = parse_documents(text, "a.yaml")
        self.assertEqual([(r.kind, r.name, r.labels) for r in res], [("Deployment", "web", {"team": "a"})])
        self.assertEqual([e.doc for e in errs], [2, 3, 4, 5])
        self.assertIn("apiVersion", errs[0].message)
        self.assertIn("metadata.name", errs[1].message)
        self.assertIn("spec must be a mapping", errs[2].message)
        self.assertIn("unknown top-level", errs[3].message)
        _, errs = parse_documents("a: [\n", "b.yaml")
        self.assertIn("invalid YAML", errs[0].message)

    def test_scan_tree(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "res", "sub"))
        os.makedirs(os.path.join(tmp, "res", ".hidden"))
        doc = "apiVersion: kowan/v1\nkind: AlertRule\nmetadata: {name: %s}\nspec: {type: cpu_high}\n"
        _write(os.path.join(tmp, "res", "a.yaml"), doc % "x")
        _write(os.path.join(tmp, "res", "sub", "b.yml"), doc % "x")
        _write(os.path.join(tmp, "res", ".hidden", "c.yaml"), doc % "y")
        _write(os.path.join(tmp, "res", "notes.txt"), "x")
        res, errs = scan_tree(tmp, "res")
        self.assertEqual(len(res), 2)
        self.assertEqual(len(errs), 1)
        self.assertIn("duplicate AlertRule 'x'", errs[0].message)
        self.assertIn("not found", scan_tree(tmp, "nope")[1][0].message)
        self.assertIn("inside", scan_tree(tmp, "../etc")[1][0].message)
        _write(os.path.join(tmp, "res", "c.yml"), "services: {}\n")
        self.assertEqual(read_repo_file(tmp, "res/a.yaml", "c.yml"), "services: {}\n")
        self.assertEqual(read_repo_file(tmp, "res/sub/b.yml", "/res/c.yml"), "services: {}\n")
        with self.assertRaises(ValueError):
            read_repo_file(tmp, "res/a.yaml", "../../../etc/passwd")


class TestDiff(unittest.TestCase):
    def test_field_changes(self):
        before = {"a": 1, "b": {"c": [1, 2], "d": "x"}, "gone": True, "l": [1]}
        after = {"a": 1, "b": {"c": [1, 3], "d": "x"}, "new": "n", "l": [1, 2]}
        self.assertEqual(field_changes(before, after), [
            {"path": "b.c[1]", "before": 2, "after": 3},
            {"path": "gone", "before": True, "after": None},
            {"path": "l", "before": [1], "after": [1, 2]},
            {"path": "new", "before": None, "after": "n"},
        ])
        self.assertEqual(field_changes({"a": 1}, {"a": 1}), [])

    def test_line_diff(self):
        lines = line_diff("Deployment", "web", {"host": "local", "compose": "a\nb\n"},
                          {"host": "local", "compose": "a\nc\n"})
        ops = [(l["op"], l["text"].strip()) for l in lines if l["op"] != " "]
        self.assertEqual(ops, [("-", "b"), ("+", "c")])
        self.assertTrue(all(l["op"] == "+" for l in line_diff("X", "n", None, {"a": 1})))
        self.assertTrue(all(l["op"] == "-" for l in line_diff("X", "n", {"a": 1}, None)))


DEPLOY = """apiVersion: kowan/v1
kind: Deployment
metadata: {name: web}
spec:
  host: local
  composeFile: stack/compose.yml
  policies: {deploy: never}
"""
COMPOSE = "services:\n  web:\n    image: nginx:1.27\n    restart: unless-stopped\n"
CHANNEL = """apiVersion: kowan/v1
kind: NotificationChannel
metadata: {name: ops}
spec:
  type: slack
  config: {url: "[[slack-url]]"}
"""
RULE = """apiVersion: kowan/v1
kind: AlertRule
metadata: {name: cpu}
spec: {type: cpu_high, threshold: 90, severity: warning, channel: ops}
"""
WORKFLOW = """apiVersion: kowan/v1
kind: Workflow
metadata: {name: nightly}
spec:
  enabled: true
  steps:
    - {name: redeploy, type: deploy, config: {action: redeploy, deployment: web}}
    - {name: tell, type: notify, config: {channel: ops, title: done}}
  triggers:
    - {type: cron, config: {cron: "0 3 * * *"}}
    - {type: webhook, config: {}}
"""
REGISTRY = """apiVersion: kowan/v1
kind: Registry
metadata: {name: ghcr}
spec: {url: ghcr.io, username: bot}
"""
TEMPLATE = """apiVersion: kowan/v1
kind: Template
metadata: {name: hello}
spec:
  type: compose
  compose: |
    services:
      a: {image: "hello:${TAG}"}
  inputs: [{name: TAG, default: "1"}]
"""


@unittest.skipUnless(HAS_GIT, "git not installed")
class TestGitOpsFlow(unittest.IsolatedAsyncioTestCase):
    """Full REST flow against a bare repository over file:// (tests only)."""

    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer

        import kowan.api.gitops as gapi
        from kowan.server import build_app
        from tests.test_integration import _cfg

        self.tmp = tempfile.mkdtemp()
        self.bare = os.path.join(self.tmp, "repo.git")
        self.work = os.path.join(self.tmp, "work")
        git(self.tmp, "init", "-q", "--bare", "-b", "main", self.bare)
        git(self.tmp, "clone", "-q", self.bare, self.work)
        git(self.work, "checkout", "-q", "-b", "main")
        self.url = "file://" + self.bare
        self._patches = [mock.patch.object(gitsource, "ALLOWED_PROTOCOLS", "file"),
                         mock.patch.object(gapi, "validate_url", lambda u: u)]
        for p in self._patches:
            p.start()
        app, self.svc = await build_app(_cfg())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        r = await self.client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        self.H = {"Authorization": "Bearer " + (await r.json())["token"]}
        self.dispatched = []

        async def fake_dispatch(dep_id, *, redeploy=False):
            self.dispatched.append((dep_id, redeploy))
            return self.svc.store.get_deployment(dep_id)

        async def fake_destroy(dep_id):
            return None

        self.svc.deployments.dispatch_deploy = fake_dispatch
        self.svc.deployments.destroy = fake_destroy
        # A secret for the channel reference.
        r = await self.client.post("/api/v1/credentials", headers=self.H, json={
            "kind": "secret", "name": "slack-url", "fields": {"value": "https://hooks.slack.test/x"}})
        self.assertEqual(r.status, 201, await r.text())

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        path = os.path.join(self.work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def commit(self, msg="c"):
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", msg)
        git(self.work, "push", "-q", "origin", "main")
        return git(self.work, "rev-parse", "HEAD")

    async def call(self, method, path, **kw):
        r = await self.client.request(method, "/api/v1" + path, headers=self.H, **kw)
        ct = r.headers.get("Content-Type", "")
        return r.status, (await r.json() if "json" in ct else await r.read())

    async def make_sync(self, **kw):
        body = {"name": "main", "repoUrl": self.url, "path": "kowan"}
        body.update(kw)
        st, s = await self.call("POST", "/gitops/syncs", json=body)
        self.assertEqual(st, 201, s)
        self.assertTrue(s["webhookSecret"])
        return s

    async def test_full_cycle(self):
        self.write("kowan/deploy.yaml", DEPLOY)
        self.write("kowan/stack/compose.yml", COMPOSE)
        self.write("kowan/ops.yaml", "\n---\n".join([CHANNEL, RULE, WORKFLOW, REGISTRY, TEMPLATE]))
        head = self.commit()
        s = await self.make_sync()
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/refresh")
        self.assertEqual(st, 200, j)
        self.assertEqual(j["plan"]["errors"], [])
        self.assertEqual((j["status"], j["lastCommit"]), ("drift", head))
        self.assertEqual(j["plan"]["summary"]["create"], 6)
        kinds = [c["kind"] for c in j["plan"]["changes"]]
        # Dependency order: registry, channel, template, deployment, rule, workflow.
        self.assertEqual(kinds, ["Registry", "NotificationChannel", "Template", "Deployment", "AlertRule", "Workflow"])

        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/apply", json={"commit": head})
        self.assertEqual(st, 200, j)
        self.assertEqual(j["run"]["status"], "applied", j["run"]["result"])
        self.assertEqual(j["sync"]["status"], "in_sync")
        store = self.svc.store
        dep = store.get_deployment_by_name("web")
        self.assertEqual(dep.compose, COMPOSE)
        self.assertEqual(dep.targets, ["local"])
        self.assertEqual(self.dispatched, [])  # policies.deploy: never
        rule = next(r for r in store.list_alert_rules() if r.name == "cpu")
        chan = next(c for c in store.list_credentials("channel") if c.name == "ops")
        self.assertEqual(rule.channel_id, chan.id)
        self.assertEqual(store.credential_config(chan.id)["url"], "https://hooks.slack.test/x")
        self.assertEqual(chan.meta["gitopsRefs"], {"url": "[[slack-url]]"})
        wf = next(w for w in store.list_workflows() if w.name == "nightly")
        self.assertEqual(wf.steps[1].config["channel"], chan.id)
        token = wf.triggers[1].config["token"]
        self.assertTrue(token)
        st, managed = await self.call("GET", f"/gitops/syncs/{s['id']}/managed")
        self.assertEqual(len(managed), 6)

        # Drift from the UI side is detected too.
        dep.compose = COMPOSE.replace("1.27", "1.26")
        store.update_deployment(dep)
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/refresh")
        self.assertEqual(j["status"], "drift")
        ch = [c for c in j["plan"]["changes"] if c["action"] == "update"]
        self.assertEqual([c["name"] for c in ch], ["web"])
        self.assertTrue(any(l["op"] == "-" and "1.26" in l["text"] for l in ch[0]["diff"]))

        # Change the repo: bump the image, drop the template, edit the workflow.
        self.write("kowan/stack/compose.yml", COMPOSE.replace("1.27", "1.28"))
        self.write("kowan/ops.yaml", "\n---\n".join([CHANNEL, RULE.replace("90", "95"),
                                                    WORKFLOW.replace("0 3 * * *", "0 4 * * *"), REGISTRY]))
        head2 = self.commit("bump")
        # A stale approval (old commit) is refused.
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/refresh")
        acts = {(c["kind"], c["name"]): c["action"] for c in j["plan"]["changes"]}
        self.assertEqual(acts[("Template", "hello")], "delete")
        self.assertEqual(acts[("AlertRule", "cpu")], "update")
        self.assertEqual(acts[("Deployment", "web")], "update")
        rule_change = next(c for c in j["plan"]["changes"] if c["name"] == "cpu")
        self.assertEqual(rule_change["fields"], [{"path": "threshold", "before": 90.0, "after": 95.0}])
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/apply", json={"commit": head})
        self.assertEqual(st, 409)
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/apply", json={"commit": head2})
        self.assertEqual(j["run"]["status"], "applied", j["run"]["result"])
        self.assertEqual([r for r in store.list_templates() if r.name == "hello"], [])
        self.assertIn("1.28", store.get_deployment_by_name("web").compose)
        wf = next(w for w in store.list_workflows() if w.name == "nightly")
        self.assertEqual(wf.triggers[0].config["cron"], "0 4 * * *")
        self.assertEqual(wf.triggers[1].config["token"], token)  # secret kept

        # Runs recorded, newest first, with plan + result.
        st, runs = await self.call("GET", f"/gitops/syncs/{s['id']}/runs")
        self.assertEqual([r["action"] for r in runs[:2]], ["apply", "refresh"])
        st, run = await self.call("GET", f"/gitops/syncs/{s['id']}/runs/{runs[0]['id']}")
        self.assertTrue(run["result"])

        # Export round trip: committing the export yields an all-noop plan.
        st, ex = await self.call("POST", "/gitops/export", json={"format": "json"})
        self.assertEqual(st, 200)
        paths = sorted(f["path"] for f in ex["files"])
        self.assertIn("deployments/web.yaml", paths)
        self.assertIn("channels/ops.yaml", paths)
        exported = "\n".join(f["content"] for f in ex["files"])
        self.assertNotIn("hooks.slack.test", exported)  # no secrets in git
        self.assertNotIn(token, exported)
        shutil.rmtree(os.path.join(self.work, "kowan"))
        for f in ex["files"]:
            self.write(os.path.join("kowan", f["path"]), f["content"])
        head3 = self.commit("export")
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/refresh")
        self.assertEqual(j["plan"]["errors"], [])
        not_noop = [(c["kind"], c["name"], c["action"], c["fields"]) for c in j["plan"]["changes"]
                    if c["action"] != "noop" and not c.get("adopt")]
        self.assertEqual(not_noop, [])

        # Zip export.
        st, data = await self.call("POST", "/gitops/export", json={"resources": [{"kind": "Deployment", "id": dep.id}]})
        self.assertEqual(st, 200)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertEqual(names, ["kowan-export/deployments/web.yaml"])

        # Push export to a branch of the sync repository.
        st, out = await self.call("POST", "/gitops/export", json={
            "push": {"syncId": s["id"], "branch": "kowan/export", "message": "export"}})
        self.assertEqual(st, 200, out)
        self.assertEqual(out["branch"], "kowan/export")
        listed = git(self.bare, "ls-tree", "-r", "--name-only", "kowan/export")
        self.assertIn("kowan/deployments/web.yaml", listed)
        self.assertNotEqual(head3, "")

    async def test_adopt_conflict_prune_and_protection(self):
        store = self.svc.store
        # An existing unmanaged rule with the same name is adopted.
        st, _ = await self.call("POST", "/alert-rules", json={"name": "cpu", "type": "cpu_high", "threshold": 80})
        self.assertEqual(st, 201)
        self.write("kowan/r.yaml", RULE.replace(", channel: ops", ""))
        self.write("kowan/d.yaml", DEPLOY.replace("composeFile: stack/compose.yml",
                                                 "protected: true\n  compose: |\n    services:\n      a: {image: 'x:1', restart: always}"))
        self.commit()
        a = await self.make_sync(prune=True)
        st, j = await self.call("POST", f"/gitops/syncs/{a['id']}/refresh")
        cpu = next(c for c in j["plan"]["changes"] if c["name"] == "cpu")
        self.assertEqual((cpu["action"], cpu["adopt"]), ("update", True))
        st, j = await self.call("POST", f"/gitops/syncs/{a['id']}/apply")
        self.assertEqual(j["run"]["status"], "applied", j["run"]["result"])

        # A second sync claiming the same resource is a conflict error.
        b = await self.make_sync(name="other")
        st, j = await self.call("POST", f"/gitops/syncs/{b['id']}/refresh")
        self.assertEqual(j["status"], "error")
        self.assertTrue(any("managed by another sync" in e["message"] for e in j["plan"]["errors"]))
        st, j = await self.call("POST", f"/gitops/syncs/{b['id']}/apply")
        self.assertEqual(st, 400)
        await self.call("DELETE", f"/gitops/syncs/{b['id']}")

        # Unmanaged resources are never deleted; managed ones are, unless protected.
        st, _ = await self.call("POST", "/alert-rules", json={"name": "unmanaged", "type": "cpu_high"})
        os.remove(os.path.join(self.work, "kowan", "r.yaml"))
        os.remove(os.path.join(self.work, "kowan", "d.yaml"))
        self.write("kowan/keep.yaml", REGISTRY)
        self.commit("remove")
        st, j = await self.call("POST", f"/gitops/syncs/{a['id']}/refresh")
        acts = {c["name"]: (c["action"], c.get("blocked", "")) for c in j["plan"]["changes"]}
        self.assertEqual(acts["cpu"], ("delete", ""))
        self.assertEqual(acts["web"][0], "delete")
        self.assertIn("protection", acts["web"][1])
        self.assertNotIn("unmanaged", acts)
        st, j = await self.call("POST", f"/gitops/syncs/{a['id']}/apply")
        results = {r["name"]: r["status"] for r in j["run"]["result"]}
        self.assertEqual((results["cpu"], results["web"]), ("ok", "skipped"))
        names = [r.name for r in store.list_alert_rules()]
        self.assertIn("unmanaged", names)
        self.assertNotIn("cpu", names)
        store.get_deployment_by_name("web")  # protected: still there

        # prune off: removed resources become orphans and are released.
        st, _ = await self.call("PUT", f"/gitops/syncs/{a['id']}", json={"prune": False})
        os.remove(os.path.join(self.work, "kowan", "keep.yaml"))
        self.write("kowan/empty.yaml", "# nothing\n")
        self.commit("orphan")
        st, j = await self.call("POST", f"/gitops/syncs/{a['id']}/refresh")
        acts = {c["name"]: c["action"] for c in j["plan"]["changes"]}
        self.assertEqual(acts["ghcr"], "orphan")
        await self.call("POST", f"/gitops/syncs/{a['id']}/apply")
        self.assertIn("ghcr", [c.name for c in store.list_credentials("registry")])
        st, managed = await self.call("GET", f"/gitops/syncs/{a['id']}/managed")
        self.assertNotIn("ghcr", [m["name"] for m in managed])

    async def test_validation_errors_block_apply(self):
        self.write("kowan/bad.yaml", "\n---\n".join([
            "apiVersion: kowan/v1\nkind: Deployment\nmetadata: {name: a}\nspec: {host: nohost, compose: 'services: {}'}\n",
            "apiVersion: kowan/v1\nkind: Deployment\nmetadata: {name: b}\nspec:\n  host: local\n  compose: |\n"
            "    services:\n      x: {image: 'y:1', depends_on: [ghost]}\n",
            "apiVersion: kowan/v1\nkind: NotificationChannel\nmetadata: {name: c}\nspec: {type: slack, config: {url: 'https://literal'}}\n",
            "apiVersion: kowan/v1\nkind: Registry\nmetadata: {name: r}\nspec: {url: x.io, password: hunter2}\n",
            "apiVersion: kowan/v1\nkind: Gadget\nmetadata: {name: g}\nspec: {}\n",
            "apiVersion: kowan/v1\nkind: AlertRule\nmetadata: {name: ar}\nspec: {type: nope}\n",
            "apiVersion: kowan/v1\nkind: Workflow\nmetadata: {name: w}\nspec: {steps: [{name: s, type: bogus}]}\n",
            "apiVersion: kowan/v1\nkind: Deployment\nmetadata: {name: e}\nspec: {host: local, compose: 'services: {}', extra: 1}\n",
        ]))
        self.commit()
        s = await self.make_sync()
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/refresh")
        self.assertEqual(j["status"], "error")
        msgs = {e["name"]: e["message"] for e in j["plan"]["errors"]}
        self.assertIn("unknown host", msgs["a"])
        self.assertIn("ghost", msgs["b"])
        self.assertIn("reference a Kowan secret", msgs["c"])
        self.assertIn("password must not be stored", msgs["r"])
        self.assertIn("invalid rule type", msgs["ar"])
        self.assertIn("unknown kind", msgs["g"])
        self.assertIn("step 1", msgs["w"])
        self.assertIn("unknown spec field", msgs["e"])
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/apply")
        self.assertEqual(st, 400)
        self.assertEqual(self.svc.store.list_deployments(), [])

    async def test_webhook_and_interval(self):
        self.write("kowan/r.yaml", RULE.replace(", channel: ops", ""))
        self.commit()
        s = await self.make_sync(autoApply=True, intervalSec=60)
        hook = "/hooks/gitops/" + s["webhookUrl"].rsplit("/", 1)[-1]
        payload = b'{"ref":"refs/heads/main","after":"' + b"a" * 40 + b'","commits":[{"added":[],"modified":["kowan/r.yaml"],"removed":[]}]}'
        bad = {"X-GitHub-Event": "push", "X-Hub-Signature-256": gitsource.sign_body("wrong", payload)}
        r = await self.client.post(hook, data=payload, headers=bad)
        self.assertEqual(r.status, 401)
        other = payload.replace(b"kowan/r.yaml", b"docs/readme.md")
        good = {"X-GitHub-Event": "push", "X-Hub-Signature-256": gitsource.sign_body(s["webhookSecret"], other)}
        r = await self.client.post(hook, data=other, headers=good)
        self.assertEqual((await r.json())["status"], "ignored")
        good["X-Hub-Signature-256"] = gitsource.sign_body(s["webhookSecret"], payload)
        r = await self.client.post(hook, data=payload, headers=good)
        self.assertEqual(r.status, 202)
        for _ in range(100):
            if [x for x in self.svc.store.list_alert_rules() if x.name == "cpu"]:
                break
            await asyncio.sleep(0.05)
        self.assertTrue([x for x in self.svc.store.list_alert_rules() if x.name == "cpu"])  # auto-applied
        r = await self.client.post("/hooks/gitops/unknown", data=b"{}")
        self.assertEqual(r.status, 404)
        # Interval loop: due syncs refresh (and auto-apply).
        gs = self.svc.gitops
        for _ in range(50):
            if not gs._lock(s["id"]).locked():
                break
            await asyncio.sleep(0.05)
        self.assertEqual(await gs.tick(), 0)  # just checked
        sync = self.svc.store.get_gitops_sync(s["id"])
        sync["last_checked_at"] = "2000-01-01T00:00:00Z"
        self.svc.store.update_gitops_sync(sync)
        self.write("kowan/r.yaml", RULE.replace(", channel: ops", "").replace("90", "99"))
        self.commit()
        self.assertEqual(await gs.tick(), 1)
        rule = next(x for x in self.svc.store.list_alert_rules() if x.name == "cpu")
        self.assertEqual(rule.threshold, 99.0)

    async def test_extension_kinds(self):
        if not hasattr(self.svc.store, "list_schedules") or not hasattr(self.svc.store, "list_autoupdate_policies"):
            self.skipTest("schedules / auto-update not present")
        self.write("kowan/d.yaml", DEPLOY)
        self.write("kowan/stack/compose.yml", COMPOSE)
        self.write("kowan/x.yaml", "\n---\n".join([
            "apiVersion: kowan/v1\nkind: Schedule\nmetadata: {name: nightly-stop}\n"
            "spec: {targetType: deployment, target: web, action: stop, cron: '0 1 * * *'}\n",
            "apiVersion: kowan/v1\nkind: AutoUpdatePolicy\nmetadata: {name: web}\nspec: {mode: notify}\n",
        ]))
        self.commit()
        s = await self.make_sync()
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/refresh")
        self.assertEqual(j["plan"]["errors"], [])
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/apply")
        self.assertEqual(j["run"]["status"], "applied", j["run"]["result"])
        dep = self.svc.store.get_deployment_by_name("web")
        sch = [x for x in self.svc.store.list_schedules() if x["name"] == "nightly-stop"]
        self.assertEqual(sch[0]["targetId"], dep.id)
        pol = self.svc.store.get_autoupdate_policy("deployment", "", dep.id)
        self.assertEqual(pol["mode"], "notify")
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/refresh")
        self.assertEqual(j["status"], "in_sync", j["plan"]["changes"])
        st, ex = await self.call("POST", "/gitops/export", json={"format": "json"})
        paths = [f["path"] for f in ex["files"]]
        self.assertIn("schedules/nightly-stop.yaml", paths)
        self.assertIn("auto-update/web.yaml", paths)

    async def test_sync_crud_validation(self):
        st, j = await self.call("POST", "/gitops/syncs", json={"name": "", "repoUrl": self.url})
        self.assertEqual(st, 400)
        st, j = await self.call("POST", "/gitops/syncs", json={"name": "x", "repoUrl": self.url, "intervalSec": 5})
        self.assertEqual(st, 400)
        st, j = await self.call("POST", "/gitops/syncs", json={"name": "x", "repoUrl": self.url, "path": "../a"})
        self.assertEqual(st, 400)
        st, j = await self.call("POST", "/gitops/syncs", json={"name": "x", "repoUrl": self.url, "bogus": 1})
        self.assertEqual(st, 400)
        s = await self.make_sync(name="x")
        st, _ = await self.call("POST", "/gitops/syncs", json={"name": "x", "repoUrl": self.url})
        self.assertEqual(st, 409)
        st, j = await self.call("GET", "/gitops/syncs")
        self.assertNotIn("webhookSecret", j[0])
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/apply")
        self.assertEqual(st, 409)  # nothing fetched yet
        st, j = await self.call("POST", f"/gitops/syncs/{s['id']}/webhook")
        self.assertNotEqual(j["webhookUrl"], s["webhookUrl"])
        st, kinds = await self.call("GET", "/gitops/kinds")
        self.assertIn("Deployment", [k["kind"] for k in kinds])
        st, _ = await self.call("DELETE", f"/gitops/syncs/{s['id']}")
        self.assertEqual(st, 200)
        st, _ = await self.call("GET", f"/gitops/syncs/{s['id']}")
        self.assertEqual(st, 404)


class TestGitOpsApiEdges(TestGitOpsFlow):
    """Reuses the flow fixture; only adds the API edge cases."""

    async def test_export_formats_hooks_and_errors(self):
        s = await self.make_sync(name="edges")
        st, res = await self.call("GET", "/gitops/resources")
        self.assertEqual(st, 200)
        st, out = await self.call("POST", "/gitops/export", json={"format": "json"})
        self.assertEqual(st, 200)
        self.assertIn("files", out)
        st, _ = await self.call("POST", "/gitops/export", json={"format": "tar"})
        self.assertEqual(st, 400)
        st, _ = await self.call("POST", "/gitops/export", json={"resources": "x"})
        self.assertEqual(st, 400)
        st, _ = await self.call("POST", "/gitops/export", json={"resources": [{"kind": "Deployment"}]})
        self.assertEqual(st, 400)
        st, _ = await self.call("POST", "/gitops/export", json={"push": {"syncId": s["id"]}})
        self.assertEqual(st, 400)
        st, _ = await self.call("GET", f"/gitops/syncs/{s['id']}/runs/missing")
        self.assertEqual(st, 404)
        st, runs = await self.call("GET", f"/gitops/syncs/{s['id']}/runs", params={"limit": "x"})
        self.assertEqual(st, 200)
        st, plan = await self.call("GET", f"/gitops/syncs/{s['id']}/plan")
        self.assertEqual(st, 200)
        st, _ = await self.call("PUT", f"/gitops/syncs/{s['id']}", json={"name": "edges", "repoUrl": self.url,
                                                                       "intervalSec": "often"})
        self.assertEqual(st, 400)
        # Moving the sync to another repository forgets the old plan and checkout.
        other = os.path.join(self.tmp, "other.git")
        git(self.tmp, "init", "-q", "--bare", "-b", "main", other)
        st, upd = await self.call("PUT", f"/gitops/syncs/{s['id']}", json={
            "name": "edges", "repoUrl": "file://" + other, "enabled": False, "autoApply": True})
        self.assertEqual(st, 200, upd)
        token = s["webhookUrl"].rsplit("/", 1)[1]
        r = await self.client.post(f"/hooks/gitops/{token}", data=b"{}",
                                   headers={"X-GitHub-Event": "ping",
                                            "X-Hub-Signature-256": gitsource.sign_body(s["webhookSecret"], b"{}")})
        self.assertEqual((await r.json())["status"], "pong")
        body = json.dumps({"ref": "refs/heads/main", "after": "a" * 40}).encode()
        r = await self.client.post(f"/hooks/gitops/{token}", data=body,
                                   headers={"X-GitHub-Event": "push",
                                            "X-Hub-Signature-256": gitsource.sign_body(s["webhookSecret"], body)})
        self.assertEqual((await r.json())["reason"], "sync is disabled")
        r = await self.client.post("/hooks/gitops/nope", data=b"{}")
        self.assertEqual(r.status, 404)


class TestEngineOrdering(unittest.TestCase):
    """Apply order with fake kinds: creates/updates by ascending order,
    deletes by descending order, markers written per success, one failure
    does not stop the rest."""

    def setUp(self):
        kreg = importlib.import_module("kowan.gitops.kinds")
        from kowan.gitops.kinds import Current, KindHandler, SpecError
        from kowan.store import Store

        calls = self.calls = []
        state = self.state = {"A": {}, "B": {}}

        class Fake(KindHandler):
            def __init__(self, kind, order):
                self.kind, self.order, self.plural = kind, order, kind.lower()

            async def normalize(self, res, ctx):
                if res.spec.get("bad"):
                    raise SpecError("bad spec")
                return {"v": res.spec.get("v", 0)}

            def current(self, ctx):
                return {n: Current(i, n, {"v": v}) for n, (i, v) in state[self.kind].items()}

            async def create(self, ctx, name, spec, pol):
                if spec["v"] == -1:
                    raise RuntimeError("boom")
                calls.append(("create", self.kind, name))
                state[self.kind][name] = (f"{self.kind}-{name}", spec["v"])
                return f"{self.kind}-{name}"

            async def update(self, ctx, cur, spec, pol):
                calls.append(("update", self.kind, cur.name))
                state[self.kind][cur.name] = (cur.id, spec["v"])

            async def delete(self, ctx, cur):
                calls.append(("delete", self.kind, cur.name))
                del state[self.kind][cur.name]

        self._saved = dict(kreg._REGISTRY)
        kreg._REGISTRY.clear()
        kreg.register_kind(Fake("B", 20))
        kreg.register_kind(Fake("A", 10))

        class Svc:
            pass

        self.svc = Svc()
        self.svc.store = Store(":memory:")

    def tearDown(self):
        kreg = importlib.import_module("kowan.gitops.kinds")

        kreg._REGISTRY.clear()
        kreg._REGISTRY.update(self._saved)

    def _res(self, *items):
        from kowan.gitops.resources import Resource

        return [Resource(kind=k, name=n, spec=spec) for k, n, spec in items]

    def test_order_and_managed_deletes(self):
        from kowan.gitops import engine
        from kowan.gitops.kinds import Context

        ctx = Context(svc=self.svc, commit="c1")
        self.state["A"]["preexisting"] = ("A-pre", 1)  # unmanaged: never deleted
        pr = asyncio.run(engine.plan(ctx, "s1", self._res(("B", "b1", {"v": 1}), ("A", "a1", {"v": 1}),
                                                          ("A", "fail", {"v": -1}))))
        self.assertTrue(pr.ok)
        self.assertEqual([(c["kind"], c["name"]) for c in pr.plan["changes"]],
                         [("A", "a1"), ("A", "fail"), ("B", "b1")])
        res = asyncio.run(engine.apply(ctx, "s1", pr))
        self.assertEqual(self.calls, [("create", "A", "a1"), ("create", "B", "b1")])
        self.assertEqual({r["name"]: r["status"] for r in res}, {"a1": "ok", "fail": "failed", "b1": "ok"})
        self.assertEqual(engine.run_status(res), "partial")
        managed = {(m["kind"], m["name"]) for m in self.svc.store.list_gitops_managed("s1")}
        self.assertEqual(managed, {("A", "a1"), ("B", "b1")})

        # Remove everything from git: only managed resources are deleted, B before A.
        self.calls.clear()
        pr = asyncio.run(engine.plan(ctx, "s1", []))
        self.assertEqual([(c["kind"], c["name"], c["action"]) for c in pr.plan["changes"]],
                         [("B", "b1", "delete"), ("A", "a1", "delete")])
        asyncio.run(engine.apply(ctx, "s1", pr))
        self.assertEqual(self.calls, [("delete", "B", "b1"), ("delete", "A", "a1")])
        self.assertIn("preexisting", self.state["A"])
        self.assertEqual(self.svc.store.list_gitops_managed("s1"), [])

    def test_errors_block_apply_and_updates(self):
        from kowan.gitops import engine
        from kowan.gitops.kinds import Context, SpecError

        ctx = Context(svc=self.svc)
        pr = asyncio.run(engine.plan(ctx, "s1", self._res(("A", "x", {"bad": True}), ("C", "y", {}))))
        self.assertEqual(sorted(e["message"] for e in pr.plan["errors"]), ["bad spec", "unknown kind 'C'"])
        with self.assertRaises(SpecError):
            asyncio.run(engine.apply(ctx, "s1", pr))
        self.state["A"]["x"] = ("A-x", 1)
        pr = asyncio.run(engine.plan(ctx, "s1", self._res(("A", "x", {"v": 2}))))
        c = pr.plan["changes"][0]
        self.assertEqual((c["action"], c["adopt"], c["fields"]), ("update", True, [{"path": "v", "before": 1, "after": 2}]))
        asyncio.run(engine.apply(ctx, "s1", pr))
        pr = asyncio.run(engine.plan(ctx, "s1", self._res(("A", "x", {"v": 2}))))
        self.assertEqual((pr.plan["changes"][0]["action"], pr.plan["changes"][0]["adopt"], pr.has_changes),
                         ("noop", False, False))
        # Another sync cannot take it over.
        pr = asyncio.run(engine.plan(ctx, "s2", self._res(("A", "x", {"v": 2}))))
        self.assertIn("managed by another sync", pr.plan["errors"][0]["message"])


if __name__ == "__main__":
    unittest.main()
