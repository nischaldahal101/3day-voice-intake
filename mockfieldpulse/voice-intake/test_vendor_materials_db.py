"""Real-Postgres checks for stage P2's vendor_material_lists SQL (jobs.py) and the
per-customer lease end to end through fieldpulse_sync.sync_vendor_materials.
SKIPPED unless VI_TEST_DATABASE_URL points at a THROWAWAY database. Made-up data.

    VI_TEST_DATABASE_URL=postgresql://127.0.0.1/vi_test \
        python -m unittest test_vendor_materials_db -v

Runs in its own process (imports the real jobs.py).
"""

import os
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

_URL = os.environ.get("VI_TEST_DATABASE_URL")


def _result(vendor, doc_type, lines):
    return {"vendor": vendor, "document_type": doc_type, "document_number": None,
            "document_date": None, "job_name": None, "lines": lines,
            "totals": {"subtotal": None, "tax": None, "freight": None, "total": None},
            "checks": [{"level": "warn", "code": "x", "message": "made up"}]}


@unittest.skipUnless(_URL, "set VI_TEST_DATABASE_URL to a throwaway Postgres to run")
class TestVendorListsDB(unittest.TestCase):
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

    def cid(self):
        return str(10_000_000 + uuid.uuid4().int % 9_000_000)

    def add_doc(self, cid, vendor="Ferguson", status="done", lines=None, name="Pat Example"):
        d = self.jobs.create_vendor_document(f"vendor-docs/{uuid.uuid4()}.pdf", filename="m.pdf",
                                             file_size=10, customer_id=cid,
                                             customer={"id": cid, "display_name": name})
        if status != "queued":
            self.jobs.update_vendor_document(
                d["id"], status, result=_result(vendor, "quote", lines or [
                    {"part_number": "ABC12345", "name": "Tile", "details": None, "notes": None,
                     "quantity": 2, "unit": None, "unit_price": 3.5, "line_total": 7.0}])
                if status == "done" else None)
        return d

    def test_claim_busy_resync_and_finish(self):
        J, cid = self.jobs, self.cid()
        self.assertIsNone(J.get_vendor_material_list(cid))
        row = J.claim_vendor_material_list(cid)
        self.assertEqual((row["status"], row["fieldpulse_list_id"], row["create_pending"],
                          row["held"]), ("new", None, False, False))
        self.assertIsNone(J.claim_vendor_material_list(cid, manual=True))   # busy -> resync
        again = J.finish_vendor_material_list(cid, "sent", mode="live", list_id=4242,
                                              request={"method": "POST"},
                                              history_entry={"status": "sent", "n": 1})
        self.assertEqual(again, (True, True))       # follow-up needed; lease released anyway
        row = J.claim_vendor_material_list(cid)     # the follow-up claims + consumes the flags
        self.assertTrue(row["resync_manual"])
        self.assertEqual(J.finish_vendor_material_list(cid, "sent", mode="live",
                                                       history_entry={"n": 2}), (False, False))
        rec = J.get_vendor_material_list(cid)
        self.assertEqual(rec["fieldpulse_list_id"], 4242)                  # kept when unset
        self.assertEqual([h["n"] for h in rec["history"]], [2, 1])         # newest first
        self.assertIsNotNone(J.claim_vendor_material_list(cid))           # lease released
        J.finish_vendor_material_list(cid, "dry_run", held=True)
        self.assertTrue(J.get_vendor_material_list(cid)["held"])

    def test_mark_sending_sets_pending_and_clears_id_in_one_write(self):
        J, cid = self.jobs, self.cid()
        J.claim_vendor_material_list(cid)
        J.finish_vendor_material_list(cid, "sent", list_id=4243)
        J.claim_vendor_material_list(cid)
        J.mark_vendor_material_list_sending(cid, {"method": "POST", "path": "/material-lists"},
                                            "live")
        rec = J.get_vendor_material_list(cid)
        self.assertEqual((rec["status"], rec["create_pending"], rec["fieldpulse_list_id"]),
                         ("sending", True, None))
        J.finish_vendor_material_list(cid, "skipped", error="No vendor documents")
        self.assertTrue(J.get_vendor_material_list(cid)["create_pending"])  # not cleared
        J.claim_vendor_material_list(cid)
        J.finish_vendor_material_list(cid, "sent", list_id=4244, create_pending=False)
        rec = J.get_vendor_material_list(cid)
        self.assertEqual((rec["create_pending"], rec["fieldpulse_list_id"]), (False, 4244))

    def test_history_cap(self):
        J, cid = self.jobs, self.cid()
        for n in range(15):
            J.claim_vendor_material_list(cid)
            J.finish_vendor_material_list(cid, "dry_run", mode="dry_run", history_entry={"n": n})
        hist = J.get_vendor_material_list(cid)["history"]
        self.assertEqual([h["n"] for h in hist], list(range(14, 4, -1)))

    def test_expired_lease_can_be_claimed(self):
        J, cid = self.jobs, self.cid()
        J.claim_vendor_material_list(cid)
        with J.pool.connection() as conn:
            conn.execute("UPDATE vendor_material_lists SET lease_until = now() - interval '1 s' "
                         "WHERE customer_id = %s", [cid])
        self.assertIsNotNone(J.claim_vendor_material_list(cid))

    def test_customer_aggregates_and_documents(self):
        J, cid = self.jobs, self.cid()
        a = self.add_doc(cid, name="Old Name")
        self.add_doc(cid, status="queued", name="New Name")
        self.add_doc(cid, status="failed")
        docs = J.list_customer_vendor_documents(cid)
        self.assertEqual(docs[0]["id"], a["id"])                       # oldest first
        self.assertEqual(len(docs), 3)
        J.claim_vendor_material_list(cid)
        J.finish_vendor_material_list(cid, "dry_run", mode="dry_run")
        row = next(r for r in J.list_vendor_document_customers() if r["customer_id"] == cid)
        self.assertEqual((row["document_count"], row["processing_count"], row["warn_count"]),
                         (3, 1, 1))
        self.assertEqual(row["customer"]["display_name"], "Pat Example")   # newest snapshot
        self.assertEqual(row["list_status"], "dry_run")

    def test_two_documents_finishing_together_create_one_list(self):
        J, fs, cid = self.jobs, self.fs, self.cid()
        self.add_doc(cid, vendor="Ferguson")
        sent, lock = [], threading.Lock()
        second_doc_done = threading.Event()

        def slow_send(request, cfg):
            with lock:
                sent.append((request["method"], request["path"],
                             [g["title"] for g in request["body"]["groupings"]]))
                n = len(sent)
            if n == 1:
                # While the first POST is "in flight", a second document finishes and
                # its sync starts: it must not POST a second list.
                self.add_doc(cid, vendor="Onyx Collection",
                             lines=[{"part_number": "ONX60301", "name": "Base", "details": None,
                                     "notes": None, "quantity": 1, "unit": None,
                                     "unit_price": 600.0, "line_total": 600.0}])
                second_doc_done.set()
                time.sleep(0.5)
                return 201, {"error": False, "response": {"id": 9_100_001}}
            return 200, {"error": False}

        env = {"FIELDPULSE_WRITE_MODE": "live", "FIELDPULSE_WRITE_BASE_URL": "https://fp.invalid",
               "FIELDPULSE_WRITE_API_KEY": "k", "FIELDPULSE_MATERIAL_ITEM_ID": "900001"}
        results = []
        with mock.patch.dict(os.environ, env), mock.patch.object(fs, "_send",
                                                                 side_effect=slow_send):
            t1 = threading.Thread(target=lambda: results.append(fs.sync_vendor_materials(cid)))
            t1.start()
            second_doc_done.wait(5)
            results.append(fs.sync_vendor_materials(cid))
            t1.join(10)
            self.assertEqual(sorted(r["status"] for r in results), ["queued", "sent"])
            first = next(r for r in results if r["status"] == "sent")
            self.assertIn("follow_up", first)            # the worker queues this run (F-78)
            self.assertEqual(fs.sync_vendor_materials(cid)["status"], "sent")
        self.assertEqual([s[:2] for s in sent],
                         [("POST", "/material-lists"), ("PUT", "/material-lists/9100001")])
        self.assertEqual(sent[1][2], ["Ferguson", "Onyx"])     # the rebuild has both
        rec = J.get_vendor_material_list(cid)
        self.assertEqual((rec["status"], rec["fieldpulse_list_id"]), ("sent", 9_100_001))
        self.assertEqual(rec["last_request"]["method"], "PUT")
        self.assertNotIn("k-", repr(rec))

    def env(self, **kw):
        base = {"FIELDPULSE_WRITE_MODE": "live", "FIELDPULSE_WRITE_BASE_URL": "https://fp.invalid",
                "FIELDPULSE_WRITE_API_KEY": "k", "FIELDPULSE_MATERIAL_ITEM_ID": "900001"}
        base.update(kw)
        return mock.patch.dict(os.environ, base)

    def test_f76_process_dies_after_replacement_post_dispatch(self):
        """PUT 404 -> replacement POST goes out -> the worker dies (no finish at all)."""
        J, fs, cid = self.jobs, self.fs, self.cid()
        self.add_doc(cid)
        J.claim_vendor_material_list(cid)
        J.finish_vendor_material_list(cid, "sent", list_id=9_100_002)

        class Died(BaseException):
            pass
        calls = []

        def send(request, cfg):
            calls.append(request["method"])
            if request["method"] == "PUT":
                return 404, {"error": True}
            raise Died()
        with self.env(), mock.patch.object(fs, "_send", side_effect=send):
            with self.assertRaises(Died):
                fs.sync_vendor_materials(cid)
        rec = J.get_vendor_material_list(cid)
        self.assertEqual((rec["status"], rec["create_pending"], rec["fieldpulse_list_id"]),
                         ("sending", True, None))
        with J.pool.connection() as conn:              # the lease runs out
            conn.execute("UPDATE vendor_material_lists SET lease_until = now() - interval '1 s' "
                         "WHERE customer_id = %s", [cid])
        with self.env(), mock.patch.object(fs, "_send", side_effect=send):
            out = fs.sync_vendor_materials(cid)        # automatic recovery
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(calls, ["PUT", "POST"])       # nothing more went out

    def test_f75_f77_unknown_then_dry_run_then_send_mode(self):
        J, fs, cid = self.jobs, self.fs, self.cid()
        self.add_doc(cid)
        from fieldpulse_sync import _OutcomeUnknown
        with self.env(), mock.patch.object(fs, "_send", side_effect=_OutcomeUnknown("x")):
            self.assertEqual(fs.sync_vendor_materials(cid)["status"], "unknown")
        with self.env(FIELDPULSE_WRITE_MODE="dry_run"), \
                mock.patch.object(fs, "_send", side_effect=AssertionError("no send")):
            self.assertEqual(fs.sync_vendor_materials(cid)["status"], "dry_run")
        rec = J.get_vendor_material_list(cid)
        self.assertEqual((rec["create_pending"], rec["held"]), (True, True))
        with self.env(FIELDPULSE_WRITE_MODE="test_customer_only", FIELDPULSE_TEST_CUSTOMER_ID=cid), \
                mock.patch.object(fs, "_send", side_effect=AssertionError("no send")):
            self.assertEqual(fs.sync_vendor_materials(cid)["status"], "held")
        with self.env(FIELDPULSE_WRITE_MODE="test_customer_only", FIELDPULSE_TEST_CUSTOMER_ID=cid), \
                mock.patch.object(fs, "_send", return_value=(201, {"response": {"id": 9_100_003}})):
            self.assertEqual(fs.sync_vendor_materials(cid, manual=True)["status"], "sent")
        rec = J.get_vendor_material_list(cid)
        self.assertEqual((rec["create_pending"], rec["held"], rec["fieldpulse_list_id"]),
                         (False, False, 9_100_003))


if __name__ == "__main__":
    unittest.main()
