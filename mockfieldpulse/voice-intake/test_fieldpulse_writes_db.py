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
              "return_appointment": {"scheduled": True, "details": "2026-11-20 at 10 am"},
              "materials": [{"area": "Shower", "item": "subway tile", "details": "white",
                             "quantity": None, "unit": None, "quote": "white subway"}]}
        with mock.patch.object(fs, "_send", side_effect=AssertionError("must not send")):
            out = fs.sync_consultation(j, ex)
            again = fs.sync_consultation(j, ex)
        self.assertEqual(out["status"], "dry_run")
        self.assertEqual(again["status"], "dry_run")
        rows = J.list_fieldpulse_writes(j["id"])
        self.assertEqual([(r["step"], r["status"], r["mode"]) for r in rows],
                         [("comment", "planned", "dry_run"), ("return_task", "planned", "dry_run"),
                          ("material_list", "planned", "dry_run")])
        self.assertEqual(rows[1]["request"]["body"]["type"], "task")
        self.assertEqual(rows[2]["request"]["path"], "/material-lists")
        self.assertEqual(rows[2]["request"]["body"]["groupings"][0]["title"], "Shower")
        # Mode switched to live later: the dry-run rows are held, nothing is sent.
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "live",
                                          "FIELDPULSE_WRITE_BASE_URL": "https://fp.invalid",
                                          "FIELDPULSE_WRITE_API_KEY": "k"}), \
                mock.patch.object(fs, "_send", side_effect=AssertionError("must not send")):
            held = fs.sync_consultation(j, ex)
        self.assertEqual(held["status"], "blocked")
        self.assertEqual([s["status"] for s in held["steps"]], ["held", "held", "held"])
        self.assertEqual([(r["status"], r["mode"]) for r in J.list_fieldpulse_writes(j["id"])],
                         [("planned", "dry_run")] * 3)
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
                         ["planned", "planned", "planned"])
        self.assertEqual(recent[j["id"]]["customer"]["display_name"], "Jane Doe")
        old_s = recent[old["id"]]["summary"]
        self.assertIsNone(old_s["fieldpulse_sync"])
        self.assertEqual(old_s["match_path"], "matched_by_id")
        self.assertIsNone(recent[old["id"]]["customer"])

    # --- Stage 3: admin panel SQL ---------------------------------------------------

    def test_release_only_unsent_dry_run_rows(self):
        J = self.jobs
        jid = self.new_job()["id"]
        req = {"method": "POST", "path": "/comments", "body": {"text": "a"}}
        J.record_fieldpulse_write(jid, "comment", "planned", "dry_run", request=req)
        J.record_fieldpulse_write(jid, "return_task", "skipped", "dry_run", error="vague")
        J.record_fieldpulse_write(jid, "other", "planned", "live", request=req)
        self.assertTrue(J.claim_fieldpulse_write(jid, "other", 3))
        J.finish_fieldpulse_write(jid, "other", "succeeded")
        with self.assertRaises(J.JobError):
            J.release_held_fieldpulse_writes(jid, "dry_run")
        self.assertEqual(J.release_held_fieldpulse_writes(jid, "live"), 2)
        rows = {r["step"]: r for r in J.list_fieldpulse_writes(jid)}
        self.assertEqual((rows["comment"]["status"], rows["comment"]["mode"]), ("planned", "live"))
        self.assertEqual(rows["return_task"]["mode"], "live")
        self.assertEqual(rows["other"]["status"], "succeeded")
        self.assertTrue(J.claim_fieldpulse_write(jid, "comment", 3))       # now claimable
        self.assertEqual(J.release_held_fieldpulse_writes(jid, "live"), 0)

    def test_save_manual_send_merges_caps_and_keeps_status(self):
        J = self.jobs
        jid = self.new_job()["id"]
        J.update_job_status(jid, "done", result={"extraction": {"a": 1},
                                                 "fieldpulse_sync": {"status": "dry_run"}})
        before = J.get_job(jid)
        self.assertTrue(J.save_manual_send(jid, {"id": "ms_1", "status": "queued"}))
        self.assertTrue(J.save_manual_send(jid, {"id": "ms_1", "status": "finished",
                                                 "outcome": "succeeded"},
                                           sync={"status": "succeeded"}))
        after = J.get_job(jid)
        self.assertEqual(after["result"]["fieldpulse_manual_sends"],
                         [{"id": "ms_1", "status": "finished", "outcome": "succeeded"}])
        self.assertEqual(after["result"]["fieldpulse_sync"], {"status": "succeeded"})
        self.assertEqual(after["result"]["extraction"], {"a": 1})
        self.assertEqual((after["status"], after["updated_at"]),
                         (before["status"], before["updated_at"]))
        for i in range(25):
            J.save_manual_send(jid, {"id": f"ms_x{i}", "status": "queued"})
        sends = J.get_job(jid)["result"]["fieldpulse_manual_sends"]
        self.assertEqual(len(sends), 20)
        self.assertEqual(sends[-1]["id"], "ms_x24")
        self.assertFalse(J.save_manual_send("job_missing", {"id": "ms_1"}))
        with self.assertRaises(J.JobError):
            J.save_manual_send(jid, {"status": "queued"})
        # Concurrent saves never lose an entry (row lock).
        threads = [threading.Thread(target=J.save_manual_send,
                                    args=(jid, {"id": f"ms_c{i}", "status": "queued"}))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = {s["id"] for s in J.get_job(jid)["result"]["fieldpulse_manual_sends"]}
        self.assertTrue({f"ms_c{i}" for i in range(5)} <= ids)

    def test_admin_list_rows_and_garbage_json(self):
        J = self.jobs
        good = self.new_job()["id"]
        J.update_job_status(good, "done", result={
            "input": "paste", "transcript": "x" * 50000,
            "extraction": {"project_type": "bath", "client": {"name": "Jane"},
                           "sales": {"close_score_1_10": 8}},
            "fieldpulse_sync": {"status": "dry_run", "mode": "dry_run",
                                "comment_text": "c" * 5000,
                                "steps": [{"step": "comment", "status": "planned"}, "junk"]},
            "fieldpulse_manual_sends": [{"id": "ms_1", "status": "queued"}]})
        J.record_fieldpulse_write(good, "comment", "planned", "dry_run", request={})
        weird = []
        for result in (["a", "list"], "a string", 42,
                       {"extraction": "str", "fieldpulse_sync": "str", "fieldpulse": "str",
                        "fieldpulse_manual_sends": "str"},
                       {"extraction": {"sales": [1], "client": "x"},
                        "fieldpulse_sync": {"steps": {"not": "a list"}}}):
            jid = self.new_job()["id"]
            J.update_job_status(jid, "done", result=result)
            weird.append(jid)
        failed = self.new_job()["id"]
        J.update_job_status(failed, "failed", error="E" * 1000)
        rows = {r["id"]: r for r in J.list_jobs_for_admin(5000)}
        g = rows[good]
        self.assertEqual((g["input"], g["project_type"], g["client_name"], g["close_score"]),
                         ("paste", "bath", "Jane", 8))
        self.assertEqual(g["sync"], {"status": "dry_run", "mode": "dry_run",
                                     "steps": [{"step": "comment", "status": "planned"}]})
        self.assertEqual(g["writes"], [{"step": "comment", "status": "planned", "mode": "dry_run"}])
        self.assertEqual(g["last_manual_send"], {"id": "ms_1", "status": "queued"})
        self.assertTrue(g["has_paste"])
        self.assertNotIn("x" * 100, str(g))                 # no transcript in list rows
        self.assertNotIn("c" * 100, str(g))                 # no comment text
        for jid in weird:
            self.assertIn(jid, rows)
            self.assertIsNone(rows[jid]["close_score"])
            self.assertFalse(rows[jid]["legacy_mock"])
        self.assertEqual(rows[weird[4]]["sync"]["steps"], [])
        self.assertEqual(len(rows[failed]["error"]), 300)
        order = [r["created_at"] for r in J.list_jobs_for_admin(5000)]
        self.assertEqual(order, sorted(order, reverse=True))

    def test_delete_job_is_one_transaction_and_guarded(self):
        J = self.jobs
        keep = self.new_job()["id"]
        jid = self.new_job()["id"]
        J.update_job_status(jid, "done", result={"extraction": {}})
        for step in ("comment", "return_task"):
            J.record_fieldpulse_write(jid, step, "planned", "dry_run", request={"m": step})
            J.record_fieldpulse_write(keep, step, "planned", "dry_run", request={"m": step})
        J.save_customer_link("fieldpulse", "18809123", "mock_1")
        seen = []
        out = J.delete_job(jid, guard=lambda job: seen.append(job["status"]) or "busy")
        self.assertEqual((out["deleted"], out["reason"], seen), (False, "busy", ["done"]))
        self.assertIsNotNone(J.get_job(jid))
        self.assertEqual(len(J.list_fieldpulse_writes(jid)), 2)
        out = J.delete_job(jid, guard=lambda job: None)
        self.assertEqual((out["deleted"], out["writes_deleted"], out["job"]["id"]), (True, 2, jid))
        self.assertIsNone(J.get_job(jid))
        self.assertEqual(J.list_fieldpulse_writes(jid), [])
        self.assertIsNone(J.delete_job(jid))
        self.assertFalse(J.save_manual_send(jid, {"id": "ms_x", "status": "queued"}))
        with self.assertRaises(J.JobError):
            J.update_job_status(jid, "done")
        # Other jobs, their writes, and the per-customer link are untouched.
        self.assertIsNotNone(J.get_job(keep))
        self.assertEqual(len(J.list_fieldpulse_writes(keep)), 2)
        self.assertEqual(J.get_customer_link("fieldpulse", "18809123"), "mock_1")
        # A crashing guard rolls back: nothing deleted.
        def boom(job):
            raise RuntimeError("guard crashed")
        with self.assertRaises(RuntimeError):
            J.delete_job(keep, guard=boom)
        self.assertIsNotNone(J.get_job(keep))
        self.assertEqual(len(J.list_fieldpulse_writes(keep)), 2)

    def test_delete_waits_for_a_row_lock_holder(self):
        J = self.jobs
        jid = self.new_job()["id"]
        J.update_job_status(jid, "done", result={})
        holding, release, done = threading.Event(), threading.Event(), []

        def hold():
            with J.pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM consultation_jobs WHERE id = %s FOR UPDATE", [jid])
                holding.set()
                release.wait(5)
                cur.execute("UPDATE consultation_jobs SET status = 'processing' WHERE id = %s",
                            [jid])
            done.append(True)

        t = threading.Thread(target=hold)
        t.start()
        holding.wait(5)
        threading.Timer(0.5, release.set).start()
        out = J.delete_job(jid, guard=lambda job: "busy" if job["status"] == "processing" else None)
        t.join(5)
        # The guard saw the committed 'processing' status, not the stale 'done'.
        self.assertEqual((out["deleted"], out.get("reason")), (False, "busy"))
        self.assertIsNotNone(J.get_job(jid))

    # --- F-28: claim and delete are coordinated through the job row ---------

    def _live(self):
        return mock.patch.dict(os.environ, {
            "FIELDPULSE_WRITE_MODE": "live", "FIELDPULSE_WRITE_BASE_URL": "https://fp.invalid",
            "FIELDPULSE_WRITE_API_KEY": "k"})

    def test_claim_then_delete_is_refused_until_finished(self):
        J = self.jobs
        jid = self.new_job()["id"]
        J.update_job_status(jid, "done", result={})
        J.record_fieldpulse_write(jid, "comment", "planned", "live", request={"m": 1})
        self.assertTrue(J.claim_fieldpulse_write(jid, "comment", 3))     # worker is mid-send
        out = J.delete_job(jid, guard=lambda job: None)
        self.assertFalse(out["deleted"])
        self.assertIn("in progress", out["reason"])
        self.assertIn("comment", out["reason"])
        self.assertEqual(len(J.list_fieldpulse_writes(jid)), 1)
        # Finished as "sent, no answer" (always has an error): no longer blocks.
        J.finish_fieldpulse_write(jid, "comment", "sent", error="no response")
        self.assertTrue(J.delete_job(jid, guard=lambda job: None)["deleted"])

    def test_delete_then_claim_never_sends(self):
        J, fs = self.jobs, self.fs
        j = J.get_job(self.new_job()["id"])
        J.update_job_status(j["id"], "done", result={})
        J.record_fieldpulse_write(j["id"], "comment", "planned", "live", request={"m": 1})
        self.assertTrue(J.delete_job(j["id"])["deleted"])
        self.assertFalse(J.claim_fieldpulse_write(j["id"], "comment", 3))
        J.record_fieldpulse_write(j["id"], "comment", "planned", "live", request={"m": 1})  # no-op
        self.assertEqual(J.list_fieldpulse_writes(j["id"]), [])
        # The worker still holds the old job dict (delayed manual send): nothing goes out.
        ex = {"project_type": "bath", "notes_summary": "Bath refresh.",
              "return_appointment": {"scheduled": False}}
        with self._live(), mock.patch.object(fs, "_send", side_effect=AssertionError("sent!")):
            fs.sync_consultation(j, ex, manual=True)
            fs.sync_consultation(j, ex)
        self.assertEqual(J.list_fieldpulse_writes(j["id"]), [])

    def test_claim_waits_for_a_running_delete_then_fails(self):
        J = self.jobs
        jid = self.new_job()["id"]
        J.update_job_status(jid, "done", result={})
        J.record_fieldpulse_write(jid, "comment", "planned", "live", request={"m": 1})
        in_delete, claimed = threading.Event(), []

        def slow_guard(job):          # runs while delete_job holds FOR UPDATE
            in_delete.set()
            threading.Event().wait(0.7)
            return None

        def claim():
            in_delete.wait(5)
            claimed.append(J.claim_fieldpulse_write(jid, "comment", 3))

        t = threading.Thread(target=claim)
        t.start()
        out = J.delete_job(jid, guard=slow_guard)
        t.join(5)
        self.assertTrue(out["deleted"])
        self.assertEqual(claimed, [False])

    def test_delete_waits_for_a_running_claim_then_refuses(self):
        J = self.jobs
        jid = self.new_job()["id"]
        J.update_job_status(jid, "done", result={})
        J.record_fieldpulse_write(jid, "comment", "planned", "live", request={"m": 1})
        holding, results = threading.Event(), []

        def claim_slowly():           # the claim's own lock + UPDATE, committed late
            with J.pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM consultation_jobs WHERE id = %s FOR KEY SHARE", [jid])
                cur.execute("UPDATE fieldpulse_writes SET status = 'sent', error = NULL, "
                            "attempts = attempts + 1 WHERE job_id = %s", [jid])
                holding.set()
                threading.Event().wait(0.7)
            results.append("claimed")

        t = threading.Thread(target=claim_slowly)
        t.start()
        holding.wait(5)
        out = J.delete_job(jid, guard=lambda job: None)
        t.join(5)
        self.assertEqual(results, ["claimed"])
        self.assertFalse(out["deleted"])
        self.assertIn("in progress", out["reason"])
        self.assertIsNotNone(J.get_job(jid))

    def test_manual_send_end_to_end_releases_and_is_idempotent(self):
        J, fs = self.jobs, self.fs
        j = J.get_job(self.new_job()["id"])
        ex = {"project_type": "bath", "notes_summary": "Bath refresh.",
              "return_appointment": {"scheduled": False}}
        with mock.patch.object(fs, "_send", side_effect=AssertionError("must not send")):
            fs.sync_consultation(j, ex)                                  # dry run
            out = fs.sync_consultation(j, ex, manual=True)               # still dry run
        self.assertIn("write mode is dry_run", out["message"])
        sent = []
        live = {"FIELDPULSE_WRITE_MODE": "live", "FIELDPULSE_WRITE_BASE_URL": "https://fp.invalid",
                "FIELDPULSE_WRITE_API_KEY": "k"}
        with mock.patch.dict(os.environ, live), \
                mock.patch.object(fs, "_send", side_effect=lambda r, c: sent.append(r) or
                                  (200, {"error": False, "response": {"id": 77}})):
            self.assertEqual(fs.sync_consultation(j, ex)["status"], "blocked")   # held
            self.assertEqual(sent, [])
            out = fs.sync_consultation(j, ex, manual=True)
            again = fs.sync_consultation(j, ex, manual=True)
        self.assertEqual((out["status"], again["status"]), ("succeeded", "succeeded"))
        self.assertEqual([r["path"] for r in sent], ["/comments"])
        rows = {r["step"]: r for r in J.list_fieldpulse_writes(j["id"])}
        self.assertEqual((rows["comment"]["status"], rows["comment"]["mode"],
                          rows["comment"]["fieldpulse_record_id"]), ("succeeded", "live", "77"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
