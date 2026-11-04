"""Offline tests for customer_source.py, the read-only /app/customers route,
the probe, integer customer ids + the selected-customer identity through
upload-url/submit, and the mock writer's external-id -> mock-customer links.

No network: every socket connect raises (one test opens a loopback-only
server and allows 127.0.0.1 for that test alone), dotenv is disabled (no
local .env is read), and jobs/queue/storage are stubbed. Run:

    python -m unittest test_customer_source -v
"""

import io
import json
import os
import re
import socket
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

# --- Isolation (must run before any project import) -------------------------

import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False
for _name in list(os.environ):
    if any(w in _name for w in ("CLERK", "ANTHROPIC", "R2_", "DATABASE", "REDIS",
                                "FIELDPULSE", "AWS_", "DEEPGRAM")):
        os.environ.pop(_name)
os.environ.update(
    ANTHROPIC_API_KEY="dummy", DEEPGRAM_API_KEY="dummy", CLERK_SECRET_KEY="sk_test_dummy",
    AWS_EC2_METADATA_DISABLED="true",
)


def _no_network(self, address):
    raise RuntimeError(f"network blocked in tests: {address!r}")


_REAL_CONNECT = socket.socket.connect
socket.socket.connect = _no_network

_created_jobs = []
_links = {}          # (source, external_id) -> mock id; stands in for customer_links
_jobs = types.ModuleType("jobs")
_jobs.create_job = lambda **kw: (_created_jobs.append(kw) or {"id": "job-1", "status": "queued"})
_jobs.get_job = lambda *a: None


def _save_link(source, external_id, mock_id, replace=None):
    key = (source, external_id)
    if key not in _links or _links[key] == replace:
        _links[key] = mock_id
    return _links[key]


_jobs.get_customer_link = lambda source, external_id: _links.get((source, external_id))
_jobs.save_customer_link = _save_link
_jobs.list_recent_jobs = lambda *a: []
_jobs.update_job_status = lambda *a, **k: None
_jobs.JobError = Exception
sys.modules["jobs"] = _jobs
_queue = types.ModuleType("queue_setup")
_queue.enqueue_consultation = lambda job_id: None
sys.modules["queue_setup"] = _queue
_storage = types.ModuleType("storage")


class _StorageError(Exception):
    pass


_storage.StorageError = _StorageError
_storage.audio_exists = lambda key: True
_storage.generate_consultation_key = lambda fn: "consultations/abc-123.webm"
_storage.generate_upload_url = lambda key, ct: "https://r2.invalid/put"
_storage.download_audio = lambda *a, **k: None
sys.modules["storage"] = _storage

import customer_source as cs  # noqa: E402
from customer_source import CustomerSourceError, list_customers  # noqa: E402


class FakeRaw:
    """Stands in for urllib3's response: read1() hands the body out in
    pieces and can run a hook (e.g. advance a fake clock) before each piece."""

    def __init__(self, body, piece=None, on_read=None):
        self._body = body
        self._piece = piece or max(1, len(body))
        self._on_read = on_read
        self.connection = None

    def read1(self, amt=None, decode_content=None):
        if self._on_read and self._body:
            self._on_read()
        out, self._body = self._body[:self._piece], self._body[self._piece:]
        return out


class FakeResp:
    def __init__(self, payload=None, status=200, text=None, bad_json=False,
                 piece=None, on_read=None):
        if text is not None:
            body = text.encode()
        elif bad_json:
            body = b"<html>not json</html>"
        else:
            body = json.dumps(payload).encode()
        self.status_code = status
        self.raw = FakeRaw(body, piece, on_read)
        self.text = body.decode("utf-8", "replace")
        self.headers = {"Content-Type": "application/json"}
        self.closed = False

    def json(self):                       # non-streaming callers (probe, writer)
        return json.loads(self.text)

    def close(self):
        self.closed = True


def as_transport(r):
    """What the real _http_get returns for a FakeResp: (status, body bytes).
    Runs the FakeResp's on_read hook once (e.g. to advance a fake clock)."""
    if r.raw._on_read:
        r.raw._on_read()
    return r.status_code, r.raw._body


def real(items, **meta):
    """A real-FieldPulse-style page: {"error": false, "response": [...]}."""
    return {"error": False, "response": items, **meta}


def mock_page(items, page, total_pages):
    return {"data": items, "meta": {"current_page": page, "per_page": 200,
                                    "total": None, "total_pages": total_pages}}


class Base(unittest.TestCase):
    _VARS = ("FIELDPULSE_READ_BASE_URL", "FIELDPULSE_READ_API_KEY",
             "FIELDPULSE_BASE_URL", "FIELDPULSE_API_KEY")

    def setUp(self):
        for k in self._VARS:
            os.environ.pop(k, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in self._VARS])
        self.calls = []

    def serve(self, *responses):
        """Patch the GET function; return queued responses in order."""
        queue = list(responses)

        def fake_get(url, params=None, headers=None, deadline=None):
            self.calls.append({"url": url, "headers": headers, "params": params,
                               "deadline": deadline})
            if not queue:
                raise AssertionError(f"unexpected extra GET (page {params and params.get('page')})")
            r = queue.pop(0)
            if isinstance(r, BaseException):
                raise r
            return as_transport(r)

        p = mock.patch.object(cs, "_http_get", side_effect=fake_get)
        p.start()
        self.addCleanup(p.stop)


class TestConfig(Base):
    def test_falls_back_to_mock_env(self):
        os.environ["FIELDPULSE_BASE_URL"] = "http://mock.local/"
        os.environ["FIELDPULSE_API_KEY"] = "mock-key"
        self.serve(FakeResp(mock_page([], 1, 1)))
        self.assertEqual(list_customers(), [])
        self.assertEqual(self.calls[0]["url"], "http://mock.local/customers")
        self.assertEqual(self.calls[0]["headers"]["x-api-key"], "mock-key")

    def test_defaults_unchanged_when_nothing_set(self):
        self.serve(FakeResp(mock_page([], 1, 1)))
        list_customers()
        self.assertEqual(self.calls[0]["url"], "http://localhost:5000/customers")
        self.assertEqual(self.calls[0]["headers"]["x-api-key"], "dev-test-key-12345")

    def test_read_vars_take_precedence(self):
        os.environ["FIELDPULSE_BASE_URL"] = "http://mock.local"
        os.environ["FIELDPULSE_API_KEY"] = "mock-key"
        os.environ["FIELDPULSE_READ_BASE_URL"] = "https://real.invalid/stage/"
        os.environ["FIELDPULSE_READ_API_KEY"] = "read-key"
        self.serve(FakeResp({"error": False, "response": []}))
        list_customers()
        self.assertEqual(self.calls[0]["url"], "https://real.invalid/stage/customers")
        self.assertEqual(self.calls[0]["headers"]["x-api-key"], "read-key")

    def test_read_url_without_key_is_config_error_and_sends_nothing(self):
        os.environ["FIELDPULSE_READ_BASE_URL"] = "https://real.invalid/stage"
        os.environ["FIELDPULSE_API_KEY"] = "mock-key"
        self.serve()
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertEqual(cm.exception.http_status, 500)
        self.assertEqual(self.calls, [])

    def test_read_key_without_url_is_config_error(self):
        os.environ["FIELDPULSE_READ_API_KEY"] = "read-key"
        self.serve()
        with self.assertRaises(CustomerSourceError):
            list_customers()
        self.assertEqual(self.calls, [])


class TestShapes(Base):
    def test_mock_shape_multi_page(self):
        p1 = [{"id": f"cust_{i}", "created_at": f"2026-01-{i:02d}T00:00:00+00:00"} for i in range(1, 4)]
        p2 = [{"id": "cust_9", "created_at": "2026-02-01T00:00:00+00:00"}]
        self.serve(FakeResp(mock_page(p1, 1, 2)), FakeResp(mock_page(p2, 2, 2)))
        out = list_customers()
        self.assertEqual([c["id"] for c in out], ["cust_9", "cust_3", "cust_2", "cust_1"])
        self.assertEqual([c["params"]["page"] for c in self.calls], [1, 2])

    def test_real_wrapper_list(self):
        self.serve(FakeResp({"error": False, "response": [
            {"id": 18809123, "display_name": "Doe, Jane", "phone_e164": "+15555550100",
             "created_at": "2026-03-01 10:00:00"},
        ]}), FakeResp(real([])))
        out = list_customers()
        self.assertEqual(len(self.calls), 2)          # no metadata -> fetch until empty
        self.assertEqual(len({c["deadline"] for c in self.calls}), 1)   # one shared deadline
        self.assertEqual(out[0]["id"], 18809123)
        self.assertIsInstance(out[0]["id"], int)
        self.assertEqual(out[0]["phone"], "+15555550100")
        self.assertEqual(out[0]["created_at"], "2026-03-01T10:00:00Z")
        self.assertEqual(set(out[0]), {"id", "display_name", "first_name", "last_name",
                                       "phone", "email", "created_at"})

    def test_real_wrapper_dict_paginated_laravel_style(self):
        r1 = {"error": False, "response": {"data": [{"id": 1, "created_at": "2026-01-01 00:00:00"}],
                                           "current_page": 1, "last_page": 2}}
        r2 = {"error": False, "response": {"data": [{"id": 2, "created_at": "2026-01-02 00:00:00"}],
                                           "current_page": 2, "last_page": 2}}
        self.serve(FakeResp(r1), FakeResp(r2))
        self.assertEqual([c["id"] for c in list_customers()], [2, 1])

    def test_real_wrapper_total_count_paging(self):
        r1 = {"error": False, "response": {"customers": [{"id": 1}, {"id": 2}]}, "total": 3}
        r2 = {"error": False, "response": {"customers": [{"id": 3}]}, "total": 3}
        self.serve(FakeResp(r1), FakeResp(r2))
        self.assertEqual(sorted(c["id"] for c in list_customers()), [1, 2, 3])
        self.assertEqual(len(self.calls), 2)

    def test_next_url_null_stops(self):
        self.serve(FakeResp({"error": False, "response": {"data": [{"id": 1}], "next_page_url": None}}))
        list_customers()
        self.assertEqual(len(self.calls), 1)

    def test_full_page_without_paging_info_fetches_next(self):
        full = [{"id": i} for i in range(cs._PER_PAGE)]
        self.serve(FakeResp({"error": False, "response": full}),
                   FakeResp({"error": False, "response": [{"id": 99999}]}),
                   FakeResp(real([])))
        self.assertEqual(len(list_customers()), cs._PER_PAGE + 1)

    def test_malformed_shapes_raise_not_empty(self):
        bad = [
            {},
            {"unexpected": "schema"},
            {"customers": None},
            {"data": None},
            {"error": False, "response": None},
            {"error": False, "response": {"message": "ok"}},
            {"error": True, "response": []},
            {"error": "Something broke"},
            "a string",
            None,
            {"data": ["not-an-object"]},
            {"data": [{"name": "no id"}]},
            {"data": [{"id": True}]},
            {"data": [{"id": ""}]},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                self.calls.clear()
                self.serve(FakeResp(payload))
                with self.assertRaises(CustomerSourceError) as cm:
                    list_customers()
                self.assertEqual(cm.exception.http_status, 502)
                mock.patch.stopall()

    def test_error_message_has_keys_not_values(self):
        # Round 3 (F-21): only allowlisted key names are shown; others counted.
        self.serve(FakeResp({"secret_customer_name": "Jane Doe PII", "meta": "x"}))
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertIn("meta", str(cm.exception))
        self.assertIn("(+1 unlisted key)", str(cm.exception))
        self.assertNotIn("secret_customer_name", str(cm.exception))
        self.assertNotIn("Jane Doe PII", str(cm.exception))

    def test_non_200_and_non_json(self):
        self.serve(FakeResp({"message": "Forbidden"}, status=403, text='{"message":"Forbidden"}'))
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertIn("HTTP 403", str(cm.exception))
        self.assertNotIn("Forbidden", str(cm.exception))
        mock.patch.stopall()
        self.serve(FakeResp(bad_json=True))
        with self.assertRaises(CustomerSourceError):
            list_customers()

    def test_empty_valid_list_is_empty(self):
        self.serve(FakeResp({"error": False, "response": []}))
        self.assertEqual(list_customers(), [])


class TestPagingSafety(Base):
    def test_pagination_not_advancing_raises(self):
        page = {"error": False, "response": {"data": [{"id": 1}, {"id": 2}], "last_page": 5}}
        self.serve(FakeResp(page), FakeResp(page))
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertIn("did not advance", str(cm.exception))

    def test_page_cap_raises_instead_of_partial(self):
        pages = [FakeResp(mock_page([{"id": f"c{i}"}], i, 10_000)) for i in range(1, cs._MAX_PAGES + 1)]
        self.serve(*pages)
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertIn("Refusing to return a partial list", str(cm.exception))
        self.assertEqual(len(self.calls), cs._MAX_PAGES)

    def test_empty_page_contradicting_metadata_is_error(self):
        # F-13: was test_empty_page_stops_even_if_more_claimed (returned a partial list).
        cases = [
            (mock_page([{"id": "a"}], 1, 3), mock_page([], 2, 3)),                       # total_pages
            (real([{"id": 1}], total=3, last_page=3), real([], total=3, last_page=3)),  # total/last_page
            (real([{"id": 1}], has_more=True), real([], has_more=True)),                # has_more
            (real([{"id": 1}], total="3"), real([], total="3")),                        # numeric string
        ]
        for p1, p2 in cases:
            with self.subTest(p1=p1):
                mock.patch.stopall()
                self.serve(FakeResp(p1), FakeResp(p2))
                with self.assertRaises(CustomerSourceError) as cm:
                    list_customers()
                self.assertIn("partial list", str(cm.exception))

    def test_empty_page_with_consistent_metadata_is_terminal(self):
        # p1 says more (has_more), p2 is empty and its metadata agrees we're done.
        self.serve(FakeResp(real([{"id": 1}], total=1, has_more=True)),
                   FakeResp(real([], total=1, has_more=False)))
        self.assertEqual([c["id"] for c in list_customers()], [1])
        self.assertEqual(len(self.calls), 2)
        mock.patch.stopall()
        self.calls.clear()
        self.serve(FakeResp(mock_page([{"id": "a"}], 1, 1)))      # mock: one GET, as before
        self.assertEqual(len(list_customers()), 1)
        self.assertEqual(len(self.calls), 1)

    def test_duplicate_ids_across_pages_deduped(self):
        self.serve(FakeResp(mock_page([{"id": "a"}, {"id": "b"}], 1, 2)),
                   FakeResp(mock_page([{"id": "b"}, {"id": "c"}], 2, 2)))
        self.assertEqual(sorted(c["id"] for c in list_customers()), ["a", "b", "c"])


class TestTimeouts(Base):
    def test_request_gets_the_shared_deadline(self):
        with mock.patch.object(cs.time, "monotonic", return_value=100.0):
            self.serve(FakeResp(mock_page([], 1, 1)))
            list_customers()
        self.assertEqual(self.calls[0]["deadline"], 100.0 + cs._TOTAL_BUDGET_S)
        self.assertLess(cs._TOTAL_BUDGET_S, 30)

    def test_timeout_retried_once_then_succeeds(self):
        self.serve(socket.timeout("slow"), FakeResp(mock_page([{"id": "a"}], 1, 1)))
        self.assertEqual(len(list_customers()), 1)
        self.assertEqual(len(self.calls), 2)

    def test_two_timeouts_give_504(self):
        self.serve(socket.timeout("slow"), socket.timeout("slow"))
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertEqual(cm.exception.http_status, 504)

    def test_connection_error_twice_is_unreachable(self):
        self.serve(ConnectionRefusedError("down"), ConnectionRefusedError("down"))
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertEqual(cm.exception.status, "fieldpulse_unreachable")

    def test_total_deadline_across_pages(self):
        clock = [0.0]

        def fake_mono():
            return clock[0]

        def slow_page(url, params=None, headers=None, deadline=None):
            self.calls.append(deadline)
            clock[0] += 6.0  # each page "takes" 6s
            return as_transport(FakeResp(mock_page([{"id": f"p{params['page']}"}],
                                                   params["page"], 100)))

        with mock.patch.object(cs.time, "monotonic", side_effect=fake_mono), \
                mock.patch.object(cs, "_http_get", side_effect=slow_page):
            with self.assertRaises(CustomerSourceError) as cm:
                list_customers()
        self.assertEqual(cm.exception.http_status, 504)
        # Every page shares the one deadline set at the start of the listing.
        self.assertLessEqual(len(self.calls), 4)
        self.assertEqual(set(self.calls), {cs._TOTAL_BUDGET_S})


class TestSorting(Base):
    def test_newest_first_missing_last_stable(self):
        items = [
            {"id": 1, "created_at": "2025-01-01T00:00:00Z"},
            {"id": 2},                                         # no date
            {"id": 3, "created_at": "2026-06-01 12:00:00"},
            {"id": 4, "created_at": "garbage"},               # unparseable -> last
            {"id": 5, "created_at": "2025-01-01T00:00:00Z"},  # tie with 1 -> keeps order
            {"id": 6, "date_created": 1767225600},             # epoch seconds 2026-01-01
            {"id": 7, "created": 1767312000000},               # epoch ms 2026-01-02
            {"id": 8, "created_at": {"date": "2026-07-01 00:00:00.000000", "timezone": "UTC"}},
            {"id": 9, "created_at": "03/15/2026"},
            {"id": 10, "created_at": None, "createdAt": "2026-05-01T00:00:00.000Z"},
        ]
        self.serve(FakeResp({"error": False, "response": items}), FakeResp(real([])))
        out = list_customers()
        self.assertEqual([c["id"] for c in out], [8, 3, 10, 9, 7, 6, 1, 5, 2, 4])
        self.assertIsNone(out[-1]["created_at"])
        self.assertIsNone(out[-2]["created_at"])
        self.assertEqual(out[3]["created_at"], "2026-03-15")       # date-only keeps precision
        self.assertTrue(all(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", c["created_at"])
                            for i, c in enumerate(out[:-2]) if i != 3))

    def test_offset_timezone_normalised(self):
        self.serve(FakeResp(mock_page([
            {"id": "a", "created_at": "2026-01-01T23:00:00-06:00"},  # 05:00Z next day
            {"id": "b", "created_at": "2026-01-02T01:00:00Z"},
        ], 1, 1)))
        out = list_customers()
        self.assertEqual([c["id"] for c in out], ["a", "b"])
        self.assertEqual(out[0]["created_at"], "2026-01-02T05:00:00Z")


class TestGetOnly(unittest.TestCase):
    def test_module_has_no_write_capable_http(self):
        src = (_HERE / "customer_source.py").read_text()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        # Exactly one request is ever issued, and its verb is the literal "GET".
        self.assertEqual(re.findall(r"\.request\(([^,]*),", code), ['"GET"'])
        self.assertNotRegex(code, r"\b(post|put|patch|delete|putrequest|urlopen)\s*\(")
        self.assertNotRegex(code, r"\b(POST|PUT|PATCH|DELETE)\b")
        self.assertNotRegex(code, r"^\s*(import|from) (requests|urllib3)\b")
        self.assertNotRegex(code, r"Session\b")

    def test_probe_is_get_only_and_never_prints_values(self):
        import requests
        import probe_fieldpulse_customers as probe
        src = (_HERE / "probe_fieldpulse_customers.py").read_text()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        self.assertNotRegex(code, r"\b(post|put|patch|delete|request)\s*\(")
        self.assertIs(probe._http_get, requests.get)

        pii = {"id": 777123, "display_name": "Zzyzx, Quentin", "email": "quentin@pii.example",
               "phone": "+16185550199", "notes": "secret note", "created_at": "2026-04-05 06:07:08"}
        payload = {"error": False, "response": {"data": [pii], "total": 4321, "last_page": 9,
                                                 "next_page_url": "https://x.invalid/customers?page=2"}}
        os.environ["FIELDPULSE_READ_BASE_URL"] = "https://real.invalid/stage"
        os.environ["FIELDPULSE_READ_API_KEY"] = "SUPER-SECRET-READ-KEY"
        try:
            resp = FakeResp(payload)
            resp.headers = {"Content-Type": "application/json"}
            with mock.patch.object(probe, "_http_get", return_value=resp) as g, \
                    mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
                rc = probe.main()
            text = out.getvalue()
        finally:
            os.environ.pop("FIELDPULSE_READ_BASE_URL")
            os.environ.pop("FIELDPULSE_READ_API_KEY")
        self.assertEqual(rc, 0)
        self.assertEqual(g.call_count, 1)
        for leaked in ("777123", "Zzyzx", "Quentin", "quentin@pii", "6185550199", "secret note",
                       "2026-04-05", "SUPER-SECRET-READ-KEY", "x.invalid"):
            self.assertNotIn(leaked, text)
        for expected in ("display_name", "created_at", "response.data", "length 1",
                         "9999-99-99 99:99:99", "last_page: 9", "total: 4321"):
            self.assertIn(expected, text)

    def test_probe_refuses_without_read_vars(self):
        import probe_fieldpulse_customers as probe
        with mock.patch.object(probe, "_http_get") as g, \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO):
            self.assertEqual(probe.main(), 2)
        g.assert_not_called()

    def test_writers_never_read_read_vars(self):
        for f in ("consultation_writer.py", "fieldpulse_writer.py", "worker.py", "pipeline.py"):
            self.assertNotIn("FIELDPULSE_READ", (_HERE / f).read_text(), f)


class TestOrchestratorRoutes(Base):
    @classmethod
    def setUpClass(cls):
        import orchestrator
        cls.orch = orchestrator
        orchestrator.app.before_request_funcs[None] = [
            f for f in orchestrator.app.before_request_funcs.get(None, [])
            if f.__name__ != "_require_clerk_session"
        ]
        cls.client = orchestrator.app.test_client()

    def test_get_customers_shape_with_created_at(self):
        self.serve(FakeResp({"error": False, "response": [
            {"id": 5, "display_name": "Old", "created_at": "2024-01-01 00:00:00"},
            {"id": 6, "display_name": "New", "created_at": "2026-01-01 00:00:00"},
        ]}), FakeResp(real([])))
        r = self.client.get("/app/customers")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(list(body), ["customers"])
        self.assertEqual([c["id"] for c in body["customers"]], [6, 5])
        self.assertEqual(body["customers"][0]["created_at"], "2026-01-01T00:00:00Z")

    def test_get_customers_malformed_is_502(self):
        self.serve(FakeResp({"unexpected": "schema"}))
        r = self.client.get("/app/customers")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json()["status"], "fieldpulse_error")

    def test_post_customers_removed(self):
        self.serve()
        r = self.client.post("/app/customers", json={"first_name": "A", "last_name": "B"})
        self.assertEqual(r.status_code, 405)
        self.assertEqual(self.calls, [])

    def test_integer_customer_id_through_upload_url_and_submit(self):
        _created_jobs.clear()
        r = self.client.post("/consultation/upload-url", json={
            "filename": "a.webm", "content_type": "audio/webm", "customer_id": 18809123})
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.client.post("/consultation/submit", json={
            "audio_key": "consultations/abc-123.webm", "customer_id": 18809123})
        self.assertEqual(r.status_code, 202, r.get_json())
        self.assertEqual(_created_jobs[-1]["customer_id"], "18809123")

    def test_customer_id_variants(self):
        ok = {"cust_ab12": "cust_ab12", "  cust_ab12 ": "cust_ab12", "42": "42", 42: "42",
              None: None, "": None}
        for raw, want in ok.items():
            with self.subTest(raw=raw):
                _created_jobs.clear()
                r = self.client.post("/consultation/submit", json={
                    "audio_key": "consultations/abc-123.webm", "customer_id": raw})
                self.assertEqual(r.status_code, 202, r.get_json())
                self.assertEqual(_created_jobs[-1]["customer_id"], want)
        for raw in (True, 4.5, {"id": 1}, [1], "abc", "cust_", "-5", "1" * 21):
            with self.subTest(raw=raw):
                r = self.client.post("/consultation/submit", json={
                    "audio_key": "consultations/abc-123.webm", "customer_id": raw})
                self.assertEqual(r.status_code, 400, r.get_json())
                r = self.client.post("/consultation/upload-url", json={
                    "filename": "a.webm", "content_type": "audio/webm", "customer_id": raw})
                self.assertEqual(r.status_code, 400, r.get_json())


class FakeMock:
    """In-memory mock FieldPulse for consultation_writer: customers, jobs,
    notes. Records every request so tests can prove what was touched."""

    def __init__(self, cw):
        self.cw = cw
        self.customers = {}
        self.jobs = {}
        self.seen = []
        self.n = 0

    def add_customer(self, cid, **fields):
        self.customers[cid] = {"id": cid, **fields}

    def add_job(self, jid, customer_id):
        self.jobs[jid] = {"id": jid, "customer_id": customer_id, "notes": [], "patched": []}

    def __call__(self, method, url, json=None, params=None, headers=None, timeout=None):
        assert url.startswith(self.cw.BASE_URL), url
        path = url[len(self.cw.BASE_URL):]
        self.seen.append((method, path))
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/customers":
            return FakeResp(mock_page(list(self.customers.values()), 1, 1))
        if method == "GET" and parts[0] == "customers" and len(parts) == 2:
            c = self.customers.get(parts[1])
            return FakeResp({"data": c}) if c else FakeResp({"error": "Customer not found"}, status=404)
        if method == "POST" and path == "/customers":
            self.n += 1
            cid = f"cust_new{self.n}"
            self.customers[cid] = {"id": cid, **json}
            return FakeResp({"data": self.customers[cid]}, status=201)
        if method == "GET" and path == "/jobs":
            return FakeResp({"data": [j for j in self.jobs.values()
                                      if j["customer_id"] == params["customer_id"]]})
        if method == "POST" and path == "/jobs":
            self.n += 1
            jid = f"job_new{self.n}"
            self.add_job(jid, json["customer_id"])
            return FakeResp({"data": self.jobs[jid]}, status=201)
        if method == "PATCH" and parts[0] == "jobs":
            self.jobs[parts[1]]["patched"].append(json)
            return FakeResp({"data": self.jobs[parts[1]]})
        if method == "POST" and parts[0] == "jobs" and parts[2] == "notes":
            self.jobs[parts[1]]["notes"].append(json["body"])
            return FakeResp({"data": {"id": f"note_{len(self.jobs[parts[1]]['notes'])}"}}, status=201)
        raise AssertionError(f"unexpected {method} {path}")


class TestWriterCustomerLinks(unittest.TestCase):
    """F-15: a real (external) customer id never attaches to a mock customer
    by name; it reuses its link or creates a new linked mock customer."""

    def setUp(self):
        import consultation_writer as cw
        self.cw = cw
        _links.clear()
        self.fp = FakeMock(cw)
        # An unrelated existing mock "Jane Doe" with a job: must never be touched.
        self.fp.add_customer("cust_old", display_name="Doe, Jane", first_name="Jane",
                             last_name="Doe", phone="+16180000000")
        self.fp.add_job("job_unrelated", "cust_old")
        p = mock.patch.object(cw.requests, "request", side_effect=self.fp)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def jane(cid, phone):
        return {"id": cid, "display_name": "Doe, Jane", "first_name": "Jane",
                "last_name": "Doe", "phone": phone, "email": None}

    EXTRACTION = {"client": {"name": "Jane Doe", "phone": None, "email": None}}

    def write(self, cid, identity=None):
        return self.cw.write_consultation_to_fieldpulse(
            self.EXTRACTION, customer_id=cid, customer=identity)

    def test_two_real_customers_with_same_name_get_separate_mock_customers(self):
        a = self.write(111, self.jane("111", "+16185550001"))
        b = self.write("222", self.jane("222", "+16185550002"))
        self.assertEqual(a["match_path"], "created_new_for_selected")
        self.assertEqual(b["match_path"], "created_new_for_selected")
        self.assertNotEqual(a["customer_id"], b["customer_id"])
        self.assertNotIn("cust_old", (a["customer_id"], b["customer_id"]))
        self.assertEqual(self.fp.jobs["job_unrelated"]["notes"], [])
        self.assertEqual(self.fp.jobs["job_unrelated"]["patched"], [])
        self.assertNotIn(("GET", "/customers"), self.fp.seen)          # no name/phone search
        self.assertEqual(_links, {("fieldpulse", "111"): a["customer_id"],
                                  ("fieldpulse", "222"): b["customer_id"]})
        made = self.fp.customers[a["customer_id"]]
        self.assertEqual((made["display_name"], made["phone"]), ("Doe, Jane", "+16185550001"))

    def test_repeat_consultation_reuses_link_without_duplicating(self):
        first = self.write("111", self.jane("111", "+16185550001"))
        posts_before = self.fp.seen.count(("POST", "/customers"))
        again = self.write("111", self.jane("111", "+16185550001"))
        self.assertEqual(again["match_path"], "matched_by_link")
        self.assertEqual(again["customer_id"], first["customer_id"])
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertEqual(self.fp.seen.count(("POST", "/customers")), posts_before)
        self.assertEqual(len(self.fp.jobs[first["job_id"]]["notes"]), 2)

    def test_mock_id_still_matches_directly(self):
        out = self.write("cust_old", None)
        self.assertEqual(out["match_path"], "matched_by_id")
        self.assertEqual(out["customer_id"], "cust_old")
        self.assertEqual(out["job_id"], "job_unrelated")
        self.assertEqual(_links, {})

    def test_missing_mock_id_keeps_old_search_fallback(self):
        out = self.write("cust_missing", None)
        self.assertEqual(out["match_path"], "matched_by_name")    # unchanged legacy behaviour
        self.assertIn(("GET", "/customers"), self.fp.seen)
        self.assertEqual(_links, {})

    def test_no_identity_still_never_matches_by_name(self):
        out = self.write("333", None)                              # e.g. the paste path
        self.assertEqual(out["match_path"], "created_new_for_selected")
        self.assertNotEqual(out["customer_id"], "cust_old")
        self.assertTrue(any("details were not sent" in f for f in out["flags"]))

    def test_stale_link_is_replaced(self):
        _links[("fieldpulse", "111")] = "cust_gone"
        out = self.write("111", self.jane("111", "+1"))
        self.assertEqual(out["match_path"], "created_new_for_selected")
        self.assertEqual(_links[("fieldpulse", "111")], out["customer_id"])

    def test_concurrent_link_winner_is_used(self):
        self.fp.add_customer("cust_winner", display_name="Doe, Jane")
        real_save = _jobs.save_customer_link

        def racing_save(source, external_id, mock_id, replace=None):
            _links[(source, external_id)] = "cust_winner"            # other job got there first
            return real_save(source, external_id, mock_id, replace)

        with mock.patch.object(_jobs, "save_customer_link", racing_save):
            out = self.write("111", self.jane("111", "+1"))
        self.assertEqual(out["customer_id"], "cust_winner")
        self.assertEqual(out["match_path"], "matched_by_link")

    def test_writer_only_talks_to_mock_base(self):
        self.write("111", self.jane("111", "+1"))
        self.assertTrue(self.cw.BASE_URL.startswith("http://localhost:5000"))
        self.assertTrue(all(p.startswith("/") for _, p in self.fp.seen))


class TestWorkerPassesIdentity(unittest.TestCase):
    def test_worker_hands_job_customer_to_pipeline(self):
        import worker
        identity = {"id": "111", "display_name": "Doe, Jane"}
        job = {"id": "job-9", "audio_key": "consultations/a-1.webm", "customer_id": "111",
               "customer": identity, "source": "3day-app"}
        seen = {}

        def fake_run(path, customer_id=None, source=None, customer=None):
            seen.update(customer_id=customer_id, customer=customer)
            return {"extraction": {}, "fieldpulse": {}, "transcript": "x"}

        with mock.patch.object(worker, "get_job", return_value=job), \
                mock.patch.object(worker, "update_job_status"), \
                mock.patch.object(worker, "download_audio"), \
                mock.patch.object(worker, "run_from_audio", side_effect=fake_run):
            worker.process_consultation_job("job-9")
        self.assertEqual(seen, {"customer_id": "111", "customer": identity})


class TestRound2Pagination(Base):
    """F-12 / F-13 / F-20."""

    def test_api_ignoring_limit_with_no_metadata_fetches_until_empty(self):
        p1 = [{"id": i} for i in range(1, 21)]                 # API's own 20/page default
        self.serve(FakeResp(real(p1)), FakeResp(real([{"id": 21}])), FakeResp(real([])))
        out = list_customers()
        self.assertEqual(len(out), 21)
        self.assertEqual([c["params"]["page"] for c in self.calls], [1, 2, 3])

    def test_api_ignoring_page_with_no_metadata_stops_on_repeat(self):
        same = [{"id": i} for i in range(1, 21)]
        self.serve(FakeResp(real(same)), FakeResp(real(same)))
        self.assertEqual(len(list_customers()), 20)
        self.assertEqual(len(self.calls), 2)

    def test_numeric_string_metadata_is_honoured(self):
        for meta in ({"last_page": "3"}, {"total": "3"}, {"total_pages": 3.0},
                     {"total_count": " 3 "}):
            with self.subTest(meta=meta):
                mock.patch.stopall()
                self.calls.clear()
                pages = [FakeResp({"error": False, "response": {"data": [{"id": n}], **meta}})
                         for n in (1, 2, 3)]
                self.serve(*pages)
                self.assertEqual(sorted(c["id"] for c in list_customers()), [1, 2, 3])
                self.assertEqual(len(self.calls), 3)

    def test_unknown_metadata_never_terminating_hits_cap_not_partial(self):
        pages = [FakeResp(real([{"id": i}])) for i in range(1, cs._MAX_PAGES + 1)]
        self.serve(*pages)
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertIn("partial list", str(cm.exception))

    def test_reported_page_mismatch_is_error(self):
        self.serve(FakeResp(mock_page([{"id": "a"}], 1, 3)),
                   FakeResp(mock_page([{"id": "b"}], 1, 3)))      # asked for 2, got "1"
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertIn("page 1 when page 2", str(cm.exception))

    def test_repeat_page_while_metadata_says_done_is_error(self):
        p = {"error": False, "response": {"data": [{"id": 1}]}, "total_pages": 2}
        self.serve(FakeResp(p), FakeResp(p))
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertIn("did not advance", str(cm.exception))

    def test_mixed_int_and_string_ids_dedupe_and_keep_outward_type(self):
        self.serve(FakeResp(mock_page([{"id": 42}], 1, 2)), FakeResp(mock_page([{"id": "42"}, {"id": 7}], 2, 2)))
        out = list_customers()
        self.assertEqual([c["id"] for c in out], [42, 7])
        self.assertIsInstance(out[0]["id"], int)

    def test_mixed_type_repeat_is_detected_as_not_advancing(self):
        self.serve(FakeResp(real([{"id": 42}], last_page=5)), FakeResp(real([{"id": "42"}], last_page=5)))
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertIn("did not advance", str(cm.exception))


class TestRound2Deadline(Base):
    """F-14: the 20s budget is wall-clock total, across phases and pages."""

    def _clocked(self, plan):
        """plan: list of (seconds before headers, seconds while reading body,
        payload). Returns a fake GET that advances a fake clock."""
        clock = [0.0]
        queue = list(plan)

        def fake_get(url, params=None, headers=None, deadline=None):
            self.calls.append({"deadline": deadline, "at": clock[0]})
            before, during, payload = queue.pop(0)
            clock[0] += before + during
            return 200, json.dumps(payload).encode()

        return clock, fake_get

    def run_plan(self, plan):
        clock, fake_get = self._clocked(plan)
        with mock.patch.object(cs.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(cs, "_http_get", side_effect=fake_get):
            try:
                return list_customers(), clock[0]
            except CustomerSourceError as exc:
                return exc, clock[0]

    def test_separate_phase_case_from_finding_now_504(self):
        # Page 1: 4.9s connect + 9.9s read = 14.8s. Page 2: 4.9s + 5.1s -> t=24.8.
        out, t = self.run_plan([
            (4.9, 9.9, mock_page([{"id": "a"}], 1, 2)),
            (4.9, 5.1, mock_page([{"id": "b"}], 2, 2)),
        ])
        self.assertIsInstance(out, CustomerSourceError)
        self.assertEqual((out.http_status, out.status), (504, "fieldpulse_timeout"))
        # Page 2 gets the SAME absolute deadline (t=20), not fresh 5s+10s limits.
        # (A fake GET can't block; the real cut-off is proven by the loopback tests.)
        self.assertEqual([c["deadline"] for c in self.calls], [cs._TOTAL_BUDGET_S] * 2)

    def test_single_slow_completed_response_is_504(self):
        out, _ = self.run_plan([(31.0, 0.0, mock_page([{"id": "a"}], 1, 1))])
        self.assertIsInstance(out, CustomerSourceError)
        self.assertEqual(out.http_status, 504)

    def test_body_read_crossing_deadline_is_504(self):
        out, _ = self.run_plan([(1.0, 25.0, mock_page([{"id": "a"}], 1, 1))])
        self.assertIsInstance(out, CustomerSourceError)
        self.assertEqual(out.http_status, 504)

    def test_fast_listing_succeeds(self):
        out, t = self.run_plan([(0.2, 0.1, mock_page([{"id": "a"}], 1, 1))])
        self.assertEqual([c["id"] for c in out], ["a"])



def _loopback_only(sock, address):
    if address[0] not in ("127.0.0.1", "localhost"):
        raise RuntimeError(f"network blocked in tests: {address!r}")
    return _REAL_CONNECT(sock, address)


class TestRound3HardDeadline(Base):
    """F-14 (Round 3): the real transport against a 127.0.0.1-only server.
    Every phase (status line, headers, body in any framing, silence) is cut
    at the shared deadline, measured as elapsed wall-clock time, with the
    retry ENABLED. Trickles send one byte every 40ms (a whole response would
    take ~17s), each gap well under the per-recv silence limit."""

    BUDGET = 0.6
    SLACK = 0.25

    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen = cls.seen = []
        good = json.dumps({"data": [{"id": "a"}], "meta": {"total_pages": 1}}).encode()
        pad = json.dumps({"data": [], "meta": {"total_pages": 1}}).encode() + b" " * 400

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def handle(self):                   # client hanging up after a cut is expected
                try:
                    super().handle()
                except OSError:
                    pass

            def trickle(self, data):
                try:
                    for b in data:
                        self.wfile.write(bytes([b]))
                        self.wfile.flush()
                        time.sleep(0.04)
                except OSError:
                    pass

            def do_GET(self):
                mode = self.path.split("/")[1]
                seen.append((self.command, self.path, self.headers.get("x-api-key"),
                             self.headers.get("Host")))
                if mode == "silent":
                    time.sleep(3)
                    return
                if mode == "headers":           # status line + headers trickled
                    self.close_connection = True
                    self.trickle(b"HTTP/1.1 200 OK\r\n" + b"X-Pad: " + b"p" * 400 + b"\r\n\r\n")
                    return
                if mode == "close":             # HTTP/1.0-style: no length, read to EOF
                    self.close_connection = True
                    self.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                                     b"Connection: close\r\n\r\n")
                    self.trickle(pad)
                    return
                body = good if mode.startswith("ok") else pad
                chunked = "chunked" in mode
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if mode.startswith("ok"):
                    self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
                                     if chunked else body)
                    return
                self.trickle(b"".join(b"1\r\n" + bytes([b]) + b"\r\n" for b in body)
                             if chunked else body)

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def run_mode(self, mode):
        os.environ["FIELDPULSE_BASE_URL"] = f"http://127.0.0.1:{self.srv.server_address[1]}/{mode}"
        os.environ["FIELDPULSE_API_KEY"] = "loopback-key"
        with mock.patch.object(socket.socket, "connect", _loopback_only), \
                mock.patch.multiple(cs, _TOTAL_BUDGET_S=self.BUDGET, _READ_TIMEOUT_S=0.3,
                                    _CONNECT_TIMEOUT_S=0.3, _MIN_ATTEMPT_S=0.05,
                                    _RETRY_MIN_REMAINING_S=0.1):
            t0 = time.monotonic()
            try:
                out = list_customers()
            except CustomerSourceError as exc:
                out = exc
            return out, time.monotonic() - t0

    def test_every_slow_phase_is_cut_at_the_deadline(self):
        for mode in ("headers", "close", "plain", "chunked", "silent"):
            with self.subTest(mode=mode):
                out, elapsed = self.run_mode(mode)
                self.assertIsInstance(out, CustomerSourceError)
                self.assertEqual((out.http_status, out.status), (504, "fieldpulse_timeout"))
                self.assertLess(elapsed, self.BUDGET + self.SLACK, f"{mode}: {elapsed:.3f}s")

    def test_fast_responses_still_work_and_send_a_plain_get(self):
        for mode in ("ok", "okchunked"):
            with self.subTest(mode=mode):
                self.seen.clear()
                out, elapsed = self.run_mode(mode)
                self.assertEqual([c["id"] for c in out], ["a"])
                self.assertLess(elapsed, self.BUDGET)
                self.assertEqual(self.seen, [(
                    "GET", f"/{mode}/customers?page=1&per_page=200&limit=200", "loopback-key",
                    f"127.0.0.1:{self.srv.server_address[1]}")])

    def test_url_userinfo_is_not_sent_and_bad_url_is_config_error(self):
        port = self.srv.server_address[1]
        self.seen.clear()
        os.environ["FIELDPULSE_READ_BASE_URL"] = f"http://user:SECRET@127.0.0.1:{port}/ok"
        os.environ["FIELDPULSE_READ_API_KEY"] = "k"
        with mock.patch.object(socket.socket, "connect", _loopback_only):
            self.assertEqual([c["id"] for c in list_customers()], ["a"])
        self.assertNotIn("SECRET", repr(self.seen))
        os.environ["FIELDPULSE_READ_BASE_URL"] = "ftp://nope"
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertEqual((cm.exception.status, cm.exception.http_status), ("config_error", 500))


class TestRound2ErrorDisclosure(Base):
    """F-16: fixed, status-based messages; no upstream body/URL/credentials."""

    SENT = "SENTINEL-7f3a"

    def assert_clean(self, text):
        for bad in (self.SENT, "user:", "token=", "real.invalid"):
            self.assertNotIn(bad, text)

    def capture(self, fn):
        with self.assertLogs("voice_intake", level="DEBUG") as logs:
            cs.logger.debug("start")
            out = fn()
        return out, "\n".join(logs.output)

    def test_status_bodies_are_never_echoed(self):
        os.environ["FIELDPULSE_READ_BASE_URL"] = f"https://user:{self.SENT}@real.invalid/stage?token={self.SENT}"
        os.environ["FIELDPULSE_READ_API_KEY"] = self.SENT
        cases = [
            (401, f"Invalid API key: {self.SENT}", "rejected the read API key (HTTP 401)"),
            (403, json.dumps({"message": "Missing Authentication Token"}), "path not found"),
            (403, json.dumps({"message": f"Missing Authentication Token {self.SENT}"}), "refused the read API key"),
            (403, f"<html>{self.SENT}</html>", "refused the read API key"),
            (404, self.SENT, "HTTP 404"),
            (429, self.SENT, "HTTP 429"),
            (503, self.SENT, "server error (HTTP 503)"),
            (418, self.SENT, "HTTP 418"),
        ]
        for status, body, want in cases:
            with self.subTest(status=status, body=body[:30]):
                mock.patch.stopall()
                self.serve(FakeResp(status=status, text=body))
                with self.assertRaises(CustomerSourceError) as cm:
                    list_customers()
                self.assertIn(want, str(cm.exception))
                self.assert_clean(str(cm.exception))

    def test_transport_errors_and_logs_never_show_url_or_secret(self):
        os.environ["FIELDPULSE_READ_BASE_URL"] = f"https://user:{self.SENT}@real.invalid/stage?token={self.SENT}"
        os.environ["FIELDPULSE_READ_API_KEY"] = self.SENT
        for exc_cls, status in ((ConnectionResetError, "fieldpulse_unreachable"),
                                (socket.timeout, "fieldpulse_timeout")):
            with self.subTest(exc=exc_cls.__name__):
                mock.patch.stopall()
                msg = f"HTTPSConnectionPool(host='real.invalid') url=/stage?token={self.SENT}"
                self.serve(exc_cls(msg), exc_cls(msg))

                def run():
                    with self.assertRaises(CustomerSourceError) as cm:
                        list_customers()
                    return cm.exception
                err, logs = self.capture(run)
                self.assertEqual(err.status, status)
                self.assert_clean(str(err))
                self.assert_clean(logs)

    def test_route_response_and_log_are_clean(self):
        import orchestrator
        os.environ["FIELDPULSE_READ_BASE_URL"] = f"https://user:{self.SENT}@real.invalid/stage?token={self.SENT}"
        os.environ["FIELDPULSE_READ_API_KEY"] = self.SENT
        self.serve(FakeResp(status=401, text=f"Invalid API key: {self.SENT}"))
        client = TestOrchestratorRoutes.client
        r, logs = self.capture(lambda: client.get("/app/customers"))
        self.assertEqual(r.status_code, 502)
        self.assert_clean(r.get_data(as_text=True))
        self.assert_clean(logs)
        self.assertIn("HTTP 401", r.get_json()["error"])
        self.assertTrue(orchestrator.app)

    def test_shape_errors_sanitise_unusual_keys(self):
        self.serve(FakeResp({f"Jane Doe {self.SENT}": 1, f"JaneDoe_{self.SENT}": 2,
                             "error": True}))
        with self.assertRaises(CustomerSourceError) as cm:
            list_customers()
        self.assertIn("error", str(cm.exception))
        self.assertIn("(+2 unlisted keys)", str(cm.exception))
        self.assertNotIn(self.SENT, str(cm.exception))


class TestRound2Fields(Base):
    """F-17: simplified fields are str or None."""

    def test_bad_field_types_are_normalised(self):
        self.serve(FakeResp(real([
            {"id": 1, "display_name": {"unexpected": "object"}, "first_name": ["a"],
             "last_name": True, "phone": 6185550100, "email": {"x": 1}},
            {"id": 2, "display_name": "  Doe, Jane  ", "phone": None, "phone_e164": "+16185550101",
             "email": 3.5, "first_name": 12.0},
        ], total=2)))
        out = {c["id"]: c for c in list_customers()}
        self.assertEqual(out[1], {"id": 1, "display_name": None, "first_name": None,
                                  "last_name": None, "phone": "6185550100", "email": None,
                                  "created_at": None})
        self.assertEqual(out[2]["display_name"], "Doe, Jane")
        self.assertEqual(out[2]["phone"], "+16185550101")
        self.assertEqual(out[2]["email"], "3.5")
        self.assertEqual(out[2]["first_name"], "12")
        for c in out.values():
            for k, v in c.items():
                if k != "id":
                    self.assertTrue(v is None or isinstance(v, str), (k, v))


class TestRound2Dates(Base):
    """F-18 / F-19."""

    def listing(self, items):
        self.serve(FakeResp(real(items, total=len(items))))
        return list_customers()

    def test_carbon_timezone_is_honoured(self):
        out = self.listing([
            {"id": 1, "created_at": {"date": "2026-11-03 00:30:00.000000", "timezone_type": 3,
                                     "timezone": "America/Chicago"}},     # = 06:30Z
            {"id": 2, "created_at": "2026-11-03T03:00:00Z"},
        ])
        self.assertEqual([c["id"] for c in out], [1, 2])
        self.assertEqual(out[0]["created_at"], "2026-11-03T06:30:00Z")

    def test_carbon_offset_and_unsupported_zone(self):
        out = self.listing([
            {"id": 1, "created_at": {"date": "2026-11-03 10:00:00", "timezone": "+05:00"}},
            {"id": 2, "created_at": {"date": "2026-11-03 10:00:00", "timezone": "Mars/Olympus"}},
            {"id": 3, "created_at": {"date": "2026-11-03 10:00:00", "timezone": "UTC"}},
        ])
        by = {c["id"]: c["created_at"] for c in out}
        self.assertEqual(by[1], "2026-11-03T05:00:00Z")
        self.assertIsNone(by[2])                                   # rejected, not guessed
        self.assertEqual(by[3], "2026-11-03T10:00:00Z")
        self.assertEqual(out[-1]["id"], 2)

    def test_date_only_keeps_calendar_precision(self):
        out = self.listing([{"id": 1, "created_at": "2026-11-03"},
                            {"id": 2, "created_at": "11/03/2026"}])
        self.assertEqual([c["created_at"] for c in out], ["2026-11-03", "2026-11-03"])

    def test_mixed_precision_sorting(self):
        out = self.listing([
            {"id": "evening-before", "created_at": "2026-11-03T03:00:00Z"},   # Nov 2 in Chicago
            {"id": "date-only", "created_at": "2026-11-03"},
            {"id": "next-day", "created_at": "2026-11-04T13:00:00Z"},
            {"id": "day-before", "created_at": "2026-11-02"},
        ])
        self.assertEqual([c["id"] for c in out], ["next-day", "date-only", "evening-before", "day-before"])


class TestRound2Probe(unittest.TestCase):
    """F-21: sentinel tests for the probe's output."""

    SENT = "SENTINEL-9c1d"

    def run_probe(self, resp=None, exc=None, base=None):
        import probe_fieldpulse_customers as probe
        env = {"FIELDPULSE_READ_BASE_URL": base or "https://real.invalid/stage",
               "FIELDPULSE_READ_API_KEY": self.SENT + "-KEY"}
        kw = {"side_effect": exc} if exc else {"return_value": resp}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(probe, "_http_get", **kw), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = probe.main()
        return rc, out.getvalue()

    def test_unicode_date_and_unusual_keys_do_not_leak(self):
        pii = {"id": 1, "created_at": "Ünïcødé Ñåmé 2026-11-03 ٣", f"Jane {self.SENT}": "x",
               "cursor_customer_id": 777123}
        payload = {"error": False, "response": {"data": [pii], "next_cursor_id": 888456,
                                                 "total": "4321", f"page {self.SENT}": 5}}
        resp = FakeResp(payload)
        resp.headers = {"Content-Type": f"application/json; charset=utf-8; x={self.SENT}"}
        rc, text = self.run_probe(resp)
        self.assertEqual(rc, 0)
        for leaked in ("Ü", "Ñ", "å", "٣", "nïc", self.SENT, "777123", "888456", "charset"):
            self.assertNotIn(leaked, text)
        self.assertIn("Media type: application/json", text)
        self.assertIn("aaaaaaa aaaa 9999-99-99 a", text)
        self.assertIn("numeric string 4321", text)
        self.assertIn("unlisted key", text)
        self.assertIn("<FIELDPULSE_READ_BASE_URL>/customers", text)

    def test_misconfigured_url_is_never_printed(self):
        base = f"https://user:{self.SENT}@real.invalid/stage?token={self.SENT}"
        import requests
        rc, text = self.run_probe(exc=requests.ConnectionError(f"failed {base}"), base=base)
        self.assertEqual(rc, 1)
        self.assertNotIn(self.SENT, text)
        self.assertNotIn("real.invalid", text)
        self.assertIn("Request failed: ConnectionError", text)

    def test_odd_media_type_and_error_body(self):
        resp = FakeResp({"message": "Missing Authentication Token", f"x {self.SENT}": 1}, status=403)
        resp.headers = {"Content-Type": f"{self.SENT} <script>"}
        rc, text = self.run_probe(resp)
        self.assertEqual(rc, 1)
        self.assertIn("Media type: other", text)
        self.assertIn("wrong path", text)
        self.assertNotIn(self.SENT, text)


class TestRound2Identity(Base):
    """F-15 route side: identity persisted on the job; paste ids validated."""

    @classmethod
    def setUpClass(cls):
        TestOrchestratorRoutes.setUpClass()
        cls.client = TestOrchestratorRoutes.client

    def submit(self, **body):
        _created_jobs.clear()
        return self.client.post("/consultation/submit", json={
            "audio_key": "consultations/abc-123.webm", **body})

    def test_identity_is_normalised_and_stored(self):
        r = self.submit(customer_id=18809123, customer={
            "id": 18809123, "display_name": " Doe, Jane ", "first_name": "Jane",
            "last_name": {"bad": 1}, "phone": 6185550100, "email": None, "extra": "dropped"})
        self.assertEqual(r.status_code, 202, r.get_json())
        self.assertEqual(_created_jobs[-1]["customer"], {
            "id": "18809123", "display_name": "Doe, Jane", "first_name": "Jane",
            "last_name": None, "phone": "6185550100", "email": None})
        self.assertEqual(_created_jobs[-1]["customer_id"], "18809123")

    def test_no_identity_is_still_accepted(self):
        r = self.submit(customer_id="cust_ab12")
        self.assertEqual(r.status_code, 202)
        self.assertIsNone(_created_jobs[-1]["customer"])

    def test_bad_identity_rejected(self):
        for body in ({"customer_id": 1, "customer": {"id": 2}},
                     {"customer_id": 1, "customer": "Jane"},
                     {"customer_id": None, "customer": {"id": 1}},
                     {"customer_id": 1, "customer": {"id": "abc"}}):
            with self.subTest(body=body):
                r = self.submit(**body)
                self.assertEqual(r.status_code, 400, r.get_json())

    def test_paste_path_rejects_invalid_ids_before_any_work(self):
        import orchestrator
        with mock.patch.object(orchestrator, "_handle_consultation") as h:
            for raw in ("abc", True, {"id": 1}, "1" * 21):
                with self.subTest(raw=raw):
                    r = self.client.post("/process-consultation",
                                         json={"transcript": "hello", "customer_id": raw})
                    self.assertEqual(r.status_code, 400)
            h.assert_not_called()
            h.return_value = ("ok", 200)
            r = self.client.post("/process-consultation", json={"transcript": "hi", "customer_id": 42})
            self.assertEqual(h.call_args[0][1], "42")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRound3BadDates(Base):
    """F-23: malformed offsets / out-of-range conversions -> null date (sorts
    last), never a 500 for the whole list. Through the real Flask route."""

    def test_route_survives_malformed_offsets(self):
        bad = [
            {"date": "2026-11-03 00:30:00", "timezone": "+24:00"},
            {"date": "2026-11-03 00:30:00", "timezone": "-99:00"},
            {"date": "2026-11-03 00:30:00", "timezone": "+05:60"},
            {"date": "0001-01-01 00:00:00", "timezone": "+05:00"},   # shifts before year 1
            "0001-01-01T00:00:00+05:00",
        ]
        items = [{"id": i, "display_name": f"Bad {i}", "created_at": v} for i, v in enumerate(bad)]
        items.append({"id": 99, "display_name": "Good",
                      "created_at": {"date": "2026-11-03 00:30:00", "timezone": "+23:59"}})
        self.serve(FakeResp(real(items, last_page=1)))
        r = TestOrchestratorRoutes.client.get("/app/customers")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        out = r.get_json()["customers"]
        self.assertEqual(out[0]["id"], 99)
        self.assertEqual(out[0]["created_at"], "2026-11-02T00:31:00Z")
        self.assertEqual([c["id"] for c in out[1:]], [0, 1, 2, 3, 4])   # input order kept
        self.assertTrue(all(c["created_at"] is None for c in out[1:]))

    def test_bad_first_field_falls_through_to_next_known_field(self):
        self.serve(FakeResp(real([{"id": 1, "created_at": {"date": "2026-01-01 00:00:00",
                                                           "timezone": "+24:00"},
                                   "createdAt": "2026-02-01T00:00:00Z"}], last_page=1)))
        self.assertEqual(list_customers()[0]["created_at"], "2026-02-01T00:00:00Z")


class TestRound3Allowlists(unittest.TestCase):
    """F-21: identifier-shaped private text in key names / media types is
    never printed; only allowlisted names and media types are."""

    SENT = "jane_doe_SENTINEL9c1d"

    def test_safe_keys_only_names_allowlisted_keys(self):
        out = cs.safe_keys({self.SENT: 1, "display_name": 2, "x" * 5: 3, "total": 4})
        self.assertEqual(out, ["display_name", "total", "(+2 unlisted keys)"])
        self.assertNotIn(self.SENT, repr(out))
        self.assertEqual(cs.safe_keys({}), [])
        self.assertEqual(cs.safe_keys({k: 1 for k in cs.LIST_KEYS}, limit=2)[-1], "(+4 unlisted keys)")

    def test_unrecognized_200_error_hides_identifier_shaped_keys(self):
        with mock.patch.object(cs, "_http_get",
                               return_value=(200, json.dumps({self.SENT: "omitted"}).encode())), \
                mock.patch.dict(os.environ, {}, clear=False):
            with self.assertRaises(CustomerSourceError) as cm:
                list_customers()
        self.assertNotIn(self.SENT, str(cm.exception))
        self.assertIn("(+1 unlisted key)", str(cm.exception))

    def test_probe_hides_identifier_shaped_keys_and_media_types(self):
        import probe_fieldpulse_customers as probe
        pii = {"id": 1, "created_at": "2026-11-03 10:00:00", self.SENT: "omitted",
               f"{self.SENT}_date": "x", "display_name": "Q"}
        payload = {"error": False, f"page_{self.SENT}": 3,
                   "response": {"data": [pii], "total": 1, f"next_{self.SENT}": {"a": 1}}}
        for ctype, want in ((f"application/{self.SENT}", "other"),
                            (f"application/json; x={self.SENT}", "application/json"),
                            ("", "(none)"), (f"{self.SENT}", "other")):
            with self.subTest(ctype=ctype):
                resp = FakeResp(payload)
                resp.headers = {"Content-Type": ctype}
                env = {"FIELDPULSE_READ_BASE_URL": "https://real.invalid/stage",
                       "FIELDPULSE_READ_API_KEY": "k"}
                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(probe, "_http_get", return_value=resp), \
                        mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(probe.main(), 0)
                text = out.getvalue()
                self.assertNotIn("SENTINEL9c1d", text)
                self.assertIn(f"Media type: {want}", text)
                self.assertIn("<unlisted key>", text)
                self.assertIn("display_name", text)
                self.assertIn("created_at", text)
