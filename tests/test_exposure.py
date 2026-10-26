"""Exposure: port classification, Traefik label parsing + generation,
proxy settings parsing, compose editing and acme.json reading."""

import base64
import datetime as dt
import json
import unittest

import yaml

from kowan.exposure import certs, classify, compose, labels as lbl, proxy


class TestClassify(unittest.TestCase):
    def test_scope(self):
        self.assertEqual(classify.bind_scope("0.0.0.0"), "public")
        self.assertEqual(classify.bind_scope("::"), "public")
        self.assertEqual(classify.bind_scope(""), "public")
        self.assertEqual(classify.bind_scope("127.0.0.1"), "local")
        self.assertEqual(classify.bind_scope("::1"), "local")
        self.assertEqual(classify.bind_scope("192.168.1.10"), "interface")

    def test_risk(self):
        r = classify.classify_port("0.0.0.0", 5432)
        self.assertTrue(r["warn"])
        self.assertEqual(r["risk"]["kind"], "database")
        self.assertFalse(classify.classify_port("127.0.0.1", 5432)["warn"])
        self.assertFalse(classify.classify_port("0.0.0.0", 80, "nginx:latest")["warn"])
        self.assertEqual(classify.risk_for(8080, "adminer:4")["kind"], "admin")
        self.assertEqual(classify.risk_for(1234, "docker.io/library/postgres:16")["label"], "PostgreSQL")
        self.assertIsNone(classify.risk_for(1234, "myorg/postgresql-exporter-ui"))


class TestTraefikLabels(unittest.TestCase):
    def test_rule_parsing(self):
        self.assertEqual(lbl.parse_traefik_rule("Host(`a.com`, `b.com`) && PathPrefix(`/api`)"), ["a.com", "b.com"])
        self.assertEqual(lbl.parse_traefik_rule('Host("x.io") || Host(`Y.io`)'), ["x.io", "y.io"])
        self.assertEqual(lbl.parse_traefik_rule("HostRegexp(`{sub:[a-z]+}.x.io`)"), [])
        self.assertEqual(lbl.parse_traefik_rule("HostSNI(`db.x.io`)"), ["db.x.io"])

    def test_routes(self):
        labels = {
            "traefik.enable": "true",
            "traefik.http.routers.app.rule": "Host(`app.example.com`)",
            "traefik.http.routers.app.entrypoints": "websecure",
            "traefik.http.routers.app.tls.certresolver": "le",
            "traefik.http.services.app.loadbalancer.server.port": "3000",
            "traefik.tcp.routers.pg.rule": "HostSNI(`pg.example.com`)",
        }
        rs = lbl.traefik_routes(labels)
        self.assertEqual([(r["domain"], r["protocol"]) for r in rs], [("app.example.com", "http"), ("pg.example.com", "tcp")])
        app = rs[0]
        self.assertEqual((app["tls"], app["certResolver"], app["entrypoints"], app["port"]), (True, "le", ["websecure"], 3000))
        disabled = lbl.traefik_routes({**labels, "traefik.enable": "false"})
        self.assertFalse(disabled[0]["enabled"])
        v1 = lbl.traefik_routes({"traefik.frontend.rule": "Host:old.example.com,old2.example.com"})
        self.assertEqual([r["domain"] for r in v1], ["old.example.com", "old2.example.com"])

    def test_expose_unexpose_roundtrip(self):
        out = lbl.expose_labels("traefik", domain="App.Example.com", port=80, router="kowan-web",
                                network="proxy", entrypoint="websecure", cert_resolver="le", network_added=True)
        self.assertEqual(out["traefik.http.routers.kowan-web.rule"], "Host(`app.example.com`)")
        self.assertEqual(out["traefik.http.routers.kowan-web.tls.certresolver"], "le")
        self.assertEqual(out["traefik.docker.network"], "proxy")
        self.assertEqual(out[lbl.MARK_NETWORK], "proxy")
        parsed = lbl.traefik_routes(out)
        self.assertEqual(parsed[0]["domain"], "app.example.com")
        container = {"keep": "me", **out}
        for k in lbl.unexpose_keys(container):
            container.pop(k)
        self.assertEqual(container, {"keep": "me"})
        # Only Traefik is supported.
        with self.assertRaises(ValueError):
            lbl.expose_labels("caddy", domain="c.example.com", port=8080, router="r")
        with self.assertRaises(ValueError):
            lbl.expose_labels("traefik", domain="not a domain", port=80, router="r")
        with self.assertRaises(ValueError):
            lbl.expose_labels("traefik", domain="ok.example.com", port=0, router="r")

    def test_router_name_and_domain(self):
        self.assertEqual(lbl.router_name("My_App.1"), "kowan-my-app-1")
        self.assertTrue(lbl.valid_domain("a.b.example.co"))
        self.assertFalse(lbl.valid_domain("-bad.com"))
        self.assertEqual(lbl.normalize_domain("https://X.io/path"), "x.io")


class TestProxySettings(unittest.TestCase):
    def test_kind(self):
        self.assertEqual(proxy.proxy_kind("traefik:v3.6"), "traefik")
        self.assertEqual(proxy.proxy_kind("docker.io/library/traefik@sha256:abc"), "traefik")
        self.assertIsNone(proxy.proxy_kind("traefik/whoami:latest"))
        self.assertIsNone(proxy.proxy_kind("lucaslorentz/caddy-docker-proxy:2.9-alpine"))
        self.assertIsNone(proxy.proxy_kind("caddy:2"))

    def test_flags(self):
        cfg = proxy.flags_to_config([
            "--providers.docker=true", "--providers.docker.exposedByDefault=false",
            "--entryPoints.web.address=:80", "--entrypoints.websecure.address=:443",
            "--certificatesresolvers.le.acme.storage=/letsencrypt/acme.json",
            "--certificatesresolvers.le.acme.httpchallenge.entrypoint=web",
            "--api.insecure",
        ], ["TRAEFIK_PROVIDERS_DOCKER_NETWORK=proxy", "PATH=/bin"])
        s = proxy.traefik_settings(cfg)
        self.assertEqual((s["secureEntrypoint"], s["httpEntrypoint"]), ("websecure", "web"))
        self.assertEqual(s["certResolvers"], ["le"])
        self.assertEqual(s["resolvers"]["le"]["challenge"], "http-01")
        self.assertEqual(s["resolvers"]["le"]["storage"], "/letsencrypt/acme.json")
        self.assertEqual(s["dockerNetwork"], "proxy")
        self.assertFalse(s["exposedByDefault"])
        self.assertTrue(s["apiEnabled"])

    def test_file_config(self):
        doc = proxy.parse_file("entryPoints:\n  https:\n    address: ':443'\ncertificatesResolvers:\n  myres:\n    acme:\n      tlsChallenge: {}\n")
        s = proxy.traefik_settings(proxy.merge_file_config({}, doc))
        self.assertEqual(s["secureEntrypoint"], "https")
        self.assertEqual(s["resolvers"]["myres"]["challenge"], "tls-alpn-01")

    def test_pick_network(self):
        p = {"networks": ["bridge", "edge"], "dockerNetwork": ""}
        self.assertEqual(proxy.pick_network(p, ["bridge"]), {"network": "bridge", "connect": False})
        self.assertEqual(proxy.pick_network(p, ["other"]), {"network": "edge", "connect": True})
        self.assertEqual(proxy.pick_network({**p, "dockerNetwork": "edge"}, ["bridge"]), {"network": "edge", "connect": True})

    def test_traefik_compose(self):
        text = proxy.traefik_compose(email="ops@example.com", http_port=18080, https_port=18443, staging=True)
        doc = yaml.safe_load(text)
        cmd = doc["services"]["traefik"]["command"]
        self.assertIn("--certificatesresolvers.letsencrypt.acme.email=ops@example.com", cmd)
        self.assertTrue(any("acme-staging" in c for c in cmd))
        self.assertEqual(doc["services"]["traefik"]["ports"], ["18080:80", "18443:443"])
        self.assertEqual(doc["networks"]["proxy"]["name"], "kowan-proxy")
        with self.assertRaises(ValueError):
            proxy.traefik_compose(email="nope")


class TestComposeEdit(unittest.TestCase):
    SRC = """services:
  web:
    image: nginx
    labels:
      - keep=1
  db:
    image: postgres
    networks: [backend]
networks:
  backend: {}
"""

    def test_expose_and_unexpose(self):
        labels = lbl.expose_labels("traefik", domain="w.example.com", port=80, router="kowan-web",
                                   network="kowan-proxy", network_added=True)
        out = compose.expose(self.SRC, "web", labels, "kowan-proxy")
        doc = yaml.safe_load(out)
        web = doc["services"]["web"]
        self.assertEqual(web["labels"]["keep"], "1")
        self.assertEqual(web["networks"], ["default", compose.NET_KEY])
        self.assertEqual(doc["networks"][compose.NET_KEY], {"external": True, "name": "kowan-proxy"})
        self.assertEqual(compose.exposure_of(out, "web")["domain"], "w.example.com")
        back = compose.unexpose(out, "web", lbl.unexpose_keys(compose.service_labels(out, "web")), True)
        doc2 = yaml.safe_load(back)
        self.assertEqual(doc2["services"]["web"]["labels"], {"keep": "1"})
        self.assertNotIn("networks", doc2["services"]["web"])
        self.assertEqual(list(doc2["networks"]), ["backend"])

    def test_expose_dict_networks_and_errors(self):
        src = "services:\n  a:\n    image: x\n    networks:\n      backend:\n        aliases: [api]\n"
        out = compose.expose(src, "a", {"x": "y"}, "proxy")
        self.assertIn(compose.NET_KEY, yaml.safe_load(out)["services"]["a"]["networks"])
        with self.assertRaises(compose.ComposeEditError):
            compose.expose(src, "missing", {}, None)
        with self.assertRaises(compose.ComposeEditError):
            compose.expose("services:\n  a:\n    image: x\n    network_mode: host\n", "a", {}, "proxy")
        with self.assertRaises(compose.ComposeEditError):
            compose.services("not: [valid")


class TestCerts(unittest.TestCase):
    def _cert(self, name, days):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.timezone.utc)
        subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "(STAGING) Fake LE")]))
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256()))
        return cert.public_bytes(serialization.Encoding.PEM)

    def test_acme_json(self):
        pem = self._cert("app.example.com", 60)
        acme = {"le": {"Account": {}, "Certificates": [
            {"domain": {"main": "app.example.com"}, "certificate": base64.b64encode(pem).decode(), "key": "SECRET"}]}}
        out = certs.parse_acme_json(json.dumps(acme).encode())
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual((c["domain"], c["resolver"], c["status"]), ("app.example.com", "le", "valid"))
        self.assertTrue(c["staging"])
        self.assertNotIn("key", json.dumps(out).lower().replace("keep", ""))
        self.assertIs(certs.match("app.example.com", out), c)
        self.assertIsNone(certs.match("other.example.com", out))
        self.assertEqual(certs.cert_info(self._cert("x.io", 3))["status"], "expiring")
        self.assertEqual(certs.parse_acme_json(b"garbage"), [])


if __name__ == "__main__":
    unittest.main()
