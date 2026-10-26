"""Workload environments: compose stack variables (validation, interpolation
over a git .env, revisions and rollback) and config/secret rows, whose values
live in the deployment's own secret (kowan/deployment/envsecrets.py)."""

import unittest

from kowan.deployment import gitsource
from kowan.deployment.service import compose_with_variables
from tests.apicase import ServerCase

COMPOSE = "services:\n  db:\n    image: postgres:${TAG}\n    environment:\n      - P=${DB_PASSWORD}\n"


def compose(name="shop", variables=None, **over):
    return {"name": name, "type": "compose", "strategy": "standalone", "targets": ["local"],
            "compose": COMPOSE, "variables": variables if variables is not None else [], **over}


class TestRendering(unittest.TestCase):
    def test_variables_win_over_the_env_file(self):
        out = gitsource.render_compose("a=${A} b=${B} c=${C}", "A=1\nB=2\n", {"B": "x", "C": "y"})
        self.assertEqual(out, "a=1 b=x c=y")
        self.assertEqual(gitsource.render_compose("a=${A}", ""), "a=${A}")


class TestStackVariables(ServerCase):
    def secrets(self):
        return self.svc.store.list_credentials("secret")

    async def test_validation(self):
        container = {"name": "c", "type": "container", "strategy": "standalone", "targets": ["local"],
                     "spec": {"image": "nginx"}, "variables": [{"key": "A", "value": "1"}]}
        for body, msg in ((container, "only for compose"),
                          (compose(variables=[{"key": "1A", "value": "x"}]), "1A"),
                          (compose(variables=[{"key": "A", "value": "1"}, {"key": "A", "value": "2"}]), "twice"),
                          (compose(variables=[{"key": "S", "value": "", "secret": True}]), "set a value")):
            _, err = await self.call("POST", "/deployments", "operator", json=body, expect=400)
            self.assertIn(msg, err["error"])
        self.assertEqual(self.secrets(), [])
        # Rows without a name are dropped.
        _, dep = await self.call("POST", "/deployments", "operator", json=compose(
            variables=[{"key": " ", "value": "x"}, {"key": "TAG", "value": "16"}]), expect=201)
        self.assertEqual(dep["variables"], [{"key": "TAG", "value": "16"}])

    async def test_secret_rows_live_in_the_deployments_secret(self):
        _, dep = await self.call("POST", "/deployments", "operator", json=compose(variables=[
            {"key": "TAG", "value": "16"}, {"key": "DB_PASSWORD", "value": "s3cret", "secret": True}]), expect=201)
        did = dep["id"]
        ref = "[[shop-env.DB_PASSWORD]]"
        self.assertEqual(dep["variables"], [{"key": "TAG", "value": "16"},
                                            {"key": "DB_PASSWORD", "value": ref, "secret": True}])
        [secret] = self.secrets()
        self.assertEqual((secret.name, secret.meta["deploymentId"]), ("shop-env", did))
        self.assertEqual(self.svc.store.credential_config(secret.id), {"DB_PASSWORD": "s3cret"})
        stored = self.svc.store.get_deployment(did)
        self.assertNotIn("s3cret", stored.model_dump_json())
        resolved = self.svc.store.resolve_secrets(compose_with_variables(stored))
        self.assertIn("image: postgres:16", resolved)
        self.assertIn("P=s3cret", resolved)
        _, seen = await self.call("GET", f"/deployments/{did}", "viewer", expect=200)
        self.assertEqual({v["value"] for v in seen["variables"]}, {"********"})

        async def put(variables, **kw):
            return await self.call("PUT", f"/deployments/{did}", "operator", json=compose(variables=variables), **kw)

        # Sent back as returned (or left empty): the value stays.
        await put([{"key": "TAG", "value": "16"}, {"key": "DB_PASSWORD", "value": ref, "secret": True}], expect=200)
        await put([{"key": "TAG", "value": "16"}, {"key": "DB_PASSWORD", "value": "", "secret": True}], expect=200)
        self.assertEqual(self.svc.store.credential_config(secret.id), {"DB_PASSWORD": "s3cret"})
        # Renamed: the value follows; a new value replaces it.
        _, dep = await put([{"key": "DB_PASS", "value": ref, "secret": True}], expect=200)
        self.assertEqual(dep["variables"], [{"key": "DB_PASS", "value": "[[shop-env.DB_PASS]]", "secret": True}])
        self.assertEqual(self.svc.store.credential_config(secret.id), {"DB_PASS": "s3cret"})
        await put([{"key": "DB_PASS", "value": "n3w", "secret": True}, {"key": "API", "value": "k", "secret": True}],
                  expect=200)
        self.assertEqual(self.svc.store.credential_config(secret.id), {"DB_PASS": "n3w", "API": "k"})
        _, err = await put([{"key": "OTHER", "value": "", "secret": True}], expect=400)
        self.assertIn("set a value for the secret OTHER", err["error"])
        # Back to config: the field goes, and the secret with its last field.
        await put([{"key": "DB_PASS", "value": "plain"}, {"key": "API", "value": "", "secret": True}], expect=200)
        self.assertEqual(self.svc.store.credential_config(secret.id), {"API": "k"})
        await put([{"key": "DB_PASS", "value": "plain"}], expect=200)
        self.assertEqual(self.secrets(), [])

        # The secret belongs to the workload: no rename, owner kept, gone with it.
        await put([{"key": "K", "value": "v", "secret": True}], expect=200)
        [secret] = self.secrets()
        base = {"kind": "secret", "type": "generic", "fields": {}, "description": "edited"}
        _, err = await self.call("PUT", f"/credentials/{secret.id}", json={**base, "name": "renamed"}, expect=400)
        self.assertIn("cannot be renamed", err["error"])
        await self.call("PUT", f"/credentials/{secret.id}", json={**base, "name": secret.name}, expect=200)
        self.assertEqual(self.secrets()[0].meta["deploymentId"], did)
        await self.call("DELETE", f"/deployments/{did}", "operator", expect=200)
        self.assertEqual(self.secrets(), [])

    async def test_names_do_not_clash_with_shared_secrets(self):
        await self.call("POST", "/credentials", json={"kind": "secret", "type": "generic", "name": "shop-env",
                                                       "fields": {"value": "shared"}}, expect=201)
        _, dep = await self.call("POST", "/deployments", "operator", json=compose(variables=[
            {"key": "DB_PASSWORD", "value": "x", "secret": True}]), expect=201)
        self.assertEqual(dep["variables"][0]["value"], "[[shop-env-2.DB_PASSWORD]]")

    async def test_container_env_secret_reaches_the_container(self):
        self.engine.add_image("nginx:1.27")
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "api", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27", "env": [{"key": "MODE", "value": "prod"},
                                                    {"key": "API_KEY", "value": "k3y", "secret": True}]}}, expect=201)
        self.assertEqual(dep["spec"]["env"][1], {"key": "API_KEY", "value": "[[api-env.API_KEY]]", "secret": True})
        await self.svc.deployments.deploy(dep["id"])
        env = self.engine.container("api")["Config"]["Env"]
        self.assertIn("API_KEY=k3y", env)
        self.assertIn("MODE=prod", env)

    async def test_revisions_and_rollback(self):
        _, dep = await self.call("POST", "/deployments", "operator", json=compose(variables=[
            {"key": "TAG", "value": "16"}, {"key": "PW", "value": "x", "secret": True}]), expect=201)
        did = dep["id"]
        await self.call("PUT", f"/deployments/{did}", "operator", json=compose(variables=[
            {"key": "TAG", "value": "17"}, {"key": "PW", "value": "", "secret": True}]), expect=200)
        _, revs = await self.call("GET", f"/deployments/{did}/revisions", "operator", expect=200)
        self.assertEqual(len(revs), 2)
        _, rev = await self.call("GET", f"/deployments/{did}/revisions/1", "operator", expect=200)
        self.assertEqual(rev["config"]["variables"], [{"key": "TAG", "value": "16"},
                                                      {"key": "PW", "value": "[[shop-env.PW]]"}])
        _, rev = await self.call("GET", f"/deployments/{did}/revisions/1", "viewer", expect=200)
        self.assertEqual({v["value"] for v in rev["config"]["variables"]}, {"********"})
        await self.call("POST", f"/deployments/{did}/revisions/1/rollback", "operator", json={"deploy": False}, expect=200)
        back = self.svc.store.get_deployment(did)
        self.assertEqual([(v.key, v.value) for v in back.variables], [("TAG", "16"), ("PW", "[[shop-env.PW]]")])


if __name__ == "__main__":
    unittest.main()
