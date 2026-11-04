"""Offline tests for the stage-3 admin panel: admin_panel.py shaping, the
/admin/* routes (auth, list, detail, send, audio), the manual FieldPulse send
(fieldpulse_sync manual=True + worker.send_consultation_to_fieldpulse), and
storage.generate_download_url. No network, no Postgres, no Redis (reuses the
stubs + network block from test_fieldpulse_sync). Run:

    python -m unittest test_admin_panel -v
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

# Same env scrub, network block and jobs/queue/storage stubs as the stage-2 suite.
from test_fieldpulse_sync import (  # noqa: E402
    Base, FakeStore, empty_extraction, full_extraction, job,
)
import fieldpulse_sync as fs  # noqa: E402

for _mod, _attrs in (("jobs", ("list_jobs_for_admin", "list_fieldpulse_writes",
                               "save_manual_send", "release_held_fieldpulse_writes")),
                     ("queue_setup", ("enqueue_manual_send",)),
                     ("storage", ("generate_download_url",))):
    for _a in _attrs:
        if not hasattr(sys.modules[_mod], _a):
            setattr(sys.modules[_mod], _a, lambda *a, **k: None)

import admin_panel as ap  # noqa: E402

SECRET = "secret-key-xyz"


class ReleasingStore(FakeStore):
    """FakeStore + the release rule from jobs.release_held_fieldpulse_writes."""

    def release_held_fieldpulse_writes(self, job_id, mode):
        assert mode in ("test_customer_only", "live")
        n = 0
        for (jid, _), row in self.rows.items():
            if jid == job_id and row["mode"] == "dry_run" and row["status"] in (
                    "planned", "failed", "skipped"):
                row["mode"] = mode
                n += 1
        return n


def light_row(**kw):
    """A row as jobs.list_jobs_for_admin returns it."""
    base = {"id": "job_1", "status": "done", "customer_id": "18809123",
            "customer": {"id": "18809123", "display_name": "Jane Doe"},
            "source": "3day-app-record", "has_audio": True, "has_paste": False, "error": None,
            "created_at": "2026-11-03T20:00:00+00:00", "updated_at": "2026-11-03T20:05:00+00:00",
            "input": "audio", "project_type": "kitchen", "client_name": None, "close_score": 9,
            "sync": {"status": "dry_run", "mode": "dry_run",
                     "steps": [{"step": "comment", "status": "planned"}]},
            "legacy_mock": False, "writes": [{"step": "comment", "status": "planned",
                                              "mode": "dry_run"}],
            "last_manual_send": None}
    base.update(kw)
    return base


# --- FieldPulse status + source ---------------------------------------------------

class TestClassification(unittest.TestCase):
    def test_fieldpulse_status_table(self):
        W = lambda *pairs: [{"step": f"s{i}", "status": s, "mode": m}  # noqa: E731
                            for i, (s, m) in enumerate(pairs)]
        held = {"status": "blocked", "steps": [{"step": "comment", "status": "held"}]}
        cases = [
            ("done", None, W(("succeeded", "live"), ("skipped", "live")), False, "succeeded"),
            ("done", None, W(("succeeded", "live"), ("failed", "live")), False, "partial"),
            ("done", None, W(("succeeded", "live"), ("planned", "live")), False, "partial"),
            ("done", None, W(("failed", "live")), False, "failed"),
            ("done", None, W(("sent", "live"), ("succeeded", "live")), False, "sent"),
            ("done", held, W(("planned", "dry_run")), False, "held"),
            ("done", {"status": "dry_run"}, W(("planned", "dry_run")), False, "dry_run"),
            ("done", None, W(("planned", "dry_run")), False, "dry_run"),
            ("done", {"status": "off", "steps": []}, [], False, "not_sent"),
            ("done", {"status": "skipped", "steps": []}, [], False, "not_sent"),
            ("done", {"status": "blocked", "steps": [{"status": "skipped"}]}, [], False,
             "not_sent"),
            ("done", {"status": "failed", "steps": []}, [], False, "failed"),   # sync crash
            ("processing", {"status": "pending"}, [], False, "pending"),
            ("queued", None, [], False, "pending"),
            ("done", None, [], True, "legacy_mock"),
            ("failed", None, [], False, "not_attempted"),
            ("done", "garbage", "garbage", False, "not_attempted"),
            ("done", {"status": 5, "steps": "x"}, [None, 3], False, "not_attempted"),
        ]
        for job_status, sync, writes, legacy, want in cases:
            with self.subTest(sync=sync, writes=writes, legacy=legacy):
                self.assertEqual(ap.fieldpulse_status(job_status, sync, writes, legacy), want)
                self.assertIn(want, ap.FP_LABELS)

    def test_source_kind(self):
        self.assertEqual(ap.source_kind("3day-app-paste", False, True), "paste")
        self.assertEqual(ap.source_kind("3day-app", False, False, "paste"), "paste")
        self.assertEqual(ap.source_kind("3day-app-upload", True, False), "upload")
        self.assertEqual(ap.source_kind("3day-app-record", True, False), "record")
        self.assertEqual(ap.source_kind("3day-app", True, False), "audio")   # old jobs
        self.assertEqual(ap.source_kind(None, False, False), "unknown")
        self.assertEqual(ap.source_kind(42, False, False), "unknown")


# --- List ----------------------------------------------------------------------------

class TestList(unittest.TestCase):
    def rows(self):
        return [
            light_row(id="job_new"),
            light_row(id="job_held", customer={"first_name": "Bob", "last_name": "Smith"},
                      sync={"status": "blocked", "steps": [{"step": "comment", "status": "held"}]}),
            light_row(id="job_fail", status="failed", error="Transcription failed: boom",
                      sync=None, writes=[], close_score=None, input=None),
            light_row(id="job_part", writes=[{"step": "comment", "status": "succeeded",
                                              "mode": "live"},
                                             {"step": "return_task", "status": "failed",
                                              "mode": "live"}]),
            light_row(id="job_legacy", customer=None, customer_id="cust_ab12", sync=None,
                      writes=[], legacy_mock=True, source="3day-app", client_name="Greg H",
                      close_score="9"),
            light_row(id="job_paste", source="3day-app-paste", has_audio=False, has_paste=True,
                      input="paste", customer=None, customer_id=None, close_score=42),
        ]

    def call(self, rows=None, **kw):
        with mock.patch.object(ap.jobs, "list_jobs_for_admin", return_value=self.rows() if rows is None else rows):
            return ap.list_consultations(**kw)

    def test_shape_order_and_legacy(self):
        code, body = self.call()
        self.assertEqual(code, 200)
        ids = [i["id"] for i in body["items"]]
        self.assertEqual(ids, ["job_new", "job_held", "job_fail", "job_part", "job_legacy",
                               "job_paste"])                     # order kept (SQL: newest first)
        by = {i["id"]: i for i in body["items"]}
        self.assertEqual(by["job_new"]["fieldpulse_status"], "dry_run")
        self.assertEqual(by["job_new"]["source"], "record")
        self.assertEqual(by["job_new"]["close_score"], 9)
        self.assertEqual(by["job_held"]["customer_name"], "Bob Smith")
        self.assertEqual(by["job_held"]["fieldpulse_status"], "held")
        self.assertEqual(by["job_fail"]["fieldpulse_status"], "not_attempted")
        self.assertTrue(by["job_fail"]["needs_attention"])          # failed job
        self.assertEqual(by["job_part"]["fieldpulse_status"], "partial")
        self.assertEqual(by["job_legacy"]["fieldpulse_status"], "legacy_mock")
        self.assertEqual(by["job_legacy"]["customer_name"], "Greg H (name heard in the consultation)")
        self.assertEqual(by["job_legacy"]["source"], "audio")
        self.assertIsNone(by["job_legacy"]["close_score"])          # "9" string: not a number
        self.assertIsNone(by["job_paste"]["close_score"])           # out of range
        self.assertIsNone(by["job_paste"]["customer_name"])
        self.assertEqual(by["job_paste"]["source"], "paste")
        self.assertEqual(body["counts"]["attention"], 3)            # held, failed job, partial
        for i in body["items"]:
            self.assertNotIn("_search", i)
            self.assertNotIn("transcript", i)
            self.assertNotIn("comment_text", json.dumps(i))

    def test_search_filter_and_paging(self):
        self.assertEqual([i["id"] for i in self.call(search="  SMITH ")[1]["items"]], ["job_held"])
        self.assertEqual([i["id"] for i in self.call(search="greg")[1]["items"]], ["job_legacy"])
        self.assertEqual([i["id"] for i in self.call(search="cust_ab")[1]["items"]], ["job_legacy"])
        code, body = self.call(fp_filter="attention")
        self.assertEqual([i["id"] for i in body["items"]], ["job_held", "job_fail", "job_part"])
        self.assertEqual(body["total"], 3)
        self.assertEqual([i["id"] for i in self.call(fp_filter="legacy_mock")[1]["items"]],
                         ["job_legacy"])
        code, body = self.call(offset=2, limit=2)
        self.assertEqual([i["id"] for i in body["items"]], ["job_fail", "job_part"])
        self.assertEqual(body["total"], 6)
        self.assertEqual(self.call(limit=100000)[1]["limit"], ap.MAX_PAGE)
        self.assertEqual(self.call(fp_filter="nope")[0], 400)
        self.assertEqual(self.call(offset="x")[0], 400)
        self.assertEqual(self.call(rows=[])[1]["items"], [])


# --- Detail + no secrets ----------------------------------------------------------------

def done_job(**kw):
    j = job(status="done", audio_key="consultations/abc-123.webm", transcript=None,
            source="3day-app-record", error=None, updated_at="2026-11-03T20:05:00+00:00",
            result={"input": "audio", "extraction": full_extraction(),
                    "transcript": "REP: hello. HOMEOWNER: " + "words " * 2000,
                    "transcription_seconds": 12.0,
                    "fieldpulse_sync": {"status": "dry_run", "mode": "dry_run", "steps": [],
                                        "comment_text": "AI consultation summary"}})
    j.update(kw)
    return j


class TestDetail(unittest.TestCase):
    def call(self, jobrow, writes=()):
        with mock.patch.object(ap.jobs, "get_job", return_value=jobrow), \
                mock.patch.object(ap.jobs, "list_fieldpulse_writes", return_value=list(writes)):
            return ap.consultation_detail("job_abc")

    def test_full_detail(self):
        writes = [{"job_id": "job_abc", "step": "comment", "status": "planned", "mode": "dry_run",
                   "request": {"method": "POST", "path": "/comments",
                               "body": {"commentable_type": "customer", "commentable_id": 18809123,
                                        "text": "AI consultation summary\n\nSUMMARY"}},
                   "response": None, "fieldpulse_record_id": None, "error": None, "attempts": 0,
                   "created_at": "x", "updated_at": "y"}]
        code, d = self.call(done_job(), writes)
        self.assertEqual(code, 200)
        self.assertEqual(d["customer_name"], "Jane Doe")
        self.assertEqual(d["source"], "record")
        self.assertTrue(d["has_audio"])
        self.assertTrue(d["can_send"])
        self.assertEqual(d["fieldpulse_status"], "dry_run")
        self.assertEqual(d["fieldpulse_writes"][0]["request"]["body"]["text"],
                         "AI consultation summary\n\nSUMMARY")
        self.assertEqual(d["extraction"]["sales"]["close_score_1_10"], 9)
        self.assertTrue(d["transcript"].startswith("REP: hello."))
        # Raw view: everything, transcript only once.
        self.assertIn("characters: shown above", d["raw"]["result"]["transcript"])
        self.assertEqual(d["raw"]["audio_key"], "consultations/abc-123.webm")
        self.assertNotIn("upload_url", json.dumps(d))

    def test_secrets_and_signed_urls_are_scrubbed(self):
        signed = ("https://acct.r2.cloudflarestorage.com/b/consultations/a.webm?"
                  "X-Amz-Algorithm=AWS4&X-Amz-Credential=AKIA123&X-Amz-Signature=deadbeef")
        writes = [{"step": "comment", "status": "failed", "mode": "live",
                   "request": {"method": "POST", "path": "/comments", "body": {"text": "t"},
                               "headers": {"x-api-key": SECRET}},
                   "response": {"http_status": 403, "body": {"message": "no",
                                                             "Authorization": SECRET,
                                                             "api_key": SECRET,
                                                             "nested": [{"token": SECRET}]}},
                   "error": f"HTTP 403 see {signed}", "attempts": 1}]
        j = done_job()
        j["result"]["fieldpulse"] = {"fieldpulse_url": "http://mock/x", "x-api-key": SECRET}
        j["result"]["extraction"]["flags"].append(f"link {signed}")
        code, d = self.call(j, writes)
        text = json.dumps(d)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("deadbeef", text)
        self.assertNotIn("AKIA123", text)
        self.assertIn("[signed link removed]", text)
        self.assertEqual(d["fieldpulse_writes"][0]["response"]["body"]["message"], "no")
        self.assertEqual(d["legacy_fieldpulse"]["fieldpulse_url"], "http://mock/x")
        # Known extraction keys survive the scrub.
        self.assertEqual(set(d["extraction"]), set(full_extraction()))

    def test_legacy_failed_and_garbage_rows(self):
        self.assertEqual(self.call(None)[0], 404)
        legacy = job(status="done", audio_key="consultations/x.webm", source="3day-app",
                     customer=None, customer_id="cust_ab12",
                     result={"extraction": {"project_type": "kitchen"}, "transcript": "t",
                             "fieldpulse": {"match_path": "matched_by_id"}})
        code, d = self.call(legacy)
        self.assertEqual((d["fieldpulse_status"], d["source"], d["customer_name"]),
                         ("legacy_mock", "audio", "Customer cust_ab12"))
        self.assertIsNone(d["fieldpulse_sync"])
        failed = job(status="failed", error="Extraction failed: x", audio_key=None,
                     transcript="pasted words here", source="3day-app-paste", result=None)
        code, d = self.call(failed)
        self.assertEqual(code, 200)
        self.assertFalse(d["can_send"])
        self.assertEqual(d["transcript"], "pasted words here")     # from the paste column
        self.assertIn("pasted transcript", d["raw"]["transcript"])
        self.assertTrue(d["needs_attention"])
        garbage = job(status="done", result=["not", "a", "dict"], customer="str")
        code, d = self.call(garbage)
        self.assertEqual(code, 200)
        self.assertIsNone(d["extraction"])
        self.assertFalse(d["can_send"])


# --- Routes: auth + wiring -----------------------------------------------------------------

class TestRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import orchestrator
        cls.orch = orchestrator
        cls.client = orchestrator.app.test_client()

    def test_every_admin_route_requires_a_clerk_session(self):
        app = self.orch.app
        with mock.patch.dict(app.before_request_funcs,
                             {None: [self.orch._require_clerk_session]}), \
                mock.patch.object(ap.jobs, "get_job", side_effect=AssertionError("no auth")), \
                mock.patch.object(ap.jobs, "list_jobs_for_admin",
                                  side_effect=AssertionError("no auth")):
            for method, path in (("get", "/admin/consultations"),
                                 ("get", "/admin/consultations/job_1"),
                                 ("get", "/admin/consultations/job_1/audio-url"),
                                 ("post", "/admin/consultations/job_1/send")):
                for headers in ({}, {"Authorization": "Bearer not-a-real-token"}):
                    with self.subTest(path=path, headers=bool(headers)):
                        r = getattr(self.client, method)(path, headers=headers)
                        self.assertEqual(r.status_code, 401)
        self.assertNotIn("/admin/consultations", self.orch._PUBLIC_PATHS)

    def unauthed(self):
        app = self.orch.app
        hooks = [f for f in app.before_request_funcs.get(None, [])
                 if f.__name__ != "_require_clerk_session"]
        return mock.patch.dict(app.before_request_funcs, {None: hooks})

    def test_list_and_detail_routes(self):
        with self.unauthed(), \
                mock.patch.object(ap.jobs, "list_jobs_for_admin", return_value=[light_row()]), \
                mock.patch.object(ap.jobs, "get_job", return_value=done_job()), \
                mock.patch.object(ap.jobs, "list_fieldpulse_writes", return_value=[]):
            r = self.client.get("/admin/consultations?q=jane&fp=dry_run&limit=10")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["items"][0]["id"], "job_1")
            self.assertEqual(self.client.get("/admin/consultations?fp=bad").status_code, 400)
            r = self.client.get("/admin/consultations/job_abc")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["id"], "job_abc")

    def test_send_route_only_enqueues(self):
        saved, queued = [], []
        with self.unauthed(), \
                mock.patch.object(ap.jobs, "get_job", return_value=done_job()), \
                mock.patch.object(ap.jobs, "save_manual_send",
                                  side_effect=lambda jid, e, sync=None: saved.append(e) or True), \
                mock.patch.object(ap.queue_setup, "enqueue_manual_send",
                                  side_effect=lambda *a: queued.append(a)), \
                mock.patch.object(fs, "sync_consultation", side_effect=AssertionError("no sync")), \
                mock.patch("requests.post", side_effect=AssertionError("no HTTP")):
            r = self.client.post("/admin/consultations/job_abc/send",
                                 json={"requested_by": "Mike " + "x" * 500})
        self.assertEqual(r.status_code, 202, r.get_json())
        rid = r.get_json()["request_id"]
        self.assertEqual(queued, [("job_abc", rid)])
        self.assertEqual(saved[0]["status"], "queued")
        self.assertEqual(len(saved[0]["requested_by"]), 120)

    def test_audio_route(self):
        with self.unauthed(), \
                mock.patch.object(ap.jobs, "get_job", return_value=done_job()), \
                mock.patch.object(ap.storage, "generate_download_url",
                                  return_value="https://r2.invalid/get?sig") as gen:
            r = self.client.get("/admin/consultations/job_abc/audio-url")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json(), {"url": "https://r2.invalid/get?sig", "expires_in": 900})
            gen.assert_called_once_with("consultations/abc-123.webm", expires_in=900)
        with self.unauthed(), \
                mock.patch.object(ap.jobs, "get_job", return_value=done_job(audio_key=None)):
            self.assertEqual(self.client.get("/admin/consultations/j/audio-url").status_code, 404)
        with self.unauthed(), \
                mock.patch.object(ap.jobs, "get_job", return_value=done_job()), \
                mock.patch.object(ap.storage, "generate_download_url",
                                  side_effect=ap.storage.StorageError("R2 is not configured")):
            r = self.client.get("/admin/consultations/j/audio-url")
            self.assertEqual(r.status_code, 502)
            self.assertNotIn("R2", r.get_json()["error"])


class TestRequestManualSend(unittest.TestCase):
    def call(self, jobrow, enqueue=None):
        self.saved, self.queued = [], []
        with mock.patch.object(ap.jobs, "get_job", return_value=jobrow), \
                mock.patch.object(ap.jobs, "save_manual_send",
                                  side_effect=lambda jid, e, sync=None: self.saved.append(e) or True), \
                mock.patch.object(ap.queue_setup, "enqueue_manual_send",
                                  side_effect=enqueue or (lambda *a: self.queued.append(a))):
            return ap.request_manual_send("job_abc", "user_1", "Mike")

    def test_refuses_unfinished_jobs(self):
        self.assertEqual(self.call(None)[0], 404)
        for bad in (job(status="processing", result=None), job(status="failed", result=None),
                    job(status="done", result={"extraction": None}),
                    job(status="done", result="junk")):
            with self.subTest(bad=bad["status"]):
                self.assertEqual(self.call(bad)[0], 409)
                self.assertEqual((self.saved, self.queued), ([], []))

    def test_dedupes_a_recent_queued_request(self):
        from datetime import datetime, timezone
        j = done_job()
        j["result"]["fieldpulse_manual_sends"] = [
            {"id": "ms_old", "status": "queued",
             "requested_at": datetime.now(timezone.utc).isoformat()}]
        code, body = self.call(j)
        self.assertEqual((code, body["request_id"], body.get("already_queued")),
                         (202, "ms_old", True))
        self.assertEqual(self.queued, [])
        j["result"]["fieldpulse_manual_sends"][0]["requested_at"] = "2020-01-01T00:00:00+00:00"
        code, body = self.call(j)
        self.assertNotEqual(body["request_id"], "ms_old")
        self.assertEqual(len(self.queued), 1)

    def test_queue_down_is_recorded_and_503(self):
        def boom(*a):
            raise ConnectionError("redis down")
        code, body = self.call(done_job(), enqueue=boom)
        self.assertEqual(code, 503)
        self.assertEqual([e["status"] for e in self.saved], ["queued", "error"])
        self.assertEqual(self.saved[0]["id"], self.saved[1]["id"])


# --- Manual send through fieldpulse_sync (mode gating, release, idempotency) --------------

class TestManualSend(Base):
    def setUp(self):
        super().setUp()
        self.store = ReleasingStore()
        p = mock.patch.object(fs, "_STORE", self.store)
        p.start()
        self.addCleanup(p.stop)

    def rows(self):
        return [(r["step"], r["status"], r["mode"]) for r in
                self.store.list_fieldpulse_writes("job_abc")]

    def dry_run_first(self):
        out = fs.sync_consultation(job(), full_extraction())
        self.assertEqual(out["status"], "dry_run")

    def test_dry_run_and_off_never_send(self):
        self.dry_run_first()
        out = fs.sync_consultation(job(), full_extraction(), manual=True)
        self.assertEqual(self.sent, [])
        self.assertTrue(out["manual"])
        self.assertTrue(out["message"].startswith("Not sent: write mode is dry_run."), out["message"])
        self.assertEqual(self.rows(), [("comment", "planned", "dry_run"),
                                       ("return_task", "planned", "dry_run")])
        self.setenv(FIELDPULSE_WRITE_MODE="off")
        out = fs.sync_consultation(job(), full_extraction(), manual=True)
        self.assertEqual(out["message"], "Not sent: write mode is off. Nothing was sent to "
                                         "FieldPulse.")
        self.setenv(FIELDPULSE_WRITE_MODE="garbage")                      # -> dry_run
        out = fs.sync_consultation(job(), full_extraction(), manual=True)
        self.assertIn("write mode is dry_run", out["message"])
        self.assertEqual(self.sent, [])

    def test_send_mode_without_config_never_sends(self):
        self.dry_run_first()
        self.setenv(FIELDPULSE_WRITE_MODE="live")                          # no URL / key
        out = fs.sync_consultation(job(), full_extraction(), manual=True)
        self.assertEqual(self.sent, [])
        self.assertIn("FIELDPULSE_WRITE_BASE_URL", out["message"])
        self.assertTrue(all(m == "dry_run" for _, _, m in self.rows()))

    def test_test_customer_only_sends_only_the_test_customer(self):
        self.dry_run_first()
        self.sendable(mode="test_customer_only", test_id="999")
        out = fs.sync_consultation(job(), full_extraction(), manual=True)
        self.assertEqual(self.sent, [])
        self.assertIn("not the test customer", out["message"])
        self.assertTrue(all(m == "dry_run" for _, _, m in self.rows()))  # marker kept
        self.sendable(mode="test_customer_only", test_id="18809123")
        self.allow_send([(200, {"error": False, "response": {"id": 11}}),
                         (200, {"error": False, "response": {"id": 12}})])
        out = fs.sync_consultation(job(), full_extraction(), manual=True)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(self.rows(), [("comment", "succeeded", "test_customer_only"),
                                       ("return_task", "succeeded", "test_customer_only")])

    def test_live_releases_held_rows_and_never_resends(self):
        self.dry_run_first()
        self.sendable(mode="live")
        # Without manual: still held (stage-2 rule unchanged).
        self.assertEqual(fs.sync_consultation(job(), full_extraction())["status"], "blocked")
        self.assertEqual(self.sent, [])
        self.allow_send([(200, {"error": False, "response": {"id": 1}}),
                         (500, {"error": True, "message": "boom"})])
        out = fs.sync_consultation(job(), full_extraction(), manual=True)
        self.assertEqual(out["status"], "partial")
        self.assertEqual(len(self.sent), 2)
        # Retry: only the failed task goes again; the comment is never re-sent.
        self.allow_send([(200, {"error": False, "response": {"id": 2}})])
        out = fs.sync_consultation(job(), full_extraction(), manual=True)
        self.assertEqual(out["status"], "succeeded")
        self.assertEqual([r["path"] for r in self.sent], ["/comments", "/jobs", "/jobs"])
        self.allow_send([])
        out = fs.sync_consultation(job(), full_extraction(), manual=True)
        self.assertEqual(len(self.sent), 3)                                # nothing more
        self.assertEqual(out["status"], "succeeded")

    def test_sent_unknown_is_never_resent_manually(self):
        self.sendable(mode="live")
        self.allow_send([fs._OutcomeUnknown("ReadTimeout: no response"),
                         (200, {"error": False, "response": {"id": 1}})])
        fs.sync_consultation(job(), full_extraction())
        self.allow_send([])
        out = fs.sync_consultation(job(), full_extraction(), manual=True)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(out["steps"][0]["status"], "sent")

    def test_manual_retry_goes_past_the_automatic_cap(self):
        self.sendable(mode="live")
        self.allow_send([fs._NotDelivered("connect timeout")] * 3
                        + [(200, {"response": {"id": 9}})] * 3)
        ex = full_extraction(scheduled=False, details=None)                 # comment only
        for _ in range(3):
            fs.sync_consultation(job(), ex)
        fs.sync_consultation(job(), ex)                                     # cap: not sent
        self.assertEqual(len(self.sent), 3)
        out = fs.sync_consultation(job(), ex, manual=True)
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(out["status"], "succeeded")

    def test_skipped_task_stays_skipped_and_no_customer_id(self):
        self.dry_run_first()
        self.sendable(mode="live")
        self.allow_send([(200, {"response": {"id": 1}})])
        out = fs.sync_consultation(job(), full_extraction(scheduled=False, details=None),
                                   manual=True)
        self.assertEqual([r["path"] for r in self.sent], ["/comments"])
        self.assertEqual(out["status"], "succeeded")
        out = fs.sync_consultation(job(customer_id="cust_ab12"), empty_extraction(), manual=True)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(len(self.sent), 1)


class TestWorkerManualSend(unittest.TestCase):
    def setUp(self):
        import worker
        self.w = worker
        self.saved = []

    def run_it(self, jobrow, sync=None):
        with mock.patch.object(self.w, "get_job", return_value=jobrow), \
                mock.patch.object(self.w, "save_manual_send",
                                  side_effect=lambda jid, e, sync=None:
                                  self.saved.append((jid, e, sync)) or True), \
                mock.patch.object(self.w, "sync_consultation",
                                  side_effect=sync or (lambda j, ex, manual: {
                                      "status": "dry_run", "mode": "dry_run", "manual": manual,
                                      "message": "Not sent: write mode is dry_run."})), \
                mock.patch.object(self.w, "update_job_status",
                                  side_effect=AssertionError("must not change job status")):
            self.w.send_consultation_to_fieldpulse("job_abc", "ms_1")

    def test_records_outcome_and_new_summary(self):
        self.run_it(done_job())
        jid, entry, sync = self.saved[-1]
        self.assertEqual((jid, entry["id"], entry["status"], entry["outcome"]),
                         ("job_abc", "ms_1", "finished", "dry_run"))
        self.assertEqual(entry["message"], "Not sent: write mode is dry_run.")
        self.assertTrue(sync["manual"])

    def test_unfinished_job_and_crash_are_recorded(self):
        self.run_it(job(status="failed", result=None))
        self.assertEqual(self.saved[-1][1]["status"], "error")
        self.assertIsNone(self.saved[-1][2])

        def boom(*a, **k):
            raise RuntimeError("db down")
        self.run_it(done_job(), sync=boom)                                  # never raises
        self.assertEqual(self.saved[-1][1]["status"], "error")
        self.assertIn("RuntimeError", self.saved[-1][1]["message"])
        self.saved.clear()
        self.run_it(None)
        self.assertEqual(self.saved, [])


# --- storage.generate_download_url (real module, local signing only) ----------------------

class TestDownloadUrl(unittest.TestCase):
    def test_presigned_get(self):
        env = {"R2_ENDPOINT_URL": "https://acct.r2.cloudflarestorage.com",
               "R2_ACCESS_KEY_ID": "AKIATEST", "R2_SECRET_ACCESS_KEY": "s3cr3t",
               "R2_BUCKET_NAME": "bucket"}
        with mock.patch.dict(os.environ, env):
            spec = importlib.util.spec_from_file_location("storage_real", _HERE / "storage.py")
            real = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(real)
            url = real.generate_download_url("consultations/abc.webm")
            self.assertIn("consultations/abc.webm", url)
            self.assertIn("Expires=900", url.replace("X-Amz-Expires", "Expires"))
            self.assertNotIn("s3cr3t", url)
            for bad in ("other/abc.webm", "consultations/../x", None):
                with self.assertRaises(real.StorageError):
                    real.generate_download_url(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
