"""Network map: graph building, filtering and deterministic layered layout."""

import copy
import json
import random
import unittest

from kowan.api import netmap


def sample(shuffle_seed=None):
    containers = [
        {"id": "c-web", "name": "shop-web-1", "state": "running", "image": "nginx",
         "labels": {"com.docker.compose.depends_on": "api:service_started:false",
                    "traefik.http.routers.w.rule": "Host(`shop.example.com`)"},
         "stack": "shop", "service": "web",
         "ports": [{"ip": "0.0.0.0", "publicPort": 8080, "privatePort": 80, "protocol": "tcp"}]},
        {"id": "c-api", "name": "shop-api-1", "state": "running", "image": "api",
         "labels": {"com.docker.compose.depends_on": "db:service_healthy:true"},
         "stack": "shop", "service": "api", "ports": []},
        {"id": "c-db", "name": "shop-db-1", "state": "exited", "image": "postgres:16", "labels": {},
         "stack": "shop", "service": "db",
         "ports": [{"ip": "0.0.0.0", "publicPort": 5432, "privatePort": 5432, "protocol": "tcp"}]},
        {"id": "c-solo", "name": "solo", "state": "running", "image": "redis", "labels": {}, "stack": "",
         "service": "", "ports": [{"ip": "127.0.0.1", "publicPort": 6379, "privatePort": 6379, "protocol": "tcp"}]},
    ]
    networks = [
        {"id": "n1", "name": "shop_default", "driver": "bridge", "containers": ["c-web", "c-api", "c-db"]},
        {"id": "n2", "name": "bridge", "driver": "bridge", "containers": ["c-solo"]},
        {"id": "n3", "name": "proxy", "driver": "bridge", "containers": ["c-web"]},
    ]
    if shuffle_seed is not None:
        rnd = random.Random(shuffle_seed)
        rnd.shuffle(containers)
        rnd.shuffle(networks)
    return containers, networks


class TestNetmap(unittest.TestCase):
    def test_depends_on_label(self):
        self.assertEqual(netmap.parse_depends_on("db:service_started:false, redis:service_healthy:true"), ["db", "redis"])
        self.assertEqual(netmap.parse_depends_on(""), [])

    def test_graph(self):
        g = netmap.build_host_graph("h", "host-a", *sample())
        kinds = sorted(n["kind"] for n in g["nodes"].values())
        self.assertEqual(kinds.count("container"), 4)
        self.assertEqual(kinds.count("network"), 3)
        self.assertEqual(kinds.count("port"), 3)
        self.assertEqual(kinds.count("domain"), 1)
        dep = {(e["from"], e["to"]) for e in g["edges"] if e["kind"] == "depends"}
        self.assertEqual(dep, {("h:c:c-web", "h:c:c-api"), ("h:c:c-api", "h:c:c-db")})
        pg = g["nodes"]["h:p:0.0.0.0:5432/tcp"]
        self.assertTrue(pg["warn"])
        self.assertEqual(g["nodes"]["h:p:127.0.0.1:6379/tcp"]["state"], "local")

    def test_layout_deterministic_and_ordered(self):
        a = netmap.layout([netmap.build_host_graph("h", "host-a", *sample())])
        for seed in range(5):
            b = netmap.layout([netmap.build_host_graph("h", "host-a", *sample(seed))])
            self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        by = {n["id"]: n for n in a["nodes"]}
        # depends_on order inside the stack: db, api, web (dependencies first)
        ys = [by[f"h:c:c-{x}"]["y"] for x in ("db", "api", "web")]
        self.assertEqual(ys, sorted(ys))
        # the standalone group comes after named stacks
        self.assertGreater(by["h:c:c-solo"]["y"], by["h:c:c-web"]["y"])
        # columns
        self.assertEqual(by["h:n:proxy"]["x"], netmap.COL_NETWORK_X)
        self.assertEqual(by["h:d:shop.example.com"]["x"], netmap.COL_INGRESS_X)
        # no overlaps within a column
        for x in {n["x"] for n in a["nodes"]}:
            col = sorted((n for n in a["nodes"] if n["x"] == x), key=lambda n: n["y"])
            for p, q in zip(col, col[1:]):
                self.assertGreaterEqual(q["y"], p["y"] + p["h"])
        self.assertEqual(a["groups"][0]["kind"], "host")

    def test_multi_host_bands(self):
        g1 = netmap.build_host_graph("h1", "beta", *sample())
        g2 = netmap.build_host_graph("h2", "alpha", *sample())
        out = netmap.layout([g1, g2])
        hosts = [g for g in out["groups"] if g["kind"] == "host"]
        self.assertEqual([h["label"] for h in hosts], ["alpha", "beta"])
        self.assertGreater(hosts[1]["y"], hosts[0]["y"] + hosts[0]["h"])

    def test_filters(self):
        g = netmap.build_host_graph("h", "host-a", *sample())
        f = netmap.filter_graph(g, stack="shop")
        self.assertNotIn("h:c:c-solo", f["nodes"])
        self.assertIn("h:p:0.0.0.0:8080/tcp", f["nodes"])
        f = netmap.filter_graph(g, network="proxy")
        self.assertEqual(sorted(k for k, n in f["nodes"].items() if n["kind"] == "container"), ["h:c:c-web"])
        self.assertEqual(sorted(k for k, n in f["nodes"].items() if n["kind"] == "network"), ["h:n:proxy"])
        f = netmap.filter_graph(g, stack="-")
        self.assertEqual([k for k, n in f["nodes"].items() if n["kind"] == "container"], ["h:c:c-solo"])
        self.assertIs(netmap.filter_graph(g), g)

    def test_cycle_does_not_hang(self):
        c, n = sample()
        c[2]["labels"] = {"com.docker.compose.depends_on": "web"}
        out = netmap.layout([netmap.build_host_graph("h", "x", c, n)])
        self.assertEqual(sum(1 for x in out["nodes"] if x["kind"] == "container"), 4)


if __name__ == "__main__":
    unittest.main()
