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
        self.opened_with = []            # open() calls (wait flag)
        self.close_timeouts = []
        self.stats = {"pool_min": 1, "pool_max": 5, "pool_size": 5, "pool_available": 0,
                      "requests_waiting": 3, "requests_errors": 7, "usage_ms": 12}
        self.conn_error = None           # raise this from connection() (e.g. PoolTimeout)
        self.conn_delay = 0.0            # sleep this long in connection() (a hang)
        self.close_delay = 0.0
        self.conn_timeouts = []

    def open(self, wait=False, timeout=30.0):
        self.opened_with.append(wait)

    def wait(self, timeout=30.0):
        pass

    def close(self, timeout=5.0):
        self.close_timeouts.append(timeout)
        if self.close_delay:
            import time
            time.sleep(self.close_delay)
        self.closed = True

    def get_stats(self):
        return dict(self.stats)

    @contextmanager
    def connection(self, timeout=None):
        self.conn_timeouts.append(timeout)
        if self.conn_delay:
            import time
            time.sleep(self.conn_delay)
        if self.conn_error:
            raise self.conn_error
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
            "idx_consultation_jobs_status", "idx_consultation_jobs_created_at",
            "vendor_documents", "idx_vendor_documents_created_at",
            "idx_vendor_documents_customer", "vendor_material_lists"]))
        self.assertEqual(sorted(columns), [("consultation_jobs", "customer"),
                                           ("consultation_jobs", "transcript")])

    def test_unknown_statement_means_always_run_ddl(self):
        sql = self.jobs._SCHEMA_SQL + "\nALTER TABLE consultation_jobs ADD CONSTRAINT x CHECK (true);\n"
        self.assertIsNone(self.jobs._schema_objects(sql))
        with mock.patch.object(self.jobs, "_SCHEMA_SQL", sql):
            self.assertTrue(self.jobs._schema_missing(_FakeConn(self.jobs.pool)))

    def test_pool_hardening_settings(self):
        kw = self.jobs.pool.kwargs
        self.assertIs(kw["check"], self.jobs._check_connection)
        self.assertEqual(kw["timeout"], 10.0)
        self.assertEqual(kw["max_idle"], 300.0)
        self.assertEqual(kw["max_lifetime"], 1800.0)
        opts = kw["kwargs"]["options"]
        for o in ("statement_timeout=20000", "lock_timeout=5000",
                  "idle_in_transaction_session_timeout=60000"):
            self.assertIn(o, opts)

    def test_pool_client_timeouts_and_keepalives(self):
        # review-log F-42: silently dropped TCP must not hang checkouts/queries
        conn_kw = self.jobs.pool.kwargs["kwargs"]
        expected = {"connect_timeout": 10, "keepalives": 1, "keepalives_idle": 30,
                    "keepalives_interval": 10, "keepalives_count": 3,
                    "tcp_user_timeout": 30000}
        for key, value in expected.items():
            self.assertEqual(conn_kw.get(key), value, key)
        self.assertIs(conn_kw["row_factory"], self.jobs.dict_row)
        self.assertLessEqual(self.jobs._CHECK_TIMEOUT_S, 5.0)

    def test_pool_conninfo_params_are_valid_for_libpq(self):
        # libpq must accept every key (tcp_user_timeout is a no-op off Linux)
        from psycopg.conninfo import make_conninfo, conninfo_to_dict
        kw = {k: v for k, v in self.jobs.pool.kwargs["kwargs"].items()
              if k != "row_factory"}
        d = conninfo_to_dict(make_conninfo("postgresql://h/db", **kw))
        self.assertEqual(d["tcp_user_timeout"], "30000")
        self.assertEqual(d["keepalives_idle"], "30")


class _FakeResult:
    def __init__(self, status):
        self.status = status


_END = object()      # end of results (libpq: ReadyForQuery received)


class _FakePgconn:
    """pgconn whose server answers only with `answers` (ExecStatus values, then
    _END for the end of results); once they run out it never answers again."""
    def __init__(self, status=None, answers=None):
        import socket
        from psycopg.pq import TransactionStatus
        self.transaction_status = status or TransactionStatus.IDLE
        self._a, self._b = socket.socketpair()      # _a is never readable
        self.socket = self._a.fileno()
        self.answers = list(answers or [])
        self.sent = []

    def send_query(self, q):
        self.sent.append(q)

    def flush(self):
        return 0

    def consume_input(self):
        pass

    def is_busy(self):
        return not self.answers

    def get_result(self):
        if not self.answers:
            raise AssertionError("get_result() while busy would block in libpq")
        a = self.answers.pop(0)
        return None if a is _END else _FakeResult(a)

    def close(self):
        self._a.close(); self._b.close()


class _FakePgServer:
    """Minimal PostgreSQL wire-protocol server on 127.0.0.1 (one connection):
    trust auth, then answers each simple query with `reply(sock)`. Lets the
    check run against real libpq/psycopg with a server that misbehaves.
    Runs in a thread: a check that regresses to a blocking libpq get_result()
    (which holds the GIL) makes these tests HANG rather than fail; that hang is
    the F-44 bug itself."""
    def __init__(self, reply):
        import socket, threading
        self.reply = reply
        self.lsock = socket.socket()
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(1)
        self.port = self.lsock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @staticmethod
    def msg(kind, payload=b""):
        import struct
        return kind + struct.pack("!I", len(payload) + 4) + payload

    def _read(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _serve(self):
        import struct
        try:
            sock, _ = self.lsock.accept()
        except OSError:
            return
        self.sock = sock
        try:
            (n,) = struct.unpack("!I", self._read(sock, 4))
            self._read(sock, n - 4)                                 # StartupMessage
            m = self.msg
            sock.sendall(m(b"R", struct.pack("!I", 0))              # AuthenticationOk
                         + m(b"S", b"client_encoding\0UTF8\0")
                         + m(b"S", b"server_version\x0016.0\0")
                         + m(b"K", struct.pack("!II", 1, 2))
                         + m(b"Z", b"I"))
            while not self.stop.is_set():
                kind = self._read(sock, 1)
                (n,) = struct.unpack("!I", self._read(sock, 4))
                self._read(sock, n - 4)
                if kind == b"X":
                    return
                if kind == b"Q":
                    self.reply(self, sock)
        except (EOFError, OSError):
            pass

    def close(self):
        self.stop.set()
        self.lsock.close()
        if getattr(self, "sock", None):
            self.sock.close()
        self.thread.join(2)


class CheckConnectionTests(unittest.TestCase):
    def setUp(self):
        _FakePool.missing, _FakePool.fail_on = 0, None
        self.jobs = _load_jobs()

    def _conn(self, pg):
        self.addCleanup(pg.close)
        return mock.Mock(pgconn=pg)

    def test_unresponsive_connection_fails_within_deadline(self):
        import time
        from psycopg import OperationalError
        pg = _FakePgconn()
        with mock.patch.object(self.jobs, "_CHECK_TIMEOUT_S", 0.3):
            t0 = time.monotonic()
            with self.assertRaises(OperationalError):
                self.jobs._check_connection(self._conn(pg))
            self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(pg.sent, [b""])

    def test_withheld_end_of_results_fails_within_deadline(self):
        # review-log F-44: first result arrives, the end of results never does
        import time
        from psycopg import OperationalError
        from psycopg.pq import ExecStatus
        pg = _FakePgconn(answers=[ExecStatus.EMPTY_QUERY])
        with mock.patch.object(self.jobs, "_CHECK_TIMEOUT_S", 0.3):
            t0 = time.monotonic()
            with self.assertRaises(OperationalError):
                self.jobs._check_connection(self._conn(pg))
            self.assertLess(time.monotonic() - t0, 2.0)

    def test_healthy_connection_passes(self):
        from psycopg.pq import ExecStatus
        self.jobs._check_connection(self._conn(_FakePgconn(answers=[ExecStatus.EMPTY_QUERY, _END])))

    def test_not_idle_error_or_extra_results_fail(self):
        from psycopg import OperationalError
        from psycopg.pq import ExecStatus, TransactionStatus
        for pg in (_FakePgconn(status=TransactionStatus.INTRANS),
                   _FakePgconn(answers=[ExecStatus.FATAL_ERROR, _END]),
                   _FakePgconn(answers=[ExecStatus.EMPTY_QUERY, ExecStatus.EMPTY_QUERY, _END]),
                   _FakePgconn(answers=[_END])):
            with self.assertRaises(OperationalError):
                self.jobs._check_connection(self._conn(pg))

    # --- real libpq against a fake server (no Postgres needed) -----------------

    def _real_check(self, reply, timeout_s):
        import time
        import psycopg
        srv = _FakePgServer(reply)
        self.addCleanup(srv.close)
        conn = psycopg.connect(host="127.0.0.1", port=srv.port, user="u", dbname="d",
                               sslmode="disable", gssencmode="disable", connect_timeout=5)
        self.addCleanup(conn.close)
        with mock.patch.object(self.jobs, "_CHECK_TIMEOUT_S", timeout_s):
            t0 = time.monotonic()
            try:
                self.jobs._check_connection(conn)
                return None, time.monotonic() - t0
            except psycopg.OperationalError as e:
                return e, time.monotonic() - t0

    def test_real_libpq_healthy_reply_passes(self):
        err, took = self._real_check(
            lambda s, sock: sock.sendall(s.msg(b"I") + s.msg(b"Z", b"I")), 5.0)
        self.assertIsNone(err)
        self.assertLess(took, 1.0)

    def test_real_libpq_partial_reply_then_stall_fails_within_deadline(self):
        # review-log F-44: EmptyQueryResponse arrives, ReadyForQuery never does
        err, took = self._real_check(lambda s, sock: sock.sendall(s.msg(b"I")), 1.0)
        self.assertIsNotNone(err)
        self.assertIn("timed out", str(err))
        self.assertLess(took, 2.0)

    def test_real_libpq_late_tail_fails_at_deadline_not_after(self):
        # F-44 repro shape: ReadyForQuery delayed past the deadline must not pass late
        import time
        def reply(s, sock):
            sock.sendall(s.msg(b"I"))
            time.sleep(2.5)
            sock.sendall(s.msg(b"Z", b"I"))
        err, took = self._real_check(reply, 1.0)
        self.assertIsNotNone(err)
        self.assertLess(took, 2.0)

    def test_real_libpq_no_reply_fails_within_deadline(self):
        err, took = self._real_check(lambda s, sock: None, 1.0)
        self.assertIsNotNone(err)
        self.assertLess(took, 2.0)


class PoolRepairTests(unittest.TestCase):
    """F-82: repeated PoolTimeouts replace the module-level pool; /health ping."""

    def setUp(self):
        _FakePool.missing, _FakePool.fail_on = 0, None
        self.jobs = _load_jobs()
        self.first = self.jobs.pool
        self.clock = [1000.0]
        p = mock.patch.object(self.jobs.time, "monotonic", lambda: self.clock[0])
        p.start()
        self.addCleanup(p.stop)

    def _join_closers(self):
        import threading
        for t in threading.enumerate():
            if t.name == "jobs-old-pool-close":
                t.join(2)

    def test_settings_shared_by_import_pool_and_replacement(self):
        self.jobs.note_pool_timeout()
        self.assertTrue(self.jobs.note_pool_timeout()["pool_replaced"])
        self.assertIsNot(self.jobs.pool, self.first)
        self.assertEqual(self.jobs.pool.kwargs, self.first.kwargs)
        self.assertEqual(self.jobs.pool.conninfo, self.first.conninfo)
        self.assertEqual(self.jobs.pool.opened_with, [False])   # no blocking wait

    def test_one_timeout_does_not_replace(self):
        out = self.jobs.note_pool_timeout()
        self.assertFalse(out["pool_replaced"])
        self.assertIs(self.jobs.pool, self.first)
        # numeric counters only, from the fixed key list (usage_ms is not in it)
        self.assertEqual(out["stats"], {"pool_min": 1, "pool_max": 5, "pool_size": 5,
                                        "pool_available": 0, "requests_waiting": 3,
                                        "requests_errors": 7})

    def test_two_timeouts_outside_window_do_not_replace(self):
        self.jobs.note_pool_timeout()
        self.clock[0] += self.jobs._REPAIR_WINDOW_S + 1
        self.assertFalse(self.jobs.note_pool_timeout()["pool_replaced"])
        self.assertIs(self.jobs.pool, self.first)

    def test_second_timeout_replaces_and_closes_old_in_background(self):
        self.first.close_delay = 0.3          # a slow/stuck old pool
        import time as _t
        self.jobs.note_pool_timeout()
        t0 = _t.perf_counter()
        out = self.jobs.note_pool_timeout()
        self.assertLess(_t.perf_counter() - t0, 0.2, "caller waited for the old pool")
        self.assertTrue(out["pool_replaced"])
        self.assertEqual(out["stats"]["requests_errors"], 7)   # the OLD pool's counters
        self._join_closers()
        self.assertTrue(self.first.closed)
        self.assertEqual(self.first.close_timeouts, [self.jobs._OLD_POOL_CLOSE_S])
        self.assertFalse(self.jobs.pool.closed)

    def test_cooldown_then_repairs_again(self):
        self.jobs.note_pool_timeout()
        self.jobs.note_pool_timeout()
        second = self.jobs.pool
        for _ in range(5):                    # a storm inside the cooldown: no new pool
            self.clock[0] += 5
            self.assertFalse(self.jobs.note_pool_timeout()["pool_replaced"])
        self.assertIs(self.jobs.pool, second)
        self.clock[0] = 1000.0 + self.jobs._REPAIR_COOLDOWN_S + 1
        self.assertTrue(self.jobs.note_pool_timeout()["pool_replaced"])
        self.assertIsNot(self.jobs.pool, second)
        self._join_closers()
        self.assertTrue(second.closed)

    def test_jobs_functions_use_the_new_pool(self):
        self.jobs.note_pool_timeout()
        self.jobs.note_pool_timeout()
        new = self.jobs.pool
        self.first.executed.clear()
        new.executed.clear()
        try:
            self.jobs.get_job("job_0123456789ab")
        except KeyError:                          # the fake row is not a job row; fine here
            pass
        self.assertTrue(new.executed, "get_job did not use the replacement pool")
        self.assertEqual(self.first.executed, [])

    def test_replacement_failure_keeps_old_pool(self):
        self.jobs.note_pool_timeout()
        with mock.patch.object(self.jobs, "_new_pool", side_effect=RuntimeError("boom")):
            out = self.jobs.note_pool_timeout()
        self.assertFalse(out["pool_replaced"])
        self.assertIs(self.jobs.pool, self.first)
        self.assertFalse(self.first.closed)

    def test_thread_safe_single_replacement(self):
        import threading
        self.jobs.note_pool_timeout()
        made = []
        real = self.jobs._new_pool
        with mock.patch.object(self.jobs, "_new_pool", lambda: made.append(1) or real()):
            ts = [threading.Thread(target=self.jobs.note_pool_timeout) for _ in range(20)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len(made), 1)

    # --- db_ping (/health) --------------------------------------------------

    def test_ping_ok(self):
        self.assertTrue(self.jobs.db_ping())
        self.assertIn("SELECT 1", self.first.executed)
        self.assertEqual(self.first.conn_timeouts[-1], self.jobs._HEALTH_CONN_TIMEOUT_S)

    def test_ping_pool_timeout_counts_toward_repair(self):
        self.first.conn_error = psycopg_pool.PoolTimeout("couldn't get a connection after 3.00 sec")
        self.assertFalse(self.jobs.db_ping())
        self.assertIs(self.jobs.pool, self.first)
        self.assertFalse(self.jobs.db_ping())
        self.assertIsNot(self.jobs.pool, self.first)      # second failure repaired it
        self.jobs.pool.conn_error = None
        self.assertTrue(self.jobs.db_ping())

    def test_ping_other_error_is_not_a_pool_timeout(self):
        self.first.conn_error = pg_errors.OperationalError("server at db.internal failed")
        with mock.patch.object(self.jobs, "note_pool_timeout") as note:
            self.assertFalse(self.jobs.db_ping())
            self.assertFalse(self.jobs.db_ping())
        note.assert_not_called()

    def test_ping_hang_is_bounded_and_counts(self):
        import time as _t
        self.first.conn_delay = 1.0
        before = len(self.first.conn_timeouts)    # bootstrap's own checkout
        with mock.patch.object(self.jobs, "_HEALTH_DEADLINE_S", 0.2), \
                mock.patch.object(self.jobs, "note_pool_timeout") as note:
            t0 = _t.perf_counter()
            self.assertFalse(self.jobs.db_ping())
            self.assertLess(_t.perf_counter() - t0, 0.6)
            self.assertFalse(self.jobs.db_ping())        # waits on the SAME hung ping
        self.assertEqual(note.call_count, 2)
        self.assertEqual(len(self.first.conn_timeouts) - before, 1, "a second ping thread was started")
        self.jobs._ping[0].join(2)

    def test_stale_hung_ping_neither_blocks_nor_repairs_the_new_pool(self):
        """F-83 (backtester repro): a ping hung on the replaced pool must not keep
        /health at 503, nor replace the healthy pool again after the cooldown."""
        self.first.conn_delay = 1.0                       # old pool: checkout hangs
        with mock.patch.object(self.jobs, "_HEALTH_DEADLINE_S", 0.1):
            self.assertFalse(self.jobs.db_ping())
            self.assertFalse(self.jobs.db_ping())         # 2nd timeout -> replaced
            new = self.jobs.pool
            self.assertIsNot(new, self.first)
            stale = self.jobs._ping[0]
            self.assertTrue(stale.is_alive(), "test needs the old ping still hung")
            for _ in range(3):                            # health recovers on the new pool
                self.assertTrue(self.jobs.db_ping())
            self.assertIn("SELECT 1", new.executed)
            self.assertTrue(stale.is_alive())
            # The stale ping's eventual timeout must not count for the new pool.
            self.clock[0] += self.jobs._REPAIR_COOLDOWN_S + 1
            out = self.jobs.note_pool_timeout(seen_pool=self.first)
            self.assertFalse(out["pool_replaced"])
            out = self.jobs.note_pool_timeout(seen_pool=self.first)
            self.assertFalse(out["pool_replaced"])
            self.assertTrue(self.jobs.db_ping())          # after cooldown: still healthy
        self.assertIs(self.jobs.pool, new, "the healthy pool was replaced again")
        self.assertEqual(self.jobs._timeouts, [])
        stale.join(2)
        self._join_closers()


if __name__ == "__main__":
    unittest.main()
