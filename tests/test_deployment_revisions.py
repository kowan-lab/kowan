"""Deployment history: every change to what runs is a numbered revision, two
revisions diff as YAML, and a rollback restores one as a new revision and
redeploys it. Viewers see the history without secrets."""

import unittest

from kowan.deployment import revisions as revs
from kowan.models import ContainerSpec, Deployment, GitSource
from tests.test_api_deployments import DeploymentCase


class TestRevisionFunctions(unittest.TestCase):
    def dep(self, **kw):
        base = dict(id="d1", name="web", type="container", strategy="standalone", targets=["local"],
                    spec=ContainerSpec(image="nginx:1", env=[{"key": "PW", "value": "s3cret"}], extraArgs=["-e X=1"]))
        base.update(kw)
        return Deployment(**base)

    def test_snapshot_keeps_what_runs_and_drops_switches(self):
        a = revs.snapshot(self.dep())
        b = revs.snapshot(self.dep(enabled=False, protected=True, status="deployed"))
        self.assertEqual(revs.encode(a), revs.encode(b))
        self.assertEqual(a["container"]["image"], "nginx:1")
        self.assertNotIn("restartPolicy", a["container"])  # defaults dropped
        self.assertNotIn("compose", a)

    def test_compose_and_git_snapshots(self):
        c = revs.snapshot(self.dep(type="compose", spec=None, compose="services: {}\n"))
        self.assertEqual(c["compose"], "services: {}\n")
        g = revs.snapshot(self.dep(type="compose", spec=None, compose="mirror",
                                   git=GitSource(repoUrl="https://u:p@git.example/a.git", webhookSecret="x"),
                                   deployedCommit="abc"))
        self.assertNotIn("compose", g)
        self.assertEqual(g["commit"], "abc")
        self.assertNotIn("webhookSecret", g["git"])

    def test_redact_hides_secrets(self):
        cfg = revs.snapshot(self.dep())
        cfg["compose"] = "services:\n  db:\n    environment:\n      PASSWORD: hunter2\n"
        cfg["git"] = {"repoUrl": "https://u:tok@git.example/a.git"}
        r = revs.redact(cfg)
        self.assertEqual(r["container"]["env"][0]["value"], "********")
        self.assertEqual(r["container"]["extraArgs"], ["********"])
        self.assertNotIn("hunter2", r["compose"])
        self.assertNotIn("tok", r["git"]["repoUrl"])
        self.assertEqual(cfg["container"]["env"][0]["value"], "s3cret")  # original untouched

    def test_diff_and_changed_keys(self):
        a = revs.snapshot(self.dep())
        b = revs.snapshot(self.dep(spec=ContainerSpec(image="nginx:2"), targets=["h2"]))
        self.assertEqual(revs.changed_keys(None, b), [])
        self.assertEqual(set(revs.changed_keys(a, b)), {"container", "targets"})
        lines = revs.line_diff(a, b)
        self.assertIn({"op": "-", "text": "  image: nginx:1"}, lines)
        self.assertIn({"op": "+", "text": "  image: nginx:2"}, lines)
        self.assertTrue(all(line["op"] == "+" for line in revs.line_diff(None, b)))
        self.assertEqual(revs.to_yaml(None), "")
        self.assertIn("compose: |", revs.to_yaml({"compose": "a\nb\n"}))

    def test_apply_restores_and_pins_git(self):
        dep = self.dep(type="compose", spec=None, compose="",
                       git=GitSource(repoUrl="https://git.example/a.git", ref="main", autoDeploy=True, webhookSecret="keep"),
                       deployedCommit="new")
        cfg = revs.snapshot(dep)
        cfg["commit"] = "old"
        self.assertTrue(revs.apply(dep, cfg))
        self.assertEqual(dep.git.ref, "old")
        self.assertFalse(dep.git.auto_deploy)
        self.assertEqual(dep.git.webhook_secret, "keep")
        # Same commit: no pin, the branch stays.
        dep2 = self.dep(type="compose", spec=None, git=GitSource(repoUrl="https://g/a.git", ref="main"), deployedCommit="c1")
        cfg2 = revs.snapshot(dep2)
        self.assertFalse(revs.apply(dep2, cfg2))
        self.assertEqual(dep2.git.ref, "main")
        # Back to a non-git revision.
        plain = revs.snapshot(self.dep(type="compose", spec=None, compose="x: 1\n"))
        self.assertFalse(revs.apply(dep, plain))
        self.assertIsNone(dep.git)
        self.assertEqual(dep.compose, "x: 1\n")


class TestRevisionStore(DeploymentCase):
    async def test_store_records_only_changes_and_prunes(self):
        st = self.svc.store
        d = Deployment(id="dx", name="x", type="container", strategy="standalone", targets=["local"],
                       spec=ContainerSpec(image="a:1"), createdAt="t0", updatedAt="t0")
        st.create_deployment(d, author="root")
        d.status = "deployed"
        st.update_deployment(d)  # status only: no revision
        self.assertEqual(len(st.list_deployment_revisions("dx")), 1)
        for i in range(revs.KEEP + 3):
            d.spec.image = f"a:{i + 2}"
            st.update_deployment(d, source="edit")
        rows = st.list_deployment_revisions("dx")
        self.assertEqual(len(rows), revs.KEEP)
        self.assertEqual(rows[0]["number"], revs.KEEP + 4)
        st.delete_deployment("dx")
        self.assertEqual(st.list_deployment_revisions("dx"), [])
        with self.assertRaises(Exception):
            st.get_deployment_revision("dx", 1)


class TestRevisionsApi(DeploymentCase):
    async def deployed(self, image="nginx:1.27", env="prod"):
        dep = await self.create(name="web", type="container", spec={"image": image, "env": [{"key": "MODE", "value": env}]})
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "deployed")
        return dep

    async def edit(self, dep_id, **spec):
        _, got = await self.call("GET", f"/deployments/{dep_id}", "operator")
        got["spec"].update(spec)
        await self.call("PUT", f"/deployments/{dep_id}", "operator", json=got, expect=200)

    async def test_history_diff_and_rollback(self):
        self.engine.add_image("nginx:1.28")
        dep = await self.deployed()
        await self.edit(dep["id"], image="nginx:1.28", env=[{"key": "MODE", "value": "staging"}])
        await self.wait_status(dep["id"], "deployed")  # an edit of a deployed record redeploys

        _, hist = await self.call("GET", f"/deployments/{dep['id']}/revisions", "viewer", expect=200)
        self.assertEqual([h["number"] for h in hist], [2, 1])
        self.assertEqual(hist[0]["source"], "edit")
        self.assertEqual(hist[0]["author"], "operator")
        self.assertEqual(hist[1]["source"], "create")
        self.assertEqual(hist[0]["changes"], ["container"])
        self.assertTrue(hist[0]["live"])

        _, diff = await self.call("GET", f"/deployments/{dep['id']}/revisions/2/diff", "operator", expect=200)
        self.assertEqual((diff["from"], diff["to"]), (1, 2))
        self.assertIn({"op": "+", "text": "  image: nginx:1.28"}, diff["lines"])
        self.assertIn({"op": "+", "text": "    value: staging"}, diff["lines"])
        # Viewers get the diff with env values masked.
        _, vdiff = await self.call("GET", f"/deployments/{dep['id']}/revisions/2/diff", "viewer", expect=200)
        self.assertNotIn("staging", str(vdiff["lines"]))
        _, first = await self.call("GET", f"/deployments/{dep['id']}/revisions/1/diff", "operator", expect=200)
        self.assertEqual(first["from"], 0)
        _, explicit = await self.call("GET", f"/deployments/{dep['id']}/revisions/1/diff?against=2", "operator", expect=200)
        self.assertEqual(explicit["from"], 2)

        _, one = await self.call("GET", f"/deployments/{dep['id']}/revisions/1", "operator", expect=200)
        self.assertIn("image: nginx:1.27", one["yaml"])
        self.assertEqual(one["config"]["container"]["env"][0]["value"], "prod")

        await self.roles("POST", f"/deployments/{dep['id']}/revisions/1/rollback", "operator", json={"deploy": False})
        _, back = await self.call("POST", f"/deployments/{dep['id']}/revisions/1/rollback", "operator", json={}, expect=200)
        self.assertEqual(back["spec"]["image"], "nginx:1.27")
        await self.wait_status(dep["id"], "deployed")
        self.assertEqual(self.engine.container("web")["Config"]["Image"], "nginx:1.27")
        self.assertIn("MODE=prod", self.engine.container("web")["Config"]["Env"])
        _, hist = await self.call("GET", f"/deployments/{dep['id']}/revisions", "operator")
        self.assertEqual(hist[0]["number"], 3)
        self.assertEqual(hist[0]["source"], "rollback")
        self.assertEqual(hist[0]["note"], "rollback to #1")
        self.assertTrue(hist[0]["live"])

    async def test_rollback_without_deploy_and_errors(self):
        dep = await self.create(name="api", type="container", spec={"image": "nginx:1.27"})
        await self.edit(dep["id"], image="nginx:9")
        _, back = await self.call("POST", f"/deployments/{dep['id']}/revisions/1/rollback", "operator",
                                  json={"deploy": False}, expect=200)
        self.assertEqual(back["status"], "stopped")
        self.assertEqual(back["spec"]["image"], "nginx:1.27")
        # Rolling back to the current revision records nothing new.
        await self.call("POST", f"/deployments/{dep['id']}/revisions/3/rollback", "operator", json={"deploy": False}, expect=200)
        _, hist = await self.call("GET", f"/deployments/{dep['id']}/revisions", "operator")
        self.assertEqual(hist[0]["number"], 3)
        await self.call("POST", f"/deployments/{dep['id']}/revisions/4/rollback", "operator", json={}, expect=404)
        await self.call("GET", f"/deployments/{dep['id']}/revisions/x/diff", "operator", expect=400)
        await self.call("GET", f"/deployments/{dep['id']}/revisions/0", "operator", expect=400)
        await self.call("GET", f"/deployments/{dep['id']}/revisions/1/diff?against=z", "operator", expect=400)
        await self.call("GET", f"/deployments/{dep['id']}/revisions/42", "operator", expect=404)
        await self.call("GET", "/deployments/nope/revisions", "operator", expect=404)
        await self.call("POST", f"/deployments/{dep['id']}/revisions/1/rollback", "operator", data=b"[1]", expect=400)

    async def test_rollback_refuses_a_gone_host_and_a_taken_name(self):
        self.svc.store  # noqa: B018 - make sure the store is up
        _, h = await self.call("POST", "/hosts", "admin", json={"name": "edge-x", "mode": "edge"})
        host_id = h["host"]["id"]
        dep = await self.create(name="svc", type="container", spec={"image": "nginx:1.27"}, targets=[host_id])
        _, got = await self.call("GET", f"/deployments/{dep['id']}", "operator")
        got["targets"] = ["local"]
        got["name"] = "svc2"
        await self.call("PUT", f"/deployments/{dep['id']}", "operator", json=got, expect=200)
        await self.call("DELETE", f"/hosts/{host_id}", "admin")
        _, err = await self.call("POST", f"/deployments/{dep['id']}/revisions/1/rollback", "operator", json={}, expect=400)
        self.assertIn("no longer exists", err["error"])
        # Another deployment took the old name meanwhile.
        b = await self.create(name="b1", type="container", spec={"image": "nginx:1.27"})
        _, got = await self.call("GET", f"/deployments/{b['id']}", "operator")
        got["name"] = "b2"
        await self.call("PUT", f"/deployments/{b['id']}", "operator", json=got, expect=200)
        await self.create(name="b1", type="container", spec={"image": "nginx:1.27"})
        _, err = await self.call("POST", f"/deployments/{b['id']}/revisions/1/rollback", "operator", json={}, expect=409)
        self.assertIn("b1", err["error"])
        self.assertEqual(self.svc.store.get_deployment(b["id"]).name, "b2")

    async def test_compose_revisions_and_git_commit(self):
        dep = await self.create(name="stack", type="compose", compose="services:\n  web:\n    image: nginx:1.27\n")
        _, got = await self.call("GET", f"/deployments/{dep['id']}", "operator")
        got["compose"] = "services:\n  web:\n    image: nginx:1.28\n"
        await self.call("PUT", f"/deployments/{dep['id']}", "operator", json=got, expect=200)
        _, diff = await self.call("GET", f"/deployments/{dep['id']}/revisions/2/diff", "operator")
        self.assertIn({"op": "+", "text": "      image: nginx:1.28"}, diff["lines"])
        # A deploy that ran a new git commit records it.
        d = self.svc.store.get_deployment(dep["id"])
        d.git = GitSource(repoUrl="https://git.example/a.git")
        d.deployed_commit = "c0ffee1234567890"
        self.svc.store.update_deployment(d, source="git", note="commit c0ffee123456")
        _, hist = await self.call("GET", f"/deployments/{dep['id']}/revisions", "operator")
        self.assertEqual(hist[0]["source"], "git")
        self.assertEqual(set(hist[0]["changes"]), {"git", "commit", "compose"})


if __name__ == "__main__":
    unittest.main()
