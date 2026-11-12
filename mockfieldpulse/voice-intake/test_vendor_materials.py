"""Stage P2: one combined vendor material list per customer. Offline: no network,
no Postgres, no Redis (reuses test_fieldpulse_sync's env scrub, network block and
stubs, and test_documents' FakeDocsDB). All data is MADE UP.

Covers vendor_materials.build_material_list (merge rules, prices, flags, notes, caps),
fieldpulse_sync.sync_vendor_materials (mode gates, POST then PUT, PUT 404 -> POST,
unknown outcomes, missing placeholder, empty list, lease/resync), the worker hooks and
the /documents/customers routes. The real SQL is in test_vendor_materials_db.py.

Run: /tmp/vi-py314/bin/python -m unittest test_vendor_materials
"""

import os
import unittest
from unittest import mock

import test_fieldpulse_sync  # noqa: F401  (env scrub, network block, stubs)
from test_documents import CUSTOMER, FakeDocsDB, RouteBase, new_doc, worker  # noqa: E402

import documents_panel as dp  # noqa: E402
import fieldpulse_sync as fs  # noqa: E402
import vendor_materials as vm  # noqa: E402

CID = "18809123"
PLACEHOLDER = "900001"


def line(part, name, qty=None, price=None, total=None, **kw):
    return {"part_number": part, "name": name, "details": kw.get("details"),
            "notes": kw.get("notes"), "quantity": qty, "unit": kw.get("unit"),
            "unit_price": price, "line_total": total}


def doc(n, doc_type, vendor, lines, totals=None, status="done", customer=None):
    return {"id": f"doc_{n:012x}", "status": status, "customer_id": CID,
            "customer": customer or dict(CUSTOMER), "filename": f"Made up {n}.pdf",
            "created_at": f"2026-11-08T10:{n:02d}:00+00:00", "stuck": False,
            "result": {"vendor": vendor, "document_type": doc_type,
                       "document_number": f"MU-{n}", "lines": lines,
                       "totals": totals or {"subtotal": None, "tax": None, "freight": None,
                                            "total": None}}}


def selection_and_quote():
    """A portfolio (selection, no prices) + a priced quote with brand-prefixed codes."""
    sel = doc(1, "selection", "Ferguson Home", [
        line("MOE6410", "Moen Align single-handle faucet", details="Brushed nickel"),
        line("SP-DSHDR533472001", "Frameless shower door 60 in"),
        line("K-TS21947-4Y-CP", "Kohler shower trim kit"),
        line("MYB5103BN", "Towel bar 24 in"),
        line(None, "Grab bar (finish TBD)"),
    ])
    quote = doc(2, "quote", "Ferguson Enterprises", [
        line("6410", "FCT MOEN ALIGN 1H", 2, 189.0, 378.0),
        line("SHDR-5334720-01", "DOOR SHWR FRMLS 60", 1, 899.5, 899.5),
        line("KTS21947-4Y-CP", "TRIM KIT SHWR", 1, 120.25, 120.25),
        line("YB5103BN", "BAR TOWEL 24", 3, 30.0, 90.0),
        line("ZZV88001", "Pressure balance valve", 1, 80.0, 80.0),
        line(None, "Freight", None, None, 45.0),
    ], totals={"subtotal": 1567.75, "tax": 120.5, "freight": 45.0, "total": 1733.25})
    return [sel, quote]


def by_sku(built):
    return {ln["sku"]: ln for s in built["sections"] for ln in s["lines"]}


# --- Builder ----------------------------------------------------------------------------

class TestPartMatching(unittest.TestCase):
    def test_brief_examples_match(self):
        for a, b in (("MOE6410", "6410"), ("A2403128020", "2403128.020"),
                     ("SP-DSHDR533472001", "SHDR-5334720-01"), ("MYB5103BN", "YB5103BN"),
                     ("K-TS21947-4Y-CP", "KTS21947-4Y-CP"), ("abc-123", "ABC 123")):
            with self.subTest(a=a, b=b):
                self.assertTrue(vm.same_part(vm.normalize_part(a), vm.normalize_part(b)))
                self.assertTrue(vm.same_part(vm.normalize_part(b), vm.normalize_part(a)))

    def test_non_matches(self):
        for a, b in (("AB123", "123"),          # shorter part < 4 chars
                     ("WXYZ12345", "12345"),    # 4-letter prefix
                     ("12A3456", "3456"),       # prefix not letters
                     ("6410X", "6410"),         # suffix, not prefix, differs
                     ("K-1", "K1"),             # too short to compare at all
                     ("MOE6410", "MOE6411")):
            with self.subTest(a=a, b=b):
                self.assertFalse(vm.same_part(vm.normalize_part(a), vm.normalize_part(b)))


class TestBuild(unittest.TestCase):
    def test_selection_plus_priced_quote(self):
        built = vm.build_material_list(selection_and_quote())
        self.assertEqual([s["vendor"] for s in built["sections"]], ["Ferguson"])
        lines = built["sections"][0]["lines"]
        self.assertEqual(len(lines), 6)          # 4 merged + grab bar + valve; no freight line
        got = by_sku(built)
        faucet = got["6410"]                     # sku from the priced document
        self.assertEqual(faucet["title"], "Moen Align single-handle faucet")   # selection name
        self.assertEqual((faucet["quantity"], faucet["unit_price"]), (2, 189.0))
        self.assertEqual(faucet["flags"], [])
        self.assertIn("Brushed nickel", faucet["description"])
        self.assertEqual([s["document_type"] for s in faucet["sources"]], ["selection", "quote"])
        self.assertEqual(got["SHDR-5334720-01"]["unit_price"], 899.5)
        self.assertEqual(got["KTS21947-4Y-CP"]["title"], "Kohler shower trim kit")
        self.assertEqual(got["YB5103BN"]["quantity"], 3)
        grab = got[None]
        self.assertEqual(grab["unit_price"], 0)
        self.assertEqual(grab["quantity"], 1)
        self.assertEqual(grab["flags"], ["Quantity not printed", "Not on the quote",
                                         "No price yet"])
        valve = got["ZZV88001"]
        self.assertEqual(valve["flags"], ["Not in the portfolio"])
        self.assertTrue(any("tax $120.50" in n and "freight $45.00" in n and "freight line" in n
                            for n in built["notes"]), built["notes"])
        self.assertFalse(any("freight" in ln["title"].lower() for ln in lines))
        self.assertEqual((built["document_count"], built["line_count"]), (2, 6))

    def test_unpriced_order_then_priced_order(self):
        docs = [doc(1, "order", "Onyx Collection", [line("ONX-BASE-6030", "Shower base 60x30", 1),
                                                    line("ONX-WALL-KIT", "Wall kit")]),
                doc(2, "order", "The Onyx Collection",
                    [line("ONXBASE6030", "BASE 60X30 WHITE", None, 640.0),
                     line("ONX-WALL-KIT", "WALL KIT", 2, 410.0)])]
        built = vm.build_material_list(docs)
        self.assertEqual([s["vendor"] for s in built["sections"]], ["Onyx"])
        got = by_sku(built)
        base = got["ONXBASE6030"]
        self.assertEqual(base["unit_price"], 640.0)
        self.assertEqual(base["quantity"], 1)                 # priced doc has none -> other doc
        self.assertEqual(base["title"], "BASE 60X30 WHITE")    # no selection: latest name
        self.assertEqual(base["flags"], [])
        self.assertEqual((got["ONX-WALL-KIT"]["quantity"], got["ONX-WALL-KIT"]["unit_price"]),
                         (2, 410.0))

    def test_latest_priced_document_wins(self):
        docs = [doc(1, "quote", "Ferguson", [line("ABC12345", "Vanity 36", 1, 900.0)]),
                doc(2, "order", "Ferguson", [line("ABC12345", "Vanity 36 in", 1, 850.0)]),
                doc(3, "selection", "Ferguson", [line("ABC12345", "Vanity, 36 inch, oak")])]
        ln = vm.build_material_list(docs)["sections"][0]["lines"][0]
        self.assertEqual(ln["unit_price"], 850.0)
        self.assertEqual(ln["title"], "Vanity, 36 inch, oak")

    def test_receipt_unknown_and_null_vendors(self):
        docs = [doc(1, "receipt", "Floor & Decor",
                    [line("100123456", "Porcelain tile 12x24", 14, 2.29, unit="sq ft"),
                     line(None, "Sales Tax", None, None, 2.4)]),
                doc(2, "quote", None, [line("Q-77001", "Something", 1, 5.0)]),
                doc(3, "receipt", "FLOOR & DECOR", [line("100123456", "PORC TILE", 6, 2.19)])]
        built = vm.build_material_list(docs)
        self.assertEqual([s["vendor"] for s in built["sections"]], ["Floor & Decor", "Other"])
        tile = built["sections"][0]["lines"]
        self.assertEqual(len(tile), 1)
        self.assertEqual((tile[0]["quantity"], tile[0]["unit_price"]), (6, 2.19))
        self.assertTrue(any("sales tax line $2.40" in n for n in built["notes"]), built["notes"])

    def test_deletion_rebuild(self):
        docs = selection_and_quote()
        before = by_sku(vm.build_material_list(docs))
        self.assertEqual(before["6410"]["unit_price"], 189.0)
        after = vm.build_material_list(docs[:1])          # the quote was deleted
        got = by_sku(after)
        self.assertEqual(got["MOE6410"]["unit_price"], 0)
        self.assertEqual(got["MOE6410"]["flags"], ["Quantity not printed", "No price yet"])
        self.assertEqual(after["notes"], [])
        empty = vm.build_material_list([])
        self.assertEqual((empty["sections"], empty["document_count"]), ([], 0))

    def test_only_done_documents_and_no_same_document_merge(self):
        docs = [doc(1, "quote", "Ferguson", [line("ABC12345", "Tile A", 1, 4.0),
                                             line("ABC12345", "Tile A (second run)", 2, 4.0)]),
                doc(2, "quote", "Ferguson", [line("XYZ99999", "Ignored", 1, 1.0)],
                    status="processing"),
                {**doc(3, "quote", "Ferguson", []), "result": None}]
        built = vm.build_material_list(docs)
        self.assertEqual(built["document_count"], 1)
        self.assertEqual([ln["quantity"] for ln in built["sections"][0]["lines"]], [1, 2])

    def test_never_invents_a_price(self):
        docs = [doc(1, "quote", "Ferguson", [line("ABC12345", "Tile", 3, None, 30.0)])]
        ln = vm.build_material_list(docs)["sections"][0]["lines"][0]
        self.assertEqual(ln["unit_price"], 0)
        self.assertIn("No price yet", ln["flags"])

    def test_caps(self):
        many = [line(f"PN{i:06d}", "T" * 400, 1, 1.0, details="d" * 900, notes="n" * 400)
                for i in range(250)]
        built = vm.build_material_list([doc(1, "quote", "Ferguson", many)])
        lines = built["sections"][0]["lines"]
        self.assertEqual(len(lines), vm.MAX_LINES)
        self.assertEqual(built["line_count"], 200)
        self.assertTrue(all(len(ln["title"]) <= 255 and len(ln["description"]) <= 1000
                            for ln in lines))
        self.assertIn("Only the first 200 of 250 lines are listed.", built["notes"])

    def test_malformed_input_is_tolerated(self):
        docs = [None, "x", {**doc(1, "quote", "Ferguson", ["bad", {"name": None}]),
                            }, doc(2, "quote", {"x": 1}, [line("ABC12345", None, True, "9")])]
        built = vm.build_material_list(docs)
        ln = built["sections"][0]["lines"][0]
        self.assertEqual(built["sections"][0]["vendor"], "Other")
        self.assertEqual((ln["title"], ln["quantity"], ln["unit_price"]), ("ABC12345", 1, 0))


# --- Sync -------------------------------------------------------------------------------

class FakeVendorStore:
    """jobs.py's vendor_material_lists rules in memory: claim takes the lease and
    consumes the resync flags; a busy claim flags resync; mark-sending sets
    create_pending + clears the id in one write; finish always releases the lease,
    leaves the resync flags for the next claim and only changes list_id /
    create_pending / held when given."""

    def __init__(self, docs=(), row=None):
        self.docs = list(docs)
        self.row = row
        self.busy = False
        self.resync = self.resync_manual = False
        self.finishes = []
        self.loads = 0

    def _ensure(self, cid):
        if self.row is None:
            self.row = {"customer_id": cid, "fieldpulse_list_id": None, "status": "new",
                        "last_response": None}
        self.row.setdefault("create_pending", False)
        self.row.setdefault("held", False)

    def claim_vendor_material_list(self, cid, manual=False):
        self._ensure(cid)
        if self.busy:
            self.resync, self.resync_manual = True, self.resync_manual or manual
            return None
        self.busy = True
        out = {**self.row, "resync_manual": self.resync_manual}
        self.resync = self.resync_manual = False
        return out

    def get_vendor_material_list(self, cid):
        return dict(self.row) if self.row else None

    def mark_vendor_material_list_sending(self, cid, request, mode):
        self.row.update(status="sending", last_request=request, create_pending=True,
                        fieldpulse_list_id=None)

    def finish_vendor_material_list(self, cid, status, mode=None, request=None, response=None,
                                    error=None, list_id="unset", create_pending="unset",
                                    held="unset", history_entry=None):
        self.finishes.append({"status": status, "request": request, "error": error,
                              "list_id": list_id})
        self.row.update(status=status, last_request=request, last_response=response,
                        last_error=error)
        for k, v in (("fieldpulse_list_id", list_id), ("create_pending", create_pending),
                     ("held", held)):
            if v != "unset":
                self.row[k] = v
        self.busy = False
        return self.resync, self.resync_manual

    def list_customer_vendor_documents(self, cid, limit=500):
        self.loads += 1
        return list(self.docs)


class SyncBase(unittest.TestCase):
    ENV = {"FIELDPULSE_WRITE_MODE": "live", "FIELDPULSE_WRITE_BASE_URL": "https://fp.invalid",
           "FIELDPULSE_WRITE_API_KEY": "k-test", "FIELDPULSE_MATERIAL_ITEM_ID": PLACEHOLDER}

    def setUp(self):
        self.store = FakeVendorStore(selection_and_quote())
        for p in (mock.patch.object(fs, "_STORE", self.store),
                  mock.patch.dict(os.environ, self.ENV)):
            p.start()
            self.addCleanup(p.stop)
        self.sent = []
        self.responses = []

        def fake_send(request, cfg):
            self.sent.append((request["method"], request["path"], request["body"]))
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        p = mock.patch.object(fs, "_send", side_effect=fake_send)
        p.start()
        self.addCleanup(p.stop)

    def sync(self, manual=False, documents=None):
        return fs.sync_vendor_materials(CID, None, documents, manual=manual)


class TestSyncGates(SyncBase):
    def test_off_touches_nothing(self):
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "off"}):
            out = self.sync()
        self.assertEqual(out["status"], "off")
        self.assertIsNone(self.store.row)
        self.assertEqual(self.sent, [])

    def test_dry_run_records_only(self):
        for mode in ("dry_run", "", "bogus"):
            with self.subTest(mode=mode), mock.patch.dict(os.environ,
                                                          {"FIELDPULSE_WRITE_MODE": mode}):
                out = self.sync(manual=True)
                self.assertEqual(out["status"], "dry_run")
                self.assertEqual(self.store.row["last_request"]["method"], "POST")
                self.assertEqual(self.store.row["last_request"]["mode"], "dry_run")
        self.assertEqual(self.sent, [])

    def test_test_customer_only(self):
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "test_customer_only",
                                          "FIELDPULSE_TEST_CUSTOMER_ID": "424242"}):
            out = self.sync(manual=True)
            self.assertEqual(out["status"], "skipped")
            self.assertIn("not the test customer", out["message"])
            self.assertEqual(self.sent, [])
            self.responses = [(201, {"error": False, "response": {"id": 5551}})]
            with mock.patch.dict(os.environ, {"FIELDPULSE_TEST_CUSTOMER_ID": CID}):
                out = self.sync()
        self.assertEqual(out["status"], "sent")
        self.assertEqual(self.store.row["fieldpulse_list_id"], 5551)

    def test_send_config_missing(self):
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_API_KEY": ""}):
            out = self.sync()
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.sent, [])

    def test_missing_placeholder_needs_config(self):
        with mock.patch.dict(os.environ, {"FIELDPULSE_MATERIAL_ITEM_ID": ""}):
            out = self.sync()
        self.assertEqual(out["status"], "needs_config")
        self.assertIn("FIELDPULSE_MATERIAL_ITEM_ID is not set", out["error"])
        self.assertEqual(dp.list_status("needs_config"), "attention")
        self.assertEqual(self.sent, [])

    def test_mock_customer_not_linked(self):
        out = fs.sync_vendor_materials("cust_ab12", None, [])
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.sent, [])


class TestSyncSend(SyncBase):
    def test_post_then_put_full_body(self):
        self.responses = [(201, {"error": False, "response": {"id": 7001}}),
                          (200, {"error": False, "response": {"id": 7001}})]
        first = self.sync()
        self.assertEqual((first["status"], first["list_id"]), ("sent", 7001))
        method, path, body = self.sent[0]
        self.assertEqual((method, path), ("POST", "/material-lists"))
        self.assertEqual(body["customer_id"], int(CID))
        self.assertIs(body["is_template"], False)
        self.assertEqual(body["title"], "Vendor materials: Pat Testerson")
        self.assertTrue(body["notes"].startswith("Built from 2 vendor documents uploaded in "
                                                 "the 3 Day app. Prices are from vendor documents;"
                                                 " $0 = no price yet. This list is replaced on "
                                                 "every update — edit Mike's own material list "
                                                 "instead."))
        self.assertIn("tax $120.50", body["notes"])
        g = body["groupings"][0]
        self.assertEqual({k: g[k] for k in ("id", "order_number", "title", "type",
                                            "component_visibility", "price_visibility")},
                         {"id": None, "order_number": 1, "title": "Ferguson", "type": "group",
                          "component_visibility": "all", "price_visibility": "group"})
        faucet = next(li for li in g["line_items"] if li.get("sku") == "6410")
        self.assertEqual(faucet, {"id": None, "item_id": int(PLACEHOLDER),
                                  "title": "Moen Align single-handle faucet", "sku": "6410",
                                  "description": "Brushed nickel.", "quantity": 2,
                                  "unit_price": 189.0, "unit_cost": 189.0, "type": "product"})
        grab = next(li for li in g["line_items"] if "sku" not in li)
        self.assertEqual((grab["unit_price"], grab["unit_cost"]), (0, 0))

        second = self.sync()
        self.assertEqual(second["status"], "sent")
        self.assertEqual(self.sent[1][:2], ("PUT", "/material-lists/7001"))
        self.assertEqual(self.sent[1][2], body)
        self.assertEqual(self.store.row["fieldpulse_list_id"], 7001)

    def test_put_404_creates_a_new_list(self):
        self.store.row = {"customer_id": CID, "fieldpulse_list_id": 7001, "status": "sent"}
        self.responses = [(404, {"error": True, "message": "Not found"}),
                          (201, {"error": False, "response": {"id": 7002}})]
        out = self.sync()
        self.assertEqual([s[:2] for s in self.sent],
                         [("PUT", "/material-lists/7001"), ("POST", "/material-lists")])
        self.assertEqual((out["status"], self.store.row["fieldpulse_list_id"]), ("sent", 7002))

    # F-88: a list deleted in the FieldPulse UI is soft-deleted; PUT on it answers 500.
    def _put_500(self, *after):
        self.store.row = {"customer_id": CID, "fieldpulse_list_id": 7001, "status": "sent"}
        self.responses = [(500, {"error": True, "message": "Server Error"}), *after]
        return self.sync()

    def test_f88_put_500_list_deleted_creates_a_new_list(self):
        out = self._put_500((200, {"error": False, "response": {
                                "id": 7001, "customer_id": int(CID),
                                "deleted_at": "2026-11-10 14:02:11"}}),
                            (201, {"error": False, "response": {"id": 7002}}))
        self.assertEqual([s[:2] for s in self.sent],
                         [("PUT", "/material-lists/7001"), ("GET", "/material-lists/7001"),
                          ("POST", "/material-lists")])
        self.assertIsNone(self.sent[1][2])                           # GET has no body
        self.assertEqual((out["status"], out["list_id"]), ("sent", 7002))
        self.assertEqual(self.store.row["fieldpulse_list_id"], 7002)
        self.assertFalse(self.store.row["create_pending"])

    def test_f88_put_500_get_404_creates_a_new_list(self):
        out = self._put_500((404, {"error": True, "message": "Not found"}),
                            (201, {"error": False, "response": {"id": 7002}}))
        self.assertEqual([s[0] for s in self.sent], ["PUT", "GET", "POST"])
        self.assertEqual((out["status"], self.store.row["fieldpulse_list_id"]), ("sent", 7002))

    def test_f88_put_500_list_of_another_customer_creates_a_new_list(self):
        out = self._put_500((200, {"error": False, "response": {
                                "id": 7001, "customer_id": 555, "deleted_at": None}}),
                            (201, {"error": False, "response": {"id": 7002}}))
        self.assertEqual([s[0] for s in self.sent], ["PUT", "GET", "POST"])
        self.assertEqual(out["status"], "sent")

    def test_f88_put_500_live_list_is_needs_attention_no_post(self):
        for live in ({"id": 7001, "customer_id": int(CID), "deleted_at": None},
                     {"id": 7001, "customer_id": CID},              # string id, no deleted_at
                     {"id": 7001, "deleted_at": None},              # owner unknown: not gone
                     {"id": 7001, "customer_id": "junk", "deleted_at": None}):
            with self.subTest(header=live):
                self.sent.clear()
                out = self._put_500((200, {"error": False, "response": live}))
                self.assertEqual([s[0] for s in self.sent], ["PUT", "GET"])
                self.assertEqual(out["status"], "failed")
                self.assertEqual(dp.list_status("failed"), "attention")
                self.assertIn("HTTP 500", out["error"])
                self.assertEqual(self.store.row["fieldpulse_list_id"], 7001)
                self.assertFalse(self.store.row.get("create_pending"))

    def test_f88_put_500_get_error_no_post(self):
        for bad in ((500, {"error": True}), (403, "Forbidden"),
                    (200, {"error": True, "message": "x"}), (200, "not json"),
                    (200, {"error": False, "response": {"deleted_at": None}}),
                    fs._OutcomeUnknown("ReadTimeout: no response"),
                    fs._NotDelivered("connect timeout")):
            with self.subTest(get=bad):
                self.sent.clear()
                out = self._put_500(bad)
                self.assertEqual([s[0] for s in self.sent], ["PUT", "GET"])
                self.assertEqual(out["status"], "failed")
                self.assertEqual(self.store.row["fieldpulse_list_id"], 7001)

    def test_f88_put_no_answer_or_422_never_gets(self):
        self.store.row = {"customer_id": CID, "fieldpulse_list_id": 7001, "status": "sent"}
        self.responses = [fs._OutcomeUnknown("x")]
        self.assertEqual(self.sync()["status"], "failed")
        self.responses = [fs._NotDelivered("connect timeout")]
        self.assertEqual(self.sync()["status"], "failed")
        self.assertEqual([s[0] for s in self.sent], ["PUT", "PUT"])

    def test_f88_dry_run_and_gate_send_nothing(self):
        self.store.row = {"customer_id": CID, "fieldpulse_list_id": 7001, "status": "sent"}
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "dry_run"}):
            self.assertEqual(self.sync(manual=True)["status"], "dry_run")
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "test_customer_only",
                                          "FIELDPULSE_TEST_CUSTOMER_ID": "424242"}):
            self.assertEqual(self.sync(manual=True)["status"], "skipped")
        self.assertEqual(self.sent, [])

    def test_put_404_then_post_no_answer_is_held(self):
        self.store.row = {"customer_id": CID, "fieldpulse_list_id": 7001, "status": "sent"}
        self.responses = [(404, {}), fs._OutcomeUnknown("ReadTimeout: no response")]
        out = self.sync()
        self.assertEqual(out["status"], "unknown")
        self.assertIsNone(self.store.row["fieldpulse_list_id"])     # the 404 id is gone
        self.sync()                                                  # automatic: no POST
        self.assertEqual(len(self.sent), 2)

    def test_post_no_answer_held_until_manual(self):
        self.responses = [fs._OutcomeUnknown("ReadTimeout: no response")]
        out = self.sync()
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(dp.list_status("unknown"), "attention")
        out = self.sync()
        self.assertEqual((out["status"], len(self.sent)), ("unknown", 1))
        self.responses = [(201, {"error": False, "response": {"id": 7003}})]
        out = self.sync(manual=True)
        self.assertEqual((out["status"], out["list_id"]), ("sent", 7003))

    def test_worker_died_mid_post_is_held(self):
        self.store.row = {"customer_id": CID, "fieldpulse_list_id": None, "status": "sending",
                          "create_pending": True}
        out = self.sync()
        self.assertEqual((out["status"], self.sent), ("unknown", []))

    def test_post_without_id_is_unknown(self):
        self.responses = [(201, {"error": False, "response": {}})]
        self.assertEqual(self.sync()["status"], "unknown")
        self.assertIsNone(self.store.row["fieldpulse_list_id"])

    def test_put_no_answer_is_failed_and_retried(self):
        self.store.row = {"customer_id": CID, "fieldpulse_list_id": 7001, "status": "sent"}
        self.responses = [fs._OutcomeUnknown("x"), (200, {"error": False})]
        self.assertEqual(self.sync()["status"], "failed")
        self.assertEqual(self.store.row["fieldpulse_list_id"], 7001)
        self.assertEqual(self.sync()["status"], "sent")
        self.assertEqual([s[0] for s in self.sent], ["PUT", "PUT"])

    def test_http_error_and_not_delivered(self):
        self.responses = [(422, {"error": True, "message": "The title field is required."}),
                          fs._NotDelivered("connect timeout")]
        out = self.sync()
        self.assertEqual(out["status"], "failed")
        self.assertIn("HTTP 422", out["error"])
        self.assertTrue(self.store.row["create_pending"])          # only an id clears it
        out = self.sync()                                          # automatic: nothing sent
        self.assertEqual((out["status"], len(self.sent)), ("unknown", 1))
        out = self.sync(manual=True)                               # manual: POST again
        self.assertEqual((out["status"], len(self.sent)), ("failed", 2))
        self.assertIsNone(self.store.row["fieldpulse_list_id"])

    def test_no_documents_left(self):
        self.store.docs = []
        out = self.sync()                                    # no list yet: nothing to do
        self.assertEqual((out["status"], self.sent), ("skipped", []))
        self.store.row.update(fieldpulse_list_id=7001, status="sent")
        self.responses = [(200, {"error": False})]
        out = self.sync()
        self.assertEqual(out["status"], "sent")
        method, path, body = self.sent[0]
        self.assertEqual((method, path, body["groupings"]), ("PUT", "/material-lists/7001", []))
        self.assertIn("No vendor documents", body["notes"])

    def test_concurrent_request_rebuilds_with_latest_documents(self):
        extra = doc(9, "receipt", "Floor & Decor", [line("100123456", "Tile", 4, 2.0)])

        def send_and_race(request, cfg):
            self.sent.append((request["method"], request["path"], request["body"]))
            if len(self.sent) == 1:                          # a second document finished
                self.store.docs.append(extra)
                self.assertEqual(fs.sync_vendor_materials(CID)["status"], "queued")
            return (201, {"error": False, "response": {"id": 8001}}) if len(self.sent) == 1 \
                else (200, {"error": False})
        with mock.patch.object(fs, "_send", side_effect=send_and_race):
            out = self.sync()
            self.assertEqual(out["status"], "sent")
            self.assertEqual(out["follow_up"], {"manual": False})   # F-78: one more run
            self.assertTrue(self.store.resync)                      # not consumed yet
            out = self.sync()                                       # the follow-up run
        self.assertEqual(out["status"], "sent")
        self.assertNotIn("follow_up", out)
        self.assertEqual([s[:2] for s in self.sent],
                         [("POST", "/material-lists"), ("PUT", "/material-lists/8001")])
        self.assertEqual([g["title"] for g in self.sent[1][2]["groupings"]],
                         ["Ferguson", "Floor & Decor"])
        self.assertFalse(self.store.busy)

    def test_never_raises(self):
        with mock.patch.object(self.store, "claim_vendor_material_list",
                               side_effect=RuntimeError("db down")):
            out = self.sync()
        self.assertEqual(out["status"], "failed")


class TestRealSend(unittest.TestCase):
    """The real _send: PUT only to /material-lists/<id>; never another verb or path."""

    def cfg(self):
        c = fs._config()
        c.update(mode="live", base_url="https://fp.invalid", api_key="k")
        return c

    def test_put_rules(self):
        import requests
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"error": False}
        with mock.patch.object(requests, "put", return_value=resp) as put, \
                mock.patch.object(requests, "get", return_value=resp) as get, \
                mock.patch.object(requests, "post", side_effect=AssertionError("no POST")):
            self.assertEqual(fs._send({"method": "PUT", "path": "/material-lists/77",
                                       "body": {}}, self.cfg())[0], 200)
            self.assertEqual(put.call_args[0][0], "https://fp.invalid/material-lists/77")
            # F-88: a body-less GET of exactly our list path, same write target and key.
            self.assertEqual(fs._send({"method": "GET", "path": "/material-lists/77",
                                       "body": None}, self.cfg())[0], 200)
            args, kw = get.call_args
            self.assertEqual(args[0], "https://fp.invalid/material-lists/77")
            self.assertEqual(kw["headers"]["x-api-key"], "k")
            self.assertIs(kw["allow_redirects"], False)
            self.assertNotIn("json", kw)
            for method, path, body in (("PUT", "/comments/1", {}),
                                       ("PUT", "/material-lists/0", {}),
                                       ("PUT", "/material-lists/1/x", {}),
                                       ("DELETE", "/material-lists/1", {}),
                                       ("GET", "/material-lists/1", {}),
                                       ("GET", "/customers/1", None),
                                       ("GET", "/material-lists", None),
                                       ("GET", "/material-lists/1/x", None),
                                       ("GET", "/material-lists/0", None)):
                with self.subTest(method=method, path=path), \
                        self.assertRaises(fs._NotDelivered):
                    fs._send({"method": method, "path": path, "body": body}, self.cfg())
            for mode in ("dry_run", "off"):
                c = self.cfg()
                c["mode"] = mode
                with self.assertRaises(fs._NotDelivered):
                    fs._send({"method": "GET", "path": "/material-lists/1", "body": None}, c)
        self.assertEqual((put.call_count, get.call_count), (1, 1))


class TestRound2Regressions(SyncBase):
    """F-75..F-79 (review round 1 of stage P2)."""

    def unknown_create(self):
        self.responses = [fs._OutcomeUnknown("ReadTimeout: no response")]
        self.assertEqual(self.sync()["status"], "unknown")
        self.assertTrue(self.store.row["create_pending"])

    def test_f75_unknown_then_delete_all_then_reupload(self):
        self.unknown_create()
        docs = self.store.docs
        self.store.docs = []                                     # every document deleted
        self.assertEqual(self.sync()["status"], "skipped")
        self.assertTrue(self.store.row["create_pending"])        # not cleared
        self.assertEqual(dp.list_status("skipped", None, True), "attention")
        self.store.docs = docs[:1]                               # re-upload + read
        out = self.sync()
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(out["message"], fs.VENDOR_CREATE_PENDING)
        self.assertEqual(len(self.sent), 1)                      # no automatic second POST

    def test_f75_unknown_then_config_gap_and_dry_run(self):
        self.unknown_create()
        with mock.patch.dict(os.environ, {"FIELDPULSE_MATERIAL_ITEM_ID": ""}):
            self.assertEqual(self.sync()["status"], "needs_config")
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "dry_run"}):
            self.assertEqual(self.sync()["status"], "dry_run")
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "off"}):
            self.assertEqual(self.sync()["status"], "off")
        self.assertTrue(self.store.row["create_pending"])
        self.assertIn(self.sync()["status"], ("held", "unknown"))   # automatic: nothing sent
        self.assertEqual(len(self.sent), 1)

    def test_f76_crash_after_replacement_post_dispatch(self):
        self.store.row = {"customer_id": CID, "fieldpulse_list_id": 7001, "status": "sent"}

        def put_404_then_crash(request, cfg):
            self.sent.append((request["method"], request["path"], None))
            if request["method"] == "PUT":
                return 404, {"error": True}
            raise RuntimeError("worker interrupted after the POST went out")
        with mock.patch.object(fs, "_send", side_effect=put_404_then_crash):
            self.assertEqual(self.sync()["status"], "failed")
        self.assertTrue(self.store.row["create_pending"])
        self.assertIsNone(self.store.row["fieldpulse_list_id"])  # cleared before dispatch
        self.assertFalse(self.store.busy)
        out = self.sync()                                        # automatic recovery run
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(len(self.sent), 2)                      # no PUT of 7001, no POST

    def test_f77_dry_run_is_held_until_manual_send(self):
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "dry_run"}):
            self.assertEqual(self.sync()["status"], "dry_run")
        self.assertTrue(self.store.row["held"])
        tco = {"FIELDPULSE_WRITE_MODE": "test_customer_only", "FIELDPULSE_TEST_CUSTOMER_ID": CID}
        with mock.patch.dict(os.environ, tco):
            out = self.sync()                                    # automatic after the switch
            self.assertEqual(out["status"], "held")
            self.assertEqual(self.store.row["last_request"]["method"], "POST")   # re-recorded
            self.assertEqual(self.sent, [])
            with mock.patch.dict(os.environ, {"FIELDPULSE_TEST_CUSTOMER_ID": "424242"}):
                self.assertEqual(self.sync(manual=True)["status"], "skipped")    # gate first
            self.assertTrue(self.store.row["held"])
            self.responses = [(201, {"error": False, "response": {"id": 7100}}),
                              (200, {"error": False})]
            out = self.sync(manual=True)                         # the explicit release
            self.assertEqual((out["status"], out["list_id"]), ("sent", 7100))
            self.assertFalse(self.store.row["held"])
            self.assertEqual(self.sync()["status"], "sent")      # automatic PUTs from now on
        self.assertEqual([m for m, _, _ in self.sent], ["POST", "PUT"])

    def test_f78_follow_up_run_brings_the_latest_document(self):
        late = doc(9, "receipt", "Floor & Decor", [line("100123456", "Tile", 4, 2.0)])
        posted = []

        def send(request, cfg):
            posted.append(request)
            if len(posted) == 1:                                 # a document finished meanwhile
                self.store.docs.append(late)
                self.assertEqual(fs.sync_vendor_materials(CID)["status"], "queued")
                return 201, {"error": False, "response": {"id": 7200}}
            return 200, {"error": False}
        queued = []
        with mock.patch.object(fs, "_send", side_effect=send), \
                mock.patch.object(worker.queue_setup, "enqueue_vendor_materials",
                                  side_effect=lambda cid, manual, customer: queued.append(cid)):
            self.assertEqual(worker.sync_vendor_materials_job(CID), "sent")
            self.assertEqual(queued, [CID])                      # the worker queued one more run
            self.assertEqual(worker.sync_vendor_materials_job(*queued), "sent")   # RQ runs it
        self.assertEqual(queued, [CID])                          # and nothing after that
        self.assertEqual([r["method"] for r in posted], ["POST", "PUT"])
        self.assertEqual([g["title"] for g in posted[1]["body"]["groupings"]],
                         ["Ferguson", "Floor & Decor"])

    def test_f78_flag_survives_a_failed_follow_up_enqueue(self):
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "dry_run"}):
            self.sync()                                          # recorded in dry run: held
        self.store.busy = True                                   # a run holds the lease
        self.assertEqual(fs.sync_vendor_materials(CID, manual=True)["status"], "queued")
        self.store.busy = False                                  # it finished; enqueue failed
        self.assertTrue(self.store.resync_manual)                # the request is still there
        self.responses = [(201, {"error": False, "response": {"id": 7301}})]
        out = self.sync()                                        # next automatic event claims it
        self.assertEqual((out["status"], out["manual"]), ("sent", True))
        self.assertFalse(self.store.resync_manual)

    def test_f79_flags_survive_the_description_cap_in_the_body(self):
        sel = doc(1, "selection", "Ferguson", [line("ABC12345", "Vanity", details="x" * 1000,
                                                    notes="y" * 500, unit="EA")])
        quote = doc(2, "quote", "Ferguson", [line("ZZZ99999", "Other", 1, 5.0)])
        self.store.docs = [sel, quote]
        self.responses = [(201, {"error": False, "response": {"id": 7400}})]
        self.sync()
        lines = self.sent[0][2]["groupings"][0]["line_items"]
        vanity = next(li for li in lines if li["sku"] == "ABC12345")
        self.assertEqual((vanity["quantity"], vanity["unit_price"]), (1, 0))
        self.assertLessEqual(len(vanity["description"]), 1000)
        self.assertTrue(vanity["description"].startswith(
            "Quantity not printed; Not on the quote; No price yet. "), vanity["description"][:80])
        self.assertTrue(vanity["description"].endswith("…"))

    def test_sync_pending_helper(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        iso = lambda d: (now - timedelta(seconds=d)).isoformat()   # noqa: E731
        self.assertTrue(dp.sync_pending(iso(10), None))
        self.assertTrue(dp.sync_pending(iso(10), iso(60)))       # list older than the doc
        self.assertFalse(dp.sync_pending(iso(60), iso(10)))      # list newer: synced
        self.assertFalse(dp.sync_pending(iso(900), None))        # too old (e.g. mode off)
        self.assertFalse(dp.sync_pending(None, None))


# --- Worker -----------------------------------------------------------------------------

class TestWorkerHooks(unittest.TestCase):
    def test_done_document_queues_the_rebuild(self):
        db = FakeDocsDB([new_doc(status="queued", result=None)])
        result = {"vendor": "Ferguson", "document_type": "quote", "lines": [], "checks": []}
        with db.patched(), mock.patch.object(worker, "download_bytes", return_value=b"%PDF"), \
                mock.patch.object(worker.vendor_documents, "read_vendor_document",
                                  return_value=result), \
                mock.patch.object(worker.queue_setup, "enqueue_vendor_materials") as enq:
            worker.process_vendor_document("doc_0123456789ab")
        enq.assert_called_once_with(CID, False, None)
        self.assertEqual(db.docs["doc_0123456789ab"]["status"], "done")

    def test_queue_failure_never_changes_the_document(self):
        db = FakeDocsDB([new_doc(status="queued", result=None)])
        with db.patched(), mock.patch.object(worker, "download_bytes", return_value=b"%PDF"), \
                mock.patch.object(worker.vendor_documents, "read_vendor_document",
                                  return_value={"lines": [], "checks": []}), \
                mock.patch.object(worker.queue_setup, "enqueue_vendor_materials",
                                  side_effect=ConnectionError("redis down")):
            worker.process_vendor_document("doc_0123456789ab")
        self.assertEqual(db.docs["doc_0123456789ab"]["status"], "done")

    def test_failed_document_queues_nothing(self):
        db = FakeDocsDB([new_doc(status="queued", result=None)])
        with db.patched(), mock.patch.object(worker, "download_bytes", return_value=b"nope"), \
                mock.patch.object(worker.queue_setup, "enqueue_vendor_materials") as enq:
            worker.process_vendor_document("doc_0123456789ab")
        enq.assert_not_called()
        self.assertEqual(db.docs["doc_0123456789ab"]["status"], "failed")

    def test_sync_job_loads_documents_under_the_lease(self):
        with mock.patch.object(worker, "sync_vendor_materials",
                               return_value={"status": "dry_run"}) as sync:
            self.assertEqual(worker.sync_vendor_materials_job(CID, True), "dry_run")
        sync.assert_called_once_with(CID, customer=None, documents=None, manual=True)


# --- Routes -----------------------------------------------------------------------------

class FakeCustomerDB:
    def __init__(self, docs, record=None):
        self.docs, self.record = docs, record

    def patched(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(mock.patch.object(dp.jobs, "list_customer_vendor_documents",
                                              lambda cid, limit=500: [d for d in self.docs
                                                                      if d["customer_id"] == cid]
                                              [:limit]))
        stack.enter_context(mock.patch.object(dp.jobs, "get_vendor_material_list",
                                              lambda cid: self.record if cid == CID else None))
        stack.enter_context(mock.patch.object(dp.jobs, "list_vendor_document_customers",
                                              lambda limit=500: [{
                                                  "customer_id": CID, "customer": CUSTOMER,
                                                  "document_count": 2,
                                                  "last_upload_at": "2026-11-08T10:02:00+00:00",
                                                  "processing_count": 0, "warn_count": 1,
                                                  "list_status": "dry_run", "list_mode": "dry_run",
                                                  "fieldpulse_list_id": None, "list_error": None,
                                                  "list_updated_at": None}]))
        return stack


class TestRoutes(RouteBase):
    def test_new_routes_require_a_clerk_session(self):
        app = self.orch.app
        with mock.patch.dict(app.before_request_funcs,
                             {None: [self.orch._require_clerk_session]}), \
                FakeCustomerDB(selection_and_quote()).patched(), \
                mock.patch.object(dp.queue_setup, "enqueue_vendor_materials") as enq:
            for method, path in (("get", "/documents/customers"),
                                 ("get", f"/documents/customers/{CID}"),
                                 ("post", f"/documents/customers/{CID}/send")):
                with self.subTest(path=path):
                    self.assertEqual(getattr(self.client, method)(path).status_code, 401)
        enq.assert_not_called()

    def test_customer_list(self):
        with self.unauthed(), FakeCustomerDB(selection_and_quote()).patched():
            body = self.client.get("/documents/customers").get_json()
        self.assertEqual(body["total"], 1)
        item = body["items"][0]
        self.assertEqual((item["customer_id"], item["customer_name"], item["document_count"],
                          item["fieldpulse_status"], item["fieldpulse_label"]),
                         (CID, "Pat Testerson", 2, "dry_run", "Dry run"))
        self.assertNotIn("phone", repr(item))

    def test_customer_detail_preview_and_record(self):
        record = {"customer_id": CID, "fieldpulse_list_id": 7001, "status": "failed",
                  "mode": "live", "last_error": "HTTP 500",
                  "last_request": {"method": "PUT", "path": "/material-lists/7001", "body": {}},
                  "last_response": {"http_status": 500}, "history": [],
                  "updated_at": "2026-11-08T11:00:00+00:00"}
        with self.unauthed(), FakeCustomerDB(selection_and_quote(), record).patched(), \
                mock.patch("requests.post", side_effect=AssertionError("no HTTP")), \
                mock.patch("requests.put", side_effect=AssertionError("no HTTP")):
            r = self.client.get(f"/documents/customers/{CID}")
            self.assertEqual(self.client.get("/documents/customers/42").status_code, 404)
            self.assertEqual(self.client.get("/documents/customers/bad%20id").status_code, 400)
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual([d["id"] for d in body["documents"]],
                         ["doc_000000000002", "doc_000000000001"])          # newest first
        self.assertEqual(body["preview"]["title"], "Vendor materials: Pat Testerson")
        self.assertEqual(body["preview"]["line_count"], 6)
        self.assertEqual(body["preview"]["sections"][0]["vendor"], "Ferguson")
        self.assertTrue(body["preview"]["notes"].startswith("Built from 2 vendor documents"))
        fp = body["fieldpulse"]
        self.assertEqual((fp["status"], fp["label"], fp["list_id"], fp["last_error"]),
                         ("attention", "Needs attention", 7001, "HTTP 500"))
        self.assertNotIn("result", body["documents"][0])

    def test_manual_send_queues(self):
        with self.unauthed(), FakeCustomerDB(selection_and_quote()).patched(), \
                mock.patch.object(dp.queue_setup, "enqueue_vendor_materials") as enq:
            r = self.client.post(f"/documents/customers/{CID}/send")
            self.assertEqual(self.client.post("/documents/customers/42/send").status_code, 404)
        self.assertEqual(r.status_code, 202)
        enq.assert_called_once_with(CID, True, None)
        with self.unauthed(), FakeCustomerDB(selection_and_quote()).patched(), \
                mock.patch.object(dp.queue_setup, "enqueue_vendor_materials",
                                  side_effect=ConnectionError("redis down")):
            self.assertEqual(self.client.post(f"/documents/customers/{CID}/send").status_code,
                             503)

    def test_list_status_labels(self):
        self.assertEqual(dp.list_status(None), "not_sent")
        self.assertEqual(dp.list_status("new"), "not_sent")
        self.assertEqual(dp.list_status("skipped"), "not_sent")
        self.assertEqual(dp.list_status("sent"), "sent")
        self.assertEqual(dp.list_status("sending", "2000-01-01T00:00:00+00:00"), "attention")
        from datetime import datetime, timezone
        self.assertEqual(dp.list_status("sending", datetime.now(timezone.utc).isoformat()),
                         "sending")


if __name__ == "__main__":
    unittest.main()
