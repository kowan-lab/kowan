"""Every MCP tool once, end to end: JSON-RPC over /mcp -> tool -> LocalClient
-> fake Engine (and the deployment / workflow services), checking results,
the role gates and the destructive-confirm guard."""

import asyncio
import json
import unittest
import uuid

from kowan.auth import generate_api_key_secret, hash_api_key_secret
from kowan.models import APIKey
from tests.apicase import ServerCase
from tests.fakecli import FakeDockerCLI

PV = "2025-06-18"


class MCPCase(ServerCase):
    async def asyncSetUp(self):
        self.cli = FakeDockerCLI()
        await super().asyncSetUp()
        self.keys = {}
        for role in ("viewer", "operator", "admin"):
            secret, prefix = generate_api_key_secret()
            self.svc.store.create_api_key(APIKey(id=str(uuid.uuid4()), name=f"{role}-mcp", prefix=prefix, role=role,
                                                 createdAt="2026-01-01T00:00:00Z"), hash_api_key_secret(secret))
            self.keys[role] = secret
        self.sessions = {}

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.cli.close()

    def hdr(self, role, sid=None):
        h = {"Authorization": "Bearer " + self.keys[role], "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if sid:
            h.update({"Mcp-Session-Id": sid, "MCP-Protocol-Version": PV})
        return h

    async def session(self, role):
        if role not in self.sessions:
            r = await self.client.post("/mcp", data=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": PV, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}),
                headers=self.hdr(role))
            sid = r.headers["Mcp-Session-Id"]
            await self.client.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                                   headers=self.hdr(role, sid))
            self.sessions[role] = sid
        return self.sessions[role]

    async def tool(self, tool_name, role="admin", ok=True, **args):
        sid = await self.session(role)
        r = await self.client.post("/mcp", data=json.dumps({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": tool_name, "arguments": args}}),
            headers=self.hdr(role, sid))
        resp = await r.json()
        result = resp.get("result") or {}
        failed = "error" in resp or bool(result.get("isError"))  # a JSON-RPC error or a tool error
        if ok is not None:
            self.assertEqual(not failed, ok, f"{tool_name}: {json.dumps(resp)[:600]}")
        return result.get("structuredContent") or {}, result


class TestReadTools(MCPCase):
    async def test_read_tools(self):
        self.engine.add_image("nginx:1.27")
        cid = self.engine.add_container("web", "nginx:1.27", ports=[(80, 8080)], env=["SECRET=s3cr3t"],
                                        logs=["GET / 200", "warn: slow"])
        self.engine.add_volume("data")
        self.engine.add_network("front")
        out, _ = await self.tool("list_hosts", "viewer")
        self.assertIn("local", json.dumps(out))
        await self.tool("get_host", "viewer", host="local")
        out, _ = await self.tool("list_containers", "viewer", all_hosts=True, include_stopped=True)
        self.assertIn("web", json.dumps(out))
        out, raw = await self.tool("inspect_container", "viewer", container="web")
        self.assertNotIn("s3cr3t", json.dumps(raw))
        out, raw = await self.tool("container_logs", "viewer", container=cid, tail=50, grep="slow")
        self.assertIn("slow", json.dumps(raw))
        await self.tool("container_stats", "viewer", container=cid)
        for name in ("list_images", "list_volumes", "list_networks", "fleet_summary", "list_deployments",
                     "list_templates", "list_alerts", "list_workflows"):
            await self.tool(name, "viewer")
        self.engine.emit("container", "start", cid, {"name": "web"})
        out, _ = await self.tool("recent_events", "viewer", since="10m", type="container")
        await self.tool("check_updates", "viewer", only_available=False)
        await self.tool("inspect_container", "viewer", ok=False, container="missing")


class TestActionTools(MCPCase):
    async def test_actions_and_admin_tools(self):
        self.engine.add_image("nginx:1.27")
        cid = self.engine.add_container("web", "nginx:1.27")
        # Role gate: a viewer cannot act.
        await self.tool("container_action", "viewer", ok=False, container=cid, action="stop")
        for action in ("stop", "start", "restart", "pause", "unpause", "kill"):
            await self.tool("container_action", "operator", container=cid, action=action)
        await self.tool("pull_image", "operator", ref="redis:7")
        self.assertTrue(self.engine.image_by_ref("redis:7"))

        out, _ = await self.tool("create_deployment", name="mcp-app", type="container",
                                 spec={"image": "nginx:1.27"}, deploy=False)
        dep_id = out["deployment"]["id"]
        await self.tool("get_deployment", "viewer", deployment="mcp-app")
        await self.tool("deployment_action", "operator", deployment="mcp-app", action="deploy")
        for _ in range(100):
            if self.svc.store.get_deployment(dep_id).status == "deployed":
                break
            await asyncio.sleep(0.05)
        await self.tool("deployment_action", "operator", deployment="mcp-app", action="stop")
        await self.tool("deployment_action", "operator", deployment="mcp-app", action="start")
        out, _ = await self.tool("deploy_template", template="builtin:whoami", name="who", deploy=False)
        self.assertEqual(out["template"], "builtin:whoami")
        await self.tool("deploy_template", ok=False, template="builtin:nope")

        # Destructive tools need confirm=true.
        await self.tool("remove_container", ok=False, container=cid)
        self.engine.containers[cid]["State"].update(Running=False, Status="exited")
        await self.tool("remove_container", container=cid, confirm=True, force=True)
        self.assertIsNone(self.engine.container("web"))
        self.engine.add_volume("junk")
        await self.tool("remove_volume", volume="junk", confirm=True)
        await self.tool("remove_image", image="redis:7", confirm=True, force=True)
        out, _ = await self.tool("prune", kind="images", confirm=True)
        await self.tool("prune", ok=False, kind="images")
        await self.tool("destroy_deployment", deployment="mcp-app", confirm=True, delete_record=True)
        await self.tool("get_deployment", "viewer", ok=False, deployment="mcp-app")

        _, wf = await self.call("POST", "/workflows", "admin", json={
            "name": "mcp-wf", "enabled": True, "steps": [{"name": "b", "type": "backup", "config": {}}]}, expect=201)
        # A backup step is admin-only, through MCP as through REST.
        await self.tool("run_workflow", "operator", ok=False, workflow="mcp-wf")
        out, _ = await self.tool("run_workflow", "admin", workflow="mcp-wf")
        self.assertTrue(out)

    async def test_update_container_tool(self):
        from unittest import mock
        from kowan.api import updates

        new = "sha256:" + "d" * 64
        self.engine.add_image("nginx:1.27")
        self.engine.registry["nginx:1.27"] = new
        cid = self.engine.add_container("web", "nginx:1.27")

        async def remote(store, ref):
            return new

        with mock.patch.object(updates, "_remote_digest", remote):
            out, raw = await self.tool("update_container", "operator", container=cid)
        self.assertTrue(out["updated"], raw)


class TestToolFilters(MCPCase):
    async def test_filters_and_details(self):
        self.engine.add_image("nginx:1.27")
        dangling = self.engine.add_image("<none>:<none>")
        self.engine.images[dangling]["RepoTags"] = []
        self.engine.add_volume("data-a")
        self.engine.add_volume("logs-b")
        self.engine.add_container("web", "nginx:1.27", ports=[(80, 8080), (443, 0)],
                                  logs=["2020-01-01T00:00:00Z old line", "x" * 5000])
        out, _ = await self.tool("list_images", "viewer", dangling=True)
        self.assertEqual(out["total"], 1)  # only the untagged one
        out, _ = await self.tool("list_images", "viewer", name="nginx")
        out, _ = await self.tool("list_volumes", "viewer", name="data")
        self.assertIn("data-a", str(out))
        self.assertNotIn("logs-b", str(out))
        await self.tool("list_networks", "viewer", limit=1)
        out, raw = await self.tool("container_logs", "viewer", container="web", since="2025-01-01T00:00:00Z")
        await self.tool("container_logs", "viewer", container="web", tail=5)
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "gitdep", "type": "compose", "strategy": "standalone", "targets": ["local"],
            "git": {"repoUrl": "https://u:secret@git.example.com/a.git", "ref": "main", "path": "c.yml"}})
        _, cdep = await self.call("POST", "/deployments", "operator", json={
            "name": "cdep", "type": "compose", "strategy": "standalone", "targets": ["local"],
            "compose": "services:\n  a:\n    image: nginx:1.27\n    environment:\n      PW: hunter2\n"})
        out, raw = await self.tool("get_deployment", "viewer", deployment="gitdep")
        self.assertNotIn("secret", str(raw))
        out, raw = await self.tool("get_deployment", "viewer", deployment="cdep")
        self.assertNotIn("hunter2", str(raw))
        await self.tool("list_deployments", "viewer", status="stopped", name="dep")
        await self.tool("list_templates", "viewer", category="web", query="nginx")
        await self.tool("list_alerts", "viewer", status="open", level="critical")
        await self.tool("list_workflows", "viewer")
        await self.tool("list_containers", "viewer", state="running", name="we")
        out, _ = await self.tool("fleet_summary", "viewer")
        self.assertTrue(out)

    async def test_admin_tool_paths(self):
        self.engine.add_image("nginx:1.27")
        for kind in ("containers", "volumes", "networks"):
            await self.tool("prune", kind=kind, confirm=True)
        out, _ = await self.tool("create_deployment", name="now", type="container",
                                 spec={"image": "nginx:1.27"}, deploy=True)
        self.assertTrue(out.get("deployed"))
        await self.tool("create_deployment", ok=False, name="now", type="container", spec={"image": "x"})
        await self.tool("create_deployment", ok=False, name="bad", type="container", spec="nope")
        out, _ = await self.tool("deploy_template", template="builtin:postgres", name="pg", deploy=False)
        self.assertIn("POSTGRES_PASSWORD", out.get("generatedSecrets", []))
        await self.tool("deploy_template", ok=False, template="builtin:nginx", name="n2",
                        inputs={"HTTP_PORT": "not-a-port"}, deploy=False)
        _, keep = await self.call("POST", "/deployments", "operator", json={
            "name": "keep", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}})
        await self.call("PUT", f"/deployments/{keep['id']}/protection", "operator", json={"protected": True})
        await self.tool("destroy_deployment", ok=False, deployment="keep", confirm=True, delete_record=True)
        out, _ = await self.tool("destroy_deployment", deployment="keep", confirm=True)
        self.assertEqual(out["status"], "destroyed")


if __name__ == "__main__":
    unittest.main()
