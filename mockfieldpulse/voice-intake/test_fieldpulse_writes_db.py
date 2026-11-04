"""Real-Postgres checks for jobs.py's paste column + fieldpulse_writes SQL and
an end-to-end dry-run sync against it. SKIPPED unless VI_TEST_DATABASE_URL
points at a THROWAWAY database (tables are created and rows inserted). Run:

    VI_TEST_DATABASE_URL=postgresql://localhost:5433/vi_test \
        python -m unittest test_fieldpulse_writes_db -v

Runs in its own process (it imports the real jobs.py, so don't combine it
with the offline suites that stub `jobs`).
"""

import os
import sys
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

_URL = os.environ.get("VI_TEST_DATABASE_URL")


@unittest.skipUnless(_URL, "set VI_TEST_DATABASE_URL to a throwaway Postgres to run")
class TestWritesDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import dotenv
        dotenv.load_dotenv = lambda *a, **k: False
        os.environ["DATABASE_URL"] = _URL
        for k in [k for k in os.environ if k.startswith("FIELDPULSE")]:
            os.environ.pop(k)
        import jobs
        import fieldpulse_sync
        cls.jobs = jobs
        cls.fs = fieldpulse_sync

    def new_job(self, **kw):
        kw.setdefault("transcript", "REP: hello there, homeowner.")
        return self.jobs.create_job(customer_id="18809123",
                                    customer={"id": "18809123", "display_name": "Jane Doe"}, **kw)

    def test_paste_job_round_trip_and_input_rule(self):
        j = self.new_job()
        got = self.jobs.get_job(j["id"])
        self.assertIsNone(got["audio_key"])
        self.assertEqual(got["transcript"], "REP: hello there, homeowner.")
        with self.assertRaises(self.jobs.JobError):
            self.jobs.create_job(audio_key="consultations/a.webm", transcript="x")
        with self.assertRaises(self.jobs.JobError):
            self.jobs.create_job()

    def test_record_claim_finish_rules(self):
        J = self.jobs
        jid = self.new_job()["id"]
        req = {"method": "POST", "path": "/comments", "body": {"text": "a"}}
        J.record_fieldpulse_write(jid, "comment", "planned", "live", request=req)
        J.record_fieldpulse_write(jid, "comment", "planned", "live", request=req)  # upsert
        self.assertEqual(len(J.list_fieldpulse_writes(jid)), 1)
        self.assertTrue(J.claim_fieldpulse_write(jid, "comment", 3))
        self.assertFalse(J.claim_fieldpulse_write(jid, "comment", 3))     # already sent
        J.record_fieldpulse_write(jid, "comment", "planned", "live", request=req)
        self.assertEqual(J.list_fieldpulse_writes(jid)[0]["status"], "sent")  # untouched
        J.finish_fieldpulse_write(jid, "comment", "succeeded", response={"http_status": 200},
                                  record_id="77")
        J.record_fieldpulse_write(jid, "comment", "skipped", "live", error="x")
        row = J.list_fieldpulse_writes(jid)[0]
        self.assertEqual((row["status"], row["fieldpulse_record_id"], row["attempts"]),
                         ("succeeded", "77", 1))
        J.finish_fieldpulse_write(jid, "comment", "failed", error="late")  # not 'sent' -> no-op
        self.assertEqual(J.list_fieldpulse_writes(jid)[0]["status"], "succeeded")
        with self.assertRaises(J.JobError):
            J.record_fieldpulse_write(jid, "comment", "succeeded", "live")

    def test_dry_run_rows_are_never_claimed_or_rewritten_by_a_send_mode(self):
        J = self.jobs
        jid = self.new_job()["id"]
        req = {"method": "POST", "path": "/comments", "body": {"text": "a"}}
        J.record_fieldpulse_write(jid, "comment", "planned", "dry_run", request=req)
        J.record_fieldpulse_write(jid, "comment", "planned", "dry_run", request=req)  # refresh ok
        self.assertFalse(J.claim_fieldpulse_write(jid, "comment", 3))
        for mode in ("live", "test_customer_only"):
            J.record_fieldpulse_write(jid, "comment", "planned", mode, request=req)
            J.record_fieldpulse_write(jid, "comment", "skipped", mode, error="x")
            self.assertFalse(J.claim_fieldpulse_write(jid, "comment", 3))
        row = J.list_fieldpulse_writes(jid)[0]
        self.assertEqual((row["status"], row["mode"], row["attempts"]), ("planned", "dry_run", 0))

    def test_attempt_cap_and_concurrent_claim(self):
        J = self.jobs
        jid = self.new_job()["id"]
        J.record_fieldpulse_write(jid, "return_task", "planned", "live", request={})
        wins = []
        barrier = threading.Barrier(5)

        def claim():
            barrier.wait()
            wins.append(J.claim_fieldpulse_write(jid, "return_task", 3))
        threads = [threading.Thread(target=claim) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(wins.count(True), 1)
        for _ in range(2):
            J.finish_fieldpulse_write(jid, "return_task", "failed", error="HTTP 500")
            self.assertTrue(J.claim_fieldpulse_write(jid, "return_task", 3))
        J.finish_fieldpulse_write(jid, "return_task", "failed", error="HTTP 500")
        self.assertFalse(J.claim_fieldpulse_write(jid, "return_task", 3))   # cap reached

    def test_dry_run_sync_end_to_end_and_recent_summary(self):
        J, fs = self.jobs, self.fs
        j = J.get_job(self.new_job()["id"])
        ex = {"project_type": "bath", "notes_summary": "Bath refresh.",
              "sales": {"close_score_1_10": 7},
              "return_appointment": {"scheduled": True, "details": "2026-11-20 at 10 am"}}
        with mock.patch.object(fs, "_send", side_effect=AssertionError("must not send")):
            out = fs.sync_consultation(j, ex)
            again = fs.sync_consultation(j, ex)
        self.assertEqual(out["status"], "dry_run")
        self.assertEqual(again["status"], "dry_run")
        rows = J.list_fieldpulse_writes(j["id"])
        self.assertEqual([(r["step"], r["status"], r["mode"]) for r in rows],
                         [("comment", "planned", "dry_run"), ("return_task", "planned", "dry_run")])
        self.assertEqual(rows[1]["request"]["body"]["type"], "task")
        # Mode switched to live later: the dry-run rows are held, nothing is sent.
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "live",
                                          "FIELDPULSE_WRITE_BASE_URL": "https://fp.invalid",
                                          "FIELDPULSE_WRITE_API_KEY": "k"}), \
                mock.patch.object(fs, "_send", side_effect=AssertionError("must not send")):
            held = fs.sync_consultation(j, ex)
        self.assertEqual(held["status"], "blocked")
        self.assertEqual([s["status"] for s in held["steps"]], ["held", "held"])
        self.assertEqual([(r["status"], r["mode"]) for r in J.list_fieldpulse_writes(j["id"])],
                         [("planned", "dry_run"), ("planned", "dry_run")])
        J.update_job_status(j["id"], "done", result={"input": "paste", "extraction": ex,
                                                     "fieldpulse_sync": out})
        # An old mock-writer job alongside.
        old = J.create_job(audio_key=f"consultations/{uuid.uuid4().hex}.webm",
                           customer_id="cust_ab12")
        J.update_job_status(old["id"], "done", result={
            "extraction": {"project_type": "kitchen"},
            "fieldpulse": {"match_path": "matched_by_id", "customer_id": "cust_ab12"}})
        recent = {r["id"]: r for r in J.list_recent_jobs(50)}
        new_s = recent[j["id"]]["summary"]
        self.assertEqual(new_s["input"], "paste")
        self.assertEqual(new_s["fieldpulse_sync"]["status"], "dry_run")
        self.assertEqual([s["status"] for s in new_s["fieldpulse_sync"]["steps"]],
                         ["planned", "planned"])
        self.assertEqual(recent[j["id"]]["customer"]["display_name"], "Jane Doe")
        old_s = recent[old["id"]]["summary"]
        self.assertIsNone(old_s["fieldpulse_sync"])
        self.assertEqual(old_s["match_path"], "matched_by_id")
        self.assertIsNone(recent[old["id"]]["customer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
