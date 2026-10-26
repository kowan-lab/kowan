"""Tests for the Workflows feature: env-style run variables.

Fast stdlib unittest, no network. Three surfaces are covered:

  * ``_expand_env``         — the ``$NAME`` / ``${NAME}`` substitution helper;
  * ``_parse_export_file``  — the ``KEY=VALUE`` export-file parser;
  * a store round-trip      — a Workflow's ``variables`` map survives create+get.
"""

import os
import tempfile
import unittest

from kowan.models import Workflow, WorkflowStep
from kowan.store import Store
from kowan.workflow.engine import _expand_env, _parse_export_file


class TestExpandEnv(unittest.TestCase):
    """``_expand_env`` does CI-style dollar-var substitution, leaving unknown
    names literal (never blanked)."""

    def test_bare_dollar_name(self):
        env = {"FOO": "bar", "N": "42"}
        self.assertEqual(_expand_env("x=$FOO", env), "x=bar")
        self.assertEqual(_expand_env("$FOO/$N", env), "bar/42")

    def test_braced_form_abuts_following_chars(self):
        env = {"FOO": "bar"}
        # The braces let the reference abut following NAME characters.
        self.assertEqual(_expand_env("${FOO}baz", env), "barbaz")
        # Bare form is greedy up to the first non-NAME char.
        self.assertEqual(_expand_env("$FOO-baz", env), "bar-baz")

    def test_unknown_name_left_literal(self):
        env = {"FOO": "bar"}
        # Unknown references are kept verbatim, NOT replaced with "".
        self.assertEqual(_expand_env("$MISSING", env), "$MISSING")
        self.assertEqual(_expand_env("${MISSING}", env), "${MISSING}")
        self.assertEqual(_expand_env("a $FOO b $MISSING c", env), "a bar b $MISSING c")

    def test_no_dollar_is_untouched(self):
        self.assertEqual(_expand_env("plain text", {"FOO": "bar"}), "plain text")
        # A lone dollar not followed by a NAME is left as-is.
        self.assertEqual(_expand_env("cost is $ 5", {}), "cost is $ 5")


class TestParseExportFile(unittest.TestCase):
    """``_parse_export_file`` parses ``KEY=VALUE`` export lines."""

    def test_basic_lines(self):
        out = _parse_export_file("A=1\nB=two\n")
        self.assertEqual(out, {"A": "1", "B": "two"})

    def test_split_on_first_equals(self):
        # Values may contain '='; only the first splits key from value.
        out = _parse_export_file("URL=https://x/y?a=b\n")
        self.assertEqual(out, {"URL": "https://x/y?a=b"})

    def test_skips_blank_and_comment_and_keyless_lines(self):
        text = "\n# a comment\n  \nGOOD=yes\nnoequalshere\n=novalue-key-empty\n"
        out = _parse_export_file(text)
        self.assertEqual(out, {"GOOD": "yes"})

    def test_key_is_trimmed(self):
        out = _parse_export_file("  KEY  =value\n")
        self.assertEqual(out, {"KEY": "value"})


class TestWorkflowVariablesRoundTrip(unittest.TestCase):
    """A Workflow's ``variables`` map survives a store create + get unchanged."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "t.db"))

    def test_variables_persist(self):
        wf = Workflow(
            id="w1",
            name="wf",
            steps=[WorkflowStep(name="s", type="script", config={"script": "echo hi"})],
            variables={"REGION": "eu", "STAGE": "prod"},
            enabled=True,
            createdAt="t",
            updatedAt="t",
        )
        self.store.create_workflow(wf)

        got = self.store.get_workflow("w1")
        self.assertEqual(got.variables, {"REGION": "eu", "STAGE": "prod"})

    def test_empty_variables_default(self):
        wf = Workflow(id="w2", name="wf2", createdAt="t", updatedAt="t")
        self.store.create_workflow(wf)
        got = self.store.get_workflow("w2")
        self.assertEqual(got.variables, {})

    def test_variables_update(self):
        wf = Workflow(id="w3", name="wf3", variables={"A": "1"}, createdAt="t", updatedAt="t")
        self.store.create_workflow(wf)
        wf.variables = {"A": "2", "B": "3"}
        self.store.update_workflow(wf)
        got = self.store.get_workflow("w3")
        self.assertEqual(got.variables, {"A": "2", "B": "3"})


if __name__ == "__main__":
    unittest.main()
