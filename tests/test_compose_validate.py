"""Compose validation: every lint rule with fixtures, line/column mapping, fixes,
the schema-lite fallback, docker CLI message mapping and the REST endpoint +
deployment gate."""

import asyncio
import unittest
from unittest import mock

from kowan.compose import SecretInfo, apply_patch, validate_text
from kowan.compose.lint import image_tag, mask_secret_tokens, published_ports
from kowan.compose.patch import apply_patch_data
from kowan.compose.positions import parse
from kowan.compose.structural import cli_available, locate, schema_lite


def run(text, **kw):
    kw.setdefault("use_cli", False)
    return asyncio.run(validate_text(text, **kw))


def codes(res):
    return [d.code for d in res.diagnostics]


def find(res, code):
    for d in res.diagnostics:
        if d.code == code:
            return d
    raise AssertionError(f"{code} not in {codes(res)}")


GOOD = """services:
  web:
    image: nginx:1.27
    restart: unless-stopped
    ports:
      - "8080:80"
"""


class TestPositions(unittest.TestCase):
    def test_key_and_item_positions(self):
        p = parse("services:\n  web:\n    image: x\n    ports:\n      - '80:80'\n      - 81\n")
        self.assertEqual(p.keys[("services", "web")], (2, 3))
        self.assertEqual(p.keys[("services", "web", "image")], (3, 5))
        self.assertEqual(p.values[("services", "web", "image")], (3, 12))
        self.assertEqual(p.keys[("services", "web", "ports", 1)], (6, 9))
        # Missing path -> deepest known ancestor.
        self.assertEqual(p.pos(["services", "web", "restart"]), (2, 3))

    def test_duplicates_and_errors(self):
        p = parse("services:\n  a:\n    image: x\n  a:\n    image: y\n")
        self.assertEqual(p.duplicates[0][1], (4, 3))
        p = parse("services:\n  a:\n    image: x\n   b: 1\n")
        self.assertIsNotNone(p.error)
        self.assertEqual(p.error_pos[0], 4)

    def test_secret_masking_keeps_positions(self):
        t = "a:\n  PW: [[db.pw]] # c\n"
        m = mask_secret_tokens(t)
        self.assertEqual(len(m), len(t))
        self.assertEqual(parse(m).doc, {"a": {"PW": "xxxxxxxxx"}})


class TestRules(unittest.TestCase):
    def test_clean_file(self):
        res = run(GOOD)
        self.assertEqual(res.diagnostics, [])
        self.assertTrue(res.ok)
        self.assertEqual(res.engine, "builtin")

    def test_image_tags(self):
        self.assertEqual(image_tag("nginx"), ("", False))
        self.assertEqual(image_tag("localhost:5000/app"), ("", False))
        self.assertEqual(image_tag("localhost:5000/app:1"), ("1", False))
        self.assertEqual(image_tag("a@sha256:abc"), ("", True))
        res = run("services:\n  a:\n    image: nginx\n    restart: always\n  b:\n    image: redis:latest\n    restart: always\n"
                  "  c:\n    image: x@sha256:00\n    restart: always\n  d:\n    image: ${IMG:-x}\n    restart: always\n")
        self.assertEqual(sorted(codes(res)), ["image-latest", "image-no-tag"])
        d = find(res, "image-no-tag")
        self.assertEqual((d.severity, d.path, d.line, d.column), ("warning", "services.a.image", 3, 12))

    def test_restart_missing_with_fix(self):
        res = run("services:\n  a:\n    image: x:1\n  b:\n    image: x:1\n    deploy:\n      restart_policy: {condition: any}\n")
        d = find(res, "restart-missing")
        self.assertEqual((d.severity, d.path, d.line), ("info", "services.a", 2))
        self.assertEqual(codes(res).count("restart-missing"), 1)
        fixed = apply_patch("services:\n  a:\n    image: x:1\n", d.fix.patch)
        self.assertIn("restart: unless-stopped", fixed)
        self.assertNotIn("restart-missing", codes(run(fixed)))

    def test_security(self):
        res = run("services:\n  a:\n    image: x:1\n    restart: always\n    privileged: true\n    network_mode: host\n"
                  "    pid: host\n    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock:ro\n"
                  "      - {type: bind, source: /run/docker.sock, target: /s}\n")
        self.assertEqual(sorted(codes(res)), ["docker-socket", "docker-socket", "network-host", "pid-host", "privileged"])
        self.assertTrue(all(d.severity == "warning" for d in res.diagnostics))
        self.assertEqual(find(res, "pid-host").line, 7)

    def test_published_ports_parsing(self):
        self.assertEqual(published_ports("8080:80"), [(8080, "tcp")])
        self.assertEqual(published_ports("127.0.0.1:53:53/udp"), [(53, "udp")])
        self.assertEqual(published_ports("80"), [])
        self.assertEqual(published_ports(80), [])
        self.assertEqual(published_ports("9000-9001:90-91"), [(9000, "tcp"), (9001, "tcp")])
        self.assertEqual(published_ports({"target": 80, "published": "7000"}), [(7000, "tcp")])
        self.assertEqual(published_ports("${P}:80"), [])

    def test_port_in_use_with_fix(self):
        text = "services:\n  a:\n    image: x:1\n    restart: always\n    ports:\n      - 127.0.0.1:8080:80/udp\n      - 9000:90\n"
        res = run(text, host_ports={(8080, "udp"): "other", (8081, "udp"): "x"})
        d = find(res, "port-in-use")
        self.assertEqual((d.severity, d.path, d.line, d.column), ("error", "services.a.ports[0]", 6, 9))
        self.assertIn("'other'", d.message)
        # 8081 is taken on the host too -> next free is 8082; ip and proto kept.
        self.assertEqual(d.fix.patch, [{"op": "set", "path": ["services", "a", "ports", 0], "value": "127.0.0.1:8082:80/udp"}])
        # tcp 8080 free: protocol matters.
        self.assertNotIn("port-in-use", codes(run(text, host_ports={(8080, "tcp"): "o"})))
        # Unknown host facts: the rule is skipped.
        self.assertNotIn("port-in-use", codes(run(text)))
        fixed = apply_patch(text, d.fix.patch)
        self.assertNotIn("port-in-use", codes(run(fixed, host_ports={(8080, "udp"): "other", (8081, "udp"): "x"})))

    def test_port_in_use_long_syntax(self):
        text = "services:\n  a:\n    image: x:1\n    restart: always\n    ports:\n      - target: 80\n        published: 8080\n"
        d = find(run(text, host_ports={(8080, "tcp"): "o"}), "port-in-use")
        self.assertEqual(d.fix.patch[0]["path"], ["services", "a", "ports", 0, "published"])
        self.assertEqual(d.fix.patch[0]["value"], 8081)

    def test_port_duplicate(self):
        res = run("services:\n  a:\n    image: x:1\n    restart: always\n    ports: ['80:80']\n"
                  "  b:\n    image: x:1\n    restart: always\n    ports: ['80:8080']\n")
        d = find(res, "port-duplicate")
        self.assertEqual(d.path, "services.b.ports[0]")

    def test_volumes(self):
        text = ("services:\n  a:\n    image: x:1\n    restart: always\n    volumes:\n      - data:/d\n"
                "      - /srv/missing:/m\n      - /srv/there:/t\n      - {type: bind, source: /srv/missing2, target: /x}\n"
                "      - ./rel:/r\nvolumes:\n  other: {}\n")
        res = run(text, path_exists=lambda p: p == "/srv/there")
        d = find(res, "volume-undeclared")
        self.assertEqual((d.severity, d.path, d.line), ("error", "services.a.volumes[0]", 6))
        self.assertEqual(d.fix.patch, [{"op": "set", "path": ["volumes", "data"], "value": {}}])
        missing = [x for x in res.diagnostics if x.code == "bind-path-missing"]
        self.assertEqual([(m.severity, m.path) for m in missing],
                         [("warning", "services.a.volumes[1]"), ("error", "services.a.volumes[3]")])
        # Not checkable -> no bind diagnostics at all.
        self.assertNotIn("bind-path-missing", codes(run(text)))
        fixed = apply_patch(text, d.fix.patch)
        self.assertNotIn("volume-undeclared", codes(run(fixed)))

    def test_depends_and_healthchecks(self):
        text = ("services:\n  web:\n    image: x:1\n    restart: always\n    depends_on: [db, ghost]\n"
                "  api:\n    image: x:1\n    restart: always\n    depends_on:\n      cache: {condition: service_healthy}\n"
                "      ok: {condition: service_healthy}\n"
                "  db:\n    image: p:1\n    restart: always\n  cache:\n    image: r:1\n    restart: always\n"
                "  ok:\n    image: r:1\n    restart: always\n    healthcheck: {test: [CMD, true]}\n")
        res = run(text)
        unknown = find(res, "depends-on-unknown")
        self.assertEqual((unknown.path, unknown.line, unknown.column), ("services.web.depends_on[1]", 5, 22))
        self.assertEqual(unknown.fix.patch, [{"op": "remove", "path": ["services", "web", "depends_on", 1]}])
        self.assertEqual(find(res, "healthcheck-missing").path, "services.db")
        req = find(res, "healthcheck-required")
        self.assertEqual((req.severity, req.path), ("warning", "services.api.depends_on.cache"))
        self.assertEqual(codes(res).count("healthcheck-required"), 1)
        fixed = apply_patch_data(parse(text).doc, unknown.fix.patch)
        self.assertEqual(fixed["services"]["web"]["depends_on"], ["db"])

    def test_container_names(self):
        text = ("services:\n  a:\n    image: x:1\n    restart: always\n    container_name: app\n"
                "  b:\n    image: x:1\n    restart: always\n    container_name: app\n"
                "  c:\n    image: x:1\n    restart: always\n    container_name: taken\n")
        res = run(text, host_container_names={"taken"})
        d = find(res, "container-name-dup")
        self.assertEqual((d.path, d.line), ("services.b.container_name", 9))
        self.assertEqual(d.fix.patch, [{"op": "remove", "path": ["services", "b", "container_name"]}])
        self.assertEqual(find(res, "container-name-in-use").path, "services.c.container_name")

    def test_env_interpolation(self):
        text = ("services:\n  a:\n    image: x:${TAG}\n    restart: always\n    environment:\n"
                "      A: ${A:-1}\n      B: ${B?need}\n      C: $$NOT\n      D: $D\n      E: ${KNOWN}\n"
                "    # F: ${COMMENTED}\n")
        res = run(text, env={"KNOWN": "1"})
        unset = [(d.message.split(" ")[0], d.line, d.column, d.path) for d in res.diagnostics if d.code == "env-unset"]
        self.assertEqual(unset, [("${TAG}", 3, 14, "services.a.image"), ("${D}", 9, 10, "services.a.environment.D")])

    def test_secrets(self):
        text = ("services:\n  a:\n    image: x:1\n    restart: always\n    environment:\n"
                "      A: [[db]]\n      B: '[[db.password]]'\n      C: [[db.nope]]\n      D: [[ghost]]\n"
                "      E: [[multi]]\n      F: [[vault.any]]\n      G: '[[plugin::x]]'\n")
        secrets = {"db": SecretInfo(keys={"password"}), "multi": SecretInfo(keys={"a", "b"}),
                   "vault": SecretInfo(external=True)}
        res = run(text, secrets=secrets)
        got = [(d.code, d.line, d.column) for d in res.diagnostics if d.code.startswith("secret")]
        self.assertEqual(got, [("secret-field-missing", 8, 10), ("secret-missing", 9, 10), ("secret-field-ambiguous", 10, 10)])
        self.assertTrue(all(d.severity == "error" for d in res.diagnostics if d.code.startswith("secret")))
        # Unknown secrets store -> rule skipped, YAML still valid thanks to masking.
        self.assertFalse(any(c.startswith("secret") for c in codes(run(text))))


class TestStructural(unittest.TestCase):
    def test_yaml_syntax(self):
        res = run("services:\n  a:\n    image: x\n   b: 1\n")
        self.assertEqual([(d.code, d.line) for d in res.diagnostics], [("yaml-syntax", 4)])

    def test_schema_lite(self):
        text = ("version: '3'\nbogus: 1\nservices:\n  a:\n    imagee: x\n    ports: '80'\n    environment: 3\n"
                "    networks: [front]\n  a2: str\nx-ext: {}\n")
        diags = schema_lite(parse(text))
        msgs = [(d.code, d.path, d.line) for d in diags]
        self.assertIn(("schema", "bogus", 2), msgs)
        self.assertIn(("schema", "services.a.imagee", 5), msgs)
        self.assertIn(("schema", "services.a.ports", 6), msgs)
        self.assertIn(("schema", "services.a.environment", 7), msgs)
        self.assertIn(("schema", "services.a.networks[0]", 8), msgs)
        self.assertIn(("schema", "services.a2", 9), msgs)
        self.assertTrue(any("neither an image" in d.message for d in diags))
        self.assertEqual(schema_lite(parse("services: {}\n")), [])
        self.assertTrue(schema_lite(parse("a: 1\n")))

    def test_locate_cli_messages(self):
        p = parse("services:\n  web:\n    image: nginx\n    imagee: x\n    ports: '80'\n")
        self.assertEqual(locate("validating -: services.web additional properties 'imagee' not allowed", p),
                         ("services.web.imagee", 4, 5))
        self.assertEqual(locate("validating -: services.web.ports must be a array", p), ("services.web.ports", 5, 5))
        self.assertEqual(locate('service "web" depends on undefined service "db": invalid compose project', p),
                         ("services.web", 2, 3))
        self.assertEqual(locate("go-yaml load error at L2.C3-L4.C4: did not find expected key", p), ("", 2, 3))
        self.assertEqual(locate("yaml: construct errors: line 1: line 4: mapping key", p), ("", 4, 1))
        self.assertEqual(locate("something else", p), ("", 0, 0))

    def test_cli_engine(self):
        if not asyncio.run(cli_available()):
            self.skipTest("docker compose CLI not available")
        res = asyncio.run(validate_text("services:\n  a:\n    image: x:1\n    restart: always\n    imagee: y\n"))
        self.assertEqual(res.engine, "docker")
        d = find(res, "compose-config")
        self.assertEqual((d.severity, d.line), ("error", 5))
        # Lint covers depends_on better than the CLI message: no duplicate.
        res = asyncio.run(validate_text("services:\n  a:\n    image: x:1\n    restart: always\n    depends_on: [b]\n"))
        self.assertEqual(codes(res), ["depends-on-unknown"])
        # env_file lives on the target host: info, not an error.
        res = asyncio.run(validate_text("services:\n  a:\n    image: x:1\n    restart: always\n    env_file: .env.prod\n"))
        self.assertTrue(res.ok, [d.to_dict() for d in res.diagnostics])
        self.assertEqual(codes(res), ["env-file-unchecked"])

    def test_cli_output_parsing(self):
        from kowan.compose.structural import _cli_diagnostics

        p = parse("services:\n  web:\n    image: nginx\n    imagee: x\n    env_file: .env\n")
        warn = 'time="x" level=warning msg="the attribute \\"version\\" is obsolete"'
        unset = 'level=warning msg="The \\"FOO\\" variable is not set. Defaulting to a blank string."'
        diags = _cli_diagnostics(0, warn + "\n" + unset + "\n\n", p)
        self.assertEqual([(d.severity, d.code) for d in diags], [("info", "compose-config")])
        self.assertIn('"version"', diags[0].message)
        diags = _cli_diagnostics(1, "validating -: services.web additional properties 'imagee' not allowed", p)
        self.assertEqual((diags[0].severity, diags[0].line), ("error", 4))
        diags = _cli_diagnostics(1, "env file /tmp/x/.env not found: stat: no such file", p)
        self.assertEqual(diags[0].code, "env-file-unchecked")
        diags = _cli_diagnostics(1, 'required variable DB_PASS is missing a value: set it', p)
        self.assertEqual(diags[0].code, "env-required")
        diags = _cli_diagnostics(1, "yaml: line 3: mapping values are not allowed", p)
        self.assertEqual(diags[0].code, "yaml-syntax")
        diags = _cli_diagnostics(2, "", p)
        self.assertIn("exited 2", diags[0].message)

    def test_schema_lite_shapes(self):
        self.assertTrue(schema_lite(parse("")))
        self.assertTrue(schema_lite(parse("- a\n- b\n")))
        self.assertTrue(schema_lite(parse("services: [a]\n")))
        self.assertTrue(schema_lite(parse("services:\n  a:\n    image: x\n    command: 3\n")))
        self.assertTrue(schema_lite(parse("services:\n  a:\n    image: x\n  a:\n    image: y\n")))

    def test_result_shape(self):
        d = run("services:\n  a:\n    image: x\n").to_dict()
        self.assertEqual(set(d), {"diagnostics", "engine", "checks", "summary"})
        diag = d["diagnostics"][0]
        self.assertEqual(set(diag) - {"fix"}, {"severity", "code", "message", "path", "line", "column"})
        self.assertEqual(d["summary"], {"error": 0, "warning": 1, "info": 1})


class TestComposeAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer

        from kowan.server import build_app
        from tests.test_integration import _cfg

        app, self.svc = await build_app(_cfg())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        r = await self.client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        self.H = {"Authorization": "Bearer " + (await r.json())["token"]}

        async def fake_dispatch(dep_id, *, redeploy=False):
            return self.svc.store.get_deployment(dep_id)

        self.svc.deployments.dispatch_deploy = fake_dispatch

    async def asyncTearDown(self):
        await self.client.close()

    async def test_validate_endpoint(self):
        r = await self.client.post("/api/v1/compose/validate", headers=self.H,
                                   json={"compose": "services:\n  a:\n    image: x\n    environment:\n      P: [[nope]]\n"})
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertIn("secret-missing", [d["code"] for d in body["diagnostics"]])
        self.assertTrue(body["checks"]["secrets"])
        r = await self.client.post("/api/v1/compose/validate", headers=self.H, json={"compose": 3})
        self.assertEqual(r.status, 400)

    async def test_deployment_gate(self):
        base = {"name": "g1", "type": "compose", "strategy": "standalone", "targets": ["local"]}
        bad = dict(base, compose="services:\n  a:\n    image: x:1\n    depends_on: [ghost]\n")
        r = await self.client.post("/api/v1/deployments", headers=self.H, json=bad)
        self.assertEqual(r.status, 400)
        body = await r.json()
        self.assertIn("compose validation failed", body["error"])
        self.assertIn("depends-on-unknown", [d["code"] for d in body["diagnostics"]])
        # Warnings never block.
        ok = dict(base, compose="services:\n  a:\n    image: x\n")
        r = await self.client.post("/api/v1/deployments", headers=self.H, json=ok)
        self.assertEqual(r.status, 201, await r.text())
        dep = await r.json()
        r = await self.client.put(f"/api/v1/deployments/{dep['id']}", headers=self.H, json=dict(bad, name="g1"))
        self.assertEqual(r.status, 400)
        # Deploy re-validates the stored compose (e.g. a secret deleted since).
        stored = self.svc.store.get_deployment(dep["id"])
        stored.compose = "services:\n  a:\n    image: x\n    environment:\n      P: [[gone]]\n"
        self.svc.store.update_deployment(stored)
        r = await self.client.post(f"/api/v1/deployments/{dep['id']}/deploy", headers=self.H)
        self.assertEqual(r.status, 400)
        with mock.patch.dict("os.environ", {"KOWAN_COMPOSE_VALIDATE": "0"}):
            r = await self.client.post(f"/api/v1/deployments/{dep['id']}/deploy", headers=self.H)
            self.assertEqual(r.status, 202)


if __name__ == "__main__":
    unittest.main()
