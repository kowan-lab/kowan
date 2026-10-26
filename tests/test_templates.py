"""App-template catalog: built-in catalog sanity, input substitution, store
round-trip, and the REST surface."""

import datetime
import os
import tempfile
import unittest

import yaml
from aiohttp.test_utils import TestClient, TestServer

from kowan.config import ServerConfig
from kowan.models import ContainerSpec
from kowan.server import build_app
from kowan.store import NotFoundError, Store
from kowan.templates import (
    RESERVED_NAME,
    Template,
    TemplateError,
    TemplateInput,
    builtins,
    render,
    substitute,
    validate_template,
)


def _sample_values(t: Template) -> dict:
    return {i.name: (i.default or "s3cretValue") for i in t.inputs}


class TestBuiltinCatalog(unittest.TestCase):
    def test_catalog_size_and_unique_ids(self):
        cat = builtins()
        self.assertGreaterEqual(len(cat), 20)
        ids = [t.id for t in cat]
        self.assertEqual(len(ids), len(set(ids)), "duplicate built-in ids")
        for t in cat:
            self.assertTrue(t.id.startswith("builtin:"), t.id)
            self.assertTrue(t.built_in)

    def test_every_builtin_parses_and_renders(self):
        for t in builtins():
            with self.subTest(t.id):
                self.assertTrue(t.name and t.description and t.category)
                validate_template(t)
                out = render(t, _sample_values(t), name=t.name)
                self.assertEqual(out["type"], t.type)
                if t.type == "container":
                    spec = ContainerSpec.model_validate(out["spec"])
                    self.assertTrue(spec.image)
                    for p in spec.ports:
                        self.assertGreater(p.host_port, 0)
                    blob = str(out["spec"])
                else:
                    doc = yaml.safe_load(out["compose"])
                    self.assertTrue(doc["services"])
                    blob = out["compose"]
                # Every declared input was substituted.
                for i in t.inputs:
                    self.assertNotIn("${%s}" % i.name, blob)
                self.assertNotIn("${%s}" % RESERVED_NAME, blob)

    def test_every_input_is_referenced(self):
        for t in builtins():
            body = str(t.spec) if t.type == "container" else t.compose
            for i in t.inputs:
                with self.subTest(f"{t.id}:{i.name}"):
                    self.assertIn("${%s}" % i.name, body)

    def test_builtins_returns_copies(self):
        a = builtins()
        a[0].name = "mutated"
        self.assertNotEqual(builtins()[0].name, "mutated")


class TestSubstitution(unittest.TestCase):
    def _container(self) -> Template:
        return Template(
            id="t", name="App", type="container",
            spec={
                "image": "nginx:${TAG}",
                "env": [{"key": "PASS", "value": "${PASS}"}, {"key": "KEEP", "value": "${OTHER}"}],
                "ports": [{"hostPort": "${HTTP_PORT}", "containerPort": 80, "protocol": "tcp"}],
                "volumes": [{"source": "${KOWAN_NAME}-data", "target": "/data"}],
            },
            inputs=[
                TemplateInput(name="TAG", label="Tag", default="stable"),
                TemplateInput(name="PASS", label="Password", required=True, secret=True),
                TemplateInput(name="HTTP_PORT", label="Port", default="8080"),
            ],
        )

    def test_substitute_only_known_names(self):
        self.assertEqual(substitute("${A}-${B}-$C", {"A": "1"}), "1-${B}-$C")
        self.assertEqual(substitute("${A}", {"A": "a$b"}, escape_dollar=True), "a$$b")

    def test_container_defaults_values_and_port_coercion(self):
        out = render(self._container(), {"PASS": "pw", "HTTP_PORT": "9000"}, name="My App")
        spec = out["spec"]
        self.assertEqual(spec["image"], "nginx:stable")
        self.assertEqual(spec["env"][0], {"key": "PASS", "value": "pw"})
        # Undeclared tokens are left for the user / compose.
        self.assertEqual(spec["env"][1]["value"], "${OTHER}")
        self.assertEqual(spec["ports"][0]["hostPort"], 9000)
        self.assertEqual(spec["volumes"][0]["source"], "my-app-data")

    def test_required_input_missing(self):
        with self.assertRaises(TemplateError):
            render(self._container(), {}, name="x")

    def test_bad_port_value_rejected(self):
        with self.assertRaises(TemplateError):
            render(self._container(), {"PASS": "x", "HTTP_PORT": "eighty"}, name="x")

    def test_compose_escapes_and_rejects_quotes(self):
        t = Template(
            id="c", name="C", type="compose",
            compose='services:\n  a:\n    image: x\n    environment:\n      P: "${P}"\n      V: ${V:-1}\n',
            inputs=[TemplateInput(name="P", label="P", required=True)],
        )
        out = render(t, {"P": "a$b"}, name="c")
        self.assertIn('P: "a$$b"', out["compose"])
        self.assertIn("${V:-1}", out["compose"])
        with self.assertRaises(TemplateError):
            render(t, {"P": 'a"b'}, name="c")

    def test_validate_rejects_bad_inputs(self):
        t = self._container()
        t.inputs.append(TemplateInput(name="lower", label="x"))
        with self.assertRaises(TemplateError):
            validate_template(t)
        t = self._container()
        t.inputs.append(TemplateInput(name="TAG", label="dup"))
        with self.assertRaises(TemplateError):
            validate_template(t)
        with self.assertRaises(TemplateError):
            validate_template(Template(name="x", type="compose", compose="nope: 1\n"))


class TestTemplateStore(unittest.TestCase):
    def test_crud_roundtrip(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        t = Template(
            id="u1", name="Mine", type="container", spec={"image": "x:${T}"},
            inputs=[TemplateInput(name="T", label="Tag", default="1")],
            createdAt="c", updatedAt="c",
        )
        store.create_template(t)
        got = store.get_template("u1")
        self.assertEqual(got.spec, {"image": "x:${T}"})
        self.assertEqual(got.inputs[0].name, "T")
        self.assertFalse(got.built_in)
        got.name = "Renamed"
        got.updated_at = "u"
        store.update_template(got)
        self.assertEqual([x.name for x in store.list_templates()], ["Renamed"])
        store.delete_template("u1")
        with self.assertRaises(NotFoundError):
            store.get_template("u1")


def _cfg():
    return ServerConfig(
        addr="127.0.0.1:0",
        data_dir=tempfile.mkdtemp(),
        jwt_secret="x" * 48,
        monitor_interval=datetime.timedelta(seconds=3600),
        metrics_retention=datetime.timedelta(hours=1),
        secret_key="",
        external_url="",
    )


class TestTemplatesAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app, self.svc = await build_app(_cfg())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        r = await self.client.post(
            "/api/v1/auth/init",
            json={"username": "root", "password": "supersecret", "setupToken": self.svc.setup_token},
        )
        self.h = {"Authorization": "Bearer " + (await r.json())["token"]}

    async def asyncTearDown(self):
        await self.client.close()

    async def test_list_render_and_custom_crud(self):
        r = await self.client.get("/api/v1/templates", headers=self.h)
        self.assertEqual(r.status, 200)
        cat = await r.json()
        self.assertGreaterEqual(len(cat), 20)
        self.assertTrue(all(t["builtIn"] for t in cat))

        r = await self.client.post(
            "/api/v1/templates/builtin:postgres/render",
            json={"name": "db", "values": {"POSTGRES_PASSWORD": "pw"}}, headers=self.h,
        )
        self.assertEqual(r.status, 200, await r.text())
        out = await r.json()
        self.assertEqual(out["type"], "container")
        self.assertEqual(out["spec"]["volumes"][0]["source"], "db-data")

        r = await self.client.post(
            "/api/v1/templates/builtin:postgres/render", json={"values": {}}, headers=self.h,
        )
        self.assertEqual(r.status, 400)

        body = {
            "name": "Echo", "category": "Utility", "type": "container",
            "spec": {"image": "hashicorp/http-echo", "ports": [
                {"hostPort": "${PORT}", "containerPort": 5678, "protocol": "tcp"}]},
            "inputs": [{"name": "PORT", "label": "Port", "default": "5678"}],
        }
        r = await self.client.post("/api/v1/templates", json=body, headers=self.h)
        self.assertEqual(r.status, 201, await r.text())
        created = await r.json()
        self.assertFalse(created["builtIn"])

        body["name"] = "Echo 2"
        r = await self.client.put(f"/api/v1/templates/{created['id']}", json=body, headers=self.h)
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual((await r.json())["name"], "Echo 2")

        r = await self.client.put("/api/v1/templates/builtin:nginx", json=body, headers=self.h)
        self.assertEqual(r.status, 400)
        r = await self.client.delete("/api/v1/templates/builtin:nginx", headers=self.h)
        self.assertEqual(r.status, 400)

        r = await self.client.post(
            "/api/v1/templates", json={"name": "bad", "type": "container", "spec": {}},
            headers=self.h,
        )
        self.assertEqual(r.status, 400)

        r = await self.client.delete(f"/api/v1/templates/{created['id']}", headers=self.h)
        self.assertEqual(r.status, 200)
        r = await self.client.get(f"/api/v1/templates/{created['id']}", headers=self.h)
        self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
