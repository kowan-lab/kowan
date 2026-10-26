"""Teams + resource-scoped permissions (kowan/auth/authorize.py).

Covers the authorize() matrix across roles/teams/scopes/levels, principal
loading (teams, direct grants, API keys), the ScopedOps proxy (host-level ops,
object checks by labels, list filtering, fail-closed unknown ops), the REST
surfaces (hosts / deployments / containers / volumes / teams / grants / API
keys / audit) and MCP tool filtering, plus "no teams = unchanged behaviour".
"""

import datetime
import json
import tempfile
import unittest
import uuid
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from kowan.auth import authorize as az
from kowan.config import ServerConfig
from kowan.docker import Provider
from kowan.errors import Forbidden
from kowan.models import APIKey, Container, Deployment, Host, Network, User, Volume
from kowan.server import build_app
from kowan.store import Store, generate_api_key_secret, hash_api_key_secret
from kowan.store.teams import GrantRow, Team

NOW = "2026-10-25T00:00:00Z"
DEP_ID = "dep-1"


def G(scope, level, host_id="", target="", resource_name=""):
    return az.Grant(scope=scope, level=level, host_id=host_id, target=target, resource_name=resource_name)


def P(role="operator", grants=(), scoped=True, kind="user"):
    return az.Principal(id="u", username="u", role=role, kind=kind, grants=tuple(grants), scoped=scoped)


def C(host="h1", **labels):
    return az.Resource(az.KIND_CONTAINER, host_id=host, labels=labels)


class TestAuthorizeMatrix(unittest.TestCase):
    def test_admin_bypasses(self):
        self.assertTrue(az.authorize(P("admin", scoped=True), az.LEVEL_MANAGE, az.Resource(az.KIND_GLOBAL)))

    def test_unscoped_follows_role_ceiling(self):
        for role, ok in (("viewer", {"read"}), ("operator", {"read", "operate", "manage"})):
            for lvl in az.LEVELS:
                self.assertEqual(az.authorize(P(role, scoped=False), lvl, C()), lvl in ok, (role, lvl))

    def test_scoped_without_grants_denied(self):
        self.assertFalse(az.authorize(P(scoped=True), az.LEVEL_READ, C()))

    def test_level_ordering(self):
        p = P(grants=[G(az.SCOPE_HOST, az.LEVEL_OPERATE, "h1")])
        self.assertTrue(az.authorize(p, az.LEVEL_READ, C()))
        self.assertTrue(az.authorize(p, az.LEVEL_OPERATE, C()))
        self.assertFalse(az.authorize(p, az.LEVEL_MANAGE, C()))

    def test_role_ceiling_caps_grants(self):
        p = P("viewer", grants=[G(az.SCOPE_ALL_HOSTS, az.LEVEL_MANAGE)])
        self.assertTrue(az.authorize(p, az.LEVEL_READ, C()))
        self.assertFalse(az.authorize(p, az.LEVEL_OPERATE, C()))

    def test_scopes(self):
        cases = [
            (G(az.SCOPE_ALL_HOSTS, "read"), az.Resource(az.KIND_GLOBAL), True),
            (G(az.SCOPE_HOST, "read", "h1"), az.Resource(az.KIND_GLOBAL), False),
            (G(az.SCOPE_HOST, "read", "h1"), az.Resource(az.KIND_IMAGE, host_id="h1"), True),
            (G(az.SCOPE_HOST, "read", "h1"), az.Resource(az.KIND_IMAGE, host_id="h2"), False),
            (G(az.SCOPE_STACK, "read", "h1", "web"), C(**{az.COMPOSE_PROJECT_LABEL: "web"}), True),
            (G(az.SCOPE_STACK, "read", "h1", "web"), C("h2", **{az.COMPOSE_PROJECT_LABEL: "web"}), False),
            (G(az.SCOPE_STACK, "read", "h1", "web"), C(**{az.COMPOSE_PROJECT_LABEL: "api"}), False),
            (G(az.SCOPE_STACK, "read", "h1", "web"), az.Resource(az.KIND_IMAGE, host_id="h1"), False),
            (G(az.SCOPE_STACK, "read", "h1", "web"), az.Resource(az.KIND_HOST, host_id="h1"), False),
            (G(az.SCOPE_LABELS, "read", "", "team=blue"), C("h9", team="blue"), True),
            (G(az.SCOPE_LABELS, "read", "", "team=blue"), C(team="red"), False),
            (G(az.SCOPE_LABELS, "read", "h1", "team=blue"), C("h2", team="blue"), False),
            (G(az.SCOPE_LABELS, "read", "", "team=blue"),
             az.Resource(az.KIND_VOLUME, host_id="h1", labels={"team": "blue"}), True),
            (G(az.SCOPE_LABELS, "read", "", "team=blue"),
             az.Resource(az.KIND_DEPLOYMENT, host_id="h1", id="x"), False),
            (G(az.SCOPE_DEPLOYMENT, "read", "h1", DEP_ID, "app"), az.Resource(az.KIND_DEPLOYMENT, "h1", DEP_ID), True),
            (G(az.SCOPE_DEPLOYMENT, "read", "h1", DEP_ID, "app"), az.Resource(az.KIND_DEPLOYMENT, "h1", "other"), False),
            (G(az.SCOPE_DEPLOYMENT, "read", "h1", DEP_ID, "app"), C(**{az.DEPLOYMENT_ID_LABEL: DEP_ID}), True),
            (G(az.SCOPE_DEPLOYMENT, "read", "h1", DEP_ID, "app"), C(**{az.COMPOSE_PROJECT_LABEL: "app"}), True),
            (G(az.SCOPE_DEPLOYMENT, "read", "h1", DEP_ID, "app"), C("h2", **{az.COMPOSE_PROJECT_LABEL: "app"}), False),
            (G(az.SCOPE_DEPLOYMENT, "manage", "h1", DEP_ID, "app"), az.Resource(az.KIND_STACK, "h1", stack="app"), True),
            (G(az.SCOPE_HOST, "read", "h1"), az.Resource(az.KIND_DEPLOYMENT, "h1", "x", stack="s"), True),
            (G(az.SCOPE_STACK, "read", "h1", "s"), az.Resource(az.KIND_DEPLOYMENT, "h1", "x", stack="s"), True),
        ]
        for g, r, want in cases:
            self.assertEqual(az.authorize(P(grants=[g]), "read", r), want, (g, r))

    def test_touches_host(self):
        self.assertTrue(az.touches_host(P(grants=[G(az.SCOPE_STACK, "read", "h1", "x")]), "h1"))
        self.assertFalse(az.touches_host(P(grants=[G(az.SCOPE_STACK, "read", "h1", "x")]), "h2"))
        self.assertTrue(az.touches_host(P(grants=[G(az.SCOPE_LABELS, "read", "", "a=b")]), "h2"))
        self.assertFalse(az.touches_host(P(grants=[]), "h1"))
        self.assertTrue(az.touches_host(P(scoped=False), "h1"))

    def test_scoped_admin_key_is_not_admin(self):
        self.assertFalse(P("admin", scoped=True, kind="apikey").is_admin)

    def test_request_level(self):
        self.assertEqual(az.request_level("GET", "/api/v1/deployments/x"), "read")
        self.assertEqual(az.request_level("POST", "/api/v1/deployments/x/deploy"), "operate")
        self.assertEqual(az.request_level("POST", "/api/v1/deployments/x/destroy"), "manage")
        self.assertEqual(az.request_level("DELETE", "/api/v1/deployments/x"), "manage")
        self.assertEqual(az.request_level("GET", "/api/v1/hosts/h/containers/c/exec"), "operate")


class TestLoadPrincipal(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.create_deployment(Deployment(id=DEP_ID, name="My App", type="compose", strategy="standalone",
                                                targets=["h1"], createdAt=NOW, updatedAt=NOW))

    def user(self, role="operator", uid="u1"):
        return SimpleNamespace(id=uid, username=uid, role=role, key_hash="")

    def test_no_teams_unscoped(self):
        p = az.load_principal(self.store, self.user())
        self.assertFalse(p.scoped)

    def test_team_member_scoped_and_grants_expand(self):
        self.store.create_team(Team(id="t1", name="web", created_at=NOW))
        self.store.add_team_member("t1", "u1", "manual", NOW)
        self.store.create_grant(GrantRow("g1", "team", "t1", "deployment", "", DEP_ID, "operate", NOW))
        p = az.load_principal(self.store, self.user())
        self.assertTrue(p.scoped)
        self.assertEqual(p.team_ids, ("t1",))
        g = p.grants[0]
        self.assertEqual((g.host_id, g.resource_name, g.source), ("h1", "my-app", "team:t1"))
        # other users stay unscoped
        self.assertFalse(az.load_principal(self.store, self.user(uid="u2")).scoped)

    def test_empty_team_fails_closed(self):
        self.store.create_team(Team(id="t1", name="web", created_at=NOW))
        self.store.add_team_member("t1", "u1", "manual", NOW)
        p = az.load_principal(self.store, self.user())
        self.assertTrue(p.scoped)
        self.assertFalse(az.authorize(p, "read", C()))

    def test_snapshot_invalidation(self):
        self.assertFalse(az.load_principal(self.store, self.user()).scoped)
        self.store.create_grant(GrantRow("g1", "user", "u1", "host", "h1", "", "read", NOW))
        self.assertTrue(az.load_principal(self.store, self.user()).scoped)
        self.store.delete_grant("g1")
        self.assertFalse(az.load_principal(self.store, self.user()).scoped)

    def test_api_key_scoping(self):
        secret, prefix = generate_api_key_secret()
        self.store.create_team(Team(id="t1", name="web", created_at=NOW))
        self.store.create_grant(GrantRow("g1", "team", "t1", "host", "h1", "", "read", NOW))
        self.store.create_api_key(APIKey(id="k1", name="ci", prefix=prefix, role="operator", createdAt=NOW,
                                         teamId="t1"), hash_api_key_secret(secret))
        u = SimpleNamespace(id="apikey", username="key:ci", role="operator", key_hash=hash_api_key_secret(secret))
        p = az.load_principal(self.store, u)
        self.assertTrue(p.scoped)
        self.assertTrue(az.authorize(p, "read", C("h1")))
        self.assertFalse(az.authorize(p, "read", C("h2")))
        # unbound key: unscoped
        s2, p2 = generate_api_key_secret()
        self.store.create_api_key(APIKey(id="k2", name="x", prefix=p2, role="viewer", createdAt=NOW),
                                  hash_api_key_secret(s2))
        u2 = SimpleNamespace(id="apikey", username="key:x", role="viewer", key_hash=hash_api_key_secret(s2))
        self.assertFalse(az.load_principal(self.store, u2).scoped)
        # narrowed key scopes
        self.store.create_grant(GrantRow("g2", "apikey", "k2", "labels", "", "team=blue", "read", NOW))
        p3 = az.load_principal(self.store, u2)
        self.assertTrue(p3.scoped)
        self.assertTrue(az.authorize(p3, "read", C("h5", team="blue")))
        self.assertFalse(az.authorize(p3, "read", C("h5")))
        # revoked key
        self.store.delete_api_key("k2")
        self.assertFalse(az.authorize(az.load_principal(self.store, u2), "read", C("h5", team="blue")))


# --------------------------------------------------------------------------- fake docker


def _labels(**kw):
    return dict(kw)


CONTAINERS = [
    Container(id="c-web", names=["/web"], image="nginx", state="running", labels=_labels(**{az.COMPOSE_PROJECT_LABEL: "web"})),
    Container(id="c-blue", names=["/blue"], image="redis", state="running", labels=_labels(team="blue")),
    Container(id="c-dep", names=["/dep"], image="app", state="running", labels=_labels(**{az.DEPLOYMENT_ID_LABEL: DEP_ID})),
    Container(id="c-other", names=["/other"], image="busybox", state="exited", labels={}),
]


class FakeOps:
    def __init__(self):
        self.calls = []

    async def ping(self):
        return None

    async def list_containers(self, all):
        return list(CONTAINERS)

    async def inspect_container(self, id):
        for c in CONTAINERS:
            if c.id == id:
                return {"Id": c.id, "Name": c.names[0], "Config": {"Labels": dict(c.labels), "Image": c.image},
                        "State": {"Status": c.state}}
        from kowan.errors import NotFound

        raise NotFound("no such container")

    async def restart_container(self, id):
        self.calls.append(("restart", id))

    async def stop_container(self, id):
        self.calls.append(("stop", id))

    async def remove_container(self, id, force):
        self.calls.append(("remove", id))

    async def create_container(self, name, spec, start=True):
        self.calls.append(("create", name))
        return "new"

    async def list_volumes(self):
        return [Volume(name="v-web", driver="local", labels={az.COMPOSE_PROJECT_LABEL: "web"}),
                Volume(name="v-x", driver="local", labels=None)]

    async def inspect_volume(self, name):
        return {"Name": name, "Labels": {az.COMPOSE_PROJECT_LABEL: "web"} if name == "v-web" else None}

    async def remove_volume(self, name, force):
        self.calls.append(("rmvol", name))

    async def list_networks(self):
        return [Network(id="n-web", name="web_default", labels={az.COMPOSE_PROJECT_LABEL: "web"}),
                Network(id="n-x", name="bridge")]

    async def list_images(self):
        return [{"Id": "sha256:1"}]

    async def system_info(self):
        return {"ok": True}

    async def prune_images(self, all):
        self.calls.append(("prune",))
        return 0

    async def compose_up(self, project, yaml):
        self.calls.append(("compose_up", project))
        return "ok"

    async def pull_image(self, ref, auth=None):
        self.calls.append(("pull", ref))
        return None

    async def mystery_op(self):
        return "boom"


class TestScopedOps(unittest.IsolatedAsyncioTestCase):
    def ctx(self, grants, role="operator"):
        c = az.RequestAuthz(None, SimpleNamespace(id="u", username="u", role=role))
        c._principal = P(role, grants=grants)
        return c

    async def run_with(self, ctx, coro_fn):
        tok = az.bind(ctx)
        try:
            return await coro_fn()
        finally:
            az.unbind(tok)

    async def test_wrap_fast_paths(self):
        raw = FakeOps()
        host = SimpleNamespace(id="h1")
        self.assertIs(az.wrap_ops(host, raw), raw)  # no context
        tok = az.bind(self.ctx([G(az.SCOPE_HOST, "manage", "h1")]))
        try:
            self.assertIs(az.wrap_ops(host, raw), raw)  # host-wide manage
            self.assertIsInstance(az.wrap_ops(SimpleNamespace(id="h2"), raw), az.ScopedOps)
        finally:
            az.unbind(tok)

    async def test_stack_scope(self):
        raw = FakeOps()
        ctx = self.ctx([G(az.SCOPE_STACK, "operate", "h1", "web")])
        ops = az.ScopedOps(raw, "h1", ctx)
        tok = az.bind(ctx)
        try:
            ids = [c.id for c in await ops.list_containers(True)]
            self.assertEqual(ids, ["c-web"])
            await ops.restart_container("c-web")
            with self.assertRaises(Forbidden):
                await ops.restart_container("c-other")
            with self.assertRaises(Forbidden):
                await ops.remove_container("c-web", False)  # operate < manage
            self.assertEqual([v.name for v in await ops.list_volumes()], ["v-web"])
            self.assertEqual([n.id for n in await ops.list_networks()], ["n-web"])
            self.assertEqual(await ops.list_images(), [])  # host-level list -> empty
            with self.assertRaises(Forbidden):
                await ops.system_info()
            with self.assertRaises(Forbidden):
                await ops.prune_images(False)
            with self.assertRaises(Forbidden):
                await ops.mystery_op()  # unknown op fails closed
            await ops.ping()
            self.assertIs(ops.inner, ops)
        finally:
            az.unbind(tok)
        self.assertEqual(raw.calls, [("restart", "c-web")])

    async def test_host_without_access(self):
        ctx = self.ctx([G(az.SCOPE_STACK, "read", "h1", "web")])
        ops = az.ScopedOps(FakeOps(), "h2", ctx)
        tok = az.bind(ctx)
        try:
            with self.assertRaises(Forbidden):
                await ops.ping()
            with self.assertRaises(Forbidden):
                await ops.list_containers(True)
        finally:
            az.unbind(tok)

    async def test_label_and_create(self):
        raw = FakeOps()
        ctx = self.ctx([G(az.SCOPE_LABELS, "manage", "", "team=blue")])
        ops = az.ScopedOps(raw, "h3", ctx)
        tok = az.bind(ctx)
        try:
            self.assertEqual([c.id for c in await ops.list_containers(True)], ["c-blue"])
            await ops.create_container("x", SimpleNamespace(labels={"team": "blue"}), True)
            with self.assertRaises(Forbidden):
                await ops.create_container("y", SimpleNamespace(labels={}), True)
            with self.assertRaises(Forbidden):
                await ops.compose_up("web", "")
        finally:
            az.unbind(tok)

    async def test_host_read_only(self):
        raw = FakeOps()
        ctx = self.ctx([G(az.SCOPE_HOST, "read", "h1")])
        ops = az.ScopedOps(raw, "h1", ctx)
        tok = az.bind(ctx)
        try:
            self.assertEqual(len(await ops.list_containers(True)), 4)
            self.assertEqual(await ops.system_info(), {"ok": True})
            with self.assertRaises(Forbidden):
                await ops.stop_container("c-web")
        finally:
            az.unbind(tok)

    async def test_deployment_elevation(self):
        raw = FakeOps()
        ctx = self.ctx([G(az.SCOPE_DEPLOYMENT, "operate", "h1", DEP_ID, "app")])
        tok = az.bind(ctx)
        try:
            ops = az.ScopedOps(raw, "h1", ctx)
            with self.assertRaises(Forbidden):
                await ops.pull_image("nginx")
            dep = Deployment(id=DEP_ID, name="app", targets=["h1"])
            az.check_deployment(dep, az.LEVEL_OPERATE)
            await ops.pull_image("nginx")  # deploy may pull on its own host
            with self.assertRaises(Forbidden):
                az.check_deployment(dep, az.LEVEL_MANAGE)
            with self.assertRaises(Forbidden):
                az.check_deployment(Deployment(id="other", name="o", targets=["h1"]), az.LEVEL_READ)
        finally:
            az.unbind(tok)


# --------------------------------------------------------------------------- REST + MCP


def _cfg():
    return ServerConfig(
        addr="127.0.0.1:0", data_dir=tempfile.mkdtemp(), jwt_secret="x" * 48,
        admin_user="admin", admin_password="adminpass", seed_admin=True,
        monitor_interval=datetime.timedelta(seconds=3600),
        metrics_retention=datetime.timedelta(hours=1), secret_key="", external_url="",
    )


class APIBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app, self.svc = await build_app(_cfg())
        self.fake = FakeOps()
        self.svc.docker = Provider(local=self.fake, local_host_id="local")
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        st = self.svc.store
        st.create_host(Host(id="h2", name="second", type="agent", mode="edge", createdAt=NOW))
        for d in (Deployment(id=DEP_ID, name="app", type="compose", strategy="standalone", targets=["local"],
                             createdAt=NOW, updatedAt=NOW),
                  Deployment(id="dep-2", name="other", type="compose", strategy="standalone", targets=["h2"],
                             createdAt=NOW, updatedAt=NOW)):
            st.create_deployment(d)
        self.admin = st.get_user_by_username("admin")
        self.users = {}
        for name, role in (("alice", "operator"), ("bob", "viewer"), ("carol", "operator")):
            u = User(id=str(uuid.uuid4()), username=name, role=role, passwordHash="", authSource="local",
                     createdAt=NOW)
            st.create_user(u)
            self.users[name] = u

    async def asyncTearDown(self):
        await self.client.close()

    def h(self, who="admin"):
        u = self.admin if who == "admin" else self.users[who]
        return {"Authorization": "Bearer " + self.svc.jwt.issue(u.id, u.username, u.role)}

    async def req(self, method, path, who="admin", **kw):
        r = await self.client.request(method, path, headers=self.h(who), **kw)
        try:
            body = await r.json()
        except Exception:  # noqa: BLE001
            body = await r.text()
        return r.status, body


class TestRESTScoping(APIBase):
    async def test_no_teams_unchanged(self):
        s, hosts = await self.req("GET", "/api/v1/hosts", "bob")
        self.assertEqual(s, 200)
        self.assertEqual({h["id"] for h in hosts}, {"local", "h2"})
        s, deps = await self.req("GET", "/api/v1/deployments", "bob")
        self.assertEqual(len(deps), 2)
        s, cs = await self.req("GET", "/api/v1/hosts/local/containers?all=1", "bob")
        self.assertEqual(len(cs), 4)
        s, _ = await self.req("POST", "/api/v1/hosts/local/containers/c-web/restart", "bob")
        self.assertEqual(s, 403)  # role gate as before
        s, _ = await self.req("POST", "/api/v1/hosts/local/containers/c-other/restart", "alice")
        self.assertEqual(s, 200)

    async def test_team_grants_end_to_end(self):
        s, team = await self.req("POST", "/api/v1/teams", json={"name": "web", "description": "web team"})
        self.assertEqual(s, 201, team)
        tid = team["id"]
        s, _ = await self.req("POST", f"/api/v1/teams/{tid}/members", json={"userId": self.users["alice"].id})
        self.assertEqual(s, 201)
        # member of a team without grants: sees nothing
        s, hosts = await self.req("GET", "/api/v1/hosts", "alice")
        self.assertEqual(hosts, [])
        s, g = await self.req("POST", "/api/v1/grants", json={
            "subjectType": "team", "subjectId": tid, "scope": "stack", "hostId": "local", "target": "web",
            "level": "operate"})
        self.assertEqual(s, 201, g)
        s, g2 = await self.req("POST", "/api/v1/grants", json={
            "subjectType": "team", "subjectId": tid, "scope": "deployment", "target": DEP_ID, "level": "read"})
        self.assertEqual(s, 201, g2)
        # bad grant
        s, err = await self.req("POST", "/api/v1/grants", json={
            "subjectType": "team", "subjectId": tid, "scope": "labels", "target": "nokey", "level": "read"})
        self.assertEqual(s, 400)

        s, hosts = await self.req("GET", "/api/v1/hosts", "alice")
        self.assertEqual([h["id"] for h in hosts], ["local"])
        s, _ = await self.req("GET", "/api/v1/hosts/h2", "alice")
        self.assertEqual(s, 403)
        s, cs = await self.req("GET", "/api/v1/hosts/local/containers?all=1", "alice")
        self.assertEqual(sorted(c["id"] for c in cs), ["c-dep", "c-web"])
        s, _ = await self.req("POST", "/api/v1/hosts/local/containers/c-web/restart", "alice")
        self.assertEqual(s, 200)
        s, _ = await self.req("POST", "/api/v1/hosts/local/containers/c-other/restart", "alice")
        self.assertEqual(s, 403)
        s, _ = await self.req("DELETE", "/api/v1/hosts/local/containers/c-web", "alice")
        self.assertEqual(s, 403)
        s, _ = await self.req("GET", "/api/v1/hosts/local/containers/c-other", "alice")
        self.assertEqual(s, 403)
        s, imgs = await self.req("GET", "/api/v1/hosts/local/images", "alice")
        self.assertEqual(imgs, [])
        s, vols = await self.req("GET", "/api/v1/hosts/local/volumes", "alice")
        self.assertEqual([v["name"] for v in vols], ["v-web"])
        s, deps = await self.req("GET", "/api/v1/deployments", "alice")
        self.assertEqual([d["id"] for d in deps], [DEP_ID])
        s, _ = await self.req("GET", "/api/v1/deployments/dep-2", "alice")
        self.assertEqual(s, 403)
        s, _ = await self.req("POST", f"/api/v1/deployments/{DEP_ID}/stop", "alice")
        self.assertEqual(s, 403)  # read-only on the deployment
        s, _ = await self.req("DELETE", f"/api/v1/deployments/{DEP_ID}", "alice")
        self.assertEqual(s, 403)
        s, wfs = await self.req("GET", "/api/v1/workflows", "alice")
        self.assertEqual(wfs, [])
        # carol (no team) still unrestricted
        s, cs = await self.req("GET", "/api/v1/hosts/local/containers?all=1", "carol")
        self.assertEqual(len(cs), 4)
        # admin bypass
        s, cs = await self.req("GET", "/api/v1/hosts/local/containers?all=1")
        self.assertEqual(len(cs), 4)

        # effective permissions
        s, perms = await self.req("GET", f"/api/v1/users/{self.users['alice'].id}/permissions")
        self.assertEqual(s, 200)
        self.assertEqual(perms["mode"], "scoped")
        self.assertEqual(len(perms["grants"]), 2)
        self.assertEqual(perms["teams"][0]["teamName"], "web")
        s, mine = await self.req("GET", "/api/v1/me/permissions", "alice")
        self.assertEqual(mine["mode"], "scoped")
        s, _ = await self.req("GET", "/api/v1/teams", "alice")
        self.assertEqual(s, 403)

        # audit has the grant changes
        s, _ = await self.req("DELETE", f"/api/v1/grants/{g['id']}")
        self.assertEqual(s, 200)
        from kowan.store.audit import AuditListOptions

        entries, _ = self.svc.store.list_audit(AuditListOptions(limit=500))
        actions = [e.action for e in entries]
        self.assertIn("grant.create", actions)
        self.assertIn("grant.delete", actions)
        self.assertIn("team.member.add", actions)
        created = [e for e in entries if e.action == "grant.create"][0]
        self.assertIn('"scope"', created.detail)

        # deleting the team restores global behaviour
        s, _ = await self.req("DELETE", f"/api/v1/teams/{tid}")
        self.assertEqual(s, 200)
        s, cs = await self.req("GET", "/api/v1/hosts/local/containers?all=1", "alice")
        self.assertEqual(len(cs), 4)

    async def test_direct_user_grant_host(self):
        s, _ = await self.req("POST", "/api/v1/grants", json={
            "subjectType": "user", "subjectId": self.users["carol"].id, "scope": "host", "hostId": "h2",
            "level": "manage"})
        self.assertEqual(s, 201)
        s, hosts = await self.req("GET", "/api/v1/hosts", "carol")
        self.assertEqual([h["id"] for h in hosts], ["h2"])
        s, _ = await self.req("GET", "/api/v1/hosts/local/containers", "carol")
        self.assertEqual(s, 403)
        s, deps = await self.req("GET", "/api/v1/deployments", "carol")
        self.assertEqual([d["id"] for d in deps], ["dep-2"])
        # create a deployment on a host outside the scope -> 403; inside -> 201
        body = {"name": "n1", "type": "compose", "strategy": "standalone", "targets": ["local"],
                "compose": "services:\n  a:\n    image: nginx\n"}
        s, _ = await self.req("POST", "/api/v1/deployments", "carol", json=body)
        self.assertEqual(s, 403)
        body["targets"] = ["h2"]
        s, created = await self.req("POST", "/api/v1/deployments", "carol", json=body)
        self.assertEqual(s, 201, created)

    async def test_api_key_scoping(self):
        s, team = await self.req("POST", "/api/v1/teams", json={"name": "ci"})
        tid = team["id"]
        await self.req("POST", "/api/v1/grants", json={"subjectType": "team", "subjectId": tid, "scope": "host",
                                                      "hostId": "h2", "level": "read"})
        s, err = await self.req("POST", "/api/v1/apikeys", json={"name": "bad", "role": "admin", "teamId": tid})
        self.assertEqual(s, 400)
        s, made = await self.req("POST", "/api/v1/apikeys", json={"name": "k", "role": "operator", "teamId": tid})
        self.assertEqual(s, 201, made)
        kh = {"Authorization": "Bearer " + made["secret"]}
        r = await self.client.get("/api/v1/hosts", headers=kh)
        self.assertEqual([h["id"] for h in await r.json()], ["h2"])
        s, made2 = await self.req("POST", "/api/v1/apikeys", json={
            "name": "k2", "role": "viewer",
            "scopes": [{"scope": "labels", "target": "team=blue", "level": "read"}]})
        self.assertEqual(s, 201, made2)
        self.assertEqual(len(made2["apiKey"]["scopes"]), 1)
        r = await self.client.get("/api/v1/hosts/local/containers?all=1",
                                  headers={"Authorization": "Bearer " + made2["secret"]})
        self.assertEqual([c["id"] for c in await r.json()], ["c-blue"])
        s, keys = await self.req("GET", "/api/v1/apikeys")
        k2 = [k for k in keys if k["name"] == "k2"][0]
        self.assertEqual(k2["scopes"][0]["target"], "team=blue")

    async def test_mcp_tools_filtered(self):
        s, team = await self.req("POST", "/api/v1/teams", json={"name": "web"})
        tid = team["id"]
        await self.req("POST", f"/api/v1/teams/{tid}/members", json={"userId": self.users["alice"].id})
        await self.req("POST", "/api/v1/grants", json={"subjectType": "team", "subjectId": tid, "scope": "stack",
                                                      "hostId": "local", "target": "web", "level": "operate"})
        hdr = dict(self.h("alice"))
        hdr.update({"Content-Type": "application/json", "Accept": "application/json"})
        r = await self.client.post("/mcp", headers=hdr, data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}))
        self.assertEqual(r.status, 200, await r.text())
        hdr["Mcp-Session-Id"] = r.headers["Mcp-Session-Id"]
        hdr["MCP-Protocol-Version"] = "2025-06-18"

        async def call(name, args):
            r = await self.client.post("/mcp", headers=hdr, data=json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}))
            body = await r.json()
            return body["result"]

        res = await call("list_hosts", {})
        self.assertEqual([h["id"] for h in res["structuredContent"]["items"]], ["local"])
        res = await call("list_containers", {"host": "local", "include_stopped": True})
        names = sorted(i["name"] for i in res["structuredContent"]["items"])
        self.assertEqual(names, ["web"])
        res = await call("list_deployments", {})
        self.assertEqual(res["structuredContent"]["items"], [])
        res = await call("get_deployment", {"deployment": DEP_ID})
        self.assertTrue(res.get("isError"))
        res = await call("list_containers", {"host": "h2"})
        self.assertTrue(res.get("isError"))


if __name__ == "__main__":
    unittest.main()
