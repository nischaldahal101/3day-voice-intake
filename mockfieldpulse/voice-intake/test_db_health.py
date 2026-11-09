"""F-82: /health checks the database; a PoolTimeout on any route becomes a fixed
503 JSON body, feeds jobs.note_pool_timeout (pool self-repair) and is reported
with numeric pool counters only. Offline (stubs from test_fieldpulse_sync; the
pool repair itself is tested on the real jobs.py in test_jobs_bootstrap).

Run: /tmp/vi-py314/bin/python -m unittest test_db_health
"""

import unittest
from unittest import mock

import test_fieldpulse_sync  # noqa: F401  (env scrub, network block, stubs)

from psycopg_pool import PoolTimeout  # noqa: E402

import admin_panel  # noqa: E402
import documents_panel  # noqa: E402

DB_HOST = "dpg-planted-host.oregon-postgres.render.com"
STATS = {"pool_size": 5, "pool_available": 0, "requests_waiting": 3}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import orchestrator
        cls.orch = orchestrator
        cls.client = orchestrator.app.test_client()

    def unauthed(self):
        app = self.orch.app
        hooks = [f for f in app.before_request_funcs.get(None, [])
                 if f.__name__ != "_require_clerk_session"]
        return mock.patch.dict(app.before_request_funcs, {None: hooks})

    def authed(self):
        """The real Clerk gate (test_customer_source removes it for its whole run)."""
        return mock.patch.dict(self.orch.app.before_request_funcs,
                               {None: [self.orch._require_clerk_session]})

    def jobs_patch(self, name, **kw):
        return mock.patch.object(self.orch.jobs, name, create=True, **kw)


class TestHealth(Base):
    def test_ok_without_auth(self):
        # real Clerk gate: /health stays public
        with self.authed(), self.jobs_patch("db_ping", return_value=True) as ping:
            r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"status": "ok", "service": "voice-intake-orchestrator"})
        ping.assert_called_once_with()

    def test_db_down_is_503_fixed_body_without_auth(self):
        with self.authed(), self.jobs_patch("db_ping", return_value=False):
            r = self.client.get("/health")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json(), {"status": "unavailable",
                                        "service": "voice-intake-orchestrator",
                                        "error": "Database unavailable"})

    def test_other_routes_still_need_auth(self):
        with self.authed():
            r = self.client.get("/documents/customers")
        self.assertEqual(r.status_code, 401)


class TestPoolTimeoutHandler(Base):
    def _hit(self, path, target, name):
        exc = PoolTimeout("couldn't get a connection after 10.00 sec")
        info = {"pool_replaced": False, "stats": dict(STATS)}
        with self.unauthed(), \
                mock.patch.object(target, name, side_effect=exc), \
                self.jobs_patch("note_pool_timeout", return_value=info) as note, \
                mock.patch.object(self.orch, "report") as rep:
            r = self.client.get(path)
        return r, note, rep, exc

    def test_documents_route_503_json_and_noted(self):
        r, note, rep, exc = self._hit("/documents/customers", documents_panel, "list_customers")
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertEqual(body, {"status": "db_unavailable",
                                "error": "The database is not responding. Try again in a minute."})
        note.assert_called_once_with()
        args, kw = rep.call_args
        self.assertIs(args[0], exc)
        self.assertEqual(args[1], "Database pool timeout")
        self.assertEqual(kw, {"route": "/documents/customers", "pool_replaced": False, **STATS})

    def test_admin_route_503_json(self):
        r, note, _, _ = self._hit("/admin/consultations", admin_panel, "list_consultations")
        self.assertEqual(r.status_code, 503)
        self.assertIn("error", r.get_json())
        note.assert_called_once_with()

    def test_job_status_route_503_json(self):
        r, note, _, _ = self._hit("/job-status/job_0123456789ab", self.orch, "get_job")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["status"], "db_unavailable")

    def test_healthy_route_unchanged(self):
        with self.unauthed(), \
                mock.patch.object(documents_panel, "list_customers",
                                  return_value=(200, {"customers": []})), \
                self.jobs_patch("note_pool_timeout") as note:
            r = self.client.get("/documents/customers")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"customers": []})
        note.assert_not_called()

    def test_body_never_contains_exception_text(self):
        exc = PoolTimeout(f"couldn't get a connection to {DB_HOST}")
        with self.unauthed(), \
                mock.patch.object(documents_panel, "list_customers", side_effect=exc), \
                self.jobs_patch("note_pool_timeout",
                                return_value={"pool_replaced": True, "stats": {}}), \
                mock.patch.object(self.orch, "report"):
            r = self.client.get("/documents/customers")
        self.assertEqual(r.status_code, 503)
        self.assertNotIn(DB_HOST, r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
