"""Stage E4: a LINKED Ferguson cart -> ONE draft estimate + ONE material list in
FieldPulse (fieldpulse_sync.sync_cart, the worker job, the /carts/<id>/send route, the
relink rule). Offline: no network, no Postgres, no Redis (test_carts' FakeCartDB holds
jobs.py's cart + slot rules in memory; fs._send is a fake that records every request).
All data is MADE UP. The real SQL is in test_carts_db.py.

Run: /tmp/vi-f88/bin/python -m unittest test_cart_send
"""

import json
import os
import unittest
from unittest import mock

import test_carts as tc  # noqa: F401  (env scrub, network block, stubs via test_fieldpulse_sync)
from test_ferguson_cart import cart_html, direct_email, item_html  # noqa: E402

import fieldpulse_sync as fs  # noqa: E402
import ferguson_cart  # noqa: E402

CID = "101"
OTHER_CID = "102"
PLACEHOLDER = "900001"
LIVE = {"FIELDPULSE_WRITE_MODE": "live", "FIELDPULSE_WRITE_BASE_URL": "https://fp.invalid",
        "FIELDPULSE_WRITE_API_KEY": "k-test", "FIELDPULSE_MATERIAL_ITEM_ID": PLACEHOLDER}


def ok(new_id):
    return (201, {"error": False, "response": {"id": new_id}})


PUT_OK = (200, {"error": False, "response": {}})


class Base(tc.Base):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, LIVE)
        self.env.start()
        self.addCleanup(self.env.stop)
        p = mock.patch.object(fs, "_STORE", self.db)
        p.start()
        self.addCleanup(p.stop)
        self.sent, self.responses = [], []

        def fake_send(request, cfg):
            self.sent.append((request["method"], request["path"], request["body"]))
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        p = mock.patch.object(fs, "_send", side_effect=fake_send)
        p.start()
        self.addCleanup(p.stop)
        self.c1 = self.new_cart()

    def new_cart(self, html=None, customer=CID):
        raw = tc.with_headers(direct_email(html or cart_html(project="Testproject")),
                              tc.FERGUSON_AR)
        cart_id = self.inbound(raw)[1]["cart_id"]
        if customer:
            self.link(cart_id, customer)
        return cart_id

    def link(self, cart_id, customer):
        self.db.carts[cart_id].update(
            status="linked", fieldpulse_customer_id=customer,
            customer={"id": int(customer), "display_name": "Testproject"},
            customer_match={"match": "auto", "state": "done", "reason": "x", "at": "x"})

    def sync(self, cart_id=None, manual=False):
        return fs.sync_cart(tc.USER_A, cart_id or self.c1, manual=manual)

    def cart(self, cart_id=None):
        return self.db.carts[cart_id or self.c1]

    def slot(self):
        return self.db.slots[(tc.USER_A, CID, "testproject")]

    def post(self, path, user=tc.USER_A, body=None, customers=None):
        with self.as_user(user), \
                mock.patch.object(tc.cp.customer_source, "list_customers",
                                  return_value=customers or []):
            r = self.client.post(path, json=body)
        return r.status_code, r.get_json()

    def methods(self):
        return [(m, p) for m, p, _ in self.sent]


class TestGates(Base):
    def test_dry_run_records_only(self):
        for mode in ("dry_run", "", "bogus"):
            with self.subTest(mode=mode), mock.patch.dict(os.environ,
                                                          {"FIELDPULSE_WRITE_MODE": mode}):
                out = self.sync(manual=True)
                self.assertEqual(out["status"], "dry_run")
                rec = self.cart()["fieldpulse"]
                self.assertEqual(rec["steps"]["estimate"]["request"]["method"], "POST")
                self.assertEqual(rec["steps"]["estimate"]["request"]["path"], "/estimates")
                self.assertEqual(rec["steps"]["material_list"]["request"]["path"],
                                 "/material-lists")
                self.assertEqual(rec["steps"]["estimate"]["request"]["mode"], "dry_run")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.cart()["status"], "linked")      # still relinkable
        self.assertTrue(self.slot()["held"])

    def test_off_does_nothing(self):
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "off"}):
            self.assertEqual(self.sync()["status"], "off")
        self.assertIsNone(self.cart()["fieldpulse"])
        self.assertEqual(self.sent, [])

    def test_test_customer_only(self):
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "test_customer_only",
                                          "FIELDPULSE_TEST_CUSTOMER_ID": "424242"}):
            out = self.sync(manual=True)
            self.assertEqual(out["status"], "skipped")
            self.assertIn("not the test customer", out["message"])
            self.assertEqual((self.sent, self.cart()["status"]), ([], "linked"))
            self.responses = [ok(5001), ok(6001)]
            with mock.patch.dict(os.environ, {"FIELDPULSE_TEST_CUSTOMER_ID": CID}):
                out = self.sync()
        self.assertEqual(out["status"], "sent")
        self.assertEqual((out["estimate_id"], out["list_id"]), (5001, 6001))

    def test_not_linked_and_mock_customer(self):
        c = self.new_cart(cart_html(project="Testproject", subtotal="$1,502.76",
                                    expires="12/11/2026"), customer=None)
        self.assertEqual(self.sync(c)["status"], "skipped")
        self.db.carts[c].update(status="linked", fieldpulse_customer_id="cust_mock3")
        out = self.sync(c)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.sent, [])

    def test_missing_placeholder_needs_config(self):
        with mock.patch.dict(os.environ, {"FIELDPULSE_MATERIAL_ITEM_ID": ""}):
            out = self.sync()
        self.assertEqual(out["status"], "needs_config")
        self.assertEqual(self.sent, [])

    def test_not_ok_and_missing_price_refused(self):
        self.cart()["ok"] = False
        out = self.sync(manual=True)
        self.assertEqual(out["status"], "refused")
        self.assertIn("couldn't be read completely", out["message"])
        self.cart()["ok"] = True
        self.cart()["parsed"]["items"][1]["unit_price"] = None
        out = self.sync(manual=True)
        self.assertEqual(out["status"], "refused")
        self.assertIn("Item 2 has no readable price", out["message"])
        self.cart()["parsed"]["items"][1].update(unit_price="46.67", quantity=None)
        self.assertIn("Item 2 has no readable quantity", self.sync(manual=True)["message"])
        self.assertEqual((self.sent, self.cart()["status"]), ([], "linked"))
        with self.as_user(tc.USER_A):
            r = self.client.post(f"/carts/{self.c1}/send")
            d = self.client.get(f"/carts/{self.c1}").get_json()
        self.assertEqual(r.status_code, 409)
        self.assertIn("readable quantity", r.get_json()["error"])
        self.assertEqual((d["fieldpulse"]["state"], d["fieldpulse"]["can_send"]),
                         ("refused", False))
        self.assertEqual(self.sends, [])


class TestSend(Base):
    def test_first_send_posts_both(self):
        self.responses = [ok(5001), ok(6001)]
        out = self.sync()
        self.assertEqual(out["status"], "sent")
        self.assertEqual(self.methods(), [("POST", "/estimates"), ("POST", "/material-lists")])
        est, lst = self.sent[0][2], self.sent[1][2]
        self.assertNotIn("status_id", est)                    # stays a draft
        self.assertEqual((est["customer_id"], est["title"]), (101, "Ferguson cart: Testproject"))
        self.assertEqual(lst["title"], "Ferguson cart: Testproject")
        self.assertEqual([g["title"] for g in lst["groupings"]], ["bathroom", "Kitchen"])
        comps = [c for g in est["line_items"] for c in g["line_components"]]
        lines = [ln for g in lst["groupings"] for ln in g["line_items"]]
        for c, ln in zip(comps, lines):
            self.assertEqual((ln["sku"], ln["quantity"], ln["unit_price"], ln["unit_cost"],
                              ln["item_id"], ln["id"]),
                             (c["sku"], c["quantity"], c["unit_price"], c["unit_price"],
                              int(PLACEHOLDER), None))
        self.assertEqual([ln["sku"] for ln in lines], ["TB-100", "OB-200-NODRAIN"])
        self.assertEqual(lst["groupings"][0]["price_visibility"], "group")
        s = self.slot()
        self.assertEqual((s["estimate_id"], s["list_id"], s["estimate_create_pending"],
                          s["list_create_pending"], s["estimate_cart_id"]),
                         (5001, 6001, False, False, self.c1))
        self.assertEqual(self.cart()["status"], "sent")
        with self.as_user(tc.USER_A):
            d = self.client.get(f"/carts/{self.c1}").get_json()
            item = self.client.get("/carts").get_json()["items"][0]
        fp = d["fieldpulse"]
        self.assertEqual((fp["state"], fp["estimate_id"], fp["list_id"], fp["can_send"]),
                         ("sent", 5001, 6001, False))
        self.assertEqual(fp["steps"]["estimate"]["response"]["http_status"], 201)
        self.assertEqual(item["fieldpulse_state"], "sent")
        self.assertFalse(d["can_link"])
        self.assertIn("already sent", d["relink_note"])
        # A second automatic run sends nothing: both targets hold this cart's lines.
        self.assertEqual(self.sync()["status"], "sent")
        self.assertEqual(len(self.sent), 2)

    def test_reforward_puts_both_and_supersedes(self):
        self.responses = [ok(5001), ok(6001)]
        self.sync()
        c2 = self.new_cart(cart_html(project="Testproject", subtotal="$1,409.42", rooms=[
            ("bathroom", [item_html("Testbrand TB-100", "TB-100", "testbrand-tb-100", 1000001,
                                    "White", "$1,409.42")])]))
        self.responses = [PUT_OK, PUT_OK]
        out = self.sync(c2)
        self.assertEqual(out["status"], "sent")
        self.assertEqual(self.methods()[2:], [("PUT", "/estimates/5001"),
                                              ("PUT", "/material-lists/6001")])
        self.assertEqual(len(self.sent[2][2]["line_items"][0]["line_components"]), 1)
        # Estimate notes: none on the POST, "" on the PUT (clears an older note); the
        # material list keeps its internal notes on both.
        self.assertNotIn("notes", self.sent[0][2])
        self.assertEqual(self.sent[2][2]["notes"], "")
        self.assertIn('"Testproject"', self.sent[3][2]["notes"])
        self.assertEqual(self.cart()["superseded_by"], c2)
        self.assertEqual(self.cart(c2)["fieldpulse"]["replaces"], self.c1)
        self.assertEqual(self.slot()["cart_id"], c2)
        # The older cart can't take the records back.
        out = self.sync(self.c1, manual=True)
        self.assertEqual(out["status"], "superseded")
        self.assertEqual(len(self.sent), 4)
        with self.as_user(tc.USER_A):
            r = self.client.post(f"/carts/{self.c1}/send")
            d = self.client.get(f"/carts/{self.c1}").get_json()
        self.assertEqual(r.status_code, 409)
        self.assertEqual((d["fieldpulse"]["state"], d["superseded_by"]), ("superseded", c2))

    def test_older_cart_sent_after_newer_is_superseded(self):
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [ok(5001), ok(6001)]
        self.sync(c2)
        out = self.sync(self.c1)
        self.assertEqual(out["status"], "superseded")
        self.assertEqual((self.cart()["superseded_by"], len(self.sent)), (c2, 2))

    def test_f109_relinked_cart_not_superseded_by_its_old_customer_slot(self):
        # C1 is dry-run for customer 101 (owns that slot, still relinkable) ...
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "dry_run"}):
            self.assertEqual(self.sync(manual=True)["status"], "dry_run")
        self.assertEqual(self.slot()["cart_id"], self.c1)
        # ... then relinked to 102 and sent there: estimate ok, material list fails.
        self.link(self.c1, OTHER_CID)
        self.responses = [ok(5001), (422, {"error": True, "message": "Made-up failure"})]
        self.assertEqual(self.sync(manual=True)["status"], "failed")
        # A newer C2 for Testproject / customer 101 is sent by hand (releases the old hold).
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [ok(5101), ok(6101)]
        out = self.sync(c2, manual=True)
        self.assertEqual(out["status"], "sent")
        self.assertIsNone(out.get("replaces"))
        self.assertIsNone(self.cart(c2)["fieldpulse"]["replaces"])
        self.assertIsNone(self.cart()["superseded_by"])          # C1 is NOT replaced
        self.assertEqual(self.slot()["cart_id"], c2)
        # C1's failed list for 102 can still be retried.
        with self.as_user(tc.USER_A):
            self.assertEqual(self.client.post(f"/carts/{self.c1}/send").status_code, 202)
        self.responses = [ok(6201)]
        out = self.sync(manual=True)
        self.assertEqual(out["status"], "sent")
        self.assertEqual(self.methods()[-1], ("POST", "/material-lists"))
        self.assertEqual(self.sent[-1][2]["customer_id"], int(OTHER_CID))

    def test_f109_stale_newer_owner_does_not_supersede_this_cart(self):
        # A NEWER cart dry-run on 101 and then relinked to 102 must not block the older
        # cart that is still on 101.
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "dry_run"}):
            self.sync(c2, manual=True)
        self.link(c2, OTHER_CID)
        self.responses = [ok(5001), ok(6001)]
        out = self.sync(manual=True)
        self.assertEqual(out["status"], "sent")
        self.assertIsNone(self.cart()["superseded_by"])
        self.assertIsNone(self.cart(c2)["superseded_by"])

    # F-109 (R3): superseded_by is LIVE (jobs._LIVE_SUPERSEDED): it only holds while the
    # replacement exists and is linked/sent to the same customer. One test per path.
    def _dry_run_pair(self):
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "dry_run"}):
            self.assertEqual(self.sync()["status"], "dry_run")
            self.assertEqual(self.sync(c2)["status"], "dry_run")
        self.assertEqual(self.cart()["superseded_by"], c2)
        return c2

    def _relink(self, cart_id, cid):
        code, body = self.post(f"/carts/{cart_id}/customer", body={"customer_id": cid},
                               customers=[{"id": int(cid), "display_name": "Synthetic"}])
        self.assertEqual(code, 200, body)
        return body

    def _view(self, cart_id=None):
        with self.as_user(tc.USER_A):
            return self.client.get(f"/carts/{cart_id or self.c1}").get_json()

    def test_f109_r3_replacement_relinked_frees_the_original(self):
        c2 = self._dry_run_pair()
        self._relink(c2, OTHER_CID)
        d = self._view()
        self.assertIsNone(d["superseded_by"])
        self.assertEqual((d["fieldpulse"]["state"], d["fieldpulse"]["can_send"],
                          d["fieldpulse"]["replaced_by"], d["fieldpulse"]["error"]),
                         ("dry_run", True, None, None))        # its own last record
        with self.as_user(tc.USER_A):
            items = self.client.get("/carts").get_json()["items"]
        row = next(i for i in items if i["id"] == self.c1)
        self.assertEqual((row["fieldpulse_state"], row["superseded_by"]), ("dry_run", None))
        self.assertEqual(self.post(f"/carts/{self.c1}/send")[0], 202)
        # The send goes to the 101 slot C2 left (released by the manual send).
        self.responses = [ok(5001), ok(6001)]
        out = self.sync(manual=True)
        self.assertEqual(out["status"], "sent")
        self.assertTrue(all(b["customer_id"] == int(CID) for _, _, b in self.sent))
        self.assertEqual(self.slot()["cart_id"], self.c1)

    def test_f109_r3_replacement_relinked_back_supersedes_again(self):
        c2 = self._dry_run_pair()
        self._relink(c2, OTHER_CID)
        self.assertIsNone(self._view()["superseded_by"])
        self._relink(c2, CID)
        self.assertEqual(self._view()["superseded_by"], c2)       # newest wins again
        self.assertEqual(self.post(f"/carts/{self.c1}/send")[0], 409)

    def test_f109_r3_replacement_deleted_frees_the_original(self):
        c2 = self._dry_run_pair()
        with self.as_user(tc.USER_A):
            self.assertEqual(self.client.delete(f"/carts/{c2}").status_code, 200)
        self.assertIsNone(self._view()["superseded_by"])
        self.assertEqual(self.post(f"/carts/{self.c1}/send")[0], 202)

    def test_f109_r3_original_relinked_is_not_replaced_on_its_new_customer(self):
        c2 = self._dry_run_pair()
        self._relink(self.c1, OTHER_CID)
        self.assertIsNone(self._view()["superseded_by"])
        self.responses = [ok(5001), ok(6001)]
        self.assertEqual(self.sync(manual=True)["status"], "sent")
        self.assertTrue(all(b["customer_id"] == int(OTHER_CID) for _, _, b in self.sent))
        self.assertEqual(self._view(c2)["superseded_by"], None)

    def test_f109_r3_replacement_relinked_during_finish(self):
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "dry_run"}):
            self.assertEqual(self.sync(c2)["status"], "dry_run")
        real = self.db.finish_cart_slot

        def racing(*args, **kwargs):
            self._relink(c2, OTHER_CID)
            return real(*args, **kwargs)
        with mock.patch.object(self.db, "finish_cart_slot", side_effect=racing):
            self.assertEqual(self.sync(manual=True)["status"], "superseded")
        self.assertIsNone(self.cart()["superseded_by"])           # not even stored
        d = self._view()
        # Its own record says 'superseded', but the replacement left: shown as sendable.
        self.assertEqual(d["fieldpulse"]["raw_status"], "superseded")
        self.assertEqual((d["superseded_by"], d["fieldpulse"]["state"], d["fieldpulse"]["error"],
                          d["fieldpulse"]["replaced_by"], d["fieldpulse"]["can_send"]),
                         (None, "not_sent", None, None, True))
        self.assertEqual(self.post(f"/carts/{self.c1}/send")[0], 202)
        self.responses = [ok(5001), ok(6001)]
        self.assertEqual(self.sync(manual=True)["status"], "sent")

    def test_f109_r3_same_customer_replacement_still_blocks(self):
        c2 = self._dry_run_pair()
        self.assertEqual(self._view()["fieldpulse"]["state"], "superseded")
        self.assertEqual(self.post(f"/carts/{self.c1}/send")[0], 409)
        self.assertEqual(self.sync(manual=True)["status"], "superseded")
        self.assertEqual(self.sent, [])

    # R4 (Nick's rule, F-114/F-115): once a NEWER cart really wrote (or tried to) to the
    # slot, the older cart is replaced FOR GOOD, even when that newer cart is deleted or
    # relinked; the way forward is to forward the cart again. One test per path.
    def _delete(self, cart_id):
        with self.as_user(tc.USER_A):
            self.assertEqual(self.client.delete(f"/carts/{cart_id}").status_code, 200)

    def _assert_replaced_for_good(self, gone=True):
        d = self._view()
        fp = d["fieldpulse"]
        self.assertEqual((fp["state"], fp["can_send"]), ("superseded", False))
        self.assertTrue(d["superseded_by"])
        self.assertEqual(fp["replaced_note"], tc.cp.REPLACED_FINAL_NOTE)
        self.assertIn("forward the cart from Ferguson again", fp["replaced_note"])
        if gone:
            self.assertIsNone(fp["replaced_by"])          # no link to a deleted cart
        with self.as_user(tc.USER_A):
            items = self.client.get("/carts").get_json()["items"]
        row = next(i for i in items if i["id"] == self.c1)
        self.assertEqual(row["fieldpulse_state"], "superseded")
        code, body = self.post(f"/carts/{self.c1}/send")
        self.assertEqual(code, 409)
        self.assertIn("forward the cart from Ferguson again", body["error"])

    def test_r4_f114_unresolved_create_then_replacement_deleted_never_posts_again(self):
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [fs._OutcomeUnknown("ReadTimeout: no response"), ok(6001)]
        self.assertEqual(self.sync(c2)["status"], "unknown")
        self.assertTrue(self.slot()["estimate_create_pending"])
        self.assertTrue(self.cart()["superseded_final_customer"])   # written at mark time
        self._delete(c2)
        self._assert_replaced_for_good()
        n = len(self.sent)
        for manual in (False, True):
            out = self.sync(manual=manual)
            self.assertEqual(out["status"], "superseded")
        self.assertEqual(len(self.sent), n)                  # no POST, no PUT
        self.assertTrue(self.slot()["estimate_create_pending"])     # warning kept on the slot

    def test_r4_f115_previously_sent_cart_stays_replaced_after_deletion(self):
        self.responses = [ok(5001), ok(6001)]
        self.assertEqual(self.sync()["status"], "sent")
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [PUT_OK, PUT_OK]
        self.assertEqual(self.sync(c2)["status"], "sent")
        self._delete(c2)
        self._assert_replaced_for_good()
        self.assertEqual((self.slot()["estimate_id"], self.slot()["list_id"]), (5001, 6001))

    def test_r4_live_replacement_view_links_and_explains(self):
        self.responses = [ok(5001), ok(6001)]
        self.sync()
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [PUT_OK, PUT_OK]
        self.sync(c2)
        fp = self._view()["fieldpulse"]
        self.assertEqual((fp["state"], fp["replaced_by"], fp["can_send"]),
                         ("superseded", c2, False))

    def test_r4_older_cart_linked_after_the_newer_sent_is_stopped_by_the_slot(self):
        # C1 (older) was not linked when C2 sent, so mark_cart_sending couldn't finalize it.
        self.db.carts[self.c1].update(status="needs_customer", fieldpulse_customer_id=None,
                                      customer=None)
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [ok(5001), ok(6001)]
        self.assertEqual(self.sync(c2)["status"], "sent")
        self.link(self.c1, CID)
        self._delete(c2)
        self.assertIsNone(self._view()["superseded_by"])     # not known yet ...
        n = len(self.sent)
        out = self.sync(manual=True)                         # ... the slot stops it
        self.assertEqual(out["status"], "superseded")
        self.assertEqual(len(self.sent), n)
        self._assert_replaced_for_good()

    def test_r4_mark_refuses_an_older_cart_when_a_newer_one_wrote(self):
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [ok(5001), ok(6001)]
        self.sync(c2)
        self._delete(c2)
        # Even if sync's own check were bypassed, the write that precedes a request refuses.
        self.assertFalse(self.db.mark_cart_sending(tc.USER_A, self.c1, CID, "testproject",
                                                   "estimate", True, {"method": "POST"},
                                                   "live"))
        self.assertEqual(self.slot()["estimate_id"], 5001)   # id kept, nothing cleared

    def test_r4_dry_run_replacement_relinked_or_deleted_still_frees_the_original(self):
        c2 = self._dry_run_pair()                            # F-109 kept: no real send
        self.assertIsNone(self.cart()["superseded_final_customer"])
        self.assertEqual(self._view()["fieldpulse"]["replaced_note"], tc.cp.REPLACED_LIVE_NOTE)
        self._delete(c2)
        self.assertIsNone(self._view()["superseded_by"])
        self.responses = [ok(5001), ok(6001)]
        self.assertEqual(self.sync(manual=True)["status"], "sent")

    def test_r4_final_supersede_ignores_replacement_relink_but_follows_own_customer(self):
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [ok(5001), ok(6001)]
        self.sync(c2)
        self.assertEqual(self.cart()["superseded_by"], c2)
        # C1 never sent (only linked), so it may be relinked: another slot, not replaced there.
        self._relink(self.c1, OTHER_CID)
        self.assertIsNone(self._view()["superseded_by"])
        self._relink(self.c1, CID)                           # back on 101: replaced again
        self.assertEqual(self._view()["fieldpulse"]["state"], "superseded")
        self.assertEqual(self.post(f"/carts/{self.c1}/send")[0], 409)

    def test_r4_newer_cart_still_replaces_after_older_real_send(self):
        self.responses = [ok(5001), ok(6001)]
        self.sync()
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [PUT_OK, PUT_OK]
        self.assertEqual(self.sync(c2)["status"], "sent")
        self.assertEqual(self.methods()[2:], [("PUT", "/estimates/5001"),
                                              ("PUT", "/material-lists/6001")])
        self.assertEqual(self.slot()["writer_cart_id"], c2)

    def test_put_404_posts_new(self):
        self.responses = [ok(5001), ok(6001)]
        self.sync()
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [(404, {"error": True, "message": "Not found"}), ok(5002), PUT_OK]
        out = self.sync(c2)
        self.assertEqual(out["status"], "sent")
        self.assertEqual(self.methods()[2:], [("PUT", "/estimates/5001"), ("POST", "/estimates"),
                                              ("PUT", "/material-lists/6001")])
        self.assertEqual((self.slot()["estimate_id"], self.slot()["list_id"]), (5002, 6001))

    def test_put_500_then_get_deleted_posts(self):
        self.responses = [ok(5001), ok(6001)]
        self.sync()
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [(500, {"error": True, "message": "Server Error"}),
                          (200, {"error": False, "response": {"id": 5001, "customer_id": 101,
                                                              "deleted_at": 1731400000}}),
                          ok(5003), PUT_OK]
        out = self.sync(c2)
        self.assertEqual(out["status"], "sent")
        self.assertEqual(self.methods()[2:], [("PUT", "/estimates/5001"),
                                              ("GET", "/estimates/5001"),
                                              ("POST", "/estimates"),
                                              ("PUT", "/material-lists/6001")])
        self.assertEqual(self.slot()["estimate_id"], 5003)

    def test_put_500_and_get_live_fails_without_post(self):
        self.responses = [ok(5001), ok(6001)]
        self.sync()
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.responses = [(500, {"error": True, "message": "Server Error"}),
                          (200, {"error": False, "response": {"id": 5001, "customer_id": 101,
                                                              "deleted_at": None}}),
                          PUT_OK]
        out = self.sync(c2)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(self.methods()[2:], [("PUT", "/estimates/5001"),
                                              ("GET", "/estimates/5001"),
                                              ("PUT", "/material-lists/6001")])
        s = self.slot()
        self.assertEqual((s["estimate_id"], s["estimate_status"], s["estimate_cart_id"],
                          s["list_cart_id"]), (5001, "failed", self.c1, c2))
        with self.as_user(tc.USER_A):
            fp = self.client.get(f"/carts/{c2}").get_json()["fieldpulse"]
        self.assertEqual((fp["state"], fp["can_send"]), ("attention", True))
        # Retry: only the estimate is sent again (PUT), the list is not re-sent.
        self.responses = [PUT_OK]
        self.assertEqual(self.sync(c2, manual=True)["status"], "sent")
        self.assertEqual(self.methods()[5:], [("PUT", "/estimates/5001")])


class TestSafety(Base):
    def test_create_pending_blocks_automatic(self):
        self.responses = [fs._OutcomeUnknown("ReadTimeout: no response"), ok(6001)]
        out = self.sync()
        self.assertEqual(out["status"], "unknown")
        s = self.slot()
        self.assertEqual((s["estimate_create_pending"], s["estimate_id"], s["list_id"]),
                         (True, None, 6001))
        out = self.sync()                                   # automatic: nothing sent
        self.assertEqual(out["status"], "unknown")
        self.assertIn("Needs attention", out["message"])
        self.assertEqual(len(self.sent), 2)
        self.responses = [ok(5009)]
        out = self.sync(manual=True)                        # manual: POST again, list untouched
        self.assertEqual(out["status"], "sent")
        self.assertEqual(self.methods()[2:], [("POST", "/estimates")])
        self.assertFalse(self.slot()["estimate_create_pending"])

    def test_post_error_answer_keeps_create_pending(self):
        self.responses = [(400, {"error": True, "message": "bad"}), ok(6001)]
        self.assertEqual(self.sync()["status"], "failed")
        self.assertTrue(self.slot()["estimate_create_pending"])
        self.sync()
        self.assertEqual(len(self.sent), 2)

    def test_held_released_only_by_manual(self):
        with mock.patch.dict(os.environ, {"FIELDPULSE_WRITE_MODE": "dry_run"}):
            self.sync()
        out = self.sync()                                   # live, automatic: held
        self.assertEqual(out["status"], "held")
        self.assertEqual(self.sent, [])
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.assertEqual(self.sync(c2)["status"], "held")   # re-forward: still held
        self.responses = [ok(5001), ok(6001)]
        out = self.sync(c2, manual=True)
        self.assertEqual(out["status"], "sent")
        self.assertFalse(self.slot()["held"])

    def test_partial_failure_retry_never_duplicates_estimate(self):
        self.responses = [ok(5001), (500, {"error": True, "message": "boom"})]
        out = self.sync()
        self.assertEqual(out["status"], "failed")
        self.assertIn("Retry sends only what failed", out["message"])
        self.responses = [ok(6001)]
        out = self.sync(manual=True)
        self.assertEqual(out["status"], "sent")
        self.assertEqual(self.methods(), [("POST", "/estimates"), ("POST", "/material-lists"),
                                          ("POST", "/material-lists")])
        self.assertEqual(self.cart()["fieldpulse"]["steps"]["estimate"]["response"]
                         ["http_status"], 201)        # the earlier request is still shown

    def test_lease_race_flags_follow_up_for_newest(self):
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        inner = {}

        def send_and_race(request, cfg):
            self.sent.append((request["method"], request["path"], request["body"]))
            if not inner:
                inner["out"] = fs.sync_cart(tc.USER_A, c2, manual=True)
            return ok(5001 + len(self.sent))
        with mock.patch.object(fs, "_send", side_effect=send_and_race):
            out = self.sync()
        self.assertEqual(inner["out"]["status"], "queued")
        self.assertEqual(len(self.sent), 2)                  # the racing run sent nothing
        self.assertEqual(out["follow_up"], {"cart_id": c2, "manual": True})
        import worker
        with mock.patch.object(worker, "sync_cart", return_value=out), \
                mock.patch.object(worker.queue_setup, "enqueue_cart_send",
                                  create=True) as enq:
            worker.send_cart_to_fieldpulse(tc.USER_A, self.c1)
        enq.assert_called_once_with(tc.USER_A, c2, True)

    def test_relink_during_send_stops_it(self):
        def relink(*a, **k):
            self.cart()["fieldpulse_customer_id"] = OTHER_CID
            return orig(*a, **k)
        orig = self.db.mark_cart_sending
        with mock.patch.object(self.db, "mark_cart_sending", side_effect=relink):
            out = self.sync()
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.slot()["cart_id"])           # slot left as it was

    def test_crash_releases_lease(self):
        with mock.patch.object(fs, "_cart_attempt", side_effect=RuntimeError("bug")):
            self.assertEqual(self.sync()["status"], "failed")
        self.assertEqual(self.db.busy_keys, set())


class TestRoutesAndLinking(Base):
    def test_relink_after_send_refused(self):
        self.responses = [ok(5001), ok(6001)]
        self.sync()
        code, body = self.post(f"/carts/{self.c1}/customer", body={"customer_id": 102},
                               customers=tc.CUSTOMERS)
        self.assertEqual(code, 409)
        self.assertIn("already sent to FieldPulse", body["error"])
        self.assertEqual(self.cart()["fieldpulse_customer_id"], CID)

    def test_link_and_auto_match_queue_a_send(self):
        code, _ = self.post(f"/carts/{self.c1}/customer", body={"customer_id": 102},
                            customers=tc.CUSTOMERS)
        self.assertEqual(code, 200)
        self.assertEqual(self.sends, [(tc.USER_A, self.c1, False)])
        c3 = self.new_cart(cart_html(project="Testproject", expires="12/12/2026"),
                           customer=None)
        with mock.patch.object(tc.cp.customer_source, "list_customers",
                               return_value=tc.CUSTOMERS):
            self.assertEqual(tc.cp.match_cart(tc.USER_A, c3), "linked")
        self.assertEqual(self.sends[-1], (tc.USER_A, c3, False))

    def test_send_route(self):
        with self.as_user(tc.USER_A):
            r = self.client.post(f"/carts/{self.c1}/send")
        self.assertEqual((r.status_code, r.get_json()["status"]), (202, "queued"))
        self.assertEqual(self.sends, [(tc.USER_A, self.c1, True)])
        with self.as_user(tc.USER_B):                        # other user's cart
            self.assertEqual(self.client.post(f"/carts/{self.c1}/send").status_code, 404)
        c = self.new_cart(cart_html(project="Testproject", expires="12/12/2026"),
                          customer=None)
        with self.as_user(tc.USER_A):
            self.assertEqual(self.client.post(f"/carts/{c}/send").status_code, 409)
        self.assertEqual(self.sent, [])                      # the route never sends

    def test_send_route_queue_down(self):
        with mock.patch.object(tc.cp.queue_setup, "enqueue_cart_send",
                               side_effect=ConnectionError("down"), create=True), \
                self.as_user(tc.USER_A):
            r = self.client.post(f"/carts/{self.c1}/send")
        self.assertEqual(r.status_code, 503)


class FakeAIResponse:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]


class TestItemTypes(Base):
    """The worker's product-type step (carts_panel.ensure_item_types): ONE Claude call per
    cart, only brand/model/finish/url slug sent, result stored and reused; AI trouble ->
    null types + flag, the send still goes ahead. Claude is always mocked."""

    def setUp(self):
        super().setUp()
        import extractor
        self.ex = extractor
        self.calls = []

    def ai(self, answer):
        """Patch the shared client's one-shot call; answer = JSON text or an Exception."""
        def create(**kw):
            self.calls.append(kw)
            if isinstance(answer, Exception):
                raise answer
            return FakeAIResponse(answer)

        def with_options(**kw):
            assert kw == {"max_retries": 0, "timeout": self.ex._TYPE_TIMEOUT_S}, kw
            return mock.Mock(messages=mock.Mock(create=create))
        return mock.patch.object(self.ex._client, "with_options", with_options)

    def ensure(self, cart_id=None):
        return tc.cp.ensure_item_types(tc.USER_A, cart_id or self.c1)

    GOOD = '{"types": [{"index": 0, "type": "Faucet"}, {"index": 1, "type": "toilet"}]}'

    def test_types_stored_once_and_used_in_titles(self):
        with self.ai(self.GOOD):
            self.assertEqual(self.ensure(), "stored")
            self.assertEqual(self.ensure(), "exists")            # no second call
        self.assertEqual(len(self.calls), 1)
        parsed = self.cart()["parsed"]
        self.assertEqual([it["item_type"] for it in parsed["items"]], ["Faucet", "Toilet"])
        self.assertEqual(parsed["item_types"]["source"], "ai")
        self.responses = [ok(5001), ok(6001)]
        self.assertEqual(self.sync()["status"], "sent")
        est, lst = self.sent[0][2], self.sent[1][2]
        comps = [c for g in est["line_items"] for c in g["line_components"]]
        self.assertEqual([c["title"] for c in comps], ["Faucet - bathroom", "Toilet - Kitchen"])
        self.assertEqual(comps[0]["description"],
                         "MODEL - Testbrand TB-100 COLOR - White NOTES -")
        self.assertEqual([ln["title"] for g in lst["groupings"] for ln in g["line_items"]],
                         ["Faucet - bathroom", "Toilet - Kitchen"])
        # Same stored types on the next send/PUT of the same project: identical titles.
        c2 = self.new_cart(cart_html(project="Testproject", expires="12/12/2026"))
        self.assertNotEqual(c2, self.c1)
        with self.ai('{"types": [{"index": 0, "type": "Faucet"}, {"index": 1, "type": "Toilet"}]}'):
            self.ensure(c2)
        self.responses = [PUT_OK, PUT_OK]
        self.sync(c2)
        self.assertEqual(self.sent[2][2]["line_items"], est["line_items"])
        with self.as_user(tc.USER_A):
            d = self.client.get(f"/carts/{self.c1}").get_json()
        self.assertEqual([it["item_type"] for it in d["cart"]["items"]], ["Faucet", "Toilet"])
        self.assertEqual(d["cart"]["item_types"]["source"], "ai")

    def test_only_brand_model_finish_slug_reach_claude(self):
        with self.ai(self.GOOD):
            self.ensure()
        sent = json.loads(self.calls[0]["messages"][0]["content"])
        self.assertEqual(set(sent), {"types", "lines"})
        self.assertEqual(sent["types"], list(ferguson_cart.ITEM_TYPES))
        for ln in sent["lines"]:
            self.assertEqual(set(ln), {"index", "brand", "model", "finish", "url_slug"})
        self.assertEqual(sent["lines"][0], {"index": 0, "brand": "Testbrand", "model": "TB-100",
                                            "finish": "White", "url_slug": "testbrand-tb-100"})
        everything = json.dumps(self.calls[0])
        for secret in ("Testproject", "testproject", "fergusonhome.com", tc.USER_A, "101"):
            self.assertNotIn(secret, everything)

    def test_unknown_or_unsure_type_is_null_and_flagged(self):
        with self.ai('{"types": [{"index": 0, "type": "Spaceship"}, {"index": 1, "type": null}]}'):
            self.ensure()
        parsed = self.cart()["parsed"]
        self.assertEqual([it["item_type"] for it in parsed["items"]], [None, None])
        self.assertEqual(parsed["item_types"]["source"], "ai")
        self.responses = [ok(5001), ok(6001)]
        self.sync()
        comps = [c for g in self.sent[0][2]["line_items"] for c in g["line_components"]]
        self.assertEqual([c["title"] for c in comps],
                         ["Testbrand TB-100 - bathroom", "Otherbrand Co OB-200-NODRAIN - Kitchen"])

    def test_f111_conflicting_duplicate_answers_are_null(self):
        lines = [{"brand": "Testbrand", "model": "TB-1"}, {"brand": "Testbrand", "model": "TB-2"}]
        vocab = ferguson_cart.ITEM_TYPES
        cases = (
            # type vs type, in both orders: null (never "first wins")
            ('[{"index":0,"type":"Faucet"},{"index":0,"type":"Toilet"},{"index":1,"type":"Toilet"}]',
             [None, "Toilet"]),
            ('[{"index":0,"type":"Toilet"},{"index":0,"type":"Faucet"}]', [None, None]),
            # type vs null, null vs type, type vs an unknown word: null
            ('[{"index":0,"type":"Faucet"},{"index":0,"type":null}]', [None, None]),
            ('[{"index":0,"type":null},{"index":0,"type":"Faucet"}]', [None, None]),
            ('[{"index":0,"type":"Faucet"},{"index":0,"type":"Spaceship"}]', [None, None]),
            # identical repeats (case/space differences only) are the same answer
            ('[{"index":0,"type":"Faucet"},{"index":0,"type":" faucet "},{"index":1,"type":"Toilet"}]',
             ["Faucet", "Toilet"]),
        )
        for rows, want in cases:
            with self.subTest(rows=rows), self.ai('{"types": %s}' % rows):
                self.assertEqual(self.ex.classify_product_types(lines, vocab), want)
        # End to end: the conflicting line is stored null and titled by brand/model.
        with self.ai('{"types": [{"index": 0, "type": "Faucet"}, {"index": 0, "type": "Toilet"},'
                     ' {"index": 1, "type": "Toilet"}]}'):
            self.ensure()
        parsed = self.cart()["parsed"]
        self.assertEqual([it["item_type"] for it in parsed["items"]], [None, "Toilet"])
        self.responses = [ok(5001), ok(6001)]
        self.sync()
        comps = [c for g in self.sent[0][2]["line_items"] for c in g["line_components"]]
        self.assertEqual(comps[0]["title"], "Testbrand TB-100 - bathroom")

    def test_ai_failure_all_null_send_goes_ahead(self):
        import anthropic
        for answer in (anthropic.APIConnectionError(request=mock.Mock()), "not json",
                       '{"types": "x"}'):
            with self.subTest(answer=str(answer)[:20]):
                self.cart()["parsed"].pop("item_types", None)
                with self.ai(answer):
                    self.assertEqual(self.ensure(), "stored")
                parsed = self.cart()["parsed"]
                self.assertEqual(parsed["item_types"]["source"], "unavailable")
                self.assertEqual([it["item_type"] for it in parsed["items"]], [None, None])
        self.responses = [ok(5001), ok(6001)]
        self.assertEqual(self.sync()["status"], "sent")

    def test_no_api_key_is_unavailable(self):
        import sys
        with mock.patch.dict(sys.modules, {"extractor": None}):    # import fails
            self.assertEqual(self.ensure(), "stored")
        self.assertEqual(self.cart()["parsed"]["item_types"]["source"], "unavailable")

    def test_job_deadline_is_not_swallowed(self):
        class JobTimeoutException(Exception):
            pass
        with self.ai(JobTimeoutException("deadline")):
            with self.assertRaises(JobTimeoutException):
                self.ensure()
        self.assertNotIn("item_types", self.cart()["parsed"])

    def test_first_result_wins(self):
        with self.ai(self.GOOD):
            self.ensure()
        stored = json.loads(json.dumps(self.cart()["parsed"]))
        self.cart()["parsed"].pop("item_types")
        self.cart()["parsed"]["item_types"] = stored["item_types"]
        self.assertFalse(self.db.set_cart_item_types(tc.USER_A, self.c1, {"items": []}))
        self.assertEqual(self.cart()["parsed"]["items"], stored["items"])

    def test_unsendable_carts_never_call_claude(self):
        unlinked = self.new_cart(customer=None)
        bad = self.new_cart(cart_html(project="Testproject", subtotal="$1.00"))
        self.cart(bad)["ok"] = False
        newer = self.new_cart(cart_html(project="Testproject", expires="12/11/2026"))
        self.cart()["superseded_by"] = newer        # live: newer is on the same customer
        with self.ai(self.GOOD):
            for cid in (unlinked, bad, self.c1, "cart_000000000fff"):
                self.assertEqual(self.ensure(cid), "skipped")
        self.assertEqual(self.calls, [])

    def test_worker_types_before_sending(self):
        import worker
        order = []
        with mock.patch.object(tc.cp, "ensure_item_types",
                               side_effect=lambda *a: order.append("types")), \
                mock.patch.object(worker, "sync_cart",
                                  side_effect=lambda *a, **k: order.append("send") or
                                  {"status": "sent"}):
            worker.send_cart_to_fieldpulse(tc.USER_A, self.c1)
        self.assertEqual(order, ["types", "send"])


class TestPure(unittest.TestCase):
    def test_safe_sentry_texts(self):
        import observability
        for msg in ("mark_cart_sending: unknown target", "finish_cart_slot: unknown slot column"):
            self.assertEqual(observability._safe_value(msg, "JobError", "jobs", None), msg)

    def test_list_body_same_lines_as_estimate(self):
        parsed = ferguson_cart.stored_cart({
            "project_name": "P", "pricing_expires": "2026-12-10",
            "items": [{"room": "Bath", "brand": "B", "model": "M1", "finish": "W",
                       "quantity": 2, "unit_price": "46.67"}]})
        est = ferguson_cart.build_estimate_body(parsed, 7, 9)
        lst = ferguson_cart.build_material_list_body(parsed, 7, 9)
        c, ln = est["line_items"][0]["line_components"][0], lst["groupings"][0]["line_items"][0]
        for k in ("title", "description", "sku", "quantity", "unit_price", "unit_cost"):
            self.assertEqual(c[k], ln[k])
        self.assertEqual(ln["unit_price"], 46.67)

    def test_stored_cart_never_guesses(self):
        for bad in ("46.6.7", "-1", "1e3", " 46.67", "", None, 46.67):
            parsed = ferguson_cart.stored_cart({"project_name": "P", "items": [
                {"model": "M", "quantity": 1, "unit_price": bad}]})
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ferguson_cart.build_estimate_body(parsed, 7, 9)

    def test_real_send_allows_only_our_estimate_put_get(self):
        import requests
        c = fs._config()
        c.update(mode="live", base_url="https://fp.invalid", api_key="k")
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"error": False}
        with mock.patch.object(requests, "put", return_value=resp) as put, \
                mock.patch.object(requests, "get", return_value=resp) as get, \
                mock.patch.object(requests, "post", side_effect=AssertionError("no POST")):
            fs._send({"method": "PUT", "path": "/estimates/77", "body": {}}, c)
            fs._send({"method": "GET", "path": "/estimates/77", "body": None}, c)
            for method, path in (("PUT", "/estimates/0"), ("PUT", "/estimates/1/status"),
                                 ("DELETE", "/estimates/1"), ("GET", "/estimates")):
                with self.subTest(path=path), self.assertRaises(fs._NotDelivered):
                    fs._send({"method": method, "path": path, "body": None}, c)
        self.assertEqual((put.call_count, get.call_count), (1, 1))


if __name__ == "__main__":
    unittest.main()
