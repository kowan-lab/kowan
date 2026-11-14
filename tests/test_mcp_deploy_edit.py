"""MCP: read a deployment's configuration and save a new version of it."""

from tests.test_mcp_e2e import MCPCase

COMPOSE = "services:\n  web:\n    image: nginx:1.27\n    environment:\n      TOKEN: real-secret\n"


class TestUpdateDeployment(MCPCase):
    async def test_read_change_and_save_a_revision(self):
        _, dep = await self.call("POST", "/deployments", "admin", json={
            "name": "shop", "type": "compose", "strategy": "standalone", "targets": ["local"], "compose": COMPOSE}, expect=201)
        await self.tool("get_deployment_config", "viewer", ok=False, deployment="shop")
        cfg, _ = await self.tool("get_deployment_config", "operator", deployment="shop")
        self.assertIn("real-secret", cfg["compose"])
        base = cfg["revision"]
        new = cfg["compose"].replace("nginx:1.27", "nginx:1.28")
        # Stale base, a masked copy, nothing to change: refused.
        await self.tool("update_deployment", "operator", ok=False, deployment="shop", baseRevision=base + 5, compose=new)
        await self.tool("update_deployment", "operator", ok=False, deployment="shop", baseRevision=base,
                        compose=new.replace("real-secret", "********"))
        await self.tool("update_deployment", "operator", ok=False, deployment="shop", baseRevision=base)
        await self.tool("update_deployment", "operator", ok=False, deployment="shop", baseRevision=base, compose="not: [valid")
        out, _ = await self.tool("update_deployment", "operator", deployment="shop", baseRevision=base, compose=new,
                                 note="nginx 1.28", deploy=False)
        self.assertEqual((out["revision"], out["changed"]), (base + 1, True))
        stored = self.svc.store.get_deployment(dep["id"])
        self.assertIn("nginx:1.28", stored.compose)
        self.assertIn("real-secret", stored.compose)
        rev = self.svc.store.list_deployment_revisions(dep["id"])[0]
        self.assertEqual((rev["note"], rev["source"]), ("nginx 1.28", "edit"))
        self.assertIn("(mcp)", rev["author"])
        out, _ = await self.tool("update_deployment", "operator", deployment="shop", baseRevision=base + 1,
                                 variables=[{"key": "TAG", "value": "1.28"}], deploy=False)
        self.assertEqual(self.svc.store.get_deployment(dep["id"]).variables[0].key, "TAG")
