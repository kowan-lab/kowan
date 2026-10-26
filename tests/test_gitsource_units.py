"""Git source helpers on their own: push webhook decisions (events, bad
payloads, refs, paths), credential injection into git's environment, URL
validation, compose reading limits and .env interpolation."""

import base64
import json
import os
import tempfile
import unittest
from unittest import mock

from kowan.deployment import gitsource as gs


class TestGitSourceUnits(unittest.TestCase):
    def test_push_decisions(self):
        from types import SimpleNamespace

        git = SimpleNamespace(webhook_secret="s3", ref="main", path="stack/c.yml", auto_deploy=True)

        def push(event, body):
            return gs.evaluate_push(git, {"X-GitHub-Event": event,
                                          "X-Hub-Signature-256": gs.sign_body("s3", body)}, body)

        self.assertEqual(push("ping", b"{}").action, "pong")
        self.assertEqual(push("issues", b"{}").action, "ignore")
        self.assertIn("not JSON", push("push", b"{nope").reason)
        self.assertIn("not an object", push("push", b"[1]").reason)
        self.assertEqual(push("push", json.dumps({"ref": "refs/heads/dev"}).encode()).action, "ignore")
        self.assertEqual(gs.evaluate_push(git, {"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=bad"},
                                          b"{}").action, "unauthorized")
        self.assertEqual(gs.evaluate_push(None, {}, b"").action, "token")
        gl = gs.evaluate_push(git, {"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "s3"},
                              json.dumps({"ref": "refs/heads/main", "checkout_sha": "c" * 40}).encode())
        self.assertNotEqual(gl.action, "unauthorized")
        self.assertTrue(gs.ref_matches("refs/tags/v1", "refs/tags/v1"))
        self.assertEqual(gs.detect_provider({"X-Gitlab-Event": "Push Hook"}), "gitlab")

    def test_git_env_and_urls(self):
        env = gs._git_env(gs.GitAuth(username="", token="tok"))
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        header = env["GIT_CONFIG_VALUE_0"]
        self.assertEqual(base64.b64decode(header.split("Basic ")[1]).decode(), "oauth2:tok")
        self.assertEqual(gs._git_env(gs.GitAuth())["GIT_CONFIG_COUNT"], "0")
        self.assertTrue(gs.GitAuth().empty())
        for bad in ("", "not a url", "ftp://x/y", "file:///etc"):
            with self.assertRaises(ValueError, msg=bad):
                gs.validate_repo_url(bad)
        self.assertEqual(gs.validate_repo_url("https://github.com/a/b.git"), "https://github.com/a/b.git")

    def test_read_compose_limits_and_env(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "stack"))
        with open(os.path.join(d, "stack", "c.yml"), "w") as f:
            f.write("services:\n  a:\n    image: nginx:${TAG}\n")
        with open(os.path.join(d, "stack", ".env"), "w") as f:
            f.write("TAG=1.27\n")
        compose, env = gs.read_compose(d, "/stack/c.yml")
        self.assertIn("nginx:1.27", gs.render_compose(compose, env))
        self.assertEqual(gs.render_compose("x", "  "), "x")
        with self.assertRaises(gs.GitError):
            gs.read_compose(d, "missing.yml")
        with self.assertRaises(gs.GitError):
            gs.read_compose(d, "../../etc/passwd")
        with mock.patch.object(gs, "_MAX_COMPOSE_BYTES", 5):
            with self.assertRaises(gs.GitError):
                gs.read_compose(d, "stack/c.yml")


class TestGitCommands(unittest.IsolatedAsyncioTestCase):
    async def test_missing_git_and_timeouts(self):
        with mock.patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("git")):
            with self.assertRaises(gs.GitError) as cm:
                await gs.ls_remote("https://github.com/a/b.git", "main")
            self.assertIn("not installed", str(cm.exception))
        with mock.patch.object(gs, "_GIT_TIMEOUT", 0.01), \
                mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "https"}):
            with self.assertRaises(gs.GitError):
                # A black-hole address: the connect hangs until the timeout.
                await gs.ls_remote("https://10.255.255.1/a/b.git", "refs/heads/main")


if __name__ == "__main__":
    unittest.main()
