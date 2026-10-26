"""MCP server tests: JSON-RPC framing, Streamable HTTP lifecycle + session
headers, auth/origin/accept guards, role filtering and enforcement, the
destructive confirm guard, output bounding and audit entries.

The whole control plane is booted in-process (like test_integration) with a
fake Docker provider, so no daemon is needed.
"""

from __future__ import annotations

import datetime
import json
import tempfile
import unittest
import uuid

from aiohttp.test_utils import TestClient, TestServer

from kowan.auth import generate_api_key_secret, hash_api_key_secret
from kowan.config import ServerConfig
from kowan.models import APIKey, Container, ContainerStats, DiskUsage, Image, Port, SystemInfo
from kowan.mcp.registry import bound_text, validate
from kowan.mcp.tools._common import parse_since
from kowan.server import build_app
from kowan.store.audit import AuditListOptions

PV = "2025-06-18"
SECRET_ENV_VALUE = "hunter2-super-secret"


# --------------------------------------------------------------------------- fakes


class FakeStream:
    def __init__(self, data: bytes):
        self._chunks = [data[i: i + 4096] for i in range(0, len(data), 4096)]
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self):
        self.closed = True


class FakeOps:
    def __init__(self):
        self.calls = []
        self.log_lines = 5000

    async def ping(self):
        return None

    async def list_containers(self, all):
        return [
            Container(id="a" * 64, names=["/web"], image="nginx:1.27", state="running", status="Up 2 hours",
                      ports=[Port(privatePort=80, publicPort=8080, type="tcp", ip="0.0.0.0")],
                      labels={"com.docker.compose.project": "site"}),
            Container(id="b" * 64, names=["/worker"], image="busybox", state="exited", status="Exited (137) 5 minutes ago"),
            Container(id="c" * 64, names=["/db"], image="postgres:16", state="running", status="Up 1 day (unhealthy)"),
        ]

    async def inspect_container(self, id):
        return {
            "Id": "a" * 64, "Name": "/web", "Image": "sha256:" + "f" * 64, "Created": "2026-01-01T00:00:00Z",
            "RestartCount": 3,
            "Config": {"Image": "nginx:1.27", "Env": [f"DB_PASSWORD={SECRET_ENV_VALUE}", "TZ=UTC"],
                       "Labels": {"com.docker.compose.project": "site"}},
            "State": {"Status": "running", "Running": True, "ExitCode": 0,
                      "Health": {"Status": "unhealthy", "FailingStreak": 2,
                                 "Log": [{"ExitCode": 1, "Output": "connection refused", "End": "x"}]}},
            "HostConfig": {"RestartPolicy": {"Name": "always"}},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]},
                                "Networks": {"site_default": {"IPAddress": "172.18.0.2"}}},
            "Mounts": [{"Type": "volume", "Name": "data", "Destination": "/data", "RW": True}],
        }

    async def container_logs(self, id, follow, tail):
        n = self.log_lines if tail <= 0 else min(tail, self.log_lines)
        start = self.log_lines - n
        lines = []
        for i in range(start, self.log_lines):
            msg = "ERROR boom" if i % 100 == 0 else f"line {i}"
            lines.append(f"2026-10-25T10:{(i // 60) % 60:02d}:{i % 60:02d}.123456789Z {msg}\n")
        return FakeStream("".join(lines).encode())

    async def container_stats(self, id):
        return ContainerStats(cpuPercent=12.5, memUsage=100 << 20, memLimit=1 << 30, memPercent=9.7)

    async def system_info(self):
        return SystemInfo(containers=3, containersRunning=2, serverVersion="27.0")

    async def disk_usage(self):
        return DiskUsage(imagesReclaimable=1 << 30, volumesReclaimable=0, buildCacheSize=0)

    async def list_images(self):
        return [Image(id="sha256:" + "1" * 64, repoTags=["nginx:1.27"], size=200 << 20, created=1700000000)]

    async def _record(self, name, *args):
        self.calls.append((name,) + args)

    async def stop_container(self, id):
        await self._record("stop", id)

    async def start_container(self, id):
        await self._record("start", id)

    async def remove_container(self, id, force):
        await self._record("remove", id, force)

    async def prune_images(self, all):
        await self._record("prune_images", all)
        return 1234


class FakeProvider:
    def __init__(self):
        self.fake = FakeOps()

    def ops(self, host):
        return self.fake


def _cfg():
    return ServerConfig(
        addr="127.0.0.1:0", data_dir=tempfile.mkdtemp(), jwt_secret="x" * 48,
        admin_user="admin", admin_password="admin", seed_admin=True,
        monitor_interval=datetime.timedelta(seconds=3600),
        metrics_retention=datetime.timedelta(hours=1), secret_key="", external_url="",
    )


# --------------------------------------------------------------------------- base


class MCPBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app, self.svc = await build_app(_cfg())
        self.provider = FakeProvider()
        self.svc.docker = self.provider
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.keys = {role: self._make_key(role) for role in ("viewer", "operator", "admin")}

    async def asyncTearDown(self):
        await self.client.close()

    def _make_key(self, role):
        secret, prefix = generate_api_key_secret()
        key = APIKey(id=str(uuid.uuid4()), name=f"{role}-key", prefix=prefix, role=role,
                     createdAt="2026-01-01T00:00:00Z")
        self.svc.store.create_api_key(key, hash_api_key_secret(secret))
        return secret

    def headers(self, role="admin", session=None, extra=None):
        h = {"Authorization": f"Bearer {self.keys[role]}", "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if session:
            h["Mcp-Session-Id"] = session
            h["MCP-Protocol-Version"] = PV
        h.update(extra or {})
        return h

    async def post_raw(self, body, headers):
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return await self.client.post("/mcp", data=data, headers=headers)

    async def initialize(self, role="admin", version=PV):
        r = await self.post_raw({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": version, "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "1"}}},
                                self.headers(role))
        self.assertEqual(r.status, 200, await r.text())
        sid = r.headers.get("Mcp-Session-Id")
        self.assertTrue(sid)
        body = await r.json()
        r2 = await self.post_raw({"jsonrpc": "2.0", "method": "notifications/initialized"},
                                 self.headers(role, sid))
        self.assertEqual(r2.status, 202)
        self.assertEqual(await r2.text(), "")
        return sid, body

    async def rpc(self, method, params=None, role="admin", session=None, msg_id=7):
        if session is None:
            session, _ = await self.initialize(role)
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        r = await self.post_raw(msg, self.headers(role, session))
        self.assertEqual(r.status, 200, await r.text())
        return await r.json()

    async def call(self, name, args=None, role="admin", session=None):
        resp = await self.rpc("tools/call", {"name": name, "arguments": args or {}}, role, session)
        return resp

    def audit_entries(self):
        entries, _ = self.svc.store.list_audit(AuditListOptions(limit=100))
        return [e for e in entries if e.action == "MCP"]


# --------------------------------------------------------------------------- tests


class TestFraming(MCPBase):
    async def test_parse_error(self):
        sid, _ = await self.initialize()
        r = await self.post_raw("{not json", self.headers(session=sid))
        self.assertEqual(r.status, 400)
        self.assertEqual((await r.json())["error"]["code"], -32700)

    async def test_invalid_request_shapes(self):
        sid, _ = await self.initialize()
        for bad in ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
                    {"id": 1, "method": "ping"},
                    {"jsonrpc": "2.0", "id": None, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]}):
            r = await self.post_raw(bad, self.headers(session=sid))
            self.assertEqual(r.status, 400, bad)
            self.assertEqual((await r.json())["error"]["code"], -32600, bad)

    async def test_method_not_found(self):
        resp = await self.rpc("nope/nothing")
        self.assertEqual(resp["error"]["code"], -32601)
        self.assertEqual(resp["id"], 7)

    async def test_invalid_params(self):
        resp = await self.rpc("tools/call", {"arguments": {}})
        self.assertEqual(resp["error"]["code"], -32602)
        resp = await self.rpc("tools/call", {"name": "list_hosts", "arguments": [1]})
        self.assertEqual(resp["error"]["code"], -32602)
        resp = await self.rpc("tools/call", {"name": "does_not_exist"})
        self.assertEqual(resp["error"]["code"], -32602)
        r = await self.post_raw({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, self.headers())
        self.assertEqual((await r.json())["error"]["code"], -32602)
        self.assertNotIn("Mcp-Session-Id", r.headers)

    async def test_ping(self):
        resp = await self.rpc("ping")
        self.assertEqual(resp["result"], {})


class TestLifecycle(MCPBase):
    async def test_initialize_result(self):
        _, body = await self.initialize()
        res = body["result"]
        self.assertEqual(res["protocolVersion"], PV)
        self.assertIn("tools", res["capabilities"])
        self.assertIn("resources", res["capabilities"])
        self.assertIn("prompts", res["capabilities"])
        self.assertEqual(res["serverInfo"]["name"], "kowan")

    async def test_version_negotiation(self):
        _, body = await self.initialize(version="2024-11-05")
        self.assertEqual(body["result"]["protocolVersion"], "2024-11-05")
        _, body = await self.initialize(version="1999-01-01")
        self.assertEqual(body["result"]["protocolVersion"], PV)

    async def test_session_required_and_validated(self):
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        r = await self.post_raw(msg, self.headers())
        self.assertEqual(r.status, 400)
        r = await self.post_raw(msg, self.headers(session="bogus"))
        self.assertEqual(r.status, 404)
        # A session is bound to the principal that created it.
        sid, _ = await self.initialize("admin")
        r = await self.post_raw(msg, self.headers("viewer", sid))
        self.assertEqual(r.status, 404)

    async def test_protocol_version_header(self):
        sid, _ = await self.initialize()
        r = await self.post_raw({"jsonrpc": "2.0", "id": 2, "method": "ping"},
                                self.headers(session=sid, extra={"MCP-Protocol-Version": "2000-01-01"}))
        self.assertEqual(r.status, 400)
        h = self.headers(session=sid)
        del h["MCP-Protocol-Version"]  # absent header is fine
        r = await self.post_raw({"jsonrpc": "2.0", "id": 2, "method": "ping"}, h)
        self.assertEqual(r.status, 200)

    async def test_get_405_and_delete(self):
        r = await self.client.get("/mcp", headers=self.headers())
        self.assertEqual(r.status, 405)
        sid, _ = await self.initialize()
        r = await self.client.delete("/mcp", headers=self.headers("viewer", sid))
        self.assertEqual(r.status, 404)  # not the owner
        r = await self.client.delete("/mcp", headers=self.headers(session=sid))
        self.assertEqual(r.status, 204)
        r = await self.post_raw({"jsonrpc": "2.0", "id": 2, "method": "ping"}, self.headers(session=sid))
        self.assertEqual(r.status, 404)
        r = await self.client.delete("/mcp", headers=self.headers())
        self.assertEqual(r.status, 400)

    async def test_client_response_message_is_202(self):
        sid, _ = await self.initialize()
        r = await self.post_raw({"jsonrpc": "2.0", "id": 99, "result": {}}, self.headers(session=sid))
        self.assertEqual(r.status, 202)


class TestGuards(MCPBase):
    async def test_auth_required(self):
        r = await self.post_raw({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                {"Content-Type": "application/json", "Accept": "application/json"})
        self.assertEqual(r.status, 401)
        self.assertIn("Bearer", r.headers.get("WWW-Authenticate", ""))
        r = await self.post_raw({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                self.headers(extra={"Authorization": "Bearer pk_wrong"}))
        self.assertEqual(r.status, 401)

    async def test_jwt_login_token_accepted(self):
        r = await self.client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        token = (await r.json())["token"]
        h = self.headers(extra={"Authorization": f"Bearer {token}"})
        r = await self.post_raw({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": PV}}, h)
        self.assertEqual(r.status, 200)
        h["Mcp-Session-Id"] = r.headers["Mcp-Session-Id"]
        r = await self.post_raw({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, h)
        names = {t["name"] for t in (await r.json())["result"]["tools"]}
        self.assertIn("remove_container", names)  # admin user

    async def test_origin(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PV}}
        r = await self.post_raw(init, self.headers(extra={"Origin": "http://evil.example"}))
        self.assertEqual(r.status, 403)
        host = f"{self.client.server.host}:{self.client.server.port}"
        r = await self.post_raw(init, self.headers(extra={"Origin": f"http://{host}"}))
        self.assertEqual(r.status, 200)
        # Loopback dev proxy (vite on :5173 -> server on loopback).
        r = await self.post_raw(init, self.headers(extra={"Origin": "http://localhost:5173"}))
        self.assertEqual(r.status, 200)

    async def test_accept_and_content_type(self):
        sid, _ = await self.initialize()
        msg = {"jsonrpc": "2.0", "id": 3, "method": "ping"}
        r = await self.post_raw(msg, self.headers(session=sid, extra={"Accept": "text/event-stream"}))
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/event-stream"))
        text = await r.text()
        self.assertTrue(text.startswith("event: message\ndata: "))
        self.assertEqual(json.loads(text.split("data: ", 1)[1])["id"], 3)
        r = await self.post_raw(msg, self.headers(session=sid, extra={"Accept": "text/html"}))
        self.assertEqual(r.status, 406)
        r = await self.post_raw(msg, self.headers(session=sid, extra={"Content-Type": "text/plain"}))
        self.assertEqual(r.status, 415)
        r = await self.post_raw(msg, self.headers(session=sid))
        self.assertTrue(r.headers["Content-Type"].startswith("application/json"))


class TestRoles(MCPBase):
    async def tool_names(self, role):
        resp = await self.rpc("tools/list", role=role)
        return {t["name"]: t for t in resp["result"]["tools"]}

    async def test_tools_list_filtered_by_role(self):
        viewer = await self.tool_names("viewer")
        operator = await self.tool_names("operator")
        admin = await self.tool_names("admin")
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in viewer.values()))
        self.assertIn("fleet_summary", viewer)
        self.assertNotIn("container_action", viewer)
        self.assertIn("container_action", operator)
        self.assertNotIn("remove_container", operator)
        self.assertTrue(set(viewer) < set(operator) < set(admin))
        for name in ("remove_container", "remove_image", "remove_volume", "prune", "destroy_deployment"):
            t = admin[name]
            self.assertTrue(t["annotations"]["destructiveHint"])
            self.assertIn("confirm", t["inputSchema"]["properties"])
        self.assertIn("outputSchema", admin["fleet_summary"])

    async def test_call_enforces_role_and_audits(self):
        resp = await self.call("container_action", {"container": "web", "action": "stop"}, role="viewer")
        self.assertEqual(resp["error"]["code"], -32003)
        self.assertEqual(self.provider.fake.calls, [])
        resp = await self.call("remove_container", {"container": "web", "confirm": True}, role="operator")
        self.assertEqual(resp["error"]["code"], -32003)
        statuses = sorted(e.status for e in self.audit_entries())
        self.assertEqual(statuses, [403, 403])

    async def test_operator_action_audited(self):
        resp = await self.call("container_action", {"container": "web", "action": "stop"}, role="operator")
        self.assertFalse(resp["result"]["isError"], resp)
        self.assertEqual(self.provider.fake.calls, [("stop", "web")])
        entries = self.audit_entries()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual((e.status, e.username, e.path), (200, "key:operator-key", "/mcp/tools/container_action"))
        self.assertIn("source=mcp", e.detail)
        # Read-only calls are not audited.
        await self.call("list_containers", role="operator")
        self.assertEqual(len(self.audit_entries()), 1)


class TestConfirmAndValidation(MCPBase):
    async def test_confirm_guard(self):
        resp = await self.call("remove_container", {"container": "web"})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("confirm", resp["result"]["content"][0]["text"])
        resp = await self.call("remove_container", {"container": "web", "confirm": False})
        self.assertTrue(resp["result"]["isError"])
        self.assertEqual(self.provider.fake.calls, [])
        self.assertEqual(self.audit_entries(), [])
        resp = await self.call("remove_container", {"container": "web", "confirm": True, "force": True})
        self.assertFalse(resp["result"]["isError"], resp)
        self.assertEqual(self.provider.fake.calls, [("remove", "web", True)])
        self.assertEqual([e.status for e in self.audit_entries()], [200])

    async def test_prune(self):
        resp = await self.call("prune", {"kind": "images", "all_images": True, "confirm": True})
        self.assertEqual(resp["result"]["structuredContent"]["spaceReclaimed"], 1234)

    async def test_argument_validation(self):
        resp = await self.call("container_logs", {"container": "web", "tail": 5000})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("tail", resp["result"]["content"][0]["text"])
        resp = await self.call("list_containers", {"bogus": 1})
        self.assertTrue(resp["result"]["isError"])
        resp = await self.call("container_action", {"container": "web", "action": "explode"})
        self.assertTrue(resp["result"]["isError"])
        resp = await self.call("inspect_container", {})
        self.assertTrue(resp["result"]["isError"])

    async def test_unknown_host_is_tool_error(self):
        resp = await self.call("list_containers", {"host": "nowhere"})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("not found", resp["result"]["content"][0]["text"])


class TestReadTools(MCPBase):
    async def test_logs_bounded(self):
        resp = await self.call("container_logs", {"container": "web"})
        data = resp["result"]["structuredContent"]
        self.assertEqual(data["returned"], 200)
        self.assertEqual(data["lines"][-1], "line 4999")
        resp = await self.call("container_logs", {"container": "web", "tail": 2000})
        self.assertEqual(resp["result"]["structuredContent"]["returned"], 2000)
        resp = await self.call("container_logs", {"container": "web", "grep": "error", "tail": 5})
        data = resp["result"]["structuredContent"]
        self.assertEqual(data["returned"], 5)
        self.assertTrue(all("ERROR" in l for l in data["lines"]))
        self.assertTrue(data["truncated"])

    async def test_inspect_hides_env_values(self):
        resp = await self.call("inspect_container", {"container": "web"})
        res = resp["result"]
        self.assertNotIn(SECRET_ENV_VALUE, json.dumps(res))
        data = res["structuredContent"]
        self.assertEqual(data["envKeys"], ["DB_PASSWORD", "TZ"])
        self.assertEqual(data["restartCount"], 3)
        self.assertEqual(data["health"]["status"], "unhealthy")
        self.assertEqual(data["ports"], ["0.0.0.0:8080->80/tcp"])

    async def test_list_containers_filters_and_paging(self):
        resp = await self.call("list_containers", {"state": "exited"})
        items = resp["result"]["structuredContent"]["items"]
        self.assertEqual([(i["name"], i["exitCode"]) for i in items], [("worker", 137)])
        resp = await self.call("list_containers", {"limit": 1})
        data = resp["result"]["structuredContent"]
        self.assertEqual((data["returned"], data["total"], data["truncated"], data["nextOffset"]), (1, 3, True, 1))
        resp = await self.call("list_containers", {"all_hosts": True, "name": "WE"})
        self.assertEqual([i["name"] for i in resp["result"]["structuredContent"]["items"]], ["web"])

    async def test_fleet_summary(self):
        resp = await self.call("fleet_summary", role="viewer")
        data = resp["result"]["structuredContent"]
        self.assertEqual(data["hosts"], {"total": 1, "online": 1})
        self.assertEqual(data["containers"], {"running": 2, "total": 3})
        crashed = {c["name"]: c for c in data["crashed"]}
        self.assertEqual(crashed["worker"]["exitCode"], 137)
        self.assertTrue(crashed["db"]["unhealthy"])
        self.assertEqual(data["reclaimableBytes"], 1 << 30)

    async def test_hosts_stats_images_deployments_templates_alerts(self):
        for name, args in [("list_hosts", {}), ("get_host", {"host": "local"}), ("container_stats", {"container": "web"}),
                           ("list_images", {}), ("list_deployments", {}), ("list_templates", {"limit": 3}),
                           ("list_alerts", {"status": "all"}), ("list_workflows", {})]:
            resp = await self.call(name, args, role="viewer")
            self.assertFalse(resp["result"]["isError"], (name, resp))
            self.assertIsInstance(resp["result"]["structuredContent"], dict)
        resp = await self.call("get_host", {"host": "local"})  # by name works too
        self.assertTrue(resp["result"]["structuredContent"]["online"])

    async def test_create_and_get_deployment_hides_env(self):
        spec = {"image": "nginx:alpine", "env": [{"key": "API_TOKEN", "value": SECRET_ENV_VALUE}]}
        resp = await self.call("create_deployment", {"name": "demo", "type": "container", "spec": spec})
        self.assertFalse(resp["result"]["isError"], resp)
        self.assertNotIn(SECRET_ENV_VALUE, json.dumps(resp))
        resp = await self.call("get_deployment", {"deployment": "demo"}, role="viewer")
        self.assertNotIn(SECRET_ENV_VALUE, json.dumps(resp))
        self.assertEqual(resp["result"]["structuredContent"]["spec"]["env"], ["API_TOKEN"])
        audits = self.audit_entries()
        self.assertEqual(len(audits), 1)
        self.assertNotIn(SECRET_ENV_VALUE, audits[0].detail)
        resp = await self.call("create_deployment", {"name": "demo", "type": "container", "spec": spec})
        self.assertTrue(resp["result"]["isError"])  # duplicate name


class TestResourcesPrompts(MCPBase):
    async def test_resources(self):
        sid, _ = await self.initialize("viewer")
        resp = await self.rpc("resources/list", role="viewer", session=sid)
        uris = {r["uri"] for r in resp["result"]["resources"]}
        self.assertIn("kowan://fleet/summary", uris)
        resp = await self.rpc("resources/read", {"uri": "kowan://fleet/summary"}, role="viewer", session=sid)
        content = resp["result"]["contents"][0]
        self.assertEqual(json.loads(content["text"])["hosts"]["total"], 1)
        resp = await self.rpc("resources/read", {"uri": "kowan://hosts/local/containers"}, role="viewer", session=sid)
        self.assertEqual(json.loads(resp["result"]["contents"][0]["text"])["total"], 3)
        resp = await self.rpc("resources/read", {"uri": "kowan://nope"}, role="viewer", session=sid)
        self.assertEqual(resp["error"]["code"], -32002)
        resp = await self.rpc("resources/templates/list", role="viewer", session=sid)
        self.assertTrue(resp["result"]["resourceTemplates"])

    async def test_prompts(self):
        sid, _ = await self.initialize("viewer")
        resp = await self.rpc("prompts/list", role="viewer", session=sid)
        names = {p["name"] for p in resp["result"]["prompts"]}
        self.assertEqual(names, {"diagnose_container", "fleet_health_report", "update_review"})
        resp = await self.rpc("prompts/get", {"name": "diagnose_container", "arguments": {"container": "web"}},
                              role="viewer", session=sid)
        self.assertIn("web", resp["result"]["messages"][0]["content"]["text"])
        resp = await self.rpc("prompts/get", {"name": "diagnose_container"}, role="viewer", session=sid)
        self.assertEqual(resp["error"]["code"], -32602)


class TestUnits(unittest.TestCase):
    def test_validate(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 3},
                                                   "s": {"type": "string", "enum": ["a"]}},
                  "required": ["n"], "additionalProperties": False}
        self.assertEqual(validate(schema, {"n": 2, "s": "a"}), [])
        self.assertTrue(validate(schema, {}))
        self.assertTrue(validate(schema, {"n": True}))
        self.assertTrue(validate(schema, {"n": 9}))
        self.assertTrue(validate(schema, {"n": 1, "s": "b"}))
        self.assertTrue(validate(schema, {"n": 1, "x": 0}))

    def test_bound_text(self):
        text, cut = bound_text("x" * 100, limit=10)
        self.assertTrue(cut)
        self.assertTrue(text.startswith("x" * 10))
        self.assertIn("truncated", text)
        self.assertEqual(bound_text("short", limit=10), ("short", False))

    def test_reused_service_helpers_exist(self):
        # The MCP tools reuse these REST-layer helpers; fail loudly if a refactor
        # renames them (other modules are being reworked concurrently).
        from kowan.api import deployments, events, updates

        for name in ("check_host", "recreate_with_image", "_drain_pull", "parse_image_ref",
                     "compare_digests", "_remote_digest", "friendly_registry_error", "_CACHE"):
            self.assertTrue(hasattr(updates, name), name)
        for name in ("_Request", "_validate", "_check_resource_name_collision", "_now_rfc3339",
                     "_generate_redeploy_token", "STATUS_STOPPED"):
            self.assertTrue(hasattr(deployments, name), name)
        for name in ("parse_event_line", "_clamp_since", "DEFAULT_LOOKBACK", "HISTORY_TIMEOUT"):
            self.assertTrue(hasattr(events, name), name)

    def test_parse_since(self):
        self.assertEqual(parse_since("15m", now=1000.0), 100.0)
        self.assertEqual(parse_since(500), 500.0)
        self.assertEqual(parse_since("2026-01-01T00:00:00Z"), 1767225600.0)
        self.assertIsNone(parse_since(""))
        with self.assertRaises(Exception):
            parse_since("yesterday-ish")


if __name__ == "__main__":
    unittest.main()
