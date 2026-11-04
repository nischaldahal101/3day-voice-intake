"""Unit tests for jobs.py schema bootstrap + pool settings (review-log F-26).

No Postgres: jobs.py is loaded under a private module name with a fake
ConnectionPool, so the import-time _bootstrap() runs against a recording fake
cursor. Does not touch sys.modules["jobs"] (other suites stub it).

Run: /tmp/vi-venv/bin/python -m unittest test_jobs_bootstrap
"""

import importlib.util
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import dotenv
import psycopg_pool
from psycopg import errors as pg_errors

_JOBS_PATH = Path(__file__).resolve().parent / "jobs.py"


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.pool.executed.append(sql)
        fail = self.conn.pool.fail_on
        if fail and fail[0] in sql:
            raise fail[1]("canceling statement due to lock timeout")

    def fetchone(self):
        return {"missing": self.conn.pool.missing}


class _FakeConn:
    def __init__(self, pool):
        self.pool = pool

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.pool.executed.append("COMMIT")

    def rollback(self):
        self.pool.executed.append("ROLLBACK")


class _FakePool:
    """Stands in for psycopg_pool.ConnectionPool; records every statement."""
    check_connection = staticmethod(psycopg_pool.ConnectionPool.check_connection)
    missing = 0          # what the catalog check returns
    fail_on = None       # (substring, exception class) to raise on execute

    def __init__(self, conninfo, **kwargs):
        self.conninfo, self.kwargs = conninfo, kwargs
        self.executed, self.closed = [], False

    def open(self):
        pass

    def wait(self):
        pass

    def close(self):
        self.closed = True

    @contextmanager
    def connection(self):
        conn = _FakeConn(self)
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise


def _load_jobs():
    spec = importlib.util.spec_from_file_location("_jobs_bootstrap_under_test", _JOBS_PATH)
    module = importlib.util.module_from_spec(spec)
    # no .env loading: it would leak real env vars into suites run in-process
    with mock.patch.object(psycopg_pool, "ConnectionPool", _FakePool), \
            mock.patch.object(dotenv, "load_dotenv", lambda *a, **k: False):
        spec.loader.exec_module(module)
    return module


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        _FakePool.missing, _FakePool.fail_on = 0, None
        self.jobs = _load_jobs()           # import-time bootstrap, schema present
        self.pool = self.jobs.pool

    def _rerun(self, missing, fail_on=None):
        self.pool.executed.clear()
        self.pool.missing, self.pool.fail_on = missing, fail_on
        with mock.patch.object(self.jobs.time, "sleep") as sleep:
            try:
                self.jobs._bootstrap()
            finally:
                self.sleeps = [c.args[0] for c in sleep.call_args_list]

    def test_schema_present_runs_no_ddl_and_no_lock(self):
        executed = self.pool.executed       # from the import itself
        self.assertEqual(len([s for s in executed if s != "COMMIT"]), 1, executed)
        self.assertIn("to_regclass", executed[0])
        joined = "\n".join(executed)
        for forbidden in ("ALTER TABLE", "CREATE TABLE", "CREATE INDEX",
                          "pg_advisory_xact_lock", "LOCK TABLE"):
            self.assertNotIn(forbidden, joined)
        self.assertFalse(self.pool.closed)

    def test_schema_missing_runs_ddl_under_advisory_lock_with_lock_timeout(self):
        self._rerun(missing=1)
        e = self.pool.executed
        i_lock = next(i for i, s in enumerate(e) if "pg_advisory_xact_lock" in s)
        i_timeout = next(i for i, s in enumerate(e) if "SET LOCAL lock_timeout" in s)
        i_ddl = e.index(self.jobs._SCHEMA_SQL)
        # advisory lock is the DDL transaction's first statement (F-22)
        self.assertEqual(e[i_lock - 1], "COMMIT")   # catalog read committed before it
        self.assertLess(i_lock, i_timeout)
        self.assertLess(i_timeout, i_ddl)
        self.assertEqual(e[i_ddl + 1], "COMMIT")
        self.assertEqual(self.sleeps, [])

    def test_lock_timeout_retries_then_raises_and_closes_pool(self):
        with self.assertRaises(pg_errors.LockNotAvailable):
            self._rerun(missing=1, fail_on=("ALTER TABLE", pg_errors.LockNotAvailable))
        ddl_runs = [s for s in self.pool.executed if s == self.jobs._SCHEMA_SQL]
        self.assertEqual(len(ddl_runs), self.jobs._DDL_ATTEMPTS)
        self.assertEqual(self.sleeps, list(self.jobs._DDL_BACKOFF_S))
        self.assertIn("ROLLBACK", self.pool.executed)
        self.assertTrue(self.pool.closed)

    def test_retry_stops_when_other_process_created_schema(self):
        calls = {"n": 0}
        real = _FakeCursor.execute

        def flaky(cur, sql, params=None):
            if sql == self.jobs._SCHEMA_SQL:
                calls["n"] += 1
                cur.conn.pool.missing = 0          # the other process finished
                raise pg_errors.LockNotAvailable("lock timeout")
            return real(cur, sql, params)

        with mock.patch.object(_FakeCursor, "execute", flaky):
            self._rerun(missing=1)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.sleeps, [self.jobs._DDL_BACKOFF_S[0]])
        self.assertFalse(self.pool.closed)

    def test_other_errors_are_not_retried(self):
        with self.assertRaises(pg_errors.UndefinedTable):
            self._rerun(missing=1, fail_on=("ALTER TABLE", pg_errors.UndefinedTable))
        self.assertEqual(self.sleeps, [])
        self.assertTrue(self.pool.closed)


class SchemaObjectsTests(unittest.TestCase):
    def setUp(self):
        _FakePool.missing, _FakePool.fail_on = 0, None
        self.jobs = _load_jobs()

    def test_every_schema_statement_is_recognised(self):
        objects = self.jobs._schema_objects()
        self.assertIsNotNone(objects, "a _SCHEMA_SQL statement isn't covered by _DDL_PATTERNS")
        relations, columns = objects
        self.assertEqual(sorted(relations), sorted([
            "consultation_jobs", "customer_links", "fieldpulse_writes",
            "idx_consultation_jobs_status", "idx_consultation_jobs_created_at"]))
        self.assertEqual(sorted(columns), [("consultation_jobs", "customer"),
                                           ("consultation_jobs", "transcript")])

    def test_unknown_statement_means_always_run_ddl(self):
        sql = self.jobs._SCHEMA_SQL + "\nALTER TABLE consultation_jobs ADD CONSTRAINT x CHECK (true);\n"
        self.assertIsNone(self.jobs._schema_objects(sql))
        with mock.patch.object(self.jobs, "_SCHEMA_SQL", sql):
            self.assertTrue(self.jobs._schema_missing(_FakeConn(self.jobs.pool)))

    def test_pool_hardening_settings(self):
        kw = self.jobs.pool.kwargs
        self.assertIs(kw["check"], psycopg_pool.ConnectionPool.check_connection)
        self.assertEqual(kw["timeout"], 10.0)
        self.assertEqual(kw["max_idle"], 300.0)
        self.assertEqual(kw["max_lifetime"], 1800.0)
        opts = kw["kwargs"]["options"]
        for o in ("statement_timeout=20000", "lock_timeout=5000",
                  "idle_in_transaction_session_timeout=60000"):
            self.assertIn(o, opts)


if __name__ == "__main__":
    unittest.main()
