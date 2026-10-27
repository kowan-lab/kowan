"""Cloud registry providers against mocked cloud APIs (httpcap.mock):
AWS ECR (SigV4-signed JSON API, token decode, paginated browse), Azure ACR
(AAD client-credentials -> exchange -> scoped access token) and Google
(RS256 service-account assertion -> access token)."""

import base64
import json
import unittest
from urllib.parse import parse_qs

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kowan import httpcap
from kowan.models import Registry
from kowan.registry.acr import ACRProvider
from kowan.docker.local import decode_registry_auth
from kowan.registry.ecr import ECRProvider
from kowan.registry.gcp import GCPProvider

transport = httpcap.mock


class TestECR(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reg = Registry(type="ecr", url="123.dkr.ecr.eu-west-1.amazonaws.com",
                            config={"region": "eu-west-1", "accessKeyId": "AKIDEXAMPLE", "secretAccessKey": "s3cr3t"})
        self.calls = []

    def handler(self, request):
        target = request.headers["X-Amz-Target"]
        self.calls.append(request)
        auth = request.headers["Authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"), auth
        assert "/eu-west-1/ecr/aws4_request" in auth and "Signature=" in auth
        op = target.rsplit(".", 1)[1]
        body = json.loads(request.content or b"{}")
        if op == "GetAuthorizationToken":
            return httpcap.Reply(200, json={"authorizationData": [{
                "authorizationToken": base64.b64encode(b"AWS:ecr-password").decode(),
                "proxyEndpoint": "https://123.dkr.ecr.eu-west-1.amazonaws.com"}]})
        if op == "DescribeRepositories":
            if not body.get("nextToken"):
                return httpcap.Reply(200, json={"repositories": [{"repositoryName": "a"}], "nextToken": "p2"})
            return httpcap.Reply(200, json={"repositories": [{"repositoryName": "b"}]})
        if op == "ListImages":
            return httpcap.Reply(200, json={"imageIds": [{"imageTag": "1.0"}, {"imageDigest": "sha256:x"}]})
        return httpcap.Reply(400, json={"message": "unknown op"})

    async def test_token_browse_and_errors(self):
        p = ECRProvider(self.reg)
        with transport(self.handler):
            user, pw, host = await p.credentials()
            self.assertEqual((user, pw), ("AWS", "ecr-password"))
            self.assertEqual(decode_registry_auth(await p.docker_auth())[2], "123.dkr.ecr.eu-west-1.amazonaws.com")
            self.assertEqual(await p.validate(), (False, None))
            self.assertEqual(await p.list_repositories(), ["a", "b"])
            self.assertEqual(await p.list_tags("a"), ["1.0"])
            with self.assertRaises(ValueError):
                await p.list_tags(" ")
        self.assertEqual(str(self.calls[0].url), "https://api.ecr.eu-west-1.amazonaws.com/")
        with transport(lambda r: httpcap.Reply(403, json={"message": "denied"})):
            ok, err = await p.validate()
            self.assertIn("403", err)
        with transport(lambda r: httpcap.Reply(200, json={"authorizationData": []})):
            self.assertIn("empty", (await p.validate())[1])
        with transport(lambda r: httpcap.Reply(200, json={"authorizationData": [{"authorizationToken": "bm9jb2xvbg=="}]})):
            self.assertIn("malformed", (await p.validate())[1])
        with transport(lambda r: httpcap.Reply(200, text="not json")):
            self.assertIn("decode", (await p.validate())[1])
        with self.assertRaises(ValueError):
            ECRProvider(Registry(type="ecr", url="x", config={"region": "eu-west-1"}))


class TestACR(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reg = Registry(type="acr", url="https://myreg.azurecr.io",
                            config={"tenantId": "t1", "clientId": "c1", "clientSecret": "cs"})

    def handler(self, request):
        url = str(request.url)
        form = parse_qs(request.content.decode()) if request.method == "POST" else {}
        if url == "https://login.microsoftonline.com/t1/oauth2/v2.0/token":
            if form.get("client_secret") != ["cs"]:
                return httpcap.Reply(401, json={"error": "invalid_client"})
            return httpcap.Reply(200, json={"access_token": "aad"})
        if url == "https://myreg.azurecr.io/oauth2/exchange":
            assert form["access_token"] == ["aad"]
            return httpcap.Reply(200, json={"refresh_token": "refresh"})
        if url == "https://myreg.azurecr.io/oauth2/token":
            assert form["refresh_token"] == ["refresh"]
            return httpcap.Reply(200, json={"access_token": "access-" + form["scope"][0]})
        if url == "https://myreg.azurecr.io/v2/_catalog":
            assert request.headers["Authorization"] == "Bearer access-registry:catalog:*"
            return httpcap.Reply(200, json={"repositories": ["web"]})
        if url == "https://myreg.azurecr.io/v2/web/tags/list":
            return httpcap.Reply(200, json={"tags": ["v1", "v2"]})
        return httpcap.Reply(404, text="nope")

    async def test_exchange_and_browse(self):
        p = ACRProvider(self.reg)
        with transport(self.handler):
            self.assertEqual((await p.credentials())[:2], ("00000000-0000-0000-0000-000000000000", "refresh"))
            self.assertEqual(decode_registry_auth(await p.docker_auth())[1], "refresh")
            self.assertEqual(await p.validate(), (False, None))
            self.assertEqual(await p.list_repositories(), ["web"])
            self.assertEqual(await p.list_tags("web"), ["v1", "v2"])
            with self.assertRaises(ValueError):
                await p.list_tags("missing")
        bad = ACRProvider(Registry(type="acr", url=self.reg.url, config={**self.reg.config, "clientSecret": "x"}))
        with transport(self.handler):
            self.assertIn("401", (await bad.validate())[1])
        with transport(lambda r: httpcap.Reply(200, json={})):
            self.assertIn("empty", (await p.validate())[1])
        with transport(lambda r: httpcap.Reply(200, text="<html>")):
            self.assertIn("decode", (await p.validate())[1])
        with self.assertRaises(ValueError):
            ACRProvider(Registry(type="acr", url="x", config={}))


class TestGCP(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode()
        self.sa = {"client_email": "sa@proj.iam.gserviceaccount.com", "private_key": pem,
                   "token_uri": "https://oauth2.googleapis.com/token"}

    def handler(self, request):
        form = parse_qs(request.content.decode())
        assertion = form["assertion"][0]
        head, claims, sig = assertion.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        self.key.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{head}.{claims}".encode(),
                                     padding.PKCS1v15(), hashes.SHA256())
        c = json.loads(base64.urlsafe_b64decode(pad(claims)))
        assert c["iss"] == self.sa["client_email"] and c["aud"] == self.sa["token_uri"]
        return httpcap.Reply(200, json={"access_token": "ya29.token"})

    async def test_assertion_and_errors(self):
        p = GCPProvider(Registry(type="gcp", url="europe-docker.pkg.dev", config={"jsonKey": json.dumps(self.sa)}))
        with transport(self.handler):
            self.assertEqual(await p.credentials(), ("oauth2accesstoken", "ya29.token", "europe-docker.pkg.dev"))
            self.assertEqual(decode_registry_auth(await p.docker_auth())[0], "oauth2accesstoken")
            self.assertEqual(await p.validate(), (False, None))
        with transport(lambda r: httpcap.Reply(400, text="invalid_grant")):
            self.assertIn("400", (await p.validate())[1])
        with transport(lambda r: httpcap.Reply(200, json={})):
            self.assertIn("empty", (await p.validate())[1])
        for key in ("{bad", json.dumps([1]), json.dumps({"client_email": "x"}),
                    json.dumps({"client_email": "x", "private_key": "not a pem"})):
            p = GCPProvider(Registry(type="gcp", url="x", config={"jsonKey": key}))
            self.assertIsNotNone((await p.validate())[1])
        with self.assertRaises(ValueError):
            GCPProvider(Registry(type="gcp", url="x", config={}))


if __name__ == "__main__":
    unittest.main()
