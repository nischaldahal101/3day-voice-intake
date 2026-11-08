"""Stage P1b: vendor documents in the app. Offline: no network, no Postgres, no
Redis, no R2 (reuses the env scrub, network block and module stubs from
test_fieldpulse_sync; an in-memory FakeDocsDB with jobs.py's vendor_documents
rules). All data is MADE UP. Covers documents_panel.py, the /documents routes,
worker.process_vendor_document (success, retry, timeouts, failures) and the
new storage.py helpers (real module, fake boto client). The real SQL is in
test_vendor_documents_db.py.

Run: /tmp/vi-py314/bin/python -m unittest test_documents
"""

import importlib.util
import io
import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import test_fieldpulse_sync  # noqa: F401  (env scrub, network block, stubs)

import anthropic  # noqa: E402
import httpx  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from rq.timeouts import JobTimeoutException  # noqa: E402

import documents_panel as dp  # noqa: E402
import extractor  # noqa: E402
import observability  # noqa: E402
import vendor_documents as vd  # noqa: E402

_HERE = Path(__file__).resolve().parent
PDF = b"%PDF-1.7\n% made-up test bytes\n%%EOF\n"
KEY = "vendor-docs/0f0e0d0c-0b0a-4908-8706-050403020100.pdf"
CUSTOMER = {"id": "18809123", "display_name": "Pat Testerson", "first_name": "Pat",
            "last_name": "Testerson", "phone": "555-0100", "email": "pat@example.test",
            "company_name": None, "address": "1 Sample Rd"}


def _load_real_storage():
    """storage.py under a private name (other suites stub sys.modules['storage'])."""
    spec = importlib.util.spec_from_file_location("storage_p1b_real", _HERE / "storage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


real_storage = _load_real_storage()
# The stub storage module needs the real key rule + content type (used at call time).
for _a in ("VENDOR_DOC_KEY_RE", "VENDOR_DOC_CONTENT_TYPE", "VENDOR_DOC_PREFIX"):
    if not hasattr(dp.storage, _a):
        setattr(dp.storage, _a, getattr(real_storage, _a))

import worker  # noqa: E402  (after the stubs have every name it imports)


class UniqueViolation(Exception):
    """Same class name as psycopg.errors.UniqueViolation."""


class FakeDocsDB:
    """jobs.py's vendor_documents rules in memory: newest first, optional customer
    filter, delete refused while queued/processing unless stuck, update on a
    missing row -> None, one row per file_key."""

    def __init__(self, docs=()):
        self.docs = {d["id"]: dict(d) for d in docs}
        self.updates = []
        self.n = 0

    def create_vendor_document(self, file_key, filename=None, file_size=None,
                               customer_id=None, customer=None):
        if any(d["file_key"] == file_key for d in self.docs.values()):
            raise UniqueViolation("duplicate key value violates unique constraint")
        self.n += 1
        doc = new_doc(id=f"doc_{self.n:012x}", status="queued", file_key=file_key,
                      filename=filename, file_size=file_size, customer_id=customer_id,
                      customer=customer, result=None,
                      created_at=f"2026-11-08T10:{self.n:02d}:00+00:00")
        self.docs[doc["id"]] = doc
        return dict(doc)

    def get_vendor_document(self, doc_id):
        d = self.docs.get(doc_id)
        return dict(d) if d else None

    def list_vendor_documents(self, customer_id=None, offset=0, limit=50):
        rows = sorted((d for d in self.docs.values()
                       if customer_id is None or d["customer_id"] == customer_id),
                      key=lambda d: d["created_at"], reverse=True)
        out = []
        for d in rows[offset:offset + limit]:
            r = d["result"] if isinstance(d["result"], dict) else {}
            out.append({**{k: d[k] for k in ("id", "status", "customer_id", "customer",
                                             "filename", "error", "created_at",
                                             "updated_at", "stuck")},
                        "vendor": r.get("vendor"), "document_type": r.get("document_type"),
                        "line_count": len(r["lines"]) if isinstance(r.get("lines"), list)
                        else None,
                        "warn_count": sum(1 for c in r.get("checks", [])
                                          if c.get("level") == "warn")
                        if isinstance(r.get("checks"), list) else None})
        return {"items": out, "total": len(rows)}

    def update_vendor_document(self, doc_id, status, result=None, error=None):
        self.updates.append((doc_id, status, error))
        d = self.docs.get(doc_id)
        if d is None:
            return None
        d["status"] = status
        if result is not None:
            d["result"] = result
        if error is not None:
            d["error"] = error
        return dict(d)

    def delete_vendor_document(self, doc_id):
        d = self.docs.get(doc_id)
        if d is None:
            return None
        if d["status"] in ("queued", "processing") and not d.get("stuck"):
            return {"deleted": False, "document": dict(d), "reason": "still being read"}
        del self.docs[doc_id]
        return {"deleted": True, "document": dict(d)}

    def patched(self):
        stack = ExitStack()
        for name in ("create_vendor_document", "get_vendor_document", "list_vendor_documents",
                     "update_vendor_document", "delete_vendor_document"):
            stack.enter_context(mock.patch.object(dp.jobs, name, getattr(self, name)))
            if hasattr(worker, name):
                stack.enter_context(mock.patch.object(worker, name, getattr(self, name)))
        return stack


def new_doc(**kw):
    d = {"id": "doc_0123456789ab", "status": "done", "customer_id": "18809123",
         "customer": dict(CUSTOMER), "filename": "Quote 123.pdf", "file_key": KEY,
         "file_size": len(PDF), "error": None, "stuck": False,
         "created_at": "2026-11-08T09:00:00+00:00", "updated_at": "2026-11-08T09:01:00+00:00",
         "result": {"vendor": "Ferguson", "document_type": "quote", "document_number": "Q-1",
                    "document_date": "11/01/2026", "job_name": "Testerson",
                    "lines": [{"part_number": "K-1", "name": "Widget", "details": None,
                               "notes": None, "quantity": 2, "unit": "EA",
                               "unit_price": 10.0, "line_total": 20.0}],
                    "totals": {"subtotal": 20.0, "tax": None, "freight": None, "total": 20.0},
                    "checks": [{"level": "warn", "code": "job_name_mismatch", "message": "x"},
                               {"level": "ok", "code": "line_math", "message": "y"}]}}
    d.update(kw)
    return d


class RouteBase(unittest.TestCase):
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


# --- Auth --------------------------------------------------------------------------

class TestAuth(RouteBase):
    def test_every_documents_route_requires_a_clerk_session(self):
        app = self.orch.app
        db = FakeDocsDB([new_doc()])
        with mock.patch.dict(app.before_request_funcs,
                             {None: [self.orch._require_clerk_session]}), \
                mock.patch.object(dp, "jobs", side_effect=AssertionError("no auth")), \
                mock.patch.object(dp, "storage", side_effect=AssertionError("no auth")):
            for method, path in (("post", "/documents/upload-url"), ("post", "/documents/submit"),
                                 ("get", "/documents"), ("get", "/documents/doc_0123456789ab"),
                                 ("delete", "/documents/doc_0123456789ab")):
                for headers in ({}, {"Authorization": "Bearer not-a-real-token"}):
                    with self.subTest(path=path, headers=bool(headers)):
                        r = getattr(self.client, method)(path, headers=headers, json={})
                        self.assertEqual(r.status_code, 401)
        self.assertEqual(len(db.docs), 1)
        for p in ("/documents", "/documents/upload-url", "/documents/submit"):
            self.assertNotIn(p, self.orch._PUBLIC_PATHS)

    def test_delete_preflight_allowed(self):
        app = self.orch.app
        with mock.patch.dict(app.before_request_funcs,
                             {None: [self.orch._require_clerk_session]}):
            r = self.client.open("/documents/doc_0123456789ab", method="OPTIONS", headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "authorization"})
        self.assertIn(r.status_code, (200, 204))
        self.assertIn("DELETE", r.headers.get("Access-Control-Allow-Methods", ""))


# --- Upload URL ------------------------------------------------------------------------

class TestUploadUrl(RouteBase):
    def post(self, body):
        with self.unauthed(), mock.patch.object(
                dp.storage, "vendor_document_upload",
                return_value={"key": KEY, "url": "https://r2.invalid/put?X-Amz-Signature=s"}):
            return self.client.post("/documents/upload-url", json=body)

    def test_ok(self):
        r = self.post({"filename": "Ferguson Quote.PDF", "size": 1234})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json(), {"key": KEY, "content_type": "application/pdf",
                                        "url": "https://r2.invalid/put?X-Amz-Signature=s"})

    def test_rejects(self):
        cases = [({"filename": "photo.jpg", "size": 10}, 400),
                 ({"filename": ".pdf", "size": 10}, 400),
                 ({"filename": None, "size": 10}, 400),
                 ({"filename": "a.pdf"}, 400),
                 ({"filename": "a.pdf", "size": "10"}, 400),
                 ({"filename": "a.pdf", "size": True}, 400),
                 ({"filename": "a.pdf", "size": 0}, 400),
                 ({"filename": "a.pdf", "size": 20 * 1024 * 1024 + 1}, 413)]
        for body, code in cases:
            with self.subTest(body=body):
                self.assertEqual(self.post(body).status_code, code)
        self.assertEqual(self.post({"filename": "a.pdf", "size": 20 * 1024 * 1024}).status_code,
                         200)
        with self.unauthed():
            self.assertEqual(self.client.post("/documents/upload-url", data="x").status_code, 400)

    def test_storage_error_is_502_without_detail(self):
        with self.unauthed(), mock.patch.object(
                dp.storage, "vendor_document_upload",
                side_effect=dp.storage.StorageError("R2 is not configured — secret detail")):
            r = self.client.post("/documents/upload-url", json={"filename": "a.pdf", "size": 9})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("secret", r.get_data(as_text=True))


# --- Submit ----------------------------------------------------------------------------

class TestSubmit(RouteBase):
    def setUp(self):
        self.db = FakeDocsDB()
        self.queued = []
        self.size = len(PDF)
        self.removed = []

    def post(self, body, enqueue=None):
        with self.unauthed(), self.db.patched(), \
                mock.patch.object(dp.storage, "object_size", lambda k: self.size), \
                mock.patch.object(dp.storage, "delete_audio", self.removed.append), \
                mock.patch.object(dp.queue_setup, "enqueue_vendor_document",
                                  enqueue or self.queued.append), \
                mock.patch("requests.request", side_effect=AssertionError("no HTTP")), \
                mock.patch("requests.post", side_effect=AssertionError("no HTTP")):
            return self.client.post("/documents/submit", json=body)

    def body(self, **kw):
        b = {"file_key": KEY, "filename": "C:\\Users\\x\\Quote 9.pdf", "customer": dict(CUSTOMER)}
        b.update(kw)
        return b

    def test_happy_path_creates_row_and_enqueues(self):
        r = self.post(self.body())
        self.assertEqual(r.status_code, 202, r.get_json())
        doc_id = r.get_json()["document_id"]
        self.assertEqual(r.get_json()["status"], "queued")
        self.assertEqual(self.queued, [doc_id])
        row = self.db.docs[doc_id]
        self.assertEqual((row["file_key"], row["filename"], row["file_size"], row["customer_id"]),
                         (KEY, "Quote 9.pdf", len(PDF), "18809123"))
        self.assertEqual(row["customer"]["display_name"], "Pat Testerson")
        self.assertEqual(set(row["customer"]), {"id", *self.orch._IDENTITY_FIELDS})

    def test_int_customer_id(self):
        r = self.post(self.body(customer={"id": 18809123, "display_name": "Pat"}))
        self.assertEqual(r.status_code, 202, r.get_json())

    def test_rejects_bad_input_without_side_effects(self):
        cases = [
            self.body(file_key="consultations/0f0e0d0c-0b0a-4908-8706-050403020100.webm"),
            self.body(file_key="vendor-docs/../consultations/x.pdf"),
            self.body(file_key="vendor-docs/0f0e0d0c-0b0a-4908-8706-050403020100.exe"),
            self.body(file_key=None),
            self.body(filename="notes.txt"),
            self.body(filename=5),
            self.body(customer=None),
            self.body(customer="Pat"),
            self.body(customer={"display_name": "Pat"}),
            self.body(customer={"id": "drop table", "display_name": "Pat"}),
        ]
        for b in cases:
            with self.subTest(body=b):
                self.assertEqual(self.post(b).status_code, 400)
        self.assertEqual(self.db.docs, {})
        self.assertEqual(self.queued, [])

    def test_object_missing_empty_or_too_big(self):
        self.size = None
        self.assertEqual(self.post(self.body()).status_code, 404)
        self.size = 0
        self.assertEqual(self.post(self.body()).status_code, 400)
        self.size = 20 * 1024 * 1024 + 1
        self.assertEqual(self.post(self.body()).status_code, 413)
        self.assertEqual(self.removed, [KEY])                 # oversized upload removed
        self.size = 20 * 1024 * 1024
        self.assertEqual(self.post(self.body()).status_code, 202)
        self.assertEqual(len(self.db.docs), 1)

    def test_head_failure_is_502(self):
        def boom(k):
            raise dp.storage.StorageError("Failed to check r2://bucket/key: detail")
        with self.unauthed(), mock.patch.object(dp.storage, "object_size", boom):
            r = self.client.post("/documents/submit", json=self.body())
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("bucket", r.get_data(as_text=True))

    def test_same_upload_twice_is_409(self):
        self.assertEqual(self.post(self.body()).status_code, 202)
        self.assertEqual(self.post(self.body()).status_code, 409)
        self.assertEqual(len(self.db.docs), 1)
        self.assertEqual(len(self.queued), 1)

    def test_queue_down_marks_failed_and_503(self):
        def down(doc_id):
            raise ConnectionError("redis down")
        r = self.post(self.body(), enqueue=down)
        self.assertEqual(r.status_code, 503)
        (row,) = self.db.docs.values()
        self.assertEqual(row["status"], "failed")
        self.assertIn("Could not queue", row["error"])


# --- List + detail ---------------------------------------------------------------------

class TestListAndDetail(RouteBase):
    def setUp(self):
        self.db = FakeDocsDB([
            new_doc(id="doc_000000000001", created_at="2026-11-08T08:00:00+00:00"),
            new_doc(id="doc_000000000002", status="processing", result=None,
                    created_at="2026-11-08T09:00:00+00:00"),
            new_doc(id="doc_000000000003", status="failed", result=None,
                    error="This file is not a PDF.", customer_id="cust_ab12",
                    customer={"id": "cust_ab12", "first_name": "Sam", "last_name": "Example"},
                    created_at="2026-11-08T10:00:00+00:00"),
            new_doc(id="doc_000000000004", result=["garbage"], customer="nope",
                    created_at="2026-11-08T07:00:00+00:00"),
        ])

    def get(self, path):
        with self.unauthed(), self.db.patched():
            return self.client.get(path)

    def test_list_newest_first_compact(self):
        r = self.get("/documents")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual([i["id"] for i in body["items"]],
                         ["doc_000000000003", "doc_000000000002", "doc_000000000001",
                          "doc_000000000004"])
        self.assertEqual(body["total"], 4)
        done = body["items"][2]
        self.assertEqual(done, {"id": "doc_000000000001", "created_at": "2026-11-08T08:00:00+00:00",
                                "status": "done", "customer_id": "18809123",
                                "customer_name": "Pat Testerson", "filename": "Quote 123.pdf",
                                "vendor": "Ferguson", "document_type": "quote", "line_count": 1,
                                "warn_count": 1, "error": None, "stuck": False})
        self.assertEqual(body["items"][0]["customer_name"], "Sam Example")
        self.assertEqual(body["items"][0]["error"], "This file is not a PDF.")
        self.assertIsNone(body["items"][3]["customer_name"])
        self.assertNotIn("lines", json.dumps(body))

    def test_list_filters_and_pages(self):
        body = self.get("/documents?customer_id=cust_ab12").get_json()
        self.assertEqual([i["id"] for i in body["items"]], ["doc_000000000003"])
        body = self.get("/documents?offset=1&limit=2").get_json()
        self.assertEqual([i["id"] for i in body["items"]],
                         ["doc_000000000002", "doc_000000000001"])
        self.assertEqual((body["offset"], body["limit"], body["total"]), (1, 2, 4))
        self.assertEqual(self.get("/documents?limit=500").get_json()["limit"], 100)
        self.assertEqual(self.get("/documents?limit=x").status_code, 400)
        self.assertEqual(self.get("/documents?customer_id=bad%20id").status_code, 400)

    def test_detail(self):
        r = self.get("/documents/doc_000000000001")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["result"]["lines"][0]["name"], "Widget")
        self.assertEqual((d["customer_name"], d["filename"], d["can_delete"]),
                         ("Pat Testerson", "Quote 123.pdf", True))
        self.assertNotIn("file_key", d)
        self.assertNotIn("vendor-docs/", json.dumps(d))
        self.assertFalse(self.get("/documents/doc_000000000002").get_json()["can_delete"])
        self.assertEqual(self.get("/documents/doc_000000000003").get_json()["error"],
                         "This file is not a PDF.")
        self.assertIsNone(self.get("/documents/doc_000000000004").get_json()["result"])
        self.assertEqual(self.get("/documents/doc_nope").status_code, 404)

    def test_detail_is_scrubbed(self):
        self.db.docs["doc_000000000001"]["result"]["notes_url"] = (
            "https://r2.invalid/x?X-Amz-Signature=abc&X-Amz-Credential=def")
        self.db.docs["doc_000000000001"]["result"]["api_key"] = "secret-xyz"
        text = self.get("/documents/doc_000000000001").get_data(as_text=True)
        self.assertNotIn("X-Amz-Signature=abc", text)
        self.assertNotIn("secret-xyz", text)

    def test_stuck_processing_can_be_deleted(self):
        self.db.docs["doc_000000000002"]["stuck"] = True
        self.assertTrue(self.get("/documents/doc_000000000002").get_json()["can_delete"])


# --- Delete ----------------------------------------------------------------------------

class TestDelete(RouteBase):
    def delete(self, db, doc_id, rm=None):
        removed = []

        def default_rm(key):
            removed.append(key)
        with self.unauthed(), db.patched(), \
                mock.patch.object(dp.storage, "delete_audio", rm or default_rm), \
                mock.patch("requests.request", side_effect=AssertionError("no HTTP")):
            return self.client.delete(f"/documents/{doc_id}"), removed

    def test_done_and_failed_deleted_row_then_pdf(self):
        for status in ("done", "failed"):
            with self.subTest(status=status):
                db = FakeDocsDB([new_doc(status=status)])
                with mock.patch.object(dp.queue_setup, "enqueue_vendor_materials") as enq:
                    r, removed = self.delete(db, "doc_0123456789ab")
                self.assertEqual(r.status_code, 200, r.get_json())
                self.assertEqual(r.get_json(), {"deleted": True, "id": "doc_0123456789ab",
                                                "file": "deleted", "materials": "queued"})
                # P2: the customer's combined list is rebuilt without it (worker job).
                enq.assert_called_once_with("18809123", False, CUSTOMER)
                self.assertEqual(removed, [KEY])
                self.assertEqual(db.docs, {})
                self.assertEqual(self.delete(db, "doc_0123456789ab")[0].status_code, 404)

    def test_in_progress_refused_and_pdf_kept(self):
        for status in ("queued", "processing"):
            with self.subTest(status=status):
                db = FakeDocsDB([new_doc(status=status)])
                r, removed = self.delete(db, "doc_0123456789ab")
                self.assertEqual(r.status_code, 409)
                self.assertEqual(removed, [])
                self.assertIn("doc_0123456789ab", db.docs)

    def test_r2_failure_keeps_db_delete(self):
        db = FakeDocsDB([new_doc()])

        def rm(key):
            raise dp.storage.StorageError("Failed to delete r2://b/k: boom")
        with mock.patch.object(dp, "report") as rep:
            r, _ = self.delete(db, "doc_0123456789ab", rm=rm)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["file"], "failed")
        self.assertEqual(db.docs, {})
        self.assertEqual(rep.call_args[0][1], "R2 vendor document delete failed")


# --- Worker job --------------------------------------------------------------------------

def _req():
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def api_connection_error():
    return anthropic.APIConnectionError(request=_req())


def api_status_error(cls, status):
    return cls("boom", response=httpx.Response(status, request=_req()), body=None)


GOOD = {"vendor": "Ferguson", "document_type": "quote", "document_number": "Q-1",
        "document_date": "11/01/2026", "job_name": "Testerson Bath",
        "lines": [{"part_number": "K-1", "name": "Widget", "quantity": 2, "unit_price": 10,
                   "line_total": 20}],
        "totals": {"subtotal": 20, "total": 20}}


class TestWorker(unittest.TestCase):
    def setUp(self):
        self.db = FakeDocsDB([new_doc(status="queued", result=None)])
        self.replies = []
        self.calls = 0
        self.sleeps = []
        self.download = PDF

    def create(self, **kw):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return type("R", (), {"content": [type("B", (), {"type": "text",
                                                         "text": json.dumps(r)})()],
                              "stop_reason": "end_turn"})()

    def run_job(self, doc_id="doc_0123456789ab"):
        def download(key, max_bytes):
            self.assertEqual((key, max_bytes), (KEY, vd.MAX_PDF_BYTES))
            if isinstance(self.download, BaseException):
                raise self.download
            return self.download

        def with_options(**kw):
            self.assertEqual(kw, {"timeout": 120, "max_retries": 0})
            return mock.Mock(messages=mock.Mock(create=self.create))
        with self.db.patched(), \
                mock.patch.object(worker, "download_bytes", download), \
                mock.patch.object(worker.time, "sleep", self.sleeps.append), \
                mock.patch.object(extractor._client, "with_options", with_options), \
                mock.patch("requests.request", side_effect=AssertionError("no FieldPulse")), \
                mock.patch("requests.post", side_effect=AssertionError("no FieldPulse")), \
                mock.patch.object(worker, "sync_consultation",
                                  side_effect=AssertionError("no FieldPulse")):
            return worker.process_vendor_document(doc_id)

    def row(self):
        return self.db.docs["doc_0123456789ab"]

    def statuses(self):
        return [s for _, s, _ in self.db.updates]

    def test_success(self):
        self.replies = [GOOD]
        self.run_job()
        row = self.row()
        self.assertEqual(self.statuses(), ["processing", "done"])
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result"]["lines"][0]["line_total"], 20.0)
        # customer snapshot name reached the reader's job-name check
        self.assertIn("job_name_match", [c["code"] for c in row["result"]["checks"]])
        self.assertEqual((self.calls, self.sleeps), (1, []))

    def test_transient_error_retried_once_then_success(self):
        for err in (api_connection_error(), anthropic.APITimeoutError(request=_req()),
                    api_status_error(anthropic.RateLimitError, 429),
                    api_status_error(anthropic.InternalServerError, 500),
                    api_status_error(anthropic.APIStatusError, 529)):
            with self.subTest(err=type(err).__name__):
                self.setUp()
                self.replies = [err, GOOD]
                self.run_job()
                self.assertEqual(self.row()["status"], "done")
                self.assertEqual((self.calls, self.sleeps), (2, [worker._DOC_RETRY_PAUSE_S]))

    def test_retry_exhausted_fails_with_fixed_message_and_raises(self):
        self.replies = [api_connection_error(), api_status_error(anthropic.InternalServerError,
                                                                 503)]
        with self.assertRaises(ValueError) as cm:
            self.run_job()
        self.assertEqual(str(cm.exception), "Vendor document read failed: Claude API error")
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.row()["status"], "failed")
        self.assertEqual(self.row()["error"], worker.DOC_ERR_CLAUDE_BUSY)          # F-74

    def test_non_transient_api_error_not_retried(self):
        self.replies = [api_status_error(anthropic.BadRequestError, 400), GOOD]
        with self.assertRaises(ValueError):
            self.run_job()
        self.assertEqual((self.calls, self.sleeps), (1, []))
        self.assertEqual((self.row()["status"], self.row()["error"]),
                         ("failed", worker.DOC_ERR_CLAUDE_REJECTED))              # F-74

    def test_transient_then_rejected_says_rejected(self):              # F-74
        self.replies = [api_connection_error(), api_status_error(anthropic.BadRequestError, 400)]
        with self.assertRaises(ValueError):
            self.run_job()
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.row()["error"], worker.DOC_ERR_CLAUDE_REJECTED)
        for msg in (worker.DOC_ERR_CLAUDE_BUSY, worker.DOC_ERR_CLAUDE_REJECTED):
            self.assertNotIn("boom", msg)

    def test_unreadable_output_not_retried(self):
        def create(**kw):
            self.calls += 1
            return type("R", (), {"content": [type("B", (), {"type": "text",
                                                             "text": "no json here"})()],
                                  "stop_reason": "end_turn"})()
        self.create = create
        with self.assertRaises(ValueError):
            self.run_job()
        self.assertEqual((self.calls, self.sleeps), (1, []))
        self.assertEqual(self.row()["error"],
                         vd.USER_MESSAGES["Vendor document read failed: Claude returned "
                                          "malformed output"])

    def test_bad_pdf_fails_quietly_without_claude(self):
        self.download = b"hello, not a pdf"
        self.run_job()                                    # not re-raised: the user's file
        self.assertEqual(self.calls, 0)
        self.assertEqual((self.row()["status"], self.row()["error"]),
                         ("failed", "This file is not a PDF."))

    def test_object_grew_too_big_fails_quietly(self):
        self.download = worker.StorageError("R2 object is larger than 20971520 bytes")
        self.run_job()
        self.assertEqual(self.row()["error"], worker.DOC_ERR_TOO_BIG)

    def test_job_timeout_propagates_and_marks_failed(self):
        # direct from the client, wrapped by the SDK (cause chain), and during the pause
        cases = {
            "direct": [JobTimeoutException("Task exceeded maximum timeout value (300 seconds)")],
        }
        wrapped = api_connection_error()
        wrapped.__cause__ = JobTimeoutException("Task exceeded maximum timeout value")
        cases["wrapped"] = [wrapped]
        for name, replies in cases.items():
            with self.subTest(name):
                self.setUp()
                self.replies = list(replies)
                with self.assertRaises(JobTimeoutException):
                    self.run_job()
                self.assertEqual((self.calls, self.sleeps), (1, []))
                self.assertEqual((self.row()["status"], self.row()["error"]),
                                 ("failed", worker.DOC_ERR_TIMEOUT))
        self.setUp()
        self.replies = [api_connection_error(), GOOD]

        def sleep(s):
            raise JobTimeoutException("Task exceeded maximum timeout value")
        with self.db.patched(), mock.patch.object(worker.time, "sleep", sleep):
            self.sleeps = None
            with mock.patch.object(worker, "download_bytes", lambda k, m: PDF), \
                    mock.patch.object(extractor._client, "with_options",
                                      lambda **kw: mock.Mock(messages=mock.Mock(
                                          create=self.create))):
                with self.assertRaises(JobTimeoutException):
                    worker.process_vendor_document("doc_0123456789ab")
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.row()["error"], worker.DOC_ERR_TIMEOUT)

    def test_storage_errors(self):
        for exc, msg in ((worker.StorageError(f"R2 key not found: {KEY}"), worker.DOC_ERR_MISSING),
                         (worker.StorageError("Failed to download r2://b/k: x"),
                          worker.DOC_ERR_STORAGE)):
            with self.subTest(msg=msg):
                self.setUp()
                self.download = exc
                with self.assertRaises(worker.StorageError):
                    self.run_job()
                self.assertEqual((self.row()["status"], self.row()["error"]), ("failed", msg))
                self.assertEqual(self.calls, 0)

    def test_unexpected_error_marks_failed(self):
        with mock.patch.object(vd, "_normalize", side_effect=KeyError("boom")):
            self.replies = [GOOD]
            with self.assertRaises(KeyError):
                self.run_job()
        self.assertEqual(self.row()["error"], worker.DOC_ERR_INTERNAL)

    def test_deleted_before_or_during_the_read(self):
        self.db.docs.clear()
        self.run_job()                                      # nothing to do, no raise
        self.assertEqual(self.calls, 0)
        self.setUp()
        self.replies = [GOOD]
        real_update = self.db.update_vendor_document

        def update(doc_id, status, **kw):
            if status == "done":
                self.db.docs.clear()
            return real_update(doc_id, status, **kw)
        self.db.update_vendor_document = update
        self.run_job()                                      # finds it gone; no raise
        self.assertEqual(self.db.docs, {})

    def test_save_done_failure_marks_failed_and_raises(self):
        self.replies = [GOOD]
        real_update = self.db.update_vendor_document

        def update(doc_id, status, **kw):
            if status == "done":
                raise RuntimeError("db down")
            return real_update(doc_id, status, **kw)
        self.db.update_vendor_document = update
        with self.assertRaises(RuntimeError):
            self.run_job()
        self.assertEqual((self.row()["status"], self.row()["error"]),
                         ("failed", worker.DOC_ERR_INTERNAL))

    def test_fail_document_ordinary_error_returns_false(self):
        def update(*a, **k):
            raise RuntimeError("db down")
        with mock.patch.object(worker, "update_vendor_document", update), \
                mock.patch.object(worker, "report") as rep:
            self.assertFalse(worker._fail_document("doc_0123456789ab", "x"))
        self.assertEqual(rep.call_args[0][1], "Could not mark vendor document failed")

    # --- F-73: an RQ deadline during a status write is never swallowed -------------

    def deadline_on(self, status, times=1):
        """Make update_vendor_document raise the real rq JobTimeoutException for
        `status` the first `times` calls (then behave normally)."""
        real = self.db.update_vendor_document
        left = {"n": times}

        def update(doc_id, st, **kw):
            if st == status and left["n"] > 0:
                left["n"] -= 1
                raise JobTimeoutException("Task exceeded maximum timeout value (300 seconds)")
            return real(doc_id, st, **kw)
        self.db.update_vendor_document = update

    def test_deadline_during_failed_write_quiet_paths(self):
        for name, download in (("bad_pdf", b"not a pdf"),
                               ("oversized", worker.StorageError(
                                   "R2 object is larger than 20971520 bytes"))):
            for times, want in ((1, ("failed", worker.DOC_ERR_TIMEOUT)),   # retry write works
                                (99, ("processing", None))):                # every write cut
                with self.subTest(name=name, times=times):
                    self.setUp()
                    self.download = download
                    self.deadline_on("failed", times)
                    with mock.patch.object(worker, "report"), \
                            self.assertRaises(JobTimeoutException):
                        self.run_job()
                    self.assertEqual((self.row()["status"], self.row()["error"]), want)
                    self.assertEqual(self.calls, 0)

    def test_deadline_during_failed_write_after_other_errors(self):
        self.replies = [api_status_error(anthropic.BadRequestError, 400)]
        self.deadline_on("failed", 1)
        with self.assertRaises(JobTimeoutException):
            self.run_job()
        self.assertEqual(self.row()["error"], worker.DOC_ERR_TIMEOUT)

    def test_ordinary_error_during_failed_write_is_not_a_quiet_return(self):
        self.download = b"not a pdf"
        real = self.db.update_vendor_document

        def update(doc_id, st, **kw):
            if st == "failed":
                raise RuntimeError("db down")
            return real(doc_id, st, **kw)
        self.db.update_vendor_document = update
        with mock.patch.object(worker, "report"), self.assertRaises(ValueError):
            self.run_job()                  # failure not saved: RQ must see it

    def test_deadline_during_processing_update(self):
        self.deadline_on("processing")
        with self.assertRaises(JobTimeoutException):
            self.run_job()
        self.assertEqual(self.calls, 0)
        self.assertEqual((self.row()["status"], self.row()["error"]),
                         ("failed", worker.DOC_ERR_TIMEOUT))

    def test_deadline_during_done_update(self):
        self.replies = [GOOD]
        self.deadline_on("done")
        with self.assertRaises(JobTimeoutException):
            self.run_job()
        self.assertEqual((self.row()["status"], self.row()["error"]),
                         ("failed", worker.DOC_ERR_TIMEOUT))

    def test_is_transient(self):
        def wrap(cause):
            e = ValueError("Vendor document read failed: Claude API error")
            e.__cause__ = cause
            return e
        self.assertTrue(vd.is_transient(wrap(api_connection_error())))
        self.assertTrue(vd.is_transient(wrap(api_status_error(anthropic.APIStatusError, 408))))
        self.assertFalse(vd.is_transient(wrap(api_status_error(anthropic.BadRequestError, 400))))
        self.assertFalse(vd.is_transient(wrap(None)))
        self.assertFalse(vd.is_transient(ValueError("Vendor document is not a PDF")))
        self.assertFalse(vd.is_transient(RuntimeError("Vendor document read failed: Claude "
                                                      "API error")))

    def test_enqueue_uses_300s_timeout(self):
        spec = importlib.util.spec_from_file_location("queue_setup_p1b", _HERE / "queue_setup.py")
        q = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(q)           # Redis client is lazy: no connection here
        with mock.patch.object(q.consultation_queue, "enqueue") as enq:
            q.enqueue_vendor_document("doc_0123456789ab")
        enq.assert_called_once_with("worker.process_vendor_document", "doc_0123456789ab",
                                    job_timeout=300)


# --- Observability ------------------------------------------------------------------------

class TestObservability(unittest.TestCase):
    def test_new_messages_and_doc_id_tag(self):
        for msg in (f"R2 key not found: {KEY}", "R2 object is larger than 20971520 bytes",
                    "create_vendor_document needs a file_key",
                    "update_vendor_document: bad status 'nope'"):
            with self.subTest(msg=msg):
                self.assertEqual(observability._safe_value(msg, "X", "y", None), msg)
        self.assertEqual(observability._safe_value("R2 key not found: vendor-docs/Pat.pdf",
                                                   "X", "y", None), "[redacted]")
        rule = observability._TAG_RULES["job_id"]
        self.assertTrue(observability._rule(rule, "doc_0123456789ab"))
        self.assertTrue(observability._rule(rule, "job_1a2b3c4d5e6f"))
        self.assertIsNone(observability._rule(rule, "doc_Pat Testerson"))

    def test_routes_are_templates(self):
        import orchestrator  # noqa: F401  (registers the app for route templates)
        rules = {r.rule for r in orchestrator.app.url_map.iter_rules()}
        for r in ("/documents", "/documents/<doc_id>", "/documents/upload-url",
                  "/documents/submit"):
            self.assertIn(r, rules)


# --- storage.py helpers (real module, fake boto client) -----------------------------------

class TestStorageHelpers(unittest.TestCase):
    def configured(self):
        s = real_storage
        return mock.patch.multiple(s, R2_ENDPOINT_URL="https://r2.invalid",
                                   R2_ACCESS_KEY_ID="a", R2_SECRET_ACCESS_KEY="b",
                                   R2_BUCKET_NAME="bucket")

    def test_upload_key_and_content_type(self):
        with self.configured(), mock.patch.object(real_storage._client, "generate_presigned_url",
                                                  return_value="https://r2.invalid/put") as g:
            up = real_storage.vendor_document_upload()
        self.assertRegex(up["key"], real_storage.VENDOR_DOC_KEY_RE)
        self.assertEqual(up["url"], "https://r2.invalid/put")
        args, kw = g.call_args
        self.assertEqual(args[0], "put_object")
        self.assertEqual(kw["Params"], {"Bucket": "bucket", "Key": up["key"],
                                        "ContentType": "application/pdf"})

    def test_key_rule(self):
        good = "vendor-docs/0f0e0d0c-0b0a-4908-8706-050403020100.pdf"
        self.assertTrue(real_storage.VENDOR_DOC_KEY_RE.match(good))
        for bad in ("consultations/0f0e0d0c-0b0a-4908-8706-050403020100.pdf",
                    good + "\n", good.upper(), "vendor-docs/../x.pdf", good[:-4] + ".PDF"):
            self.assertFalse(real_storage.VENDOR_DOC_KEY_RE.match(bad), bad)

    def test_object_size_and_audio_exists(self):
        with self.configured(), mock.patch.object(real_storage._client, "head_object",
                                                  return_value={"ContentLength": 42}):
            self.assertEqual(real_storage.object_size(KEY), 42)
            self.assertTrue(real_storage.audio_exists(KEY))
        missing = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        with self.configured(), mock.patch.object(real_storage._client, "head_object",
                                                  side_effect=missing):
            self.assertIsNone(real_storage.object_size(KEY))
            self.assertFalse(real_storage.audio_exists(KEY))
        denied = ClientError({"Error": {"Code": "403"}}, "HeadObject")
        with self.configured(), mock.patch.object(real_storage._client, "head_object",
                                                  side_effect=denied):
            with self.assertRaises(real_storage.StorageError):
                real_storage.object_size(KEY)

    def test_download_bytes(self):
        def obj(data, size=None):
            return {"ContentLength": len(data) if size is None else size,
                    "Body": io.BytesIO(data)}
        with self.configured(), mock.patch.object(real_storage._client, "get_object",
                                                  return_value=obj(PDF)):
            self.assertEqual(real_storage.download_bytes(KEY, 100), PDF)
        with self.configured(), mock.patch.object(real_storage._client, "get_object",
                                                  return_value=obj(PDF, size=10**9)):
            with self.assertRaisesRegex(real_storage.StorageError, "larger than 100 bytes"):
                real_storage.download_bytes(KEY, 100)
        with self.configured(), mock.patch.object(real_storage._client, "get_object",
                                                  return_value=obj(b"x" * 101, size=5)):
            with self.assertRaisesRegex(real_storage.StorageError, "larger than"):
                real_storage.download_bytes(KEY, 100)     # lying ContentLength
        missing = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        with self.configured(), mock.patch.object(real_storage._client, "get_object",
                                                  side_effect=missing):
            with self.assertRaisesRegex(real_storage.StorageError, "^R2 key not found: "):
                real_storage.download_bytes(KEY, 100)
        with self.assertRaisesRegex(real_storage.StorageError, "not configured"):
            with mock.patch.object(real_storage, "R2_BUCKET_NAME", None):
                real_storage.download_bytes(KEY, 100)


# --- customer_display_name ----------------------------------------------------------------

class TestDisplayName(unittest.TestCase):
    def test_names(self):
        f = dp.customer_display_name
        self.assertEqual(f({"display_name": " Pat T "}), "Pat T")
        self.assertEqual(f({"first_name": "Sam", "last_name": "Example"}), "Sam Example")
        self.assertEqual(f({"last_name": "Example"}), "Example")
        for bad in (None, "x", {}, {"display_name": 5, "first_name": None}):
            self.assertIsNone(f(bad))


if __name__ == "__main__":
    unittest.main()
