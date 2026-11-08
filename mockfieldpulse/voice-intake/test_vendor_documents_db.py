"""Real-Postgres checks for jobs.py's vendor_documents table + functions (stage
P1b). SKIPPED unless VI_TEST_DATABASE_URL points at a THROWAWAY database
(tables are created and rows inserted/deleted). Made-up data only. Run:

    VI_TEST_DATABASE_URL=postgresql://localhost:5432/vi_vendor_test \
        python -m unittest test_vendor_documents_db -v

Runs in its own process (it imports the real jobs.py, so don't combine it
with the offline suites that stub `jobs`).
"""

import os
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

_URL = os.environ.get("VI_TEST_DATABASE_URL")


@unittest.skipUnless(_URL, "set VI_TEST_DATABASE_URL to a throwaway Postgres to run")
class TestVendorDocumentsDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import dotenv
        dotenv.load_dotenv = lambda *a, **k: False
        os.environ["DATABASE_URL"] = _URL
        import jobs
        cls.jobs = jobs
    def setUp(self):
        self.cid = f"9{uuid.uuid4().int % 10**9}"     # this test's own customer id

    def new(self, **kw):
        kw.setdefault("customer_id", self.cid)
        kw.setdefault("customer", {"id": self.cid, "display_name": "Pat Testerson"})
        return self.jobs.create_vendor_document(f"vendor-docs/{uuid.uuid4()}.pdf",
                                                filename="Quote.pdf", file_size=1234, **kw)

    def age(self, doc_id, minutes):
        with self.jobs.pool.connection() as conn:
            conn.execute("UPDATE vendor_documents SET updated_at = now() - %s * interval "
                         "'1 minute' WHERE id = %s", [minutes, doc_id])
            conn.commit()

    def test_create_get_update_round_trip(self):
        J = self.jobs
        d = self.new()
        self.assertRegex(d["id"], r"^doc_[0-9a-f]{12}$")
        self.assertEqual((d["status"], d["file_size"], d["stuck"], d["result"]),
                         ("queued", 1234, False, None))
        self.assertEqual(J.get_vendor_document(d["id"])["customer"]["display_name"],
                         "Pat Testerson")
        self.assertEqual(J.update_vendor_document(d["id"], "processing")["status"], "processing")
        result = {"vendor": "Ferguson", "document_type": "quote", "lines": [{"name": "A"}] * 3,
                  "checks": [{"level": "warn"}, {"level": "ok"}, {"level": "warn"}, "junk"]}
        got = J.update_vendor_document(d["id"], "done", result=result)
        self.assertEqual(got["result"], result)
        J.update_vendor_document(d["id"], "failed", error="This file is not a PDF.")
        self.assertEqual(J.get_vendor_document(d["id"])["error"], "This file is not a PDF.")
        self.assertIsNone(J.get_vendor_document("doc_000000000000"))
        self.assertIsNone(J.update_vendor_document("doc_000000000000", "done", result={}))
        with self.assertRaises(J.JobError):
            J.update_vendor_document(d["id"], "bogus")
        with self.assertRaises(J.JobError):
            J.create_vendor_document("")

    def test_file_key_is_unique(self):
        from psycopg import errors
        d = self.new()
        with self.assertRaises(errors.UniqueViolation):
            self.jobs.create_vendor_document(d["file_key"])

    def test_list_newest_first_filter_paging_and_garbage_json(self):
        J = self.jobs
        a = self.new()
        time.sleep(0.01)
        b = self.new()
        time.sleep(0.01)
        c = self.new()
        J.update_vendor_document(a["id"], "done", result={
            "vendor": "Onyx", "document_type": "order", "lines": [{}, {}],
            "checks": [{"level": "warn"}, {"level": "info"}, 7]})
        J.update_vendor_document(b["id"], "done", result=["not", "an", "object"])
        J.update_vendor_document(c["id"], "done", result={"vendor": 5, "lines": "x",
                                                          "checks": {"level": "warn"}})
        page = J.list_vendor_documents(customer_id=self.cid)
        self.assertEqual(page["total"], 3)
        self.assertEqual([i["id"] for i in page["items"]], [c["id"], b["id"], a["id"]])
        by = {i["id"]: i for i in page["items"]}
        self.assertEqual((by[a["id"]]["vendor"], by[a["id"]]["document_type"],
                          by[a["id"]]["line_count"], by[a["id"]]["warn_count"]),
                         ("Onyx", "order", 2, 1))
        for x in (b, c):
            self.assertEqual((by[x["id"]]["vendor"], by[x["id"]]["line_count"],
                              by[x["id"]]["warn_count"]), (None, None, None))
        self.assertNotIn("result", by[a["id"]])
        p2 = J.list_vendor_documents(customer_id=self.cid, offset=1, limit=1)
        self.assertEqual(([i["id"] for i in p2["items"]], p2["total"]), ([b["id"]], 3))
        p3 = J.list_vendor_documents(customer_id=self.cid, offset=50)
        self.assertEqual((p3["items"], p3["total"]), ([], 3))
        self.assertGreaterEqual(J.list_vendor_documents()["total"], 3)       # all customers
        self.assertEqual(J.list_vendor_documents(customer_id="1")["items"], [])

    def test_delete_rules_and_stuck_rows(self):
        J = self.jobs
        for status in ("queued", "processing"):
            d = self.new()
            if status == "processing":
                J.update_vendor_document(d["id"], "processing")
            out = J.delete_vendor_document(d["id"])
            self.assertFalse(out["deleted"])
            self.assertIn("still being read", out["reason"])
            self.assertIsNotNone(J.get_vendor_document(d["id"]))
            self.age(d["id"], J.VENDOR_DOC_STUCK_MINUTES + 1)       # worker died long ago
            self.assertTrue(J.get_vendor_document(d["id"])["stuck"])
            self.assertTrue(J.list_vendor_documents(customer_id=self.cid)["items"][0]["stuck"])
            self.assertTrue(J.delete_vendor_document(d["id"])["deleted"])
            self.assertIsNone(J.get_vendor_document(d["id"]))
        d = self.new()
        J.update_vendor_document(d["id"], "done", result={})
        self.age(d["id"], 600)
        self.assertFalse(J.get_vendor_document(d["id"])["stuck"])    # done is never stuck
        out = J.delete_vendor_document(d["id"])
        self.assertTrue(out["deleted"])
        self.assertEqual(out["document"]["file_key"], d["file_key"])
        self.assertIsNone(J.delete_vendor_document(d["id"]))

    def test_delete_waits_for_a_row_lock_then_sees_the_new_status(self):
        J = self.jobs
        d = self.new()
        J.update_vendor_document(d["id"], "processing")
        got = {}
        conn = J.pool.getconn()
        try:
            conn.execute("SELECT 1 FROM vendor_documents WHERE id = %s FOR UPDATE", [d["id"]])
            t = threading.Thread(target=lambda: got.update(out=J.delete_vendor_document(d["id"])))
            t.start()
            time.sleep(0.3)
            self.assertTrue(t.is_alive())                   # blocked on the worker's lock
            conn.execute("UPDATE vendor_documents SET status = 'done' WHERE id = %s", [d["id"]])
            conn.commit()
        finally:
            J.pool.putconn(conn)
        t.join(5)
        self.assertTrue(got["out"]["deleted"])


if __name__ == "__main__":
    unittest.main()
