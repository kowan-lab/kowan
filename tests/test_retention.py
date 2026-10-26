"""Retention prunes for audit_log, alerts and workflow_runs (kowan/store/retention.py)."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from kowan.config import load_server_config
from kowan.models import Alert, AuditEntry, WorkflowRun
from kowan.store import Store
from kowan.store.retention import prune_retention

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.store._conn.close()

    def _count(self, table: str) -> int:
        return self.store._one(f"SELECT COUNT(*) AS n FROM {table}")["n"]

    def _ids(self, sql: str, params=()):
        return sorted(r[0] for r in self.store._query(sql, params))

    def test_audit_older_than_retention_is_deleted(self):
        for i, age in enumerate((1, 89, 91, 400)):
            self.store.create_audit(AuditEntry(id=f"a{i}", time=_ts(age), action="x"))
        n = self.store.prune_audit(_ts(90))
        self.assertEqual(n, 2)
        self.assertEqual(self._ids("SELECT id FROM audit_log"), ["a0", "a1"])

    def test_audit_prune_chunks(self):
        for i in range(25):
            self.store.create_audit(AuditEntry(id=f"a{i:02d}", time=_ts(100), action="x"))
        self.assertEqual(self.store.prune_audit(_ts(90), batch=7), 25)
        self.assertEqual(self._count("audit_log"), 0)

    def test_only_old_resolved_alerts_are_deleted(self):
        rows = [
            ("old-resolved", 40, True),
            ("old-active", 40, False),
            ("new-resolved", 5, True),
            ("new-active", 5, False),
        ]
        for id_, age, resolved in rows:
            self.store.create_alert(
                Alert(id=id_, level="warning", resolved=resolved, createdAt=_ts(age))
            )
        self.assertEqual(self.store.prune_resolved_alerts(_ts(30)), 1)
        self.assertEqual(
            self._ids("SELECT id FROM alerts"), ["new-active", "new-resolved", "old-active"]
        )

    def _run(self, wf: str, i: int, status: str = "success"):
        self.store.create_run(
            WorkflowRun(
                id=f"{wf}-{i:03d}", workflowId=wf, status=status,
                startedAt=(NOW - timedelta(minutes=1000 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )

    def test_keep_latest_runs_per_workflow(self):
        for i in range(12):
            self._run("w1", i)
        for i in range(3):
            self._run("w2", i)
        self.assertEqual(self.store.prune_workflow_runs(5), 7)
        self.assertEqual(
            self._ids("SELECT id FROM workflow_runs WHERE workflow_id = 'w1'"),
            [f"w1-{i:03d}" for i in range(7, 12)],
        )
        self.assertEqual(self._count("workflow_runs"), 5 + 3)  # w2 untouched
        # list_runs (the UI) still sees the newest first
        self.assertEqual(self.store.list_runs("w1", 0)[0].id, "w1-011")

    def test_running_run_is_never_pruned(self):
        self._run("w1", 0, status="running")  # oldest, still running
        for i in range(1, 6):
            self._run("w1", i)
        self.store.prune_workflow_runs(2)
        self.assertEqual(
            self._ids("SELECT id FROM workflow_runs"), ["w1-000", "w1-004", "w1-005"]
        )

    def test_keep_zero_disables(self):
        for i in range(3):
            self._run("w1", i)
        self.assertEqual(self.store.prune_workflow_runs(0), 0)
        self.assertEqual(self._count("workflow_runs"), 3)

    def test_prune_retention_applies_all_policies(self):
        self.store.create_audit(AuditEntry(id="old", time=_ts(100), action="x"))
        self.store.create_audit(AuditEntry(id="new", time=_ts(1), action="x"))
        self.store.create_alert(Alert(id="al", level="w", resolved=True, createdAt=_ts(31)))
        for i in range(4):
            self._run("w1", i)
        out = prune_retention(
            self.store,
            audit_retention=timedelta(days=90),
            alert_retention=timedelta(days=30),
            workflow_runs_keep=3,
            now=NOW,
        )
        self.assertEqual(out, {"audit": 1, "alerts": 1, "workflow_runs": 1})

    def test_non_positive_settings_disable(self):
        self.store.create_audit(AuditEntry(id="old", time=_ts(1000), action="x"))
        self.store.create_alert(Alert(id="al", level="w", resolved=True, createdAt=_ts(1000)))
        self._run("w1", 0)
        self._run("w1", 1)
        out = prune_retention(
            self.store, audit_retention=timedelta(0), alert_retention=0,
            workflow_runs_keep=0, now=NOW,
        )
        self.assertEqual(out, {"audit": 0, "alerts": 0, "workflow_runs": 0})

    def test_deletes_are_index_backed(self):
        def plan(sql, params):
            rows = self.store._query("EXPLAIN QUERY PLAN " + sql, params)
            return " ".join(r["detail"] for r in rows)

        self.assertIn(
            "idx_audit_log_time",
            plan("SELECT rowid FROM audit_log WHERE time < ? LIMIT 10", ("x",)),
        )
        self.assertIn(
            "idx_alerts_resolved_created",
            plan(
                "SELECT rowid FROM alerts WHERE resolved = 1 AND created_at < ? LIMIT 10",
                ("x",),
            ),
        )
        self.assertIn(
            "idx_workflow_runs_wf",
            plan(
                "SELECT id, ROW_NUMBER() OVER (PARTITION BY workflow_id "
                "ORDER BY started_at DESC, id DESC) FROM workflow_runs",
                (),
            ),
        )


class TestMonitorSchedulesRetention(unittest.TestCase):
    def test_runs_on_first_tick_then_hourly(self):
        import asyncio

        from kowan.monitor import monitor as mon

        calls = []

        class _Store:
            def list_hosts(self):
                return []

            def list_alert_rules(self):
                return []

            def prune_metrics(self, before):
                return 0

            def prune_audit(self, before):
                calls.append(("audit", before))
                return 0

            def prune_resolved_alerts(self, before):
                calls.append(("alerts", before))
                return 0

            def prune_workflow_runs(self, keep):
                calls.append(("runs", keep))
                return 0

        class _Cfg:
            monitor_interval = timedelta(seconds=15)
            metrics_retention = timedelta(hours=1)
            audit_retention = timedelta(days=90)
            alert_retention = timedelta(days=30)
            workflow_runs_keep = 7

        class _Svc:
            store = _Store()
            docker = None
            bus = None
            config = _Cfg()

        m = mon.Monitor(_Svc())
        clock = [1000.0]
        with mock.patch.object(mon.time, "monotonic", lambda: clock[0]):
            asyncio.run(m._tick())
            self.assertEqual([c[0] for c in calls], ["audit", "alerts", "runs"])
            self.assertIn(("runs", 7), calls)
            clock[0] += 60
            asyncio.run(m._tick())
            self.assertEqual(len(calls), 3)  # not yet an hour
            clock[0] += 3600
            asyncio.run(m._tick())
            self.assertEqual(len(calls), 6)


class TestRetentionConfig(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("KOWAN_AUDIT_RETENTION", "KOWAN_ALERT_RETENTION", "KOWAN_WORKFLOW_RUNS_KEEP"):
                os.environ.pop(k, None)
            c = load_server_config()
        self.assertEqual(c.audit_retention, timedelta(days=90))
        self.assertEqual(c.alert_retention, timedelta(days=30))
        self.assertEqual(c.workflow_runs_keep, 100)

    def test_env_overrides(self):
        env = {
            "KOWAN_AUDIT_RETENTION": "7d",
            "KOWAN_ALERT_RETENTION": "36h",
            "KOWAN_WORKFLOW_RUNS_KEEP": "20",
        }
        with mock.patch.dict(os.environ, env):
            c = load_server_config()
        self.assertEqual(c.audit_retention, timedelta(days=7))
        self.assertEqual(c.alert_retention, timedelta(hours=36))
        self.assertEqual(c.workflow_runs_keep, 20)

    def test_bad_values_fall_back(self):
        env = {"KOWAN_AUDIT_RETENTION": "soon", "KOWAN_WORKFLOW_RUNS_KEEP": "lots"}
        with mock.patch.dict(os.environ, env):
            c = load_server_config()
        self.assertEqual(c.audit_retention, timedelta(days=90))
        self.assertEqual(c.workflow_runs_keep, 100)


if __name__ == "__main__":
    unittest.main()
