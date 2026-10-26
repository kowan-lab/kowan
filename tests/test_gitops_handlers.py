"""GitOps kind handlers on their own, against a real store: every spec rule of
Deployment / Workflow / AlertRule / NotificationChannel / Registry / Template
(accepted and refused shapes), and the deployment handler's writes."""

import unittest

from kowan.gitops.kinds import Context, Current, SpecError, get_kind
from kowan.gitops.resources import Resource
from tests.apicase import ServerCase
from tests.fakecli import FakeDockerCLI


class HandlerCase(ServerCase):
    async def asyncSetUp(self):
        self.cli = FakeDockerCLI()
        await super().asyncSetUp()
        self.ctx = Context(svc=self.svc)
        await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "hook-url", "fields": {"value": "https://hooks.example/x"}}, expect=201)
        _, self.channel = await self.call("POST", "/credentials", "admin", json={
            "kind": "channel", "name": "ops", "type": "webhook", "config": {"url": "https://h.example"}}, expect=201)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.cli.close()

    async def norm(self, kind, spec, name="r"):
        return await get_kind(kind).normalize(Resource(kind=kind, name=name, spec=spec), self.ctx)

    async def refused(self, kind, spec, needle=""):
        with self.assertRaises(SpecError) as cm:
            await self.norm(kind, spec)
        if needle:
            self.assertIn(needle, str(cm.exception))


class TestDeploymentSpec(HandlerCase):
    async def test_accepted(self):
        out = await self.norm("Deployment", {"host": "local", "compose": "services:\n  web:\n    image: nginx\n",
                                             "policies": {"deploy": "never"}})
        self.assertEqual((out["type"], out["host"]), ("compose", "local"))
        out = await self.norm("Deployment", {"type": "container", "host": "local", "container": {"image": "nginx"}})
        self.assertEqual(out["container"]["image"], "nginx")
        out = await self.norm("Deployment", {"host": "local", "git": {"repoUrl": "https://github.com/a/b.git",
                                                                     "path": "/stack/compose.yml"}})
        self.assertEqual(out["git"]["path"], "stack/compose.yml")
        out = await self.norm("Deployment", {"host": "local", "strategy": "swarm", "replicas": 3,
                                             "compose": "services:\n  web:\n    image: nginx\n"})
        self.assertEqual(out["replicas"], 3)

    async def test_refused(self):
        compose = "services:\n  web:\n    image: nginx\n"
        for spec, needle in (
            ({"host": "local", "type": "helm"}, "type"),
            ({"host": "local", "strategy": "k8s", "compose": compose}, "strategy"),
            ({"compose": compose}, "host is required"),
            ({"host": "mars", "compose": compose}, "unknown host"),
            ({"host": "local", "replicas": "many", "compose": compose}, "replicas"),
            ({"host": "local", "type": "container"}, "container spec"),
            ({"host": "local", "type": "container", "container": {"image": " "}}, "image is required"),
            ({"host": "local", "type": "container", "container": {"image": "x", "ports": "bad"}}, "invalid"),
            ({"host": "local", "type": "container", "container": {"image": "x"}, "compose": compose}, "compose"),
            ({"host": "local", "git": {}}, "repoUrl"),
            ({"host": "local", "git": {"repoUrl": "file:///etc"}}, ""),
            ({"host": "local", "git": {"repoUrl": "https://g.example/r.git", "branch": "x"}}, "unknown git field"),
            ({"host": "local", "git": {"repoUrl": "https://g.example/r.git", "path": "../x"}}, "inside"),
            ({"host": "local", "git": {"repoUrl": "https://g.example/r.git", "credential": "nope"}}, "credential"),
            ({"host": "local", "git": {"repoUrl": "https://g.example/r.git"}, "compose": compose}, "either git"),
            ({"host": "local", "compose": compose, "composeFile": "c.yml"}, "either compose"),
            ({"host": "local", "composeFile": "c.yml"}, "checkout"),
            ({"host": "local"}, "require compose"),
            ({"host": "local", "compose": compose, "policies": {"redeploy": 1}}, "policies"),
            ({"host": "local", "compose": compose, "policies": {"deploy": "always"}}, "policies.deploy"),
            ({"host": "local", "compose": compose, "enabled": "yes"}, "true or false"),
            ({"host": "local", "compose": compose, "colour": "red"}, ""),
        ):
            await self.refused("Deployment", spec, needle)
        # An invalid compose file is reported with its line.
        self.cli.fail_with(1, "services.web Additional property foo is not allowed")
        await self.refused("Deployment", {"host": "local", "compose": "services:\n  web:\n    foo: 1\n"}, "compose")

    async def test_create_update_delete(self):
        h = get_kind("Deployment")
        spec = await self.norm("Deployment", {"type": "container", "host": "local", "container": {"image": "nginx"},
                                              "policies": {"deploy": "never"}})
        dep_id = await h.create(self.ctx, "fromgit", spec, {"policies": {"deploy": "never"}})
        cur = h.current(self.ctx)["fromgit"]
        self.assertEqual(cur.spec["container"]["image"], "nginx")
        # Switch to a git-sourced compose deployment, then back to inline compose.
        spec2 = await self.norm("Deployment", {"host": "local", "git": {"repoUrl": "https://g.example/r.git"}})
        await h.update(self.ctx, Current(dep_id, "fromgit", cur.spec), spec2, {})
        dep = self.svc.store.get_deployment(dep_id)
        self.assertTrue(dep.git.webhook_secret)
        spec3 = await self.norm("Deployment", {"host": "local", "compose": "services:\n  w:\n    image: nginx\n"})
        await h.update(self.ctx, Current(dep_id, "fromgit", {}), spec3, {})
        self.assertIsNone(self.svc.store.get_deployment(dep_id).git)
        # Same resource name -> refused.
        with self.assertRaises(SpecError):
            await h.create(self.ctx, "fromgit", spec3, {})
        self.assertEqual(h.delete_blocker(self.ctx, Current(dep_id, "fromgit", {"protected": True})),
                         "deletion protection is enabled")
        await h.delete(self.ctx, Current(dep_id, "fromgit", {}))
        self.assertNotIn("fromgit", h.current(self.ctx))


class TestOtherKinds(HandlerCase):
    async def test_workflow(self):
        out = await self.norm("Workflow", {"steps": [
            {"name": "n", "type": "notify", "config": {"channel": "ops", "title": "t"}},
            {"name": "p", "type": "prune", "config": {"host": "local", "target": "images"}, "continueOnError": True}],
            "triggers": [{"type": "cron", "config": {"cron": "0 3 * * *"}}]})
        self.assertEqual(out["steps"][0]["config"]["channel"], "ops")
        for spec, needle in (
            ({"steps": "x"}, "lists"), ({"steps": [1]}, "mapping"),
            ({"steps": [{"name": "a", "type": "notify", "when": 1}]}, "unknown field"),
            ({"steps": [{"name": "a", "type": "notify", "config": {"channel": "nope"}}]}, "unknown channel"),
            ({"steps": [{"name": "a", "type": "prune", "config": {"host": "mars"}}]}, "unknown host"),
            ({"triggers": [1]}, "mapping"), ({"triggers": [{"type": "cron", "x": 1}]}, "unknown field"),
            ({"triggers": [{"type": "webhook", "config": {"token": "abc"}}]}, "server-generated"),
            ({"steps": [{"name": "a", "type": "teleport"}]}, ""),
        ):
            await self.refused("Workflow", spec, needle)

    async def test_alert_rule(self):
        out = await self.norm("AlertRule", {"type": "cpu_high", "threshold": 90, "host": "local", "channel": "ops"})
        self.assertEqual(out["operator"], ">")
        for spec, needle in (({"type": "cpu_high", "threshold": "hot"}, "number"),
                             ({"type": "cpu_high", "host": "mars"}, "unknown host"),
                             ({"type": "cpu_high", "channel": "nope"}, "unknown notification channel"),
                             ({"type": "warp"}, "invalid rule type")):
            await self.refused("AlertRule", spec, needle)

    async def test_channel(self):
        out = await self.norm("NotificationChannel", {"type": "webhook", "config": {"url": "[[hook-url]]"}})
        self.assertEqual(out["minLevel"], "info")
        for spec, needle in (({}, "type is required"), ({"type": "pigeon"}, "unknown channel type"),
                             ({"type": "webhook", "minLevel": "loud"}, "minLevel"),
                             ({"type": "slack", "config": {"url": "https://literal"}}, "sensitive"),
                             ({"type": "slack", "config": {"url": "[[missing]]"}}, "unknown secret")):
            await self.refused("NotificationChannel", spec, needle)
        h = get_kind("NotificationChannel")
        cid = await h.create(self.ctx, "fromgit", out, {})
        await h.update(self.ctx, Current(cid, "fromgit", {}), {**out, "minLevel": "warning"}, {})
        with self.assertRaises(SpecError):
            await h.update(self.ctx, Current(cid, "fromgit", {}), {**out, "type": "slack"}, {})
        await h.delete(self.ctx, Current(cid, "fromgit", {}))

    async def test_registry_and_template(self):
        out = await self.norm("Registry", {"url": "ghcr.io", "username": "bob"})
        h = get_kind("Registry")
        rid = await h.create(self.ctx, "ghcr", out, {})
        await h.update(self.ctx, Current(rid, "ghcr", {}), {**out, "insecure": True}, {})
        self.assertTrue(h.current(self.ctx)["ghcr"].spec["insecure"])
        await h.delete(self.ctx, Current(rid, "ghcr", {}))
        for spec, needle in (({"url": "x", "password": "p"}, "password"), ({}, "url is required"),
                             ({"url": "x", "type": "quay-ish"}, "unknown registry type"),
                             ({"url": "x", "connection": "nope"}, "unknown connection")):
            await self.refused("Registry", spec, needle)

        t = await self.norm("Template", {"type": "container", "spec": {"image": "nginx"}, "description": "web"})
        th = get_kind("Template")
        tid = await th.create(self.ctx, "mytpl", t, {})
        await th.update(self.ctx, Current(tid, "mytpl", {}), {**t, "description": "changed"}, {})
        self.assertEqual(th.current(self.ctx)["mytpl"].spec["description"], "changed")
        await th.delete(self.ctx, Current(tid, "mytpl", {}))
        for spec in ({"type": "container", "spec": "x"}, {"type": "container", "inputs": "x"},
                     {"type": "container", "inputs": [{"bogus": True, "name": 3}]}, {"type": "helm"}):
            await self.refused("Template", spec)


if __name__ == "__main__":
    unittest.main()


class TestExtensionKinds(HandlerCase):
    async def test_schedule_kind(self):
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "nightly-app", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}})
        h = get_kind("Schedule")
        spec = await self.norm("Schedule", {"targetType": "deployment", "target": "nightly-app", "action": "restart",
                                            "cron": "0 3 * * *", "timezone": "UTC", "host": "local"})
        sid = await h.create(self.ctx, "restart-nightly", spec, {})
        self.assertIn("restart-nightly", h.current(self.ctx))
        await h.update(self.ctx, Current(sid, "restart-nightly", {}), {**spec, "cron": "0 4 * * *"}, {})
        self.assertEqual(self.svc.store.get_schedule(sid)["cron"], "0 4 * * *")
        await h.delete(self.ctx, Current(sid, "restart-nightly", {}))
        self.assertNotIn("restart-nightly", h.current(self.ctx))
        for bad, needle in (({"targetType": "deployment", "target": "x", "action": "restart", "cron": "0 3 * * *",
                              "host": "mars"}, "unknown host"),
                            ({"targetType": "deployment", "target": "x", "action": "explode", "cron": "0 3 * * *"}, ""),
                            ({"color": "red"}, "")):
            await self.refused("Schedule", bad, needle)
        self.assertTrue(dep)

    async def test_autoupdate_policy_kind(self):
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "auto-app", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}})
        h = get_kind("AutoUpdatePolicy")
        spec = await self.norm("AutoUpdatePolicy", {"target": "deployment", "mode": "notify"})
        rid = await h.create(self.ctx, "auto-app", spec, {})
        cur = h.current(self.ctx)
        self.assertIn("auto-app", cur)
        await h.update(self.ctx, cur["auto-app"], {**spec, "mode": "apply", "window": "02:00-04:00"}, {})
        self.assertEqual(self.svc.store.get_autoupdate_policy("deployment", "", dep["id"])["mode"], "apply")
        await h.delete(self.ctx, h.current(self.ctx)["auto-app"])
        self.assertNotIn("auto-app", h.current(self.ctx))
        cspec = await self.norm("AutoUpdatePolicy", {"target": "container", "mode": "pull"})
        await h.create(self.ctx, "local/web", cspec, {})
        self.assertIn("local/web", h.current(self.ctx))
        for name in ("no-slash", "mars/web", "local/"):
            with self.assertRaises(SpecError, msg=name):
                await h.create(self.ctx, name, cspec, {})
        with self.assertRaises(SpecError):
            await h.create(self.ctx, "ghost-deployment", spec, {})
        for bad in ({"target": "volume"}, {"mode": "sometimes"}, {"window": "25:00-26:00"}, {"colour": 1}):
            await self.refused("AutoUpdatePolicy", bad)
        self.assertTrue(rid)
