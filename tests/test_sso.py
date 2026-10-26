"""Single sign-on: JOSE/ID token validation, JWKS rotation, PKCE, claim
mapping, provisioning/linking, the GitHub provider and the full browser flow
(start -> callback -> exchange) against a mock IdP, plus SSO-only mode."""

import base64
import datetime
import hashlib
import json
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from kowan.api import sso as sso_api
from kowan.auth import hash_password
from kowan.authn import jose
from kowan.authn.github import GitHubClient
from kowan.authn.mapping import claim_values, map_role, map_teams
from kowan.authn.oidc import OIDCClient, OIDCError, code_challenge, new_code_verifier
from kowan.authn.provision import ProvisionError, provision
from kowan.authn.state import open_sealed, safe_return_to, seal
from kowan.config import ServerConfig
from kowan.models import User
from kowan.secrets import Cipher
from kowan.server import build_app
from kowan.store import Store
from kowan.store.sso import SSOProvider
from kowan.store.teams import Team

ISS = "https://idp.example.test"
CID = "kowan-client"
NOW = "2026-10-25T00:00:00Z"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _int(n: int) -> str:
    return b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


class Keys:
    def __init__(self):
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ec = ec.generate_private_key(ec.SECP256R1())

    def jwk_rsa(self, kid):
        n = self.rsa.public_key().public_numbers()
        return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": _int(n.n), "e": _int(n.e)}

    def jwk_ec(self, kid):
        n = self.ec.public_key().public_numbers()
        return {"kty": "EC", "kid": kid, "use": "sig", "crv": "P-256", "x": b64(n.x.to_bytes(32, "big")),
                "y": b64(n.y.to_bytes(32, "big"))}

    def sign(self, claims, kid="k1", alg="RS256"):
        header = {"alg": alg, "kid": kid, "typ": "JWT"}
        si = (b64(json.dumps(header).encode()) + "." + b64(json.dumps(claims).encode())).encode()
        if alg == "RS256":
            sig = self.rsa.sign(si, padding.PKCS1v15(), hashes.SHA256())
        elif alg == "PS256":
            sig = self.rsa.sign(si, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        elif alg == "ES256":
            r, s = decode_dss_signature(self.ec.sign(si, ec.ECDSA(hashes.SHA256())))
            sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        elif alg == "none":
            sig = b""
        else:
            import hmac

            sig = hmac.new(b"secret", si, hashlib.sha256).digest()
        return si.decode() + "." + b64(sig)


class MockIdP:
    """An in-process OIDC provider behind httpx.MockTransport."""

    def __init__(self, keys: Keys, issuer: str = ISS):
        self.keys = keys
        self.issuer = issuer
        self.jwks = [keys.jwk_rsa("k1")]
        self.jwks_hits = 0
        self.codes = {}
        self.claims_extra = {}
        self.token_requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == self.issuer + "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.issuer, "authorization_endpoint": self.issuer + "/auth",
                "token_endpoint": self.issuer + "/token", "jwks_uri": self.issuer + "/jwks",
                "userinfo_endpoint": self.issuer + "/userinfo",
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"]})
        if url == self.issuer + "/jwks":
            self.jwks_hits += 1
            return httpx.Response(200, json={"keys": self.jwks})
        if url == self.issuer + "/token":
            form = parse_qs(request.content.decode())
            self.token_requests.append((form, request.headers.get("authorization", "")))
            code = form.get("code", [""])[0]
            entry = self.codes.pop(code, None)
            if entry is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if code_challenge(form["code_verifier"][0]) != entry["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            claims = {"iss": self.issuer, "aud": CID, "sub": entry.get("sub", "user-1"), "exp": time.time() + 300,
                      "iat": time.time(), "nonce": entry["nonce"], "email": "dev@example.test",
                      "email_verified": True, "preferred_username": "dev", "groups": ["devs", "kowan-ops"]}
            claims.update(self.claims_extra)
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer",
                                             "id_token": self.keys.sign(claims)})
        if url == self.issuer + "/userinfo":
            return httpx.Response(200, json={"sub": "user-1", "name": "Dev"})
        return httpx.Response(404)

    def factory(self):
        transport = httpx.MockTransport(self.handler)
        return lambda: httpx.AsyncClient(transport=transport)

    def authorize(self, auth_url: str, sub="user-1") -> str:
        q = parse_qs(urlsplit(auth_url).query)
        assert q["code_challenge_method"] == ["S256"]
        code = uuid.uuid4().hex
        self.codes[code] = {"nonce": q["nonce"][0], "challenge": q["code_challenge"][0], "sub": sub}
        return code


def _claims(**kw):
    c = {"iss": ISS, "aud": CID, "sub": "u1", "exp": time.time() + 300, "iat": time.time(), "nonce": "n1"}
    c.update(kw)
    return c


class TestIDTokenValidation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.keys = Keys()
        self.idp = MockIdP(self.keys)
        self.idp.jwks = [self.keys.jwk_rsa("k1"), self.keys.jwk_ec("e1")]
        self.client = OIDCClient(ISS, CID, "s3cret", http=self.idp.factory())

    async def test_good_rsa_and_ec_and_pss(self):
        c = await self.client.validate_id_token(self.keys.sign(_claims()), "n1")
        self.assertEqual(c["sub"], "u1")
        c = await self.client.validate_id_token(self.keys.sign(_claims(), kid="e1", alg="ES256"), "n1")
        self.assertEqual(c["sub"], "u1")
        self.idp.jwks.append({**self.keys.jwk_rsa("p1"), "alg": "PS256"})
        self.client.jwks._keys = {}
        c = await self.client.validate_id_token(self.keys.sign(_claims(), kid="p1", alg="PS256"), "n1")
        self.assertEqual(c["sub"], "u1")

    async def assertRejected(self, token, nonce="n1", client=None):
        with self.assertRaises(OIDCError):
            await (client or self.client).validate_id_token(token, nonce)

    async def test_rejections(self):
        await self.assertRejected(self.keys.sign(_claims(exp=time.time() - 120)))
        await self.assertRejected(self.keys.sign(_claims(aud="someone-else")))
        await self.assertRejected(self.keys.sign(_claims(iss="https://evil.test")))
        await self.assertRejected(self.keys.sign(_claims()), nonce="other")
        await self.assertRejected(self.keys.sign(_claims(nonce="")), nonce="")
        await self.assertRejected(self.keys.sign(_claims(iat=time.time() + 600)))
        await self.assertRejected(self.keys.sign(_claims(nbf=time.time() + 600)))
        await self.assertRejected(self.keys.sign(_claims(aud=[CID, "other"])))  # no azp
        await self.assertRejected(self.keys.sign(_claims(azp="other")))
        await self.assertRejected(self.keys.sign(_claims(), alg="none"))
        await self.assertRejected(self.keys.sign(_claims(), alg="HS256"))
        tok = self.keys.sign(_claims())
        await self.assertRejected(tok[:-4] + ("AAAA" if not tok.endswith("AAAA") else "BBBB"))
        await self.assertRejected("not.a.jwt")
        # an EC signature presented under the RSA kid
        await self.assertRejected(self.keys.sign(_claims(), kid="k1", alg="ES256"))

    async def test_clock_skew_tolerated(self):
        c = await self.client.validate_id_token(self.keys.sign(_claims(exp=time.time() - 30)), "n1")
        self.assertEqual(c["sub"], "u1")
        c = await self.client.validate_id_token(self.keys.sign(_claims(aud=[CID, "x"], azp=CID)), "n1")
        self.assertEqual(c["aud"], [CID, "x"])

    async def test_kid_rotation_and_rate_limit(self):
        clock = [1000.0]
        client = OIDCClient(ISS, CID, http=self.idp.factory(), clock=lambda: clock[0])
        # rotate: token signed with a new key not yet in the JWKS
        await client.validate_id_token(self.keys.sign(_claims(exp=2e10, iat=999)), "n1")
        self.assertEqual(self.idp.jwks_hits, 1)
        new = Keys()
        # unknown kid right after a fetch: refresh is rate limited -> rejected
        with self.assertRaises(OIDCError):
            await client.validate_id_token(new.sign(_claims(exp=2e10, iat=999), kid="k2"), "n1")
        self.assertEqual(self.idp.jwks_hits, 1)
        # provider rotates; after the rate-limit window the unknown kid triggers one refresh
        self.idp.jwks = [new.jwk_rsa("k2")]
        clock[0] += 61
        c = await client.validate_id_token(new.sign(_claims(exp=2e10, iat=999), kid="k2"), "n1")
        self.assertEqual(c["sub"], "u1")
        self.assertEqual(self.idp.jwks_hits, 2)
        # a flood of bogus kids does not hammer the IdP
        for i in range(5):
            with self.assertRaises(OIDCError):
                await client.validate_id_token(new.sign(_claims(exp=2e10, iat=999), kid=f"bogus{i}"), "n1")
        self.assertEqual(self.idp.jwks_hits, 2)

    async def test_discovery_issuer_mismatch(self):
        idp = MockIdP(self.keys, issuer="https://other.test")
        orig = idp.handler

        def bad(req):
            r = orig(req)
            if str(req.url).endswith("openid-configuration"):
                return httpx.Response(200, json={**r.json(), "issuer": "https://evil.test"})
            return r

        idp.handler = bad
        client = OIDCClient("https://other.test", CID, http=idp.factory())
        with self.assertRaises(OIDCError) as cm:
            await client.discovery()
        self.assertEqual(cm.exception.code, "issuer_mismatch")

    async def test_entra_tenant_template(self):
        tmpl = "https://login.microsoftonline.com/{tenantid}/v2.0"
        idp = MockIdP(self.keys, issuer="https://login.microsoftonline.com/common/v2.0")
        orig = idp.handler

        def h(req):
            r = orig(req)
            if str(req.url).endswith("openid-configuration"):
                return httpx.Response(200, json={**r.json(), "issuer": tmpl})
            return r

        idp.handler = h
        client = OIDCClient("https://login.microsoftonline.com/common/v2.0", CID, http=idp.factory())
        tok = self.keys.sign(_claims(iss="https://login.microsoftonline.com/T1/v2.0", tid="T1"))
        c = await client.validate_id_token(tok, "n1")
        self.assertEqual(c["tid"], "T1")
        bad = self.keys.sign(_claims(iss="https://login.microsoftonline.com/T2/v2.0", tid="T1"))
        with self.assertRaises(OIDCError):
            await client.validate_id_token(bad, "n1")


class TestOIDCProviderFailures(unittest.IsolatedAsyncioTestCase):
    def client_for(self, handler):
        transport = httpx.MockTransport(handler)
        return OIDCClient(ISS, CID, "s3cret", http=lambda: httpx.AsyncClient(transport=transport))

    async def test_discovery_and_token_endpoint_errors(self):
        def unreachable(r):
            raise httpx.ConnectError("refused")

        for handler, code in ((unreachable, "provider_unreachable"),
                              (lambda r: httpx.Response(200, text="{not json"), "provider_error"),
                              (lambda r: httpx.Response(200, json=[1]), "provider_error"),
                              (lambda r: httpx.Response(200, json={"issuer": ISS}), "provider_error")):
            with self.assertRaises(OIDCError) as cm:
                await self.client_for(handler).discovery()
            self.assertEqual(cm.exception.code, code)

        keys = Keys()
        idp = MockIdP(keys)

        def no_basic(request):
            url = str(request.url)
            if url.endswith("/.well-known/openid-configuration"):
                return httpx.Response(200, json={
                    "issuer": ISS, "authorization_endpoint": ISS + "/auth", "token_endpoint": ISS + "/token",
                    "jwks_uri": ISS + "/jwks", "token_endpoint_auth_methods_supported": ["client_secret_post"]})
            if url == ISS + "/token":
                form = parse_qs(request.content.decode())
                assert form["client_secret"] == ["s3cret"]  # client_secret_post
                return httpx.Response(400, json={"error": "invalid_grant"})
            return idp.handler(request)

        c = self.client_for(no_basic)
        with self.assertRaises(OIDCError) as cm:
            await c.exchange_code("code", "https://k/cb", "v" * 43)
        self.assertEqual(cm.exception.code, "token_exchange_failed")
        self.assertEqual(await c.userinfo("at"), {})  # no userinfo endpoint advertised

    async def test_token_shape_rejections(self):
        keys = Keys()
        idp = MockIdP(keys)
        c = OIDCClient(ISS, CID, "s3cret", http=idp.factory())
        header = {"alg": "RS256", "kid": "k1", "crit": ["exp"]}
        si = (b64(json.dumps(header).encode()) + "." + b64(json.dumps(_claims()).encode())).encode()
        sig = keys.rsa.sign(si, padding.PKCS1v15(), hashes.SHA256())
        with self.assertRaises(OIDCError):
            await c.validate_id_token(si.decode() + "." + b64(sig), "n1")
        no_sub = _claims()
        del no_sub["sub"]
        with self.assertRaises(OIDCError):
            await c.validate_id_token(keys.sign(no_sub), "n1")
        with self.assertRaises(OIDCError):
            await c.validate_id_token(keys.sign(_claims(), kid="unknown"), "n1")
        info = await c.userinfo("at")
        self.assertEqual(info["sub"], "user-1")


class TestPKCEAndState(unittest.TestCase):
    def test_s256_challenge(self):
        # RFC 7636 4.2: BASE64URL-ENCODE(SHA256(ASCII(code_verifier))), no padding.
        v = "M25iVXpKU3puUjFaYWg3T1NDTDQtcW1ROUY5YXlwalNoc0hhakxifmZHag"
        want = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(code_challenge(v), want)
        self.assertNotIn("=", code_challenge(v))

    def test_verifier_shape(self):
        v = new_code_verifier()
        self.assertTrue(43 <= len(v) <= 128)
        self.assertRegex(v, r"^[A-Za-z0-9_-]+$")
        self.assertNotEqual(v, new_code_verifier())
        self.assertEqual(len(code_challenge(v)), 43)

    def test_sealed_state(self):
        c = Cipher(b"k" * 32)
        v = seal(c, {"s": "abc", "exp": time.time() + 10})
        self.assertEqual(open_sealed(c, v)["s"], "abc")
        self.assertIsNone(open_sealed(c, v[:-2] + "AA"))
        self.assertIsNone(open_sealed(Cipher(b"z" * 32), v))
        self.assertIsNone(open_sealed(c, seal(c, {"s": "x", "exp": time.time() - 1})))

    def test_return_to_allow_list(self):
        self.assertEqual(safe_return_to("/deployments?x=1"), "/deployments?x=1")
        for bad in ("https://evil.test", "//evil.test", "/\\evil.test", "javascript:alert(1)", "/login", "",
                    "/a\r\nSet-Cookie: x", "/api/v1/auth/sso/x"):
            self.assertEqual(safe_return_to(bad), "/", bad)


class TestMapping(unittest.TestCase):
    cfg = {"defaultRole": "viewer",
           "roleMappings": [{"claim": "groups", "value": "ops", "role": "operator"},
                            {"claim": "realm_access.roles", "value": "kowan-admin", "role": "admin"}],
           "teamMappings": [{"claim": "groups", "value": "web", "team": "Web"},
                            {"claim": "groups", "value": "ops", "team": "Ops"}]}

    def test_claim_values(self):
        self.assertEqual(claim_values({"a": {"b": ["x", 1]}}, "a.b"), ["x", "1"])
        self.assertEqual(claim_values({"g": "solo"}, "g"), ["solo"])
        self.assertEqual(claim_values({}, "g"), [])

    def test_roles_and_teams(self):
        self.assertEqual(map_role({}, self.cfg), "viewer")
        self.assertEqual(map_role({"groups": ["ops"]}, self.cfg), "operator")
        self.assertEqual(map_role({"groups": ["ops"], "realm_access": {"roles": ["kowan-admin"]}}, self.cfg), "admin")
        self.assertEqual(map_teams({"groups": ["web", "ops", "x"]}, self.cfg), ["Web", "Ops"])
        self.assertEqual(map_role({"groups": ["ops"]}, {"defaultRole": "bogus"}), "viewer")


class TestProvisioning(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.use_cipher(Cipher(b"k" * 32))
        self.p = SSOProvider(id="p1", name="kc", type="oidc", config={
            "defaultRole": "viewer",
            "roleMappings": [{"claim": "groups", "value": "ops", "role": "operator"}],
            "teamMappings": [{"claim": "groups", "value": "web", "team": "web"}]})
        self.store.create_sso_provider(self.p)
        self.store.create_team(Team(id="t-web", name="web", created_at=NOW))
        self.store.create_team(Team(id="t-man", name="manual", created_at=NOW))

    def test_jit_then_relogin_syncs(self):
        u, created = provision(self.store, self.p, {"sub": "s1", "preferred_username": "dev", "email": "d@x.test",
                                                    "email_verified": True, "groups": ["ops", "web"]}, NOW)
        self.assertTrue(created)
        self.assertEqual((u.username, u.role, u.auth_source, u.password_hash), ("dev", "operator", "sso:kc", ""))
        self.assertEqual(self.store.user_team_ids(u.id), ["t-web"])
        self.store.add_team_member("t-man", u.id, "manual", NOW)
        u2, created = provision(self.store, self.p, {"sub": "s1", "groups": []}, NOW)
        self.assertFalse(created)
        self.assertEqual(u2.id, u.id)
        self.assertEqual(self.store.get_user(u.id).role, "viewer")  # re-synced down
        self.assertEqual(self.store.user_team_ids(u.id), ["t-man"])  # manual kept, sso removed

    def test_username_collision(self):
        self.store.create_user(User(id="x", username="dev", role="viewer", createdAt=NOW))
        u, _ = provision(self.store, self.p, {"sub": "s2", "preferred_username": "dev"}, NOW)
        self.assertEqual(u.username, "dev-2")

    def test_link_by_verified_email_keeps_local_role(self):
        self.store.create_user(User(id="adm", username="root", role="admin", passwordHash="h",
                                    email="Root@Example.test", authSource="local", createdAt=NOW))
        u, created = provision(self.store, self.p, {"sub": "s3", "email": "root@example.test",
                                                    "email_verified": True, "groups": []}, NOW)
        self.assertFalse(created)
        self.assertEqual((u.id, self.store.get_user("adm").role), ("adm", "admin"))
        self.assertEqual(self.store.get_identity_user_id("p1", "s3"), "adm")

    def test_unverified_email_not_linked_nor_stored(self):
        self.store.create_user(User(id="adm", username="root", role="admin", email="root@example.test",
                                    createdAt=NOW))
        u, created = provision(self.store, self.p, {"sub": "s4", "email": "root@example.test",
                                                    "email_verified": False}, NOW)
        self.assertTrue(created)
        self.assertNotEqual(u.id, "adm")
        self.assertEqual(u.email, "")

    def test_signup_disabled_and_domains(self):
        self.p.config["allowSignup"] = False
        with self.assertRaises(ProvisionError) as cm:
            provision(self.store, self.p, {"sub": "s5"}, NOW)
        self.assertEqual(cm.exception.code, "signup_disabled")
        self.p.config["allowSignup"] = True
        self.p.config["allowedDomains"] = ["corp.test"]
        with self.assertRaises(ProvisionError):
            provision(self.store, self.p, {"sub": "s6", "email": "a@evil.test", "email_verified": True}, NOW)
        u, _ = provision(self.store, self.p, {"sub": "s7", "email": "a@corp.test", "email_verified": True}, NOW)
        self.assertEqual(u.email, "a@corp.test")

    def test_secret_encrypted_at_rest(self):
        p = SSOProvider(id="p2", name="gh", type="github", client_id="c", client_secret="topsecret")
        self.store.create_sso_provider(p)
        raw = self.store._one("SELECT secret FROM sso_providers WHERE id = 'p2'")["secret"]
        self.assertNotIn(b"topsecret", bytes(raw))
        self.assertEqual(self.store.get_sso_provider("p2", with_secret=True).client_secret, "topsecret")
        self.assertEqual(self.store.get_sso_provider("p2").client_secret, "********")


class TestGitHub(unittest.IsolatedAsyncioTestCase):
    async def test_identity(self):
        def handler(req):
            u = str(req.url)
            if u.endswith("/login/oauth/access_token"):
                form = parse_qs(req.content.decode())
                assert form["code_verifier"]
                return httpx.Response(200, json={"access_token": "gho_x", "token_type": "bearer"})
            assert req.headers["authorization"] == "Bearer gho_x"
            if u.endswith("/user"):
                return httpx.Response(200, json={"id": 42, "login": "octo", "name": "Octo"})
            if u.endswith("/user/emails"):
                return httpx.Response(200, json=[{"email": "o@x.test", "primary": True, "verified": True},
                                                 {"email": "u@x.test", "primary": False, "verified": False}])
            if "/user/orgs" in u:
                return httpx.Response(200, json=[{"login": "acme"}])
            if "/user/teams" in u:
                return httpx.Response(200, json=[{"slug": "ops", "organization": {"login": "acme"}}])
            return httpx.Response(404)

        t = httpx.MockTransport(handler)
        gh = GitHubClient("cid", "sec", http=lambda: httpx.AsyncClient(transport=t))
        url = gh.authorization_url("https://kowan.test/cb", ["read:user"], "st", "ch")
        self.assertIn("code_challenge=ch", url)
        tok = await gh.exchange_code("code", "https://kowan.test/cb", "verifier")
        claims = await gh.identity(tok)
        self.assertEqual(claims["sub"], "42")
        self.assertEqual(claims["email"], "o@x.test")
        self.assertTrue(claims["email_verified"])
        self.assertEqual(claims["groups"], ["acme", "acme/ops"])


# --------------------------------------------------------------------------- full flow


def _cfg(**kw):
    c = dict(addr="127.0.0.1:0", data_dir=tempfile.mkdtemp(), jwt_secret="x" * 48,
             admin_user="admin", admin_password="adminpass", seed_admin=True,
             monitor_interval=datetime.timedelta(seconds=3600),
             metrics_retention=datetime.timedelta(hours=1), secret_key="", external_url="https://kowan.test")
    c.update(kw)
    return ServerConfig(**c)


class TestSSOFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.keys = Keys()
        self.idp = MockIdP(self.keys)
        sso_api.HTTP_FACTORY = self.idp.factory()
        app, self.svc = await build_app(_cfg())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.cookies = {}
        admin = self.svc.store.get_user_by_username("admin")
        self.admin_h = {"Authorization": "Bearer " + self.svc.jwt.issue(admin.id, admin.username, admin.role)}

    async def asyncTearDown(self):
        sso_api.HTTP_FACTORY = None
        await self.client.close()

    async def create_provider(self, **extra):
        body = {"name": "kc", "type": "oidc", "label": "Keycloak", "issuer": ISS, "clientId": CID,
                "clientSecret": "s3cret", "config": {"defaultRole": "viewer",
                                                     "roleMappings": [{"claim": "groups", "value": "kowan-ops",
                                                                       "role": "operator"}]}}
        body.update(extra)
        r = await self.client.post("/api/v1/sso/providers", json=body, headers=self.admin_h)
        self.assertEqual(r.status, 201, await r.text())
        return await r.json()

    def jar(self, resp):
        from http.cookies import SimpleCookie

        for raw in resp.headers.getall("Set-Cookie", []):
            c = SimpleCookie()
            c.load(raw)
            for k, m in c.items():
                if m.value == "" or m["max-age"] == "0":
                    self.cookies.pop(k, None)
                else:
                    self.cookies[k] = m.value

    def ch(self):
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())} if self.cookies else {}

    async def callback(self, pid, **params):
        r = await self.client.get(f"/api/v1/auth/sso/{pid}/callback", params=params, headers=self.ch(),
                                  allow_redirects=False)
        self.jar(r)
        return r

    async def exchange(self):
        r = await self.client.post("/api/v1/auth/sso/exchange", headers=self.ch())
        self.jar(r)
        return r

    async def login(self, pid, return_to="/containers"):
        r = await self.client.get(f"/api/v1/auth/sso/{pid}/start", params={"returnTo": return_to},
                                  allow_redirects=False)
        self.assertEqual(r.status, 302)
        loc = r.headers["Location"]
        self.assertTrue(loc.startswith(ISS + "/auth?"))
        q = parse_qs(urlsplit(loc).query)
        self.assertEqual(q["redirect_uri"], [f"https://kowan.test/api/v1/auth/sso/{pid}/callback"])
        self.jar(r)
        cookie = r.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Path=/api/v1/auth/sso", cookie)
        self.assertIn("Secure", cookie)
        code = self.idp.authorize(loc)
        return q, code

    async def test_full_flow(self):
        p = await self.create_provider()
        self.assertEqual(p["clientSecret"], "********")
        self.assertTrue(p["redirectUri"].endswith(f"/api/v1/auth/sso/{p['id']}/callback"))
        r = await self.client.get("/api/v1/auth/sso/providers")
        pub = await r.json()
        self.assertEqual(pub["providers"][0]["label"], "Keycloak")
        self.assertNotIn("clientSecret", json.dumps(pub))

        q, code = await self.login(p["id"])
        r = await self.callback(p["id"], code=code, state=q["state"][0])
        self.assertEqual(r.status, 302, await r.text())
        self.assertEqual(r.headers["Location"], "/login/sso")
        self.assertNotIn("token", r.headers["Location"])
        # client_secret_basic was used and the verifier was sent
        form, auth = self.idp.token_requests[-1]
        self.assertTrue(auth.startswith("Basic "))
        self.assertIn("code_verifier", form)

        r = await self.exchange()
        self.assertEqual(r.status, 200, await r.text())
        body = await r.json()
        self.assertEqual(body["returnTo"], "/containers")
        self.assertEqual(body["user"]["username"], "dev")
        self.assertEqual(body["user"]["role"], "operator")
        me = await self.client.get("/api/v1/me", headers={"Authorization": "Bearer " + body["token"]})
        self.assertEqual((await me.json())["authSource"], "sso:kc")
        # the ticket is single-use
        r = await self.exchange()
        self.assertEqual(r.status, 401)

    async def test_state_mismatch_and_replay(self):
        p = await self.create_provider()
        q, code = await self.login(p["id"])
        r = await self.callback(p["id"], code=code, state="forged")
        self.assertEqual(r.status, 302)
        self.assertIn("sso_error=state_mismatch", r.headers["Location"])
        # without the state cookie (another browser): rejected
        self.cookies.clear()
        r = await self.callback(p["id"], code=code, state=q["state"][0])
        self.assertIn("sso_error=state_expired", r.headers["Location"])

    async def test_bad_nonce_rejected(self):
        p = await self.create_provider()
        q, code = await self.login(p["id"])
        self.idp.claims_extra = {"nonce": "attacker"}
        r = await self.callback(p["id"], code=code, state=q["state"][0])
        self.assertIn("sso_error=invalid_id_token", r.headers["Location"])

    async def test_open_redirect_blocked(self):
        p = await self.create_provider()
        q, code = await self.login(p["id"], return_to="https://evil.test/x")
        await self.callback(p["id"], code=code, state=q["state"][0])
        r = await self.exchange()
        self.assertEqual((await r.json())["returnTo"], "/")

    async def test_sso_only_mode(self):
        p = await self.create_provider()
        r = await self.client.put("/api/v1/sso/settings", json={"ssoOnly": True}, headers=self.admin_h)
        self.assertEqual(r.status, 200, await r.text())
        st = self.svc.store
        st.create_user(User(id="op", username="op", role="operator", passwordHash=hash_password("oppass12"),
                            authSource="local", createdAt=NOW))
        r = await self.client.post("/api/v1/auth/login", json={"username": "op", "password": "oppass12"})
        self.assertEqual(r.status, 403)
        r = await self.client.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass"})
        self.assertEqual(r.status, 200)  # break-glass local admin
        r = await self.client.get("/api/v1/auth/sso/providers")
        self.assertTrue((await r.json())["ssoOnly"])
        # break-glass must be a local admin
        r = await self.client.put("/api/v1/sso/settings", json={"breakGlassUser": "op"}, headers=self.admin_h)
        self.assertEqual(r.status, 400)

    async def test_validation_and_env_bootstrap(self):
        r = await self.client.post("/api/v1/sso/providers", json={"name": "Bad Name", "issuer": ISS, "clientId": "x"},
                                   headers=self.admin_h)
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/v1/sso/providers", json={"name": "x", "issuer": "http://idp.example", "clientId": "x"},
                                   headers=self.admin_h)
        self.assertEqual(r.status, 400)
        sso_api.bootstrap_from_env(self.svc, {
            "KOWAN_SSO_PROVIDERS": json.dumps([{"name": "gh", "type": "github", "clientId": "c",
                                               "clientSecretEnv": "GH_SECRET", "label": "GitHub"}]),
            "GH_SECRET": "ghsecret"})
        p = self.svc.store.get_sso_provider_by_name("gh", with_secret=True)
        self.assertEqual((p.managed, p.client_secret), ("env", "ghsecret"))
        r = await self.client.post(f"/api/v1/sso/providers/{(await self.create_provider())['id']}/test",
                                   headers=self.admin_h)
        self.assertTrue((await r.json())["ok"])

    async def test_admin_crud_settings_and_callback_errors(self):
        p = await self.create_provider()
        base = f"/api/v1/sso/providers/{p['id']}"
        r = await self.client.get(base, headers=self.admin_h)
        self.assertEqual((await r.json())["name"], "kc")
        r = await self.client.get("/api/v1/sso/providers", headers=self.admin_h)
        self.assertEqual(len((await r.json())["providers"]), 1)
        # Update keeps the stored secret when the mask comes back, validates the body.
        r = await self.client.put(base, json={"label": "KC", "clientSecret": "********",
                                              "config": {"defaultRole": "operator", "allowedDomains": ["example.test"]}},
                                  headers=self.admin_h)
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(self.svc.store.get_sso_provider(p["id"], with_secret=True).client_secret, "s3cret")
        for bad in ({"config": "x"}, {"config": {"what": 1}}, {"config": {"defaultRole": "king"}},
                    {"config": {"allowedDomains": "x"}}, {"config": {"extraAuthParams": []}},
                    {"config": {"roleMappings": [{"claim": "g"}]}}, {"type": "saml"}, {"name": "Bad Name"},
                    {"issuer": "http://idp.example"}, {"clientId": ""}, {"color": "red"}):
            r = await self.client.put(base, json=bad, headers=self.admin_h)
            self.assertEqual(r.status, 400, bad)
        other = await self.create_provider(name="other")
        r = await self.client.put(base, json={"name": "other"}, headers=self.admin_h)
        self.assertEqual(r.status, 409)
        r = await self.client.post("/api/v1/sso/providers", json={"name": "other", "issuer": ISS, "clientId": "c"},
                                   headers=self.admin_h)
        self.assertEqual(r.status, 409)
        r = await self.client.post("/api/v1/sso/providers", json={"name": "gh", "type": "github", "clientId": "c"},
                                   headers=self.admin_h)
        self.assertEqual(r.status, 400)  # GitHub needs a client secret
        r = await self.client.put(base, json={"clientSecret": "rotated"}, headers=self.admin_h)
        self.assertEqual(self.svc.store.get_sso_provider(p["id"], with_secret=True).client_secret, "rotated")

        # Callback error paths.
        q, code = await self.login(p["id"])
        r = await self.callback(p["id"], error="access_denied", state=q["state"][0])
        self.assertIn("sso_error=idp_access_denied", r.headers["Location"])
        q, code = await self.login(p["id"])
        r = await self.callback(p["id"], state=q["state"][0])
        self.assertIn("sso_error=missing_code", r.headers["Location"])
        q, code = await self.login(p["id"])
        r = await self.callback(p["id"], code="unknown", state=q["state"][0])
        self.assertIn("sso_error=", r.headers["Location"])
        r = await self.client.get("/api/v1/auth/sso/missing/start", allow_redirects=False)
        self.assertIn("sso_error=unknown_provider", r.headers["Location"])

        # Settings: break-glass first, then SSO-only; a missing user is refused.
        r = await self.client.put("/api/v1/sso/settings", json={"breakGlassUser": "ghost"}, headers=self.admin_h)
        self.assertEqual(r.status, 400)
        r = await self.client.put("/api/v1/sso/settings", json={"breakGlassUser": "admin", "ssoOnly": False},
                                  headers=self.admin_h)
        self.assertEqual(r.status, 200)
        r = await self.client.put("/api/v1/sso/settings", json={"nope": 1}, headers=self.admin_h)
        self.assertEqual(r.status, 400)
        r = await self.client.get("/api/v1/sso/settings", headers=self.admin_h)
        self.assertEqual((await r.json())["breakGlassUser"], "admin")

        for pid in (p["id"], other["id"]):
            r = await self.client.delete(f"/api/v1/sso/providers/{pid}", headers=self.admin_h)
            self.assertEqual(r.status, 200)
        r = await self.client.delete(base, headers=self.admin_h)
        self.assertEqual(r.status, 404)
        for method in ("GET", "PUT"):
            r = await self.client.request(method, base, json={}, headers=self.admin_h)
            self.assertEqual(r.status, 404)
        r = await self.client.post(base + "/test", headers=self.admin_h)
        self.assertEqual(r.status, 404)
        # SSO-only needs an enabled provider.
        r = await self.client.put("/api/v1/sso/settings", json={"ssoOnly": True}, headers=self.admin_h)
        self.assertEqual(r.status, 400)

    async def test_github_provider_test_and_env_update(self):
        r = await self.client.post("/api/v1/sso/providers", json={
            "name": "gh", "type": "github", "clientId": "c", "clientSecret": "s"}, headers=self.admin_h)
        p = await r.json()
        r = await self.client.post(f"/api/v1/sso/providers/{p['id']}/test", headers=self.admin_h)
        self.assertTrue((await r.json())["ok"])
        # Env bootstrap updates an existing provider; bad JSON and bad items are skipped.
        sso_api.bootstrap_from_env(self.svc, {
            "KOWAN_SSO_PROVIDERS": json.dumps([{"name": "gh", "type": "github", "clientId": "c2"}, "junk",
                                               {"name": "Bad Name"}]),
            "KOWAN_SSO_BREAK_GLASS_USER": "admin", "KOWAN_SSO_ONLY": "false"})
        self.assertEqual(self.svc.store.get_sso_provider_by_name("gh").client_id, "c2")
        sso_api.bootstrap_from_env(self.svc, {"KOWAN_SSO_PROVIDERS": "{not json"})
        # OIDC test failing discovery.
        bad = await self.create_provider(name="broken", issuer="https://nowhere.test")
        r = await self.client.post(f"/api/v1/sso/providers/{bad['id']}/test", headers=self.admin_h)
        self.assertFalse((await r.json())["ok"])


if __name__ == "__main__":
    unittest.main()
