"""Migrate: a workload Kowan only discovered becomes a Kowan deployment,
from Docker itself: the original compose files when the host has them, else
a compose file rebuilt from the containers; a lone container from its spec."""

import base64
import unittest

import yaml

from kowan.importer.fromdocker import absolutize, rebuild_compose
from tests.apicase import ServerCase


def _insp(name, service, **kw):
    cfg = {"Image": "app:1", "Env": ["PATH=/bin", "MODE=prod", "DB_PASSWORD=s3cret", "PRICE=$5"], "Cmd": ["serve"],
           "Labels": {"com.docker.compose.project": "shop", "com.docker.compose.service": service, "team": "web"}}
    cfg.update(kw.pop("Config", {}))
    return {"Name": "/" + name, "Config": cfg,
            "HostConfig": {"PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]}, "RestartPolicy": {"Name": "unless-stopped"},
                           "Memory": 268435456, **kw.pop("HostConfig", {})},
            "Mounts": [{"Type": "volume", "Name": "shop_data", "Destination": "/data", "RW": True},
                       {"Type": "bind", "Source": "/srv/conf", "Destination": "/conf", "RW": False}],
            "NetworkSettings": {"Networks": {"shop_default": {}, "proxy": {}}}, **kw}


class TestRebuild(unittest.TestCase):
    def test_rebuild_from_inspect(self):
        image = {"Env": ["PATH=/bin"], "Cmd": ["serve"], "Labels": {}}
        text, variables, warnings = rebuild_compose(
            [(_insp("shop-web-1", "web"), image), (_insp("shop-web-2", "web"), image), (_insp("shop-db-1", "db"), image)], "shop")
        doc = yaml.safe_load(text)
        web = doc["services"]["web"]
        self.assertEqual(web["image"], "app:1")
        self.assertNotIn("command", web)  # the image's own
        self.assertEqual(web["environment"], {"MODE": "prod", "DB_PASSWORD": "${DB_PASSWORD}", "PRICE": "$$5"})
        self.assertEqual(variables, [{"key": "DB_PASSWORD", "value": "s3cret", "secret": True}])
        self.assertEqual(web["ports"], ["8080:80"])
        self.assertEqual(web["volumes"], ["data:/data", "/srv/conf:/conf:ro"])
        self.assertEqual(doc["volumes"], {"data": {"external": True, "name": "shop_data"}})
        self.assertEqual(doc["networks"], {"proxy": {"external": True, "name": "proxy"}})
        self.assertEqual(web["networks"], ["proxy", "default"])
        self.assertEqual((web["restart"], web["mem_limit"], web["labels"], web["scale"]), ("unless-stopped", 268435456, {"team": "web"}, 2))
        self.assertNotIn("container_name", web)
        self.assertEqual(warnings, [])

    def test_relative_paths_become_absolute(self):
        compose = "services:\n  web:\n    image: x\n    build: .\n    volumes:\n      - ./data:/data\n      - named:/n\n"
        out, warnings = absolutize(compose, "/opt/shop")
        self.assertEqual(yaml.safe_load(out)["services"]["web"]["volumes"], ["/opt/shop/data:/data", "named:/n"])
        self.assertTrue(any("builds from the folder" in w for w in warnings))


class TestMigrateApi(ServerCase):
    async def test_lone_container_and_rebuilt_stack(self):
        self.engine.add_container("redis", "redis:7", env=["MODE=cache"])
        labels = {"com.docker.compose.project": "blog", "com.docker.compose.service": "web"}
        self.engine.add_container("blog-web-1", "nginx:1.27", labels=labels, ports=[(80, 8081)])
        _, items = await self.call("GET", "/import/docker", "viewer", expect=200)
        self.assertEqual({i["workload"] for i in items} >= {"ctr~local~redis", "stack~local~blog"}, True)
        await self.call("POST", "/import/docker/scan", "viewer", json={"workload": "ctr~local~redis"}, expect=403)
        _, c = await self.call("POST", "/import/docker/scan", "operator", json={"workload": "ctr~local~redis"}, expect=200)
        self.assertEqual((c["type"], c["name"], c["spec"]["image"]), ("container", "redis", "redis:7"))
        _, st = await self.call("POST", "/import/docker/scan", "operator", json={"workload": "stack~local~blog"}, expect=200)
        self.assertEqual((st["type"], st["source"], st["name"]), ("compose", "rebuilt", "blog"))
        self.assertIn("8081:80", st["compose"])
        _, res = await self.call("POST", "/import/apply", "operator", json={"items": [
            {"name": "redis", "host": "local", "type": "container", "spec": c["spec"], "deploy": True},
            {"name": "blog", "host": "local", "strategy": "standalone", "compose": st["compose"], "variables": st["variables"]},
        ]}, expect=200)
        self.assertTrue(all(r["ok"] for r in res), res)
        self.assertTrue(res[0].get("deploying"))
        dep = self.svc.store.get_deployment(res[1]["id"])
        self.assertEqual(dep.resource_name(), "blog")  # same project: it takes over the containers
        await self.call("POST", "/import/docker/scan", "operator", json={"workload": f"dep~{dep.id}"}, expect=400)

    async def test_original_files_from_the_host(self):
        compose = "services:\n  web:\n    image: nginx:1.27\n    volumes:\n      - ./html:/usr/share/nginx/html\n"
        files = {"/opt/site/docker-compose.yml": compose, "/opt/site/.env": "TAG=1.27\nAPI_TOKEN=abc\n"}

        def helper(c):  # stands in for the throwaway container reading the folder
            lines = []
            for path, text in files.items():
                lines += [f"@@FILE {path}", base64.b64encode(text.encode()).decode()]
            return 0, lines

        self.engine.runners.append(("restic/", helper))
        labels = {"com.docker.compose.project": "site", "com.docker.compose.service": "web",
                  "com.docker.compose.project.working_dir": "/opt/site",
                  "com.docker.compose.project.config_files": "/opt/site/docker-compose.yml"}
        self.engine.add_container("site-web-1", "nginx:1.27", labels=labels)
        _, st = await self.call("POST", "/import/docker/scan", "operator", json={"workload": "stack~local~site"}, expect=200)
        self.assertEqual((st["source"], st["path"]), ("files", "/opt/site/docker-compose.yml"))
        self.assertIn("/opt/site/html:/usr/share/nginx/html", st["compose"])
        self.assertEqual(st["variables"], [{"key": "TAG", "value": "1.27", "secret": False},
                                           {"key": "API_TOKEN", "value": "abc", "secret": True}])
        create = [c for c in self.engine.calls if c[0] == "POST" and c[1] == "/containers/create"][-1][3]
        self.assertTrue(all(b.endswith(":ro") for b in create["HostConfig"]["Binds"]))
        self.assertFalse(any("restic" in c["Config"]["Image"] for c in self.engine.containers.values()))  # removed


if __name__ == "__main__":
    unittest.main()
