"""Offline tests for fieldpulse_sync.py (stage 2: dry run), the worker's paste
path + save-first ordering, and the paste branch of /consultation/submit.

No network (every non-loopback socket connect raises), no dotenv, no Postgres: the write
store is an in-memory fake with the same semantics as the SQL in jobs.py
(see test_fieldpulse_writes_db.py for the real-Postgres check). Run:

    python -m unittest test_fieldpulse_sync -v
"""

import os
import socket
import sys
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False
for _name in list(os.environ):
    if any(w in _name for w in ("CLERK", "ANTHROPIC", "R2_", "DATABASE", "REDIS",
                                "FIELDPULSE", "AWS_", "DEEPGRAM", "ADMIN_BASE")):
        os.environ.pop(_name)
os.environ.update(ANTHROPIC_API_KEY="dummy", DEEPGRAM_API_KEY="dummy",
                  CLERK_SECRET_KEY="sk_test_dummy", AWS_EC2_METADATA_DISABLED="true")


_REAL_CONNECT = socket.socket.connect


def _no_network(self, address):
    # Loopback stays allowed so test_customer_source's local-server tests work
    # whichever file is imported first; nothing else can be reached.
    host = address[0] if isinstance(address, tuple) else None
    if host in ("127.0.0.1", "::1"):
        return _REAL_CONNECT(self, address)
    raise RuntimeError(f"network blocked in tests: {address!r}")


socket.socket.connect = _no_network

# Stub DB / queue / storage modules unless another test file already did.
if "jobs" not in sys.modules:
    _jobs = types.ModuleType("jobs")
    _jobs.create_job = lambda **kw: {"id": "job-1", "status": "queued"}
    _jobs.get_job = lambda *a: None
    _jobs.get_customer_link = lambda *a: None
    _jobs.save_customer_link = lambda *a, **k: None
    _jobs.list_recent_jobs = lambda *a: []
    _jobs.update_job_status = lambda *a, **k: None
    _jobs.JobError = Exception
    sys.modules["jobs"] = _jobs
if "queue_setup" not in sys.modules:
    _q = types.ModuleType("queue_setup")
    _q.enqueue_consultation = lambda job_id: None
    sys.modules["queue_setup"] = _q
if "storage" not in sys.modules:
    _s = types.ModuleType("storage")
    _s.StorageError = type("StorageError", (Exception,), {})
    _s.audio_exists = lambda key: True
    _s.generate_consultation_key = lambda fn: "consultations/abc-123.webm"
    _s.generate_upload_url = lambda key, ct: "https://r2.invalid/put"
    _s.download_audio = lambda *a, **k: None
    sys.modules["storage"] = _s

import fieldpulse_sync as fs  # noqa: E402

CHI = ZoneInfo("America/Chicago")
REF = date(2026, 11, 3)


class FakeStore:
    """Same rules as jobs.py's SQL: record never overwrites succeeded/sent, nor
    a dry_run row from another mode; claim only planned/failed non-dry_run rows
    under the attempt cap; finish only a sent row."""

    def __init__(self):
        self.rows = {}
        self.order = []

    def list_fieldpulse_writes(self, job_id):
        return [dict(self.rows[k]) for k in self.order if k[0] == job_id]

    def record_fieldpulse_write(self, job_id, step, status, mode, request=None, error=None):
        assert status in ("planned", "skipped")
        key = (job_id, step)
        row = self.rows.get(key)
        if row is None:
            self.order.append(key)
            self.rows[key] = {"job_id": job_id, "step": step, "status": status, "mode": mode,
                              "request": request, "response": None,
                              "fieldpulse_record_id": None, "error": error, "attempts": 0}
        elif row["status"] not in ("succeeded", "sent") and not (
                row["mode"] == "dry_run" and mode != "dry_run"):
            row.update(status=status, mode=mode, request=request, error=error)

    def claim_fieldpulse_write(self, job_id, step, max_attempts):
        row = self.rows.get((job_id, step))
        if (row and row["status"] in ("planned", "failed") and row["attempts"] < max_attempts
                and row["mode"] != "dry_run"):
            row.update(status="sent", attempts=row["attempts"] + 1, error=None, response=None)
            return True
        return False

    def finish_fieldpulse_write(self, job_id, step, status, response=None, record_id=None,
                                error=None):
        row = self.rows.get((job_id, step))
        if row and row["status"] == "sent":
            row.update(status=status, response=response, fieldpulse_record_id=record_id,
                       error=error)


def full_extraction(**ra):
    return {
        "project_type": "kitchen",
        "notes_summary": "Homeowners want an open kitchen. Ready to go in September.",
        "scope": {
            "project_vision": "Open up the half-wall to the dining room",
            "structural_changes": "yes", "structural_details": "remove half-wall",
            "appliances": {"refrigerator": "reinstall", "range": "new", "dishwasher": "new",
                           "microwave": "new", "hood": "new", "other": None},
            "plumbing_changes": "no", "plumbing_details": None,
            "electrical_changes": "yes", "electrical_details": "outlets on island",
            "lighting_changes": "yes", "lighting_details": "recessed + under-cabinet",
            "style": "Bright and open, white shaker",
        },
        "readiness": {"time_frame": "Start in September", "thinking_duration": "Over a year",
                      "work_done_so_far": "Pinterest boards"},
        "sales": {"budget_raw": "45 to 55 thousand", "budget_low": 45000, "budget_high": 55000,
                  "likelihood_percent": 85, "close_score_1_10": 9,
                  "waiting_on_financing": "no", "financing_details": "pay out of pocket",
                  "concerns": "Timeline around the holidays",
                  "process_presentation_shown": True},
        "return_appointment": ra or {"scheduled": True,
                                     "details": "Thursday, November 12 at 6 PM"},
        "raw_quotes": ["I'd say a nine."],
        "flags": ["client name not stated"],
    }


def empty_extraction():
    return {
        "project_type": "unknown", "notes_summary": "Short chat, little covered.",
        "scope": {"project_vision": None, "structural_changes": "unknown",
                  "plumbing_changes": "unknown", "electrical_changes": "unknown",
                  "lighting_changes": "unknown", "style": None, "appliances": None},
        "readiness": {"time_frame": None, "thinking_duration": None, "work_done_so_far": None},
        "sales": {"budget_raw": None, "budget_low": None, "budget_high": None,
                  "likelihood_percent": None, "close_score_1_10": None,
                  "waiting_on_financing": "unknown", "concerns": None},
        "return_appointment": {"scheduled": None, "details": None},
        "raw_quotes": [], "flags": ["budget not discussed", "close score (1-10) not asked"],
    }


def job(customer_id="18809123", **kw):
    return {"id": "job_abc", "customer_id": customer_id,
            "customer": {"id": customer_id, "display_name": "Jane Doe"},
            "created_at": "2026-11-03T20:00:00+00:00", **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        p = mock.patch.object(fs, "_STORE", self.store)
        p.start()
        self.addCleanup(p.stop)
        self.env = {}
        pe = mock.patch.dict(os.environ, self.env, clear=False)
        pe.start()
        self.addCleanup(pe.stop)
        for k in [k for k in os.environ if k.startswith("FIELDPULSE") or k == "ADMIN_BASE_URL"]:
            os.environ.pop(k)
        # Any send attempt fails the test unless a test replaces this.
        self.sent = []
        ps = mock.patch.object(fs, "_send", side_effect=AssertionError("must not send"))
        self.send = ps.start()
        self.addCleanup(ps.stop)

    def setenv(self, **kw):
        os.environ.update({k: str(v) for k, v in kw.items()})

    def allow_send(self, responses):
        responses = list(responses)

        def fake(request, cfg):
            self.sent.append(request)
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        self.send.side_effect = fake

    def sendable(self, mode="test_customer_only", test_id="18809123"):
        self.setenv(FIELDPULSE_WRITE_MODE=mode, FIELDPULSE_WRITE_BASE_URL="https://fp.invalid/stage",
                    FIELDPULSE_WRITE_API_KEY="secret-key-xyz", FIELDPULSE_TEST_CUSTOMER_ID=test_id)


# --- Return appointment parsing + time conversion --------------------------------

class TestReturnAppointment(Base):
    def parse(self, details, scheduled=True, ref=REF, tz=CHI):
        return fs.parse_return_appointment({"scheduled": scheduled, "details": details}, ref, tz)

    def test_usable_dates(self):
        cases = {
            "Thursday, November 12 at 6 PM": (datetime(2026, 11, 12, 18, tzinfo=CHI), 60),
            "2026-11-12 7:00 PM": (datetime(2026, 11, 12, 19, tzinfo=CHI), 60),
            "11/12 at 6pm": (datetime(2026, 11, 12, 18, tzinfo=CHI), 60),
            "12th of November at noon": (datetime(2026, 11, 12, 12, tzinfo=CHI), 60),
            "Nov 12, 6-7:30 pm": (datetime(2026, 11, 12, 18, tzinfo=CHI), 90),
            "November 12 in the morning": (datetime(2026, 11, 12, 9, tzinfo=CHI), 180),
        }
        for text, (start, minutes) in cases.items():
            with self.subTest(text=text):
                window, reason = self.parse(text)
                self.assertIsNone(reason)
                self.assertEqual(window[0], start)
                self.assertEqual((window[1] - window[0]).total_seconds(), minutes * 60)

    def test_vague_or_missing_is_skipped_with_reason(self):
        cases = ["next Thursday at six", "November 12", "November 12 at 10:00",
                 "Friday, November 12 at 6pm", "Nov 12 at 6 or 7 pm", "October 1 at 6pm",
                 "October 1, 2026 at 6pm", "Nov 31 at 6pm", "Dec 2 at 9am and Dec 3 at 9am",
                 "the 12th at 6pm", "", None]
        for text in cases:
            with self.subTest(text=text):
                window, reason = self.parse(text)
                self.assertIsNone(window)
                self.assertTrue(reason)

    def test_not_scheduled_and_not_mentioned(self):
        self.assertEqual(self.parse("Nov 12 6pm", scheduled=False)[1],
                         "no return appointment was scheduled")
        self.assertEqual(self.parse("Nov 12 6pm", scheduled=None)[1],
                         "return appointment was not mentioned")
        self.assertIsNone(fs.parse_return_appointment(None, REF, CHI)[0])
        self.assertIsNone(fs.parse_return_appointment("garbage", REF, CHI)[0])

    def test_epoch_seconds_follow_dst(self):
        # CST (UTC-6) in November, CDT (UTC-5) after 2027-03-14.
        nov, _ = self.parse("2026-11-12 6 pm")
        mar, _ = self.parse("2027-03-15 6 pm")
        self.assertEqual(int(nov[0].timestamp()), 1794528000)  # 2026-11-13 00:00 UTC
        self.assertEqual(int(mar[0].timestamp()), 1805151600)  # 2027-03-15 23:00 UTC

    def test_timezone_env_changes_epoch(self):
        ex = full_extraction(scheduled=True, details="2026-11-12 6 pm")
        chi = fs.build_plan(job(), ex, fs._config())[1]["request"]["body"]["start_time"]
        self.setenv(FIELDPULSE_TIMEZONE="America/New_York")
        ny = fs.build_plan(job(), ex, fs._config())[1]["request"]["body"]["start_time"]
        self.assertEqual(chi - ny, 3600)
        self.setenv(FIELDPULSE_TIMEZONE="Not/AZone")
        self.assertEqual(fs.build_plan(job(), ex, fs._config())[1]["request"]["body"]
                         ["start_time"], chi)


# --- Comment text -----------------------------------------------------------------

class TestCommentText(Base):
    def text(self, ex, j=None):
        return fs.build_plan(j or job(), ex, fs._config())[0]["request"]["body"]["text"]

    def test_full_comment_sections(self):
        t = self.text(full_extraction())
        for needle in ("AI consultation summary — Tuesday, November 3, 2026",
                       "Kitchen consultation", "SUMMARY", "Homeowners want an open kitchen",
                       "Close score: 9/10", "Likelihood: 85%",
                       'Budget: $45,000–$55,000 ("45 to 55 thousand")',
                       "Not waiting on financing: pay out of pocket",
                       "SCOPE HIGHLIGHTS", "Structural: changes wanted (remove half-wall)",
                       "Plumbing: no changes", "new range, dishwasher, microwave, hood",
                       "reinstall refrigerator", "TIMELINE", "Time frame: Start in September",
                       "RETURN APPOINTMENT", "Thursday, November 12, 2026, 6:00 PM–7:00 PM",
                       "AI FLAGS", "client name not stated"):
            self.assertIn(needle, t)
        self.assertNotIn("NOT MENTIONED", t)
        self.assertNotIn("Full transcript", t)          # ADMIN_BASE_URL unset

    def test_nulls_are_flagged_never_invented(self):
        t = self.text(empty_extraction())
        self.assertNotIn("/10", t)
        self.assertNotRegex(t, r"\d\s*%")
        self.assertNotIn("$", t)
        self.assertIn("not asked\n\nWritten by AI", t)      # blank line before footer
        self.assertNotIn("SALES READ", t)
        missing = t.split("NOT MENTIONED IN THE CONSULTATION")[1]
        for item in ("Close score (1-10)", "Likelihood %", "Budget", "Financing",
                     "Project vision", "Structural changes", "Plumbing", "Electrical",
                     "Lighting", "Style", "Time frame", "Return appointment"):
            self.assertIn(f"- {item}", missing)
        self.assertIn("- budget not discussed", t)
        self.assertIn("RETURN APPOINTMENT\n- Not mentioned.", t)

    def test_bad_number_types_are_not_shown(self):
        ex = full_extraction()
        ex["sales"].update(close_score_1_10="9", likelihood_percent=True, budget_low=None,
                           budget_high=None, budget_raw=None)
        ex["sales"]["close_score_1_10"] = 42
        t = self.text(ex)
        self.assertNotIn("Close score:", t)
        self.assertNotIn("Likelihood:", t)
        self.assertIn("- Close score (1-10)", t)

    def test_vague_appointment_explained_in_comment(self):
        t = self.text(full_extraction(scheduled=True, details="next Thursday at six"))
        self.assertIn('Scheduled: "next Thursday at six"', t)
        self.assertIn("No FieldPulse task was created: no exact calendar date was given", t)

    def test_admin_footer_and_truncation(self):
        self.setenv(ADMIN_BASE_URL="https://admin.example.com/")
        ex = full_extraction()
        ex["notes_summary"] = "word " * 400
        ex["flags"] = [f"flag {i} " + "x" * 280 for i in range(40)]
        t = self.text(ex)
        self.assertLessEqual(len(t), fs.MAX_COMMENT_CHARS)
        self.assertTrue(t.endswith("Full transcript and details: "
                                   "https://admin.example.com/jobs/job_abc"))
        ex["scope"]["project_vision"] = "v" * 50000
        ex["readiness"]["time_frame"] = "\n".join(["t" * 500] * 50)
        t = self.text(ex)
        self.assertLessEqual(len(t), fs.MAX_COMMENT_CHARS)
        self.assertIn("[Trimmed to fit.", t)
        self.assertIn("Full transcript and details:", t)

    def test_garbage_extraction_does_not_crash(self):
        for ex in ({}, None, {"sales": "x", "scope": [], "flags": "abc",
                              "return_appointment": 5}):
            with self.subTest(ex=ex):
                self.assertIn("AI consultation summary", self.text(ex))


# --- Plan --------------------------------------------------------------------------

class TestPlan(Base):
    def test_comment_and_unassigned_task_requests(self):
        plan = fs.build_plan(job(), full_extraction(), fs._config())
        self.assertEqual([s["step"] for s in plan], ["comment", "return_task"])
        c = plan[0]["request"]
        self.assertEqual((c["method"], c["path"]), ("POST", "/comments"))
        self.assertEqual(c["body"]["commentable_type"], "customer")
        self.assertEqual(c["body"]["commentable_id"], 18809123)
        t = plan[1]["request"]
        self.assertEqual((t["method"], t["path"]), ("POST", "/jobs"))
        body = t["body"]
        self.assertEqual(body["type"], "task")
        self.assertEqual(body["customer_id"], 18809123)
        self.assertNotIn("assignments", body)
        self.assertNotIn("tags", body)
        self.assertEqual(body["start_time"], 1794528000)
        self.assertEqual(body["end_time"] - body["start_time"], 3600)
        for r in (c, t):
            self.assertEqual(set(r), {"method", "path", "body"})   # never headers

    def test_tag_and_commentable_type_env(self):
        self.setenv(FIELDPULSE_AI_JOB_TAG_ID="558112",
                    FIELDPULSE_COMMENT_COMMENTABLE_TYPE="App\\Customer")
        plan = fs.build_plan(job(), full_extraction(), fs._config())
        self.assertEqual(plan[1]["request"]["body"]["tags"], [558112])
        self.assertEqual(plan[0]["request"]["body"]["commentable_type"], "App\\Customer")
        self.setenv(FIELDPULSE_AI_JOB_TAG_ID="not-a-number")
        plan = fs.build_plan(job(), full_extraction(), fs._config())
        self.assertNotIn("tags", plan[1]["request"]["body"])

    def test_no_usable_date_means_no_task_request(self):
        plan = fs.build_plan(job(), full_extraction(scheduled=True, details="soon"),
                             fs._config())
        self.assertIsNone(plan[1]["request"])
        self.assertEqual(plan[1]["skip_reason"], "no exact calendar date was given")


# --- Mode gating -------------------------------------------------------------------

class TestModes(Base):
    def run_sync(self, j=None, ex=None):
        return fs.sync_consultation(j or job(), ex if ex is not None else full_extraction())

    def test_default_is_dry_run_and_never_sends(self):
        with mock.patch("requests.post", side_effect=AssertionError("network")) as post, \
                mock.patch("requests.request", side_effect=AssertionError("network")):
            out = self.run_sync()
            post.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(out["status"], "dry_run")
        self.assertEqual(out["mode"], "dry_run")
        self.assertTrue(out["message"].startswith("Dry run: nothing sent to FieldPulse yet. "
                                                  "Would add a comment to Jane Doe and create "
                                                  "a task for Thursday, November 12, 2026"))
        rows = self.store.list_fieldpulse_writes("job_abc")
        self.assertEqual([(r["step"], r["status"]) for r in rows],
                         [("comment", "planned"), ("return_task", "planned")])
        self.assertEqual(rows[0]["request"]["path"], "/comments")
        self.assertIn("Close score: 9/10", out["comment_text"])

    def test_dry_run_with_skipped_task(self):
        out = self.run_sync(ex=empty_extraction())
        self.assertIn("Would add a comment to Jane Doe.", out["message"])
        self.assertIn("No task: return appointment was not mentioned.", out["message"])
        rows = {r["step"]: r for r in self.store.list_fieldpulse_writes("job_abc")}
        self.assertEqual(rows["return_task"]["status"], "skipped")
        self.assertIsNone(rows["return_task"]["request"])

    def test_dry_run_even_with_full_write_config(self):
        self.sendable(mode="dry_run")
        self.assertEqual(self.run_sync()["status"], "dry_run")
        self.send.assert_not_called()

    def test_unknown_mode_falls_back_to_dry_run(self):
        for raw in ("LIVE!", "true", "send", "test-customer-only", " "):
            with self.subTest(raw=raw):
                self.sendable()
                self.setenv(FIELDPULSE_WRITE_MODE=raw)
                self.assertEqual(self.run_sync()["status"], "dry_run")
        self.send.assert_not_called()

    def test_mode_is_case_and_space_insensitive(self):
        self.setenv(FIELDPULSE_WRITE_MODE="  OFF ")
        self.assertEqual(self.run_sync()["status"], "off")

    def test_off_records_nothing(self):
        self.sendable(mode="off")
        out = self.run_sync()
        self.assertEqual(out["status"], "off")
        self.assertEqual(self.store.rows, {})
        self.send.assert_not_called()

    def test_test_customer_only_refuses_other_customers(self):
        self.sendable(test_id="999")
        out = self.run_sync()
        self.assertEqual(out["status"], "blocked")
        self.assertIn("not the test customer", out["message"])
        self.send.assert_not_called()
        rows = self.store.list_fieldpulse_writes("job_abc")
        self.assertTrue(all(r["status"] == "skipped" for r in rows))

    def test_test_customer_only_needs_test_id_and_write_config(self):
        for drop in ("FIELDPULSE_TEST_CUSTOMER_ID", "FIELDPULSE_WRITE_BASE_URL",
                     "FIELDPULSE_WRITE_API_KEY"):
            with self.subTest(drop=drop):
                self.sendable()
                os.environ.pop(drop)
                self.assertEqual(self.run_sync()["status"], "blocked")
        self.sendable(test_id="cust_abc")
        self.assertEqual(self.run_sync()["status"], "blocked")
        self.send.assert_not_called()

    def test_live_without_write_config_is_blocked_and_ignores_read_and_mock_vars(self):
        self.setenv(FIELDPULSE_WRITE_MODE="live",
                    FIELDPULSE_READ_BASE_URL="https://real.invalid", FIELDPULSE_READ_API_KEY="r",
                    FIELDPULSE_BASE_URL="https://mock.invalid", FIELDPULSE_API_KEY="m")
        out = self.run_sync()
        self.assertEqual(out["status"], "blocked")
        self.send.assert_not_called()
        src = (_HERE / "fieldpulse_sync.py").read_text()
        self.assertNotIn('environ.get("FIELDPULSE_READ', src)
        self.assertNotIn('environ.get("FIELDPULSE_BASE_URL', src)
        self.assertNotIn('environ.get("FIELDPULSE_API_KEY', src)

    def test_mock_or_missing_customer_id_is_skipped(self):
        for cid in ("cust_ab12", None, "", "0", "-5"):
            with self.subTest(cid=cid):
                self.sendable(mode="live")
                out = self.run_sync(j=job(customer_id=cid))
                self.assertEqual(out["status"], "skipped")
                self.assertEqual(out["steps"], [])
        self.send.assert_not_called()
        self.assertEqual(self.store.rows, {})

    def test_test_customer_sends_and_records_ids(self):
        self.sendable()
        self.allow_send([(200, {"error": False, "response": {"id": 777}}),
                         (201, {"error": False, "response": {"id": 888}})])
        out = self.run_sync()
        self.assertEqual(out["status"], "succeeded")
        self.assertEqual([r["path"] for r in self.sent], ["/comments", "/jobs"])
        rows = {r["step"]: r for r in self.store.list_fieldpulse_writes("job_abc")}
        self.assertEqual(rows["comment"]["fieldpulse_record_id"], "777")
        self.assertEqual(rows["return_task"]["fieldpulse_record_id"], "888")
        self.assertEqual(rows["comment"]["attempts"], 1)
        self.assertIn("Added a comment to Jane Doe and created a task", out["message"])

    def test_live_sends_for_any_customer(self):
        self.sendable(mode="live", test_id="1")
        self.allow_send([(200, {"error": False, "response": {"id": 1}})] * 2)
        self.assertEqual(self.run_sync()["status"], "succeeded")


# --- Idempotency + error classification -------------------------------------------

class TestIdempotency(Base):
    def setUp(self):
        super().setUp()
        self.sendable()

    def test_succeeded_step_is_never_resent(self):
        self.allow_send([(200, {"error": False, "response": {"id": 1}})] * 2)
        fs.sync_consultation(job(), full_extraction())
        self.assertEqual(len(self.sent), 2)
        for mode in ("test_customer_only", "live", "dry_run"):
            self.setenv(FIELDPULSE_WRITE_MODE=mode)
            out = fs.sync_consultation(job(), full_extraction())
            self.assertEqual(len(self.sent), 2)
        rows = self.store.list_fieldpulse_writes("job_abc")
        self.assertTrue(all(r["status"] == "succeeded" for r in rows))
        self.assertEqual(out["steps"][0]["status"], "succeeded")

    def test_no_answer_stays_sent_and_is_not_retried(self):
        self.allow_send([fs._OutcomeUnknown("ReadTimeout: no response"),
                         (200, {"error": False, "response": {"id": 2}})])
        out = fs.sync_consultation(job(), full_extraction())
        self.assertEqual(out["status"], "partial")
        self.assertIn("may or may not have been created", out["steps"][0]["error"])
        fs.sync_consultation(job(), full_extraction())
        self.assertEqual(len(self.sent), 2)                # nothing re-sent

    def test_http_failure_is_retried_up_to_cap(self):
        self.allow_send([(500, {"message": "boom"})] * 2 + [(400, "bad")] * 4)
        out = fs.sync_consultation(job(), full_extraction())
        self.assertEqual(out["status"], "failed")
        self.assertIn("HTTP 500: boom", out["steps"][0]["error"])
        for _ in range(4):
            fs.sync_consultation(job(), full_extraction())
        rows = {r["step"]: r for r in self.store.list_fieldpulse_writes("job_abc")}
        self.assertEqual(rows["comment"]["attempts"], fs._MAX_ATTEMPTS)
        self.assertEqual(len(self.sent), 2 * fs._MAX_ATTEMPTS)

    def test_error_wrapper_with_200_is_failure(self):
        self.allow_send([(200, {"error": True, "message": "nope"}),
                         (200, {"message": "Missing Authentication Token"})])
        out = fs.sync_consultation(job(), full_extraction())
        self.assertEqual(out["steps"][0]["status"], "failed")
        self.assertEqual(out["steps"][1]["status"], "succeeded")   # no id, but 2xx
        self.assertIsNone(out["steps"][1]["record_id"])

    def test_not_delivered_is_failed(self):
        self.allow_send([fs._NotDelivered("connect timeout"), (200, {"response": {"id": 3}})])
        out = fs.sync_consultation(job(), full_extraction())
        self.assertEqual(out["steps"][0]["status"], "failed")
        self.assertEqual(out["status"], "partial")

    def test_store_crash_never_raises(self):
        broken = mock.Mock()
        broken.list_fieldpulse_writes.side_effect = RuntimeError("db down")
        with mock.patch.object(fs, "_STORE", broken):
            out = fs.sync_consultation(job(), full_extraction())
        self.assertEqual(out["status"], "failed")
        self.assertIn("RuntimeError", out["error"])
        self.assertEqual(self.sent, [])


class TestTransport(unittest.TestCase):
    """The real _send, with requests.post patched (no socket is opened)."""

    def setUp(self):
        self.store = FakeStore()
        p = mock.patch.object(fs, "_STORE", self.store)
        p.start()
        self.addCleanup(p.stop)
        pe = mock.patch.dict(os.environ, {}, clear=False)
        pe.start()
        self.addCleanup(pe.stop)

    def cfg(self, mode="live"):
        c = fs._config()
        c.update(mode=mode, base_url="https://fp.invalid/stage", api_key="k-123")
        return c

    def test_headers_redirects_and_refusals(self):
        import requests
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"response": {"id": 5}}
        with mock.patch.object(requests, "post", return_value=resp) as post:
            code, payload = fs._send({"method": "POST", "path": "/comments", "body": {}},
                                     self.cfg())
            self.assertEqual(code, 200)
            args, kw = post.call_args
            self.assertEqual(args[0], "https://fp.invalid/stage/comments")
            self.assertEqual(kw["headers"]["x-api-key"], "k-123")
            self.assertIs(kw["allow_redirects"], False)
            for mode in ("dry_run", "off"):
                with self.assertRaises(fs._NotDelivered):
                    fs._send({"method": "POST", "path": "/x", "body": {}}, self.cfg(mode))
            with self.assertRaises(fs._NotDelivered):
                fs._send({"method": "PUT", "path": "/x", "body": {}}, self.cfg())
            self.assertEqual(post.call_count, 1)

    def test_exception_classification(self):
        import requests
        with mock.patch.object(requests, "post", side_effect=requests.ConnectTimeout("x")):
            with self.assertRaises(fs._NotDelivered):
                fs._send({"method": "POST", "path": "/x", "body": {}}, self.cfg())
        for exc in (requests.ReadTimeout("x"), requests.ConnectionError("reset")):
            with mock.patch.object(requests, "post", side_effect=exc):
                with self.assertRaises(fs._OutcomeUnknown):
                    fs._send({"method": "POST", "path": "/x", "body": {}}, self.cfg())

    def test_key_never_stored(self):
        import requests
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"error": False, "response": {"id": 9}}
        os.environ.update(FIELDPULSE_WRITE_MODE="live",
                          FIELDPULSE_WRITE_BASE_URL="https://fp.invalid/stage",
                          FIELDPULSE_WRITE_API_KEY="k-SECRET-123")
        with mock.patch.object(requests, "post", return_value=resp):
            out = fs.sync_consultation(job(), full_extraction())
        self.assertEqual(out["status"], "succeeded")
        self.assertNotIn("k-SECRET-123", repr(self.store.rows))
        self.assertNotIn("k-SECRET-123", repr(out))


# --- Worker -------------------------------------------------------------------------

class TestWorker(unittest.TestCase):
    def setUp(self):
        import worker
        self.w = worker
        self.calls = []
        self.refs = []

    def run_job(self, jobrow, sync=None):
        w = self.w
        sync = sync or (lambda j, ex: {"status": "dry_run", "steps": []})
        with mock.patch.object(w, "get_job", return_value=jobrow), \
                mock.patch.object(w, "update_job_status",
                                  side_effect=lambda *a, **k: self.calls.append(("update", a, k))), \
                mock.patch.object(w, "download_audio",
                                  side_effect=lambda *a: self.calls.append(("download",))), \
                mock.patch.object(w, "transcribe",
                                  side_effect=lambda p: (self.calls.append(("transcribe",))
                                                         or ("audio text", 12.0))), \
                mock.patch.object(w, "extract_consultation",
                                  side_effect=lambda t, **kw: (self.calls.append(("extract", t))
                                                               or self.refs.append(kw)
                                                               or {"project_type": "bath"})), \
                mock.patch.object(w, "sync_consultation",
                                  side_effect=lambda j, ex: (self.calls.append(("sync", j, ex))
                                                             or sync(j, ex))):
            w.process_consultation_job(jobrow["id"])

    def test_paste_skips_transcription_and_saves_first(self):
        self.run_job({"id": "job_p", "audio_key": None, "transcript": "  REP: hi there  ",
                      "customer_id": "42", "customer": {"id": "42"}})
        kinds = [c[0] for c in self.calls]
        self.assertNotIn("transcribe", kinds)
        self.assertNotIn("download", kinds)
        self.assertEqual(self.calls[1], ("extract", "REP: hi there"))
        # processing -> processing(with result) -> sync -> done
        self.assertEqual(kinds, ["update", "extract", "update", "sync", "update"])
        saved = self.calls[2][2]["result"]
        self.assertEqual(self.calls[2][1][1], "processing")
        self.assertEqual(saved["input"], "paste")
        self.assertEqual(saved["transcript"], "REP: hi there")
        done = self.calls[4]
        self.assertEqual(done[1][1], "done")
        self.assertEqual(done[2]["result"]["fieldpulse_sync"]["status"], "dry_run")
        self.assertIsNone(done[2]["result"]["transcription_seconds"])

    def test_audio_path_still_transcribes(self):
        self.run_job({"id": "job_a", "audio_key": "consultations/a-1.webm",
                      "customer_id": "111", "customer": {"id": "111"}})
        kinds = [c[0] for c in self.calls]
        self.assertEqual(kinds[:4], ["update", "download", "transcribe", "extract"])
        self.assertEqual(self.calls[-1][2]["result"]["input"], "audio")
        self.assertEqual(self.calls[-1][2]["result"]["transcription_seconds"], 12.0)

    def test_sync_exception_still_ends_done(self):
        def boom(j, ex):
            raise RuntimeError("bug")
        self.run_job({"id": "job_x", "transcript": "REP: hello world", "customer_id": "1"},
                     sync=boom)
        last = self.calls[-1]
        self.assertEqual(last[1][1], "done")
        self.assertEqual(last[2]["result"]["fieldpulse_sync"]["status"], "failed")

    def test_no_input_fails(self):
        from jobs import JobError
        with self.assertRaises(JobError):
            self.run_job({"id": "job_n", "audio_key": None, "transcript": "   "})
        self.assertEqual(self.calls[-1][1][1], "failed")
        self.assertNotIn("extract", [c[0] for c in self.calls])

    def test_paste_and_audio_both_get_the_consultation_date(self):
        for row in ({"id": "job_p", "transcript": "REP: hi there", "customer_id": "42",
                     "created_at": "2026-11-04T23:30:00+00:00"},
                    {"id": "job_a", "audio_key": "consultations/a-1.webm", "customer_id": "42",
                     "created_at": "2026-11-05T03:30:00+00:00"}):   # 9:30 PM Nov 4 in Chicago
            self.refs.clear()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FIELDPULSE_TIMEZONE", None)
                self.run_job(row)
            self.assertEqual(self.refs, [{"reference_date": date(2026, 11, 4),
                                          "reference_tz": "America/Chicago"}])

    def test_worker_never_calls_mock_writer(self):
        src = (_HERE / "worker.py").read_text()
        self.assertNotIn("write_consultation_to_fieldpulse", src)
        self.assertNotIn("import consultation_writer", src)
        self.assertNotIn("from consultation_writer", src)


# --- Submit route: paste branch ------------------------------------------------------

class TestSubmitPaste(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import orchestrator
        cls.orch = orchestrator
        orchestrator.app.before_request_funcs[None] = [
            f for f in orchestrator.app.before_request_funcs.get(None, [])
            if f.__name__ != "_require_clerk_session"
        ]
        cls.client = orchestrator.app.test_client()

    def setUp(self):
        self.created = []
        p1 = mock.patch.object(self.orch, "create_job",
                               side_effect=lambda **kw: (self.created.append(kw)
                                                         or {"id": "job-1", "status": "queued"}))
        p2 = mock.patch.object(self.orch, "enqueue_consultation")
        p3 = mock.patch.object(self.orch, "audio_exists", return_value=True)
        self.enq = p2.start()
        p1.start()
        self.exists = p3.start()
        self.addCleanup(mock.patch.stopall)

    def post(self, **body):
        return self.client.post("/consultation/submit", json=body)

    def test_paste_creates_job_without_audio(self):
        r = self.post(transcript="  REP: Hello. HOMEOWNER: Hi there.  ", customer_id=42,
                      customer={"id": 42, "display_name": "Jane"}, source="3day-app-paste")
        self.assertEqual(r.status_code, 202, r.get_json())
        kw = self.created[-1]
        self.assertIsNone(kw["audio_key"])
        self.assertEqual(kw["transcript"], "REP: Hello. HOMEOWNER: Hi there.")
        self.assertEqual(kw["customer_id"], "42")
        self.assertEqual(kw["source"], "3day-app-paste")
        self.exists.assert_not_called()
        self.enq.assert_called_once_with("job-1")

    def test_paste_validation(self):
        cases = [
            ({"transcript": "x" * 30, "audio_key": "consultations/abc-123.webm"}, 400),
            ({"transcript": ""}, 400), ({"transcript": "   "}, 400),
            ({"transcript": 123}, 400), ({"transcript": ["a"]}, 400),
            ({"transcript": "too short"}, 400),
            ({"transcript": "x" * (self.orch._MAX_TRANSCRIPT_CHARS + 1)}, 413),
            ({"transcript": "x" * 30, "customer_id": "abc"}, 400),
            ({"transcript": "x" * 30, "customer_id": 1, "customer": {"id": 2}}, 400),
            ({}, 400), ({"audio_key": 5}, 400),
        ]
        for body, code in cases:
            with self.subTest(body={k: (v[:20] if isinstance(v, str) else v)
                                    for k, v in body.items()}):
                self.assertEqual(self.post(**body).status_code, code)
        self.assertEqual(self.created, [])
        self.enq.assert_not_called()

    def test_audio_path_unchanged(self):
        r = self.post(audio_key="consultations/abc-123.webm", customer_id=7)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.created[-1]["audio_key"], "consultations/abc-123.webm")
        self.assertIsNone(self.created[-1]["transcript"])
        self.exists.return_value = False
        self.assertEqual(self.post(audio_key="consultations/abc-123.webm").status_code, 404)



# --- Stage 2 follow-up: consultation date -> extractor; AI-resolved return dates -------

def ra(details, rd=None, rt=None, scheduled=True):
    out = {"scheduled": scheduled, "details": details}
    if rd is not None or rt is not None:
        out.update(resolved_date=rd, resolved_time=rt)
    return out


class TestResolvedAppointment(Base):
    """REF is Tuesday 2026-11-03 (Chicago)."""

    def parse(self, appt):
        return fs.parse_return_appointment(appt, REF, CHI)

    def test_valid_resolutions_make_a_window(self):
        cases = [
            (ra("next Thursday at 6", "2026-11-12", "18:00"), datetime(2026, 11, 12, 18, tzinfo=CHI), 60),
            (ra("tomorrow at 7pm", "2026-11-04", "19:00"), datetime(2026, 11, 4, 19, tzinfo=CHI), 60),
            (ra("Thursday the 12th at 6:30 pm", "2026-11-12", "18:30"),
             datetime(2026, 11, 12, 18, 30, tzinfo=CHI), 60),
            (ra("this Friday, 6 to 7:30 pm", "2026-11-06", "18:00"),
             datetime(2026, 11, 6, 18, tzinfo=CHI), 90),
            (ra("next Thursday evening", "2026-11-12", None), datetime(2026, 11, 12, 15, tzinfo=CHI), 120),
            (ra("next Thursday at 6 in the evening", "2026-11-12", None),
             datetime(2026, 11, 12, 18, tzinfo=CHI), 60),
            (ra("next week Monday at six o'clock", "2026-11-09", "18:00"),
             datetime(2026, 11, 9, 18, tzinfo=CHI), 60),
        ]
        for appt, start, minutes in cases:
            with self.subTest(details=appt["details"]):
                window, reason = self.parse(appt)
                self.assertIsNone(reason)
                self.assertEqual(window[0], start)
                self.assertEqual((window[1] - window[0]).total_seconds(), minutes * 60)

    def test_doubtful_resolutions_make_no_task(self):
        cases = {
            "weekday mismatch": ra("next Friday at 6pm", "2026-11-12", "18:00"),
            "two weekdays": ra("Thursday or Friday at 6pm", "2026-11-12", "18:00"),
            "past date": ra("next Thursday at 6pm", "2026-11-02", "18:00"),
            "same day": ra("at 6pm", "2026-11-03", "18:00"),
            "too far": ra("June 1 at 6pm", "2027-06-01", "18:00"),
            "missing time": ra("next Thursday", "2026-11-12", None),
            "bare hour, no time resolved": ra("next Thursday at 6", "2026-11-12", None),
            "invented time": ra("next Thursday", "2026-11-12", "18:00"),
            "hour mismatch": ra("next Thursday at 6", "2026-11-12", "19:00"),
            "explicit time mismatch": ra("next Thursday at 6 pm", "2026-11-12", "19:00"),
            "outside day part": ra("next Thursday morning", "2026-11-12", "18:00"),
            "two times": ra("next Thursday at 6 or 7 pm", "2026-11-12", "18:00"),
            "tomorrow mismatch": ra("tomorrow at 6pm", "2026-11-05", "18:00"),
            "today": ra("tonight at 7", "2026-11-04", "19:00"),
            "next week mismatch": ra("next week Thursday at 6pm", "2026-11-19", "18:00"),
            "next weekday too far": ra("next Thursday at 6pm", "2026-11-26", "18:00"),
            "said date mismatch": ra("November 13 at 6pm", "2026-11-12", "18:00"),
            "bad date format": ra("next Thursday at 6pm", "11/12/2026", "18:00"),
            "not a date": ra("next Thursday at 6pm", "2026-02-30", "18:00"),
            "bad time format": ra("next Thursday at 6pm", "2026-11-12", "6pm"),
            "no phrase": ra(None, "2026-11-12", "18:00"),
        }
        for label, appt in cases.items():
            with self.subTest(label):
                window, reason = self.parse(appt)
                self.assertIsNone(window)
                self.assertTrue(reason)

    def test_unresolved_falls_back_to_the_phrase(self):
        window, reason = self.parse(ra("sometime next week", None, None))
        self.assertIsNone(window)
        self.assertEqual(reason, "no exact calendar date was given")
        window, _ = self.parse(ra("Thursday, November 12 at 6 PM", None, None))
        self.assertEqual(window[0], datetime(2026, 11, 12, 18, tzinfo=CHI))
        self.assertIsNone(self.parse(ra("next Thursday", "2026-11-12", None, scheduled=False))[0])

    def test_reasons_name_what_the_ai_read(self):
        _, reason = self.parse(ra("next Friday at 6pm", "2026-11-12", "18:00"))
        self.assertIn("Thursday, November 12, 2026", reason)
        self.assertIn("Friday", reason)

    def test_comment_and_task_show_phrase_and_resolved_date(self):
        ex = full_extraction(**ra("next Thursday at 6", "2026-11-12", "18:00"))
        plan = fs.build_plan(job(), ex, fs._config())
        text = plan[0]["request"]["body"]["text"]
        self.assertIn("Thursday, November 12, 2026, 6:00 PM–7:00 PM (worked out from what was said",
                      text)
        self.assertIn('As said: "next Thursday at 6"', text)
        task = plan[1]["request"]["body"]
        self.assertEqual(task["start_time"], int(datetime(2026, 11, 12, 18, tzinfo=CHI).timestamp()))
        self.assertIn('Customer said: "next Thursday at 6"', task["notes"])
        self.assertIn("6:00 PM", task["notes"])

        bad = full_extraction(**ra("next Friday at 6pm", "2026-11-12", "18:00"))
        plan = fs.build_plan(job(), bad, fs._config())
        self.assertIsNone(plan[1]["request"])
        text = plan[0]["request"]["body"]["text"]
        self.assertIn('Scheduled: "next Friday at 6pm"', text)
        self.assertIn("No FieldPulse task was created: the AI read it as Thursday, November 12", text)


class TestReferenceDate(unittest.TestCase):
    def setUp(self):
        pe = mock.patch.dict(os.environ, {}, clear=False)
        pe.start()
        self.addCleanup(pe.stop)
        os.environ.pop("FIELDPULSE_TIMEZONE", None)

    def test_consultation_reference_uses_branch_timezone(self):
        self.assertEqual(fs.consultation_reference(job()), (REF, "America/Chicago"))
        os.environ["FIELDPULSE_TIMEZONE"] = "Asia/Tokyo"
        self.assertEqual(fs.consultation_reference(job()), (date(2026, 11, 4), "Asia/Tokyo"))
        os.environ["FIELDPULSE_TIMEZONE"] = "Not/AZone"
        self.assertEqual(fs.consultation_reference(job())[1], "America/Chicago")
        day, tz = fs.consultation_reference({"created_at": "garbage"})   # never raises
        self.assertIsInstance(day, date)

    def _call(self, **kw):
        import extractor
        resp = mock.Mock()
        resp.content = [mock.Mock(type="text", text='{"project_type": "bath"}')]
        with mock.patch.object(extractor._client.messages, "create", return_value=resp) as c:
            out = extractor.extract_consultation("REP: hello there", **kw)
        self.assertEqual(out, {"project_type": "bath"})
        return c.call_args.kwargs, extractor

    def test_reference_date_reaches_the_prompt(self):
        kw, ext = self._call(reference_date=date(2026, 11, 4), reference_tz="America/Chicago")
        content = kw["messages"][0]["content"]
        self.assertTrue(content.startswith(
            "Consultation date: Wednesday, November 4, 2026 (America/Chicago)\n"))
        self.assertTrue(content.endswith("REP: hello there"))
        # The cached system prompt is unchanged by the date.
        self.assertEqual(kw["system"][0]["text"], ext._inject_date(ext.SYSTEM_PROMPT_CONSULT))
        self.assertNotIn("November 4, 2026", kw["system"][0]["text"])

    def test_no_reference_date_sends_the_bare_transcript(self):
        kw, _ = self._call()
        self.assertEqual(kw["messages"][0]["content"], "REP: hello there")

    def test_prompt_asks_for_resolution_and_keeps_null_rules(self):
        prompt = (_HERE / "consultation_prompt.txt").read_text()
        for needle in ('"resolved_date": "YYYY-MM-DD or null"', '"resolved_time": "HH:MM (24h) or null"',
                       "NEVER invent or guess a time", "Consultation date:",
                       "Use null for any field not discussed or not answered. NEVER INVENT.",
                       "NEVER invent a budget number, likelihood percent, or close score.",
                       "keep the ORIGINAL phrase"):
            self.assertIn(needle, prompt)


class TestDryRunNeverAutoSent(Base):
    def test_dry_run_rows_are_held_after_a_mode_switch(self):
        out = fs.sync_consultation(job(), full_extraction())
        self.assertEqual(out["status"], "dry_run")
        self.allow_send([(200, {"error": False, "response": {"id": 1}})] * 4)
        for mode in ("test_customer_only", "live"):
            with self.subTest(mode=mode):
                self.sendable(mode=mode)
                out = fs.sync_consultation(job(), full_extraction())
                self.assertEqual(self.sent, [])
                self.assertEqual(out["status"], "blocked")
                self.assertEqual([s["status"] for s in out["steps"]], ["held", "held"])
                self.assertIn("never sent automatically", out["message"])
        rows = self.store.list_fieldpulse_writes("job_abc")
        self.assertEqual([(r["status"], r["mode"]) for r in rows],
                         [("planned", "dry_run"), ("planned", "dry_run")])
        # Back to dry_run: still just a dry run.
        self.setenv(FIELDPULSE_WRITE_MODE="dry_run")
        self.assertEqual(fs.sync_consultation(job(), full_extraction())["status"], "dry_run")
        self.assertEqual(self.sent, [])

    def test_blocked_customer_does_not_strip_the_dry_run_marker(self):
        fs.sync_consultation(job(), full_extraction())
        self.sendable(mode="test_customer_only", test_id="999")      # not this customer
        fs.sync_consultation(job(), full_extraction())
        self.assertTrue(all(r["mode"] == "dry_run" for r in self.store.list_fieldpulse_writes("job_abc")))
        self.allow_send([(200, {"response": {"id": 1}})] * 2)
        self.sendable(mode="live")
        fs.sync_consultation(job(), full_extraction())
        self.assertEqual(self.sent, [])

    def test_skipped_dry_run_task_is_not_sent_later(self):
        fs.sync_consultation(job(), full_extraction(scheduled=True, details="next week sometime"))
        self.allow_send([(200, {"response": {"id": 1}})] * 2)
        self.sendable(mode="live")
        out = fs.sync_consultation(job(), full_extraction())        # now has a usable date
        self.assertEqual(self.sent, [])
        self.assertEqual(out["status"], "blocked")

    def test_fresh_job_in_send_mode_still_sends(self):
        self.sendable(mode="live")
        self.allow_send([(200, {"error": False, "response": {"id": 5}})] * 2)
        out = fs.sync_consultation(job(), full_extraction())
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(out["status"], "succeeded")

if __name__ == "__main__":
    unittest.main(verbosity=2)
