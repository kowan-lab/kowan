"""Exposure end to end against the fake Engine: a Traefik container with its
flags and acme.json, published ports by reach, expose/unexpose of a container
and of a compose deployment, certificate status, and the Traefik installer."""

import asyncio
import base64
import datetime
import json
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.apicase import ServerCase
from tests.fakecli import FakeDockerCLI

H = "/hosts/local"
TRAEFIK_ARGS = [
    "--providers.docker=true", "--providers.docker.exposedbydefault=false",
    "--providers.docker.network=kowan-proxy",
    "--entrypoints.web.address=:80", "--entrypoints.websecure.address=:443",
    "--entrypoints.traefik.address=127.0.0.1:8080", "--api.insecure=true",
    "--certificatesresolvers.letsencrypt.acme.email=ops@example.com",
    "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json",
    "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web",
]


def _cert_pem(domain: str, days: int) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fake LE R3")]))
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
        .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


class ExposureCase(ServerCase):
    async def asyncSetUp(self):
        self.cli = FakeDockerCLI()
        await super().asyncSetUp()
        self.engine.add_network("kowan-proxy")
        acme = {"letsencrypt": {"Certificates": [{
            "domain": {"main": "shop.example.com", "sans": []},
            "certificate": base64.b64encode(_cert_pem("shop.example.com", 10)).decode(), "key": "c2VjcmV0"}]}}
        self.proxy = self.engine.add_container(
            "traefik", "traefik:v3.6", ports=[(80, 80), (443, 443)], networks=["kowan-proxy"],
            cmd=["traefik", *TRAEFIK_ARGS], files={"/letsencrypt/acme.json": json.dumps(acme).encode()})
        self.engine.containers[self.proxy]["Args"] = TRAEFIK_ARGS
        self.engine.containers[self.proxy]["Config"]["Labels"] = {"kowan.exposure.role": "proxy"}
        self.shop = self.engine.add_container("shop", ports=[(80, 18080)], labels={
            "traefik.enable": "true", "traefik.http.routers.shop.rule": "Host(`shop.example.com`)",
            "traefik.http.routers.shop.entrypoints": "websecure",
            "traefik.http.routers.shop.tls.certresolver": "letsencrypt"}, networks=["kowan-proxy"])
        self.engine.add_container("db", "postgres:16", ports=[(5432, 5432)])
        self.engine.exec_handler = self._exec

    def _exec(self, cid, argv):
        if "wget" in argv:  # Traefik API, read through exec inside the proxy
            return 0, json.dumps([{"name": "shop@docker", "status": "enabled", "tls": {"certResolver": "letsencrypt"}}]).encode()
        return None

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.cli.close()


class TestExposureView(ExposureCase):
    async def test_host_view(self):
        _, view = await self.call("GET", H + "/exposure", "viewer", expect=200)
        self.assertEqual([p["kind"] for p in view["proxies"]], ["traefik"])
        self.assertEqual(view["proxies"][0]["secureEntrypoint"], "websecure")
        domains = {d["domain"]: d for d in view["domains"]}
        self.assertIn("shop.example.com", domains)
        self.assertTrue(domains["shop.example.com"]["tls"])
        reach = {p["containerName"]: p for p in view["ports"]}
        self.assertIn("db", reach)
        _, all_hosts = await self.call("GET", "/exposure", "viewer", expect=200)
        self.assertEqual(len(all_hosts), 1)

    async def test_certificates(self):
        _, certs = await self.call("GET", H + "/exposure/certificates", "viewer", expect=200)
        row = next(r for r in certs["domains"] if r["domain"] == "shop.example.com")
        self.assertIsNotNone(row["certificate"])
        self.assertLessEqual(row["certificate"]["daysLeft"], 10)
        self.assertEqual(row["routerStatus"], "enabled")
        # Private keys never leave the server.
        self.assertNotIn("c2VjcmV0", json.dumps(certs))


class TestExposeContainer(ExposureCase):
    async def test_expose_then_unexpose(self):
        web = self.engine.add_container("blog", ports=[(8080, 0)])
        body = {"target": {"kind": "container", "id": web}, "domain": "blog.example.com", "port": 8080}
        await self.roles("POST", H + "/exposure/expose", "operator", json=body)
        _, res = await self.call("POST", H + "/exposure/expose", "operator", json=body, expect=200)
        self.assertEqual(res["status"], "exposed")
        blog = self.engine.container("blog")
        labels = blog["Config"]["Labels"]
        self.assertEqual(labels.get("kowan.exposure.domain"), "blog.example.com")
        self.assertTrue(any(k.endswith(".rule") and "blog.example.com" in v for k, v in labels.items()))
        self.assertIn("kowan-proxy", blog["NetworkSettings"]["Networks"])
        # Moving to another domain keeps track of the network Kowan joined.
        await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "container", "id": blog["Id"]}, "domain": "blog2.example.com", "port": 8080},
            expect=200)
        blog = self.engine.container("blog")

        _, res = await self.call("POST", H + "/exposure/unexpose", "operator",
                                 json={"target": {"kind": "container", "id": blog["Id"]}}, expect=200)
        labels = self.engine.container("blog")["Config"]["Labels"]
        self.assertFalse([k for k in labels if k.startswith("traefik.") or k.startswith("kowan.exposure")])
        self.assertNotIn("kowan-proxy", self.engine.container("blog")["NetworkSettings"]["Networks"])

    async def test_domain_conflict_and_validation(self):
        web = self.engine.add_container("other", ports=[(80, 0)])
        status, body = await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "container", "id": web}, "domain": "shop.example.com", "port": 80})
        self.assertEqual(status, 409, body)
        status, _ = await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "container", "id": web}, "domain": "not a domain", "port": 80})
        self.assertEqual(status, 400)
        status, _ = await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "container", "id": self.proxy}, "domain": "proxy.example.com", "port": 80})
        self.assertEqual(status, 400)

    async def test_no_proxy_is_a_conflict(self):
        del self.engine.containers[self.proxy]
        web = self.engine.add_container("lonely", ports=[(80, 0)])
        status, body = await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "container", "id": web}, "domain": "lonely.example.com", "port": 80})
        self.assertEqual(status, 409, body)
        self.assertIn("Traefik", body["error"])


class TestExposeDeployment(ExposureCase):
    async def test_compose_deployment_gets_labels_and_redeploys(self):
        compose = "services:\n  web:\n    image: nginx:1.27\n    restart: unless-stopped\n"
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "site", "type": "compose", "strategy": "standalone", "targets": ["local"], "compose": compose})
        _, res = await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "deployment", "id": dep["id"], "service": "web"},
            "domain": "site.example.com", "port": 80}, expect=200)
        self.assertEqual(res["status"], "redeploying")
        _, stored = await self.call("GET", f"/deployments/{dep['id']}", "operator")
        self.assertIn("site.example.com", stored["compose"])
        self.assertIn("kowan-proxy", stored["compose"])
        # One deploy at a time: wait for the redeploy that expose started.
        for _ in range(200):
            _, d = await self.call("GET", f"/deployments/{dep['id']}", "operator")
            if d["status"] not in ("pending", "deploying"):
                break
            await asyncio.sleep(0.05)
        _, res = await self.call("POST", H + "/exposure/unexpose", "operator", json={
            "target": {"kind": "deployment", "id": dep["id"], "service": "web"}}, expect=200)
        _, stored = await self.call("GET", f"/deployments/{dep['id']}", "operator")
        self.assertNotIn("site.example.com", stored["compose"])

    async def test_container_deployment_port_required(self):
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "c1", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}})
        status, body = await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "deployment", "id": dep["id"]}, "domain": "c1.example.com"})
        self.assertEqual(status, 400, body)


class TestExposeEdgeCases(ExposureCase):
    async def wait_idle(self, dep_id):
        for _ in range(200):
            _, d = await self.call("GET", f"/deployments/{dep_id}", "operator")
            if d["status"] not in ("pending", "deploying"):
                return
            await asyncio.sleep(0.05)

    async def test_container_deployment_expose_unexpose(self):
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "api", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27", "networks": ["default"]}})
        _, res = await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "deployment", "id": dep["id"]}, "domain": "api.example.com", "port": 8080,
            "proxyId": "traefik"}, expect=200)
        _, stored = await self.call("GET", f"/deployments/{dep['id']}", "operator")
        self.assertIn("kowan-proxy", stored["spec"]["networks"])
        self.assertTrue(any("api.example.com" in v for v in stored["spec"]["labels"].values()))
        await self.wait_idle(dep["id"])
        # Re-expose under another domain replaces the old routing labels.
        await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "deployment", "id": dep["id"]}, "domain": "api2.example.com", "port": 8080}, expect=200)
        _, stored = await self.call("GET", f"/deployments/{dep['id']}", "operator")
        self.assertFalse(any("api.example.com" in v for v in stored["spec"]["labels"].values()))
        await self.wait_idle(dep["id"])
        await self.call("POST", H + "/exposure/unexpose", "operator", json={
            "target": {"kind": "deployment", "id": dep["id"]}}, expect=200)
        _, stored = await self.call("GET", f"/deployments/{dep['id']}", "operator")
        self.assertNotIn("kowan-proxy", stored["spec"]["networks"])
        await self.wait_idle(dep["id"])
        await self.call("POST", H + "/exposure/unexpose", "operator", json={
            "target": {"kind": "deployment", "id": dep["id"]}}, expect=409)

    async def test_port_guess_and_refusals(self):
        img = self.engine.add_image("myapp:1", exposed={"3000/tcp": {}, "9100/tcp": {}})
        guessed = self.engine.add_container("guessed", "myapp:1")
        _, res = await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "container", "id": guessed}, "domain": "g.example.com"}, expect=200)
        labels = self.engine.container("guessed")["Config"]["Labels"]
        self.assertTrue(any(v == "3000" for v in labels.values()), labels)
        plain = self.engine.add_container("plain", "busybox:1")
        await self.call("POST", H + "/exposure/unexpose", "operator", json={
            "target": {"kind": "container", "id": plain}}, expect=409)
        await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "container", "id": plain}, "domain": "p.example.com"}, expect=400)
        await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "volume", "id": "x"}, "domain": "v.example.com"}, expect=400)
        await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "container", "id": plain}, "domain": "p.example.com", "port": 80,
            "proxyId": "nope"}, expect=404)
        await self.call("POST", H + "/exposure/unexpose", "operator", json={"target": {"kind": "volume"}}, expect=400)
        self.assertTrue(img)

    async def test_compose_rules(self):
        compose = "services:\n  web:\n    image: nginx:1.27\n  api:\n    image: nginx:1.27\n"
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "multi", "type": "compose", "strategy": "standalone", "targets": ["local"], "compose": compose})
        await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "deployment", "id": dep["id"]}, "domain": "m.example.com", "port": 80}, expect=400)
        await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "deployment", "id": dep["id"], "service": "web"}, "domain": "m.example.com"},
            expect=400)
        await self.call("POST", H + "/exposure/unexpose", "operator", json={
            "target": {"kind": "deployment", "id": dep["id"], "service": "web"}}, expect=409)
        # A container of a Kowan compose deployment is exposed through its deployment.
        member = self.engine.add_container("multi-web-1", labels={
            "com.docker.compose.project": "multi", "com.docker.compose.service": "web"})
        _, res = await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "container", "id": member}, "domain": "w.example.com", "port": 80}, expect=200)
        self.assertEqual(res["kind"], "deployment")
        await self.wait_idle(dep["id"])
        _, res = await self.call("POST", H + "/exposure/unexpose", "operator", json={
            "target": {"kind": "container", "id": member}}, expect=200)
        _, swarm = await self.call("POST", "/deployments", "operator", json={
            "name": "sw", "type": "compose", "strategy": "swarm", "targets": ["local"], "compose": compose})
        await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "deployment", "id": swarm["id"], "service": "web"}, "domain": "s.example.com",
            "port": 80}, expect=409)
        await self.call("POST", H + "/exposure/expose", "operator", json={
            "target": {"kind": "deployment", "id": "missing"}, "domain": "x.example.com", "port": 80}, expect=404)


class TestDeployProxy(ExposureCase):
    async def test_deploys_traefik_compose(self):
        del self.engine.containers[self.proxy]
        await self.roles("POST", H + "/exposure/proxy", "operator", json={"email": "ops@example.com"})
        status, res = await self.call("POST", H + "/exposure/proxy", "operator", json={"email": "ops@example.com"})
        self.assertIn(status, (200, 201), res)
        _, dep = await self.call("GET", f"/deployments/{res['deploymentId']}", "operator")
        self.assertIn("traefik:", dep["compose"])
        self.assertIn("letsencrypt", dep["compose"])
        status, _ = await self.call("POST", H + "/exposure/proxy", "operator", json={"email": "not-an-email"})
        self.assertEqual(status, 400)
        status, _ = await self.call("POST", H + "/exposure/proxy", "operator", json={"kind": "caddy", "email": "a@b.co"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
