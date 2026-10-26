"""Custom app templates over REST (CRUD, render, validation, store failures)
and the secrets cipher's key handling (env key forms, key file, tampering)."""

import base64
import os
import tempfile
import unittest
from unittest import mock

from kowan.secrets import cipher
from tests.apicase import ServerCase


class TestTemplatesApi(ServerCase):
    async def test_crud_render_and_validation(self):
        _, catalog = await self.call("GET", "/templates", "viewer", expect=200)
        self.assertTrue(any(t["id"] == "builtin:nginx" for t in catalog))
        _, nginx = await self.call("GET", "/templates/builtin:nginx", "viewer", expect=200)
        self.assertEqual(nginx["type"], "container")
        _, out = await self.call("POST", "/templates/builtin:nginx/render", "viewer",
                                 json={"values": {"HTTP_PORT": "8181"}}, expect=200)
        self.assertIn("8181", str(out))
        await self.call("POST", "/templates/builtin:nginx/render", "viewer", json={"values": 3}, expect=400)
        await self.call("POST", "/templates/builtin:nginx/render", "viewer", data=b"[1]", expect=400)
        await self.call("POST", "/templates/missing/render", "viewer", json={}, expect=404)

        body = {"name": "My API", "type": "container", "spec": {"image": "nginx:1.27"},
                "inputs": [{"name": "PORT", "label": "Port", "default": "80"}]}
        await self.roles("POST", "/templates", "admin", json=body)
        _, t = await self.call("POST", "/templates", "admin", json=body, expect=201)
        _, upd = await self.call("PUT", f"/templates/{t['id']}", "admin",
                                 json={**body, "description": "changed"}, expect=200)
        self.assertEqual(upd["description"], "changed")
        compose_body = {"name": "Stack", "type": "compose", "compose": "services:\n  a:\n    image: x\n",
                        "spec": {"image": "ignored"}}
        _, ct = await self.call("POST", "/templates", "admin", json=compose_body, expect=201)
        self.assertFalse(ct.get("spec"))
        for bad in ({**body, "spec": "x"}, {**body, "inputs": "x"}, {**body, "inputs": [{"name": 3}]},
                    {"name": "", "type": "container"}, [1]):
            await self.call("POST", "/templates", "admin", json=bad, expect=400)
        await self.call("PUT", f"/templates/{t['id']}", "admin", json={**body, "spec": "x"}, expect=400)
        await self.call("PUT", f"/templates/{t['id']}", "admin", data=b"[1]", expect=400)
        await self.call("PUT", "/templates/missing", "admin", json=body, expect=404)
        with mock.patch.object(self.svc.store, "list_templates", side_effect=RuntimeError("db")):
            await self.call("GET", "/templates", "viewer", expect=500)
        with mock.patch.object(self.svc.store, "create_template", side_effect=RuntimeError("db")):
            await self.call("POST", "/templates", "admin", json=body, expect=500)
        with mock.patch.object(self.svc.store, "update_template", side_effect=RuntimeError("db")):
            await self.call("PUT", f"/templates/{t['id']}", "admin", json=body, expect=500)
        with mock.patch.object(self.svc.store, "delete_template", side_effect=RuntimeError("db")):
            await self.call("DELETE", f"/templates/{t['id']}", "admin", expect=500)
        await self.call("DELETE", f"/templates/{t['id']}", "admin", expect=200)
        await self.call("DELETE", f"/templates/{t['id']}", "admin", expect=404)
        await self.call("GET", f"/templates/{t['id']}", "viewer", expect=404)


class TestCipherKeys(unittest.TestCase):
    def test_env_key_forms_and_key_file(self):
        d = tempfile.mkdtemp()
        raw = os.urandom(32)
        self.assertEqual(cipher.load_or_create_key(d, base64.b64encode(raw).decode()), raw)
        self.assertEqual(cipher.load_or_create_key(d, base64.b64encode(raw).decode().rstrip("=")), raw)
        short = cipher.load_or_create_key(d, base64.b64encode(b"short").decode())
        self.assertEqual(len(short), 32)
        self.assertEqual(len(cipher.load_or_create_key(d, "a passphrase, not base64!")), 32)
        key = cipher.load_or_create_key(d, "")
        self.assertEqual(cipher.load_or_create_key(d, ""), key)  # persisted
        self.assertEqual(oct(os.stat(os.path.join(d, cipher.KEY_FILE_NAME)).st_mode & 0o777), "0o600")
        with open(os.path.join(d, cipher.KEY_FILE_NAME), "wb") as f:
            f.write(b"too short")
        with self.assertRaises(cipher.CipherError):
            cipher.load_or_create_key(d, "")

    def test_encrypt_decrypt_and_tampering(self):
        c = cipher.Cipher(os.urandom(32))
        blob = c.encrypt(b"hello")
        self.assertEqual(c.decrypt(blob), b"hello")
        with self.assertRaises(cipher.CipherError):
            c.decrypt(blob[:5])
        with self.assertRaises(cipher.CipherError):
            c.decrypt(blob[:-1] + bytes([blob[-1] ^ 1]))
        with self.assertRaises(cipher.CipherError):
            cipher.Cipher(b"short")


if __name__ == "__main__":
    unittest.main()
