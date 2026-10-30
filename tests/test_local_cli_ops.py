"""LocalClient ops that shell out to the ``docker`` CLI (here the fake one from
tests/fakecli.py): image builds (inline Dockerfile and Git, push with login,
failures) and containers created through ``docker run`` because of extraArgs."""

import unittest

from kowan.docker.local import LocalClient
from kowan.errors import BadGateway, Validation
from kowan.models import ContainerSpec, KeyValue, PortMapping, VolumeMapping
from kowan.models.build import BuildOptions
from tests.fakecli import FakeDockerCLI
from tests.fakeengine import FakeEngine


class CLICase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cli = FakeDockerCLI()
        self.engine = FakeEngine()
        self.client = LocalClient(await self.engine.start())

    async def asyncTearDown(self):
        await self.client.close()
        await self.engine.stop()
        self.cli.close()

    def argvs(self):
        return [c["argv"] for c in self.cli.calls()]


class TestBuild(CLICase):
    async def test_inline_dockerfile_build_and_push(self):
        self.cli.succeed("sha256:feed\n")
        res = await self.client.build_image(BuildOptions(
            tag="app:1", dockerfile="FROM alpine\n", no_cache=True, build_args={"V": "1"}, push=True,
            registry_host="reg.example", registry_user="bob", registry_pass="s3cret-Pw!"))
        self.assertTrue(res.success, res.output)
        self.assertEqual(res.image_id, "sha256:feed")
        argv = self.argvs()
        build = next(a for a in argv if a[0] == "build")
        self.assertIn("--no-cache", build)
        self.assertIn("V=1", build)
        self.assertEqual(build[build.index("-t") + 1], "app:1")
        # The Dockerfile is written to the temp context and passed with -f.
        df = build[build.index("-f") + 1]
        self.assertEqual(self.cli.calls()[0]["files"][df], "FROM alpine\n")
        self.assertIn(["login", "reg.example", "-u", "bob", "--password-stdin"], argv)
        self.assertIn(["push", "app:1"], argv)
        # The password goes on stdin only (a distinctive one: the random temp
        # dir name could contain a short one by chance).
        self.assertNotIn("s3cret-Pw!", " ".join(" ".join(a) for a in argv))

    async def test_failures(self):
        self.cli.fail_with(1, "step 2 failed", only="build")
        res = await self.client.build_image(BuildOptions(tag="app:1", dockerfile="FROM x\n"))
        self.assertFalse(res.success)
        self.assertIn("step 2 failed", res.output)
        self.cli.fail_with(1, "denied", only="push")
        res = await self.client.build_image(BuildOptions(tag="app:1", dockerfile="FROM x\n", push=True))
        self.assertFalse(res.success)
        self.cli.fail_with(1, "bad login", only="login")
        res = await self.client.build_image(BuildOptions(tag="app:1", dockerfile="FROM x\n", push=True,
                                                         registry_user="u", registry_pass="p"))
        self.assertFalse(res.success)
        self.assertIn("bad login", res.output)

    async def test_validation(self):
        for opts in (BuildOptions(dockerfile="FROM x"), BuildOptions(tag="a"),
                     BuildOptions(tag="a", git_url="file:///etc"),
                     BuildOptions(tag="a", git_url="https://example.com/r.git", git_ref="--upload-pack=x"),
                     BuildOptions(tag="a", dockerfile="FROM x", context_subdir="../../etc")):
            with self.assertRaises(Validation, msg=opts):
                await self.client.build_image(opts)

    async def test_git_clone_failure(self):
        # No real network: point git at a port nothing listens on.
        res_err = None
        try:
            await self.client.build_image(BuildOptions(tag="a", git_url="https://127.0.0.1:9/none.git", git_ref="main"))
        except BadGateway as err:
            res_err = err
        self.assertIsNotNone(res_err)
        self.assertIn("git clone", str(res_err))


class TestRunViaCLI(CLICase):
    async def test_extra_args_use_docker_run(self):
        cid = self.engine.add_container("made-by-run")
        self.cli.succeed(f"Unable to find image locally\n{cid}\n")
        spec = ContainerSpec(
            image="nginx:1.27", env=[KeyValue(key="A", value="1")],
            ports=[PortMapping(hostPort=8080, containerPort=80)],
            volumes=[VolumeMapping(source="data", target="/data", readOnly=True)],
            networks=["web"], restartPolicy="always", workingDir="/srv", user="1000",
            entrypoint=["/bin/sh", "-c"], cpuLimit=0.5, memoryLimitMB=256, memoryReservationMB=128,
            privileged=True, gpus="all", openStdin=True, tty=True, labels={"team": "web"},
            extraArgs=["--cap-add", "NET_ADMIN"], command=["echo", "hi"])
        got = await self.client.create_container("svc", spec, False)
        self.assertEqual(got, cid)
        run = next(a for a in self.argvs() if a[0] == "run")
        joined = " ".join(run)
        for piece in ("--name svc", "-e A=1", "-p 8080:80/tcp", "-v data:/data:ro", "--network web",
                      "--restart always", "-w /srv", "-u 1000", "--entrypoint /bin/sh -c", "--cpus 0.5",
                      "--memory 256m", "--memory-reservation 128m", "--privileged", "--gpus all", "-i", "-t",
                      "--label team=web", "--cap-add NET_ADMIN", "nginx:1.27 echo hi"):
            self.assertIn(piece, joined)
        # start=False: the container docker run started is stopped again.
        self.assertFalse(self.engine.containers[cid]["State"]["Running"])

        self.cli.fail_with(125, "invalid flag", only="run")
        with self.assertRaises(BadGateway):
            await self.client.create_container("svc2", spec, True)


if __name__ == "__main__":
    unittest.main()
