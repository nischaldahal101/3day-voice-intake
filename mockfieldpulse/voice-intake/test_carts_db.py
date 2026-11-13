"""Real-Postgres checks for jobs.py's cart_addresses + ferguson_carts (stage E2).
SKIPPED unless VI_TEST_DATABASE_URL points at a THROWAWAY database (tables are
created and rows inserted/deleted). Made-up data only. Run:

    VI_TEST_DATABASE_URL=postgresql:///vi_carts_test \
        /tmp/vi-f88/bin/python -m unittest test_carts_db -v

Runs in its own process (it imports the real jobs.py, so don't combine it with
the offline suites that stub `jobs`).
"""

import os
import sys
import threading
import unittest
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

_URL = os.environ.get("VI_TEST_DATABASE_URL")


@unittest.skipUnless(_URL, "set VI_TEST_DATABASE_URL to a throwaway Postgres to run")
class TestCartsDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import dotenv
        dotenv.load_dotenv = lambda *a, **k: False
        os.environ["DATABASE_URL"] = _URL
        import jobs
        cls.jobs = jobs

    def setUp(self):
        tag = uuid.uuid4().hex[:8]
        self.ua, self.ub = f"user_a{tag}", f"user_b{tag}"
        self.base = f"pat{tag}"

    def new(self, user, sha=None, **kw):
        args = dict(alias="pat", sender="pat@example.invalid", message_id="<m@example.invalid>",
                    raw_key=f"ferguson-carts/{user}/{uuid.uuid4()}.eml",
                    raw_sha256=sha or uuid.uuid4().hex * 2,
                    parsed={"project_name": "Testproject", "subtotal": "12.50",
                            "items": [{"model": "TB-1"}, {"model": "TB-2"}]},
                    ok=False, warnings=["Made-up warning."], project_name="Testproject")
        args.update(kw)
        return self.jobs.create_ferguson_cart(user, **args)

    def test_alias_claim_collision_and_stability(self):
        J = self.jobs
        self.assertIsNone(J.get_cart_alias(self.ua))
        cands = [self.base, self.base + "2", self.base + "3"]
        self.assertEqual(J.ensure_cart_alias(self.ua, iter(cands)), self.base)
        self.assertEqual(J.ensure_cart_alias(self.ua, iter(["other"])), self.base)
        self.assertEqual(J.ensure_cart_alias(self.ub, iter(cands)), self.base + "2")
        self.assertEqual(J.get_cart_alias_user(self.base + "2"), self.ub)
        self.assertIsNone(J.get_cart_alias_user("nobody" + self.base))
        with self.assertRaises(J.JobError):
            J.ensure_cart_alias(f"user_c{self.base}", iter([self.base]))
        from psycopg import errors
        with self.assertRaises(errors.CheckViolation):
            J.ensure_cart_alias(f"user_d{self.base}", iter(["Bad Alias"]))

    def test_parallel_claims_give_one_alias_per_user(self):
        J = self.jobs
        got, errs = [], []

        def claim():
            try:
                got.append(J.ensure_cart_alias(self.ua, iter([self.base, self.base + "x"])))
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)
        ts = [threading.Thread(target=claim) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(set(got), {J.get_cart_alias(self.ua)})

    def test_create_duplicate_list_get_delete_scoped(self):
        J = self.jobs
        a1, created = self.new(self.ua, sha="a" * 64)
        self.assertTrue(created)
        self.assertRegex(a1["id"], r"^cart_[0-9a-f]{12}$")
        self.assertEqual((a1["status"], a1["ok"], a1["warnings"], a1["fieldpulse_customer_id"]),
                         ("needs_customer", False, ["Made-up warning."], None))
        again, created = self.new(self.ua, sha="a" * 64)
        self.assertEqual((again["id"], created), (a1["id"], False))
        self.assertEqual(J.find_cart_by_sha(self.ua, "a" * 64)["id"], a1["id"])
        b1, created = self.new(self.ub, sha="a" * 64)          # same bytes, other user
        self.assertTrue(created)
        a2, _ = self.new(self.ua, parsed=["not", "an", "object"], warnings=[])
        page = J.list_ferguson_carts(self.ua)
        self.assertEqual(page["total"], 2)
        self.assertEqual([i["id"] for i in page["items"]], [a2["id"], a1["id"]])
        by = {i["id"]: i for i in page["items"]}
        self.assertEqual((by[a1["id"]]["item_count"], by[a1["id"]]["subtotal"],
                          by[a1["id"]]["warning_count"]), (2, "12.50", 1))
        self.assertEqual((by[a2["id"]]["item_count"], by[a2["id"]]["subtotal"],
                          by[a2["id"]]["warning_count"]), (None, None, 0))
        self.assertEqual(J.list_ferguson_carts(self.ua, offset=1, limit=1)["items"][0]["id"],
                         a1["id"])
        self.assertIsNone(J.get_ferguson_cart(self.ub, a1["id"]))
        self.assertEqual(J.get_ferguson_cart(self.ua, a1["id"])["parsed"]["subtotal"], "12.50")
        self.assertIsNone(J.delete_ferguson_cart(self.ub, a1["id"]))
        gone = J.delete_ferguson_cart(self.ua, a1["id"])
        self.assertEqual(gone["raw_key"], a1["raw_key"])
        self.assertIsNone(J.get_ferguson_cart(self.ua, a1["id"]))
        self.assertIsNotNone(J.get_ferguson_cart(self.ub, b1["id"]))

    def test_set_cart_customer_conditional_and_scoped(self):     # E3
        J = self.jobs
        c, _ = self.new(self.ua)
        snap = {"id": 19237287, "display_name": "Pat Example", "phone": None}
        pending = {"match": None, "state": "pending", "reason": "Looking", "at": "x"}
        got = J.set_cart_customer(self.ua, c["id"], None, pending, ("needs_customer",))
        self.assertEqual((got["status"], got["customer"], got["customer_match"]["state"]),
                         ("needs_customer", None, "pending"))
        # Other user: nothing.
        self.assertIsNone(J.set_cart_customer(self.ub, c["id"], snap, pending,
                                              ("needs_customer", "linked")))
        auto = {"match": "auto", "state": "done", "reason": "Matched", "at": "x"}
        got = J.set_cart_customer(self.ua, c["id"], snap, auto, ("needs_customer",))
        self.assertEqual((got["status"], got["fieldpulse_customer_id"],
                          got["customer"]["display_name"], got["customer_match"]["match"]),
                         ("linked", "19237287", "Pat Example", "auto"))
        # A stale auto result (only_from needs_customer) can't touch a linked cart.
        self.assertIsNone(J.set_cart_customer(self.ua, c["id"], None, pending,
                                              ("needs_customer",)))
        manual = {"match": "manual", "state": "done", "reason": "By hand", "at": "x"}
        got = J.set_cart_customer(self.ua, c["id"], {"id": "cust_x", "display_name": "B"},
                                  manual, ("needs_customer", "linked"))
        self.assertEqual((got["fieldpulse_customer_id"], got["customer_match"]["match"]),
                         ("cust_x", "manual"))
        page = J.list_ferguson_carts(self.ua)
        item = next(i for i in page["items"] if i["id"] == c["id"])
        self.assertEqual((item["status"], item["customer_name"], item["match_state"]),
                         ("linked", "B", "done"))
        with self.assertRaises(J.JobError):
            J.set_cart_customer(self.ua, c["id"], {"id": None}, manual, ("linked",))

    def test_item_types_written_once_and_scoped(self):                 # E4 layout
        J = self.jobs
        a, _ = self.new(self.ua)
        typed = {**a["parsed"], "items": [{"model": "TB-1", "item_type": "Faucet"},
                                          {"model": "TB-2", "item_type": None}],
                 "item_types": {"source": "ai", "at": "2026-11-12T00:00:00Z"}}
        self.assertFalse(J.set_cart_item_types(self.ub, a["id"], typed))       # other user
        self.assertTrue(J.set_cart_item_types(self.ua, a["id"], typed))
        other = {**typed, "item_types": {"source": "unavailable", "at": "x"}}
        self.assertFalse(J.set_cart_item_types(self.ua, a["id"], other))       # first wins
        got = J.get_ferguson_cart(self.ua, a["id"])["parsed"]
        self.assertEqual(got["item_types"]["source"], "ai")
        self.assertEqual([i["item_type"] for i in got["items"]], ["Faucet", None])
        self.assertEqual(got["subtotal"], "12.50")

    def test_e3_columns_added_to_an_e2_table(self):
        """An E2-shaped ferguson_carts (no E3 columns) gets them from the bootstrap DDL."""
        J = self.jobs
        with J.pool.connection() as conn:
            conn.execute("ALTER TABLE ferguson_carts DROP COLUMN IF EXISTS customer_match")
            conn.commit()
            self.assertTrue(J._schema_missing(conn))
            J._apply_schema(conn)
            self.assertFalse(J._schema_missing(conn))
        c, _ = self.new(self.ua)
        self.assertIsNone(J.get_ferguson_cart(self.ua, c["id"])["customer_match"])

    def test_e4_slot_lease_mark_finish_supersede(self):              # E4
        J = self.jobs
        c1, _ = self.new(self.ua)
        c2, _ = self.new(self.ua)
        key = f"testproject {self.base}"
        snap = {"id": 19237287, "display_name": "Pat Example"}
        link = {"match": "manual", "state": "done", "reason": "x", "at": "x"}
        for c in (c1, c2):
            J.set_cart_customer(self.ua, c["id"], snap, link, ("needs_customer",))
        slot = J.claim_cart_slot(self.ua, "19237287", key, c1["id"])
        self.assertEqual((slot["estimate_id"], slot["held"], slot["cart_id"]), (None, False, None))
        # Busy: flags resync with the newest asking cart, never two holders.
        self.assertIsNone(J.claim_cart_slot(self.ua, "19237287", key, c2["id"], manual=True))
        self.assertIsNone(J.claim_cart_slot(self.ua, "19237287", key, c1["id"]))
        # Mark refuses another customer / other user; a POST sets create_pending + clears id.
        self.assertFalse(J.mark_cart_sending(self.ua, c1["id"], "999", key, "estimate", True,
                                             {"method": "POST"}, "live"))
        self.assertFalse(J.mark_cart_sending(self.ub, c1["id"], "19237287", key, "estimate",
                                             True, {"method": "POST"}, "live"))
        self.assertEqual(J.get_ferguson_cart(self.ua, c1["id"])["status"], "linked")
        self.assertTrue(J.mark_cart_sending(self.ua, c1["id"], "19237287", key, "estimate", True,
                                            {"method": "POST"}, "live"))
        got = J.get_ferguson_cart(self.ua, c1["id"])
        self.assertEqual((got["status"], got["fieldpulse"]["status"]), ("sent", "sending"))
        self.assertTrue(J.get_cart_slot(self.ua, "19237287", key)["estimate_create_pending"])
        # A sent cart can't be relinked.
        self.assertIsNone(J.set_cart_customer(self.ua, c1["id"], {"id": 5, "display_name": "B"},
                                              link, ("needs_customer", "linked")))
        again = J.finish_cart_slot(self.ua, "19237287", key,
                                   {"cart_id": c1["id"], "estimate_id": 5001,
                                    "estimate_create_pending": False, "estimate_status": "sent",
                                    "estimate_cart_id": c1["id"], "held": False},
                                   c1["id"], {"status": "sent", "at": "x"}, {"status": "sent"})
        self.assertEqual(again, (True, True, c2["id"]))
        with self.assertRaises(J.JobError):
            J.finish_cart_slot(self.ua, "19237287", key, {"lease_until": None}, c1["id"], {})
        # The next claim consumes the flags; finishing with supersede marks the old cart.
        slot = J.claim_cart_slot(self.ua, "19237287", key, c2["id"])
        self.assertEqual((slot["estimate_id"], slot["resync_manual"], slot["cart_id"]),
                         (5001, True, c1["id"]))
        J.finish_cart_slot(self.ua, "19237287", key, {"cart_id": c2["id"]}, c2["id"],
                           {"status": "sent", "replaces": c1["id"]}, {"status": "sent"},
                           supersede=(c1["id"], c2["id"]))
        self.assertEqual(J.get_ferguson_cart(self.ua, c1["id"])["superseded_by"], c2["id"])
        self.assertEqual(len(J.get_ferguson_cart(self.ua, c1["id"])["fieldpulse_history"]), 1)
        self.assertTrue(J.set_cart_fieldpulse(self.ua, c2["id"], {"status": "queued"}, {"s": 1}))
        self.assertFalse(J.set_cart_fieldpulse(self.ub, c2["id"], {"status": "queued"}))
        item = next(i for i in J.list_ferguson_carts(self.ua)["items"] if i["id"] == c1["id"])
        self.assertEqual((item["fieldpulse_status"], item["superseded_by"]), ("sent", c2["id"]))
        self.assertIsNotNone(J.claim_cart_slot(self.ua, "19237287", key, c2["id"]))  # released

    def test_schema_objects_known(self):
        rels, cols = self.jobs._schema_objects()
        for r in ("cart_addresses", "ferguson_carts", "idx_ferguson_carts_user",
                  "cart_fieldpulse_slots"):
            self.assertIn(r, rels)


if __name__ == "__main__":
    unittest.main()
