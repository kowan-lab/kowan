"""GitOps resource files, the defensive paths: parse errors with their
context, the sync path and symlinks that try to leave the checkout, files
that are too many, too large or not text, and repository file references."""

import os
import tempfile
import unittest
from unittest import mock

from kowan.gitops import resources as r

API = r.API_VERSION


class TestParse(unittest.TestCase):
    def test_errors_carry_kind_and_name(self):
        res, errs = r.parse_documents(
            f"apiVersion: {API}\nkind: Deployment\nmetadata: {{name: web}}\nspec: []\n---\n"
            f"apiVersion: {API}\nkind: \"\"\nmetadata: {{name: x}}\n---\n"
            f"apiVersion: {API}\nkind: Deployment\nmetadata: {{name: ok, labels: {{team: web}}}}\nspec: {{}}\n---\n"
            "services: {}\n---\n- just a list\n", "app.yml")
        self.assertEqual([x.name for x in res], ["ok"])
        self.assertEqual(res[0].labels, {"team": "web"})
        self.assertEqual([e.to_dict() for e in errs], [
            {"file": "app.yml", "doc": 0, "message": "spec must be a mapping", "kind": "Deployment", "name": "web"},
            {"file": "app.yml", "doc": 1, "message": "kind is required"},
        ])
        _, errs = r.parse_documents("a: [", "bad.yml")
        self.assertIn("invalid YAML (line", errs[0].message)


class TestTree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "apps", ".hidden"))
        self.write("apps/web.yml", f"apiVersion: {API}\nkind: Deployment\nmetadata: {{name: web}}\nspec: {{}}\n")
        self.write("apps/.hidden/x.yml", "not: scanned")
        self.write("apps/notes.txt", "ignored")

    def write(self, rel, text, mode="w"):
        with open(os.path.join(self.root, rel), mode) as f:
            f.write(text)

    def test_paths_and_files(self):
        self.assertEqual(r.scan_tree(self.root, "../etc")[1][0].message, "sync path must stay inside the repository")
        self.assertIn("not found", r.scan_tree(self.root, "nope")[1][0].message)
        outside = tempfile.mkdtemp()
        os.symlink(outside, os.path.join(self.root, "link"))
        self.assertIn("escapes the repository", r.scan_tree(self.root, "link")[1][0].message)
        with open(os.path.join(outside, "secret.yml"), "w") as f:
            f.write("x: 1")
        os.symlink(os.path.join(outside, "secret.yml"), os.path.join(self.root, "apps", "evil.yml"))
        self.write("apps/binary.yml", b"\xff\xfe\x00", mode="wb")
        res, errs = r.scan_tree(self.root, "apps")
        self.assertEqual([x.name for x in res], ["web"])
        msgs = {e.file: e.message for e in errs}
        self.assertEqual(msgs["apps/evil.yml"], "symlink escapes the repository")
        self.assertIn("cannot read", msgs["apps/binary.yml"])
        with mock.patch.object(r, "MAX_FILE_BYTES", 10):
            _, errs = r.scan_tree(self.root, "apps")
            self.assertIn("file is too large", [e.message for e in errs])
        with mock.patch.object(r, "MAX_FILES", 1):
            _, errs = r.scan_tree(self.root, "apps")
            self.assertIn("too many YAML files", errs[0].message)

    def test_repo_file_refs(self):
        self.write("apps/compose.yml", "services: {}")
        self.assertEqual(r.read_repo_file(self.root, "apps/web.yml", "compose.yml"), "services: {}")
        self.assertEqual(r.read_repo_file(self.root, "apps/web.yml", "/apps/compose.yml"), "services: {}")
        for ref, msg in (("../../etc/passwd", "outside"), ("missing.yml", "not found")):
            with self.assertRaisesRegex(ValueError, msg):
                r.read_repo_file(self.root, "apps/web.yml", ref)
        outside = tempfile.mkdtemp()
        os.symlink(outside, os.path.join(self.root, "out"))
        with self.assertRaisesRegex(ValueError, "outside"):
            r.read_repo_file(self.root, "web.yml", "out/x")
        with mock.patch.object(r, "MAX_FILE_BYTES", 3), self.assertRaisesRegex(ValueError, "too large"):
            r.read_repo_file(self.root, "apps/web.yml", "compose.yml")
        self.assertEqual(r.render_document("Deployment", "web", {}, {"a": "b"})["metadata"], {"name": "web", "labels": {"a": "b"}})


if __name__ == "__main__":
    unittest.main()
