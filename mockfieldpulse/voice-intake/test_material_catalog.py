"""Stage 1b: material -> FieldPulse item catalog matching. Offline: no
Anthropic, no FieldPulse (the Claude client and _http_get are faked).

Run: /tmp/vi-py314/bin/python -m unittest test_material_catalog
"""

import copy
import json
import os
import unittest
from unittest import mock

# Shared env scrub, network block, module stubs and fixtures.
from test_fieldpulse_sync import BATH_MATERIALS, Base, fs, job, with_materials  # noqa: E402

import test_admin_panel  # noqa: E402,F401  (its jobs/queue/storage stub additions first)
import admin_panel  # noqa: E402
import customer_source as cs  # noqa: E402
import extractor  # noqa: E402


def item(i, **kw):
    d = {"id": i, "name": f"Item {i}", "type": "product", "default_unit_price": "10",
         "default_unit_cost": "4", "is_active": True, "deleted_at": None}
    d.update(kw)
    return d


def page_body(items):
    return 200, json.dumps({"error": False, "response": items}).encode()


# --- Reader --------------------------------------------------------------------------

class TestCatalogReader(unittest.TestCase):
    def setUp(self):
        pe = mock.patch.dict(os.environ, {"FIELDPULSE_READ_BASE_URL": "https://fp.invalid/x/",
                                          "FIELDPULSE_READ_API_KEY": "read-key"})
        pe.start()
        self.addCleanup(pe.stop)
        os.environ.pop("FIELDPULSE_MATERIAL_ITEM_ID", None)
        self.calls = []

    def run_pages(self, pages):
        pages = list(pages)

        def fake(url, params, headers, deadline):
            self.calls.append((url, dict(params), headers.get("x-api-key")))
            r = pages.pop(0) if pages else page_body([])
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(cs, "_http_get", side_effect=fake):
            return cs.list_catalog_items()

    def test_unset_env_is_empty_and_no_call(self):
        for k in ("FIELDPULSE_READ_BASE_URL", "FIELDPULSE_READ_API_KEY"):
            with self.subTest(unset=k), mock.patch.dict(os.environ, {k: ""}):
                self.assertEqual(self.run_pages([]), [])
        self.assertEqual(self.calls, [])

    def test_pages_until_short_page(self):
        full = [item(i) for i in range(1, 101)]
        out = self.run_pages([page_body(full), page_body([item(101), item(102)])])
        self.assertEqual(len(out), 102)
        self.assertEqual([c[1] for c in self.calls], [{"page": 1, "limit": 100},
                                                      {"page": 2, "limit": 100}])
        self.assertEqual(self.calls[0][0], "https://fp.invalid/x/items")
        self.assertEqual(self.calls[0][2], "read-key")
        self.assertEqual(out[0], {"id": 1, "name": "Item 1", "type": "product",
                                  "unit_price": 10.0, "unit_cost": 4.0})

    def test_hard_cap_ten_pages(self):
        pages = [page_body([item(p * 100 + i + 1) for i in range(100)]) for p in range(15)]
        out = self.run_pages(pages)
        self.assertEqual(len(self.calls), 10)
        self.assertEqual(len(out), 1000)

    def test_page_ignored_by_api_dedupes(self):
        same = page_body([item(i) for i in range(1, 101)])
        out = self.run_pages([same] * 10)
        self.assertEqual(len(out), 100)

    def test_filtering(self):
        os.environ["FIELDPULSE_MATERIAL_ITEM_ID"] = "5"
        raw = [item(1), item(2, is_active=False), item(3, deleted_at="2026-01-01"),
               item(4, name="  "), item(5), item(True), item("7"), item(-1), item(None),
               "junk", item(8, is_active=None), item(9, default_unit_price="abc",
                                                      default_unit_cost=None, type="kit"),
               item(1)]
        out = self.run_pages([page_body(raw)])
        self.assertEqual([o["id"] for o in out], [1, 7, 9])
        self.assertEqual(out[2], {"id": 9, "name": "Item 9", "type": None,
                                  "unit_price": None, "unit_cost": None})

    def test_any_failure_is_empty(self):
        for bad in (OSError("boom host.example"), (401, b"{}"), (500, b"x"), (200, b"not json"),
                    (200, b'{"error": true}'), (200, b'"str"'), ValueError("x")):
            with self.subTest(bad=str(bad)[:30]):
                self.calls.clear()
                self.assertEqual(self.run_pages([bad]), [])

    def test_failure_on_page_two_is_empty_not_partial(self):
        out = self.run_pages([page_body([item(i) for i in range(1, 101)]), (503, b"")])
        self.assertEqual(out, [])


# --- Matcher -------------------------------------------------------------------------

CATALOG = [{"id": 11, "name": "Grab Bar", "type": "product", "unit_price": 89.0,
            "unit_cost": 30.0},
           {"id": 12, "name": "Frameless Shower Door", "type": "product", "unit_price": 1500.0,
            "unit_cost": 700.0},
           {"id": 13, "name": "Plumbing - Bathroom Basic Upgrade", "type": "service",
            "unit_price": 4800.0, "unit_cost": 2400.0}]


class FakeResponse:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]


def _patch_create(create):
    """The matcher calls _client.with_options(max_retries=0, timeout=...).messages.create
    (F-65: one short attempt, no SDK retries)."""
    def with_options(**kw):
        assert kw == {"max_retries": 0, "timeout": extractor._MATCH_TIMEOUT_S}, kw
        return mock.Mock(messages=mock.Mock(create=create))
    return mock.patch.object(extractor._client, "with_options", with_options)


class TestMatcher(unittest.TestCase):
    def ex(self):
        return {"materials": copy.deepcopy(BATH_MATERIALS)}

    def run_match(self, answer, ex=None, catalog=CATALOG):
        ex = ex or self.ex()
        create = mock.Mock(side_effect=answer if isinstance(answer, Exception) else None,
                           return_value=None if isinstance(answer, Exception)
                           else FakeResponse(answer))
        with _patch_create(create):
            n = extractor.match_catalog_items(ex, catalog)
        return n, ex, create

    def test_valid_matches_stored(self):
        ans = json.dumps({"matches": [{"index": 2, "item_id": 12}, {"index": 3, "item_id": 11},
                                      {"index": 0, "item_id": None}]})
        n, ex, create = self.run_match(ans)
        self.assertEqual(n, 2)
        cats = [m["catalog_item"] for m in ex["materials"]]
        self.assertEqual(cats[2], {"id": 12, "name": "Frameless Shower Door", "type": "product",
                                   "unit_price": 1500.0, "unit_cost": 700.0})
        self.assertEqual(cats[3]["id"], 11)
        self.assertEqual([cats[i] for i in (0, 1, 4)], [None, None, None])
        kw = create.call_args.kwargs
        self.assertEqual(kw["model"], extractor.MODEL)
        sent = json.loads(kw["messages"][0]["content"])
        self.assertEqual(sent["materials"][0], {"index": 0, "area": "Shower",
                                                "item": "subway tile",
                                                "details": "white 3x6, matte"})
        self.assertEqual(sent["catalog"][0], {"id": 11, "name": "Grab Bar", "type": "product"})

    def test_invalid_answers_are_null(self):
        ans = json.dumps({"matches": [
            {"index": 0, "item_id": 999},          # not in catalog
            {"index": 7, "item_id": 11},           # out of range
            {"index": -1, "item_id": 11},
            {"index": True, "item_id": 11},        # bool index
            {"index": 1, "item_id": True},         # bool id
            {"index": "2", "item_id": "12"},       # digit strings are fine
            {"index": 2, "item_id": 13},           # duplicate index: first wins
            {"index": 3.0, "item_id": 11},         # float index
            "junk"]})
        n, ex, _ = self.run_match(ans)
        self.assertEqual(n, 1)
        self.assertEqual([None if m["catalog_item"] is None else m["catalog_item"]["id"]
                          for m in ex["materials"]], [None, None, 12, None, None])

    def test_failures_all_null_never_raise(self):
        for ans in (RuntimeError("api down"), "not json", "[]", '{"matches": "x"}', "```json\n{}\n```"):
            with self.subTest(ans=str(ans)[:20]):
                n, ex, _ = self.run_match(ans)
                self.assertEqual(n, 0)
                self.assertTrue(all(m["catalog_item"] is None for m in ex["materials"]))

    def test_skips_without_materials_or_catalog(self):
        for ex in ({"materials": []}, {}, {"materials": None}, None):
            with self.subTest(ex=ex):
                n, _, create = self.run_match("{}", ex=ex if ex is not None else None)
                self.assertEqual(n, 0)
        create = mock.Mock()
        ex = self.ex()
        with _patch_create(create):
            self.assertEqual(extractor.match_catalog_items(ex, []), 0)
        create.assert_not_called()
        self.assertTrue(all(m["catalog_item"] is None for m in ex["materials"]))
        with _patch_create(create):
            self.assertEqual(extractor.match_catalog_items({"materials": []}, CATALOG), 0)
        create.assert_not_called()


# --- Plan ----------------------------------------------------------------------------

def matched_extraction():
    ex = with_materials(copy.deepcopy(BATH_MATERIALS))
    ex["materials"][2]["catalog_item"] = dict(CATALOG[1])        # frameless glass door
    ex["materials"][3]["catalog_item"] = dict(CATALOG[0])        # grab bar
    for i in (0, 1, 4):
        ex["materials"][i]["catalog_item"] = None
    return ex


class TestPlanWithCatalog(Base):
    def setUp(self):
        super().setUp()
        self.setenv(FIELDPULSE_MATERIAL_ITEM_ID="7001")

    def body(self, ex):
        step = fs.build_plan(job(), ex, fs._config())[2]
        return step, step["request"] and step["request"]["body"]

    def lines(self, body):
        return [li for g in body["groupings"] for li in g["line_items"]]

    def test_matched_and_unmatched_lines(self):
        step, body = self.body(matched_extraction())
        by_title = {li["title"]: li for li in self.lines(body)}
        door = by_title["Frameless Shower Door"]
        self.assertEqual((door["item_id"], door["unit_price"], door["unit_cost"], door["type"],
                          door["quantity"]), (12, 1500.0, 700.0, "product", 1))
        self.assertEqual(door["description"], "frameless glass door. Quantity not mentioned.")
        self.assertEqual(by_title["Grab Bar"]["item_id"], 11)
        tile = by_title["subway tile"]
        self.assertEqual((tile["item_id"], tile["unit_price"], tile["unit_cost"]), (7001, 0, 0))
        self.assertEqual(tile["description"], "white 3x6, matte. Quantity not mentioned.")
        self.assertIn("2 of 5 lines matched to the item list (its default prices)", body["notes"])
        self.assertEqual(step["items"], 5)

    def test_matched_description_keeps_details(self):
        ex = matched_extraction()
        ex["materials"][1]["catalog_item"] = {"id": 13, "name": "Plumbing Trim",
                                              "type": "service", "unit_price": 5,
                                              "unit_cost": None}
        _, body = self.body(ex)
        li = next(li for li in self.lines(body) if li["item_id"] == 13)
        self.assertEqual(li["description"], "faucets: Moen, matte black.")
        self.assertEqual((li["quantity"], li["unit_measure"], li["type"], li["unit_cost"]),
                         (2, "each", "service", 0))

    def test_prices_off(self):
        self.setenv(FIELDPULSE_MATERIAL_CATALOG_PRICES=" OFF ")
        _, body = self.body(matched_extraction())
        self.assertTrue(all((li["unit_price"], li["unit_cost"]) == (0, 0)
                            for li in self.lines(body)))
        self.assertIn("2 of 5 lines matched to the item list; ", body["notes"])

    def test_bad_stored_catalog_item_falls_back_to_placeholder(self):
        ex = with_materials(copy.deepcopy(BATH_MATERIALS))
        for m, bad in zip(ex["materials"], ({"id": True, "name": "x"}, {"id": 5, "name": ""},
                                            "junk", {"id": "abc", "name": "x"},
                                            {"name": "x"})):
            m["catalog_item"] = bad
        _, body = self.body(ex)
        self.assertEqual({li["item_id"] for li in self.lines(body)}, {7001})
        self.assertNotIn("matched", body["notes"])

    def test_no_placeholder_matched_lines_still_go(self):
        os.environ.pop("FIELDPULSE_MATERIAL_ITEM_ID")
        step, body = self.body(matched_extraction())
        self.assertEqual(sorted(li["item_id"] for li in self.lines(body)), [11, 12])
        self.assertIn("3 materials were not matched to the item list and left out", body["notes"])
        self.assertEqual(step["items"], 2)

    def test_no_placeholder_nothing_matched_is_skipped(self):
        os.environ.pop("FIELDPULSE_MATERIAL_ITEM_ID")
        step, body = self.body(with_materials(copy.deepcopy(BATH_MATERIALS)))
        self.assertIsNone(body)
        self.assertTrue(step["skip_reason"].startswith(fs.MATERIAL_ITEM_MISSING))

    def test_comment_and_task_unchanged_by_matching(self):
        plain = fs.build_plan(job(), with_materials(copy.deepcopy(BATH_MATERIALS)), fs._config())
        matched = fs.build_plan(job(), matched_extraction(), fs._config())
        self.assertEqual(plain[:2], matched[:2])

    def test_retry_rebuilds_identical_plan_from_stored_extraction(self):
        ex = matched_extraction()
        stored = json.loads(json.dumps(ex))             # what Postgres JSONB gives back
        self.assertEqual(fs.build_plan(job(), ex, fs._config()),
                         fs.build_plan(job(), stored, fs._config()))

    def test_unreadable_materials_reason(self):           # F-64
        step, _ = self.body(with_materials(None))
        self.assertEqual(step["skip_reason"], fs._MATERIALS_UNREADABLE)


# --- Config gap is re-sendable (owed fix 4a) -------------------------------------------

class TestConfigGapResend(Base):
    OK = (200, {"error": False, "response": {"id": 1}})

    def test_gap_is_partial_then_retry_sends_only_the_list(self):
        self.sendable()
        self.allow_send([self.OK, self.OK, self.OK])
        ex = with_materials(copy.deepcopy(BATH_MATERIALS))
        out = fs.sync_consultation(job(), ex)
        self.assertEqual([r["path"] for r in self.sent], ["/comments", "/jobs"])
        self.assertEqual(out["status"], "partial")
        rows = self.store.list_fieldpulse_writes("job_abc")
        self.assertEqual(admin_panel.fieldpulse_status("done", out, rows, False), "partial")
        self.assertIn("partial", admin_panel.ATTENTION)

        self.setenv(FIELDPULSE_MATERIAL_ITEM_ID="7001")
        out = fs.sync_consultation(job(), ex, manual=True)
        self.assertEqual([r["path"] for r in self.sent], ["/comments", "/jobs", "/material-lists"])
        self.assertEqual(out["status"], "succeeded")
        rows = self.store.list_fieldpulse_writes("job_abc")
        self.assertEqual(admin_panel.fieldpulse_status("done", out, rows, False), "succeeded")
        fs.sync_consultation(job(), ex, manual=True)
        self.assertEqual(len(self.sent), 3)                       # nothing re-sent

    def test_gap_in_dry_run_or_blocked_is_not_attention(self):
        ex = with_materials(copy.deepcopy(BATH_MATERIALS))
        out = fs.sync_consultation(job(), ex)                     # dry_run default
        rows = self.store.list_fieldpulse_writes("job_abc")
        self.assertEqual(out["status"], "dry_run")
        self.assertEqual(admin_panel.fieldpulse_status("done", out, rows, False), "dry_run")

    def test_gap_for_a_blocked_customer_is_not_attention(self):
        ex = with_materials(copy.deepcopy(BATH_MATERIALS))
        self.sendable()
        other = job(customer_id="555")                            # not the test customer
        out = fs.sync_consultation(other, ex)
        rows = self.store.list_fieldpulse_writes(other["id"])
        self.assertEqual(out["status"], "blocked")
        self.assertNotIn(admin_panel.fieldpulse_status("done", out, rows, False),
                         admin_panel.ATTENTION)

    def test_no_materials_skip_is_still_not_attention(self):
        self.sendable()
        self.allow_send([self.OK, self.OK])
        out = fs.sync_consultation(job(), with_materials([]))
        rows = self.store.list_fieldpulse_writes("job_abc")
        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(admin_panel.fieldpulse_status("done", out, rows, False), "succeeded")


# --- Worker wiring -------------------------------------------------------------------

class TestWorkerMatch(unittest.TestCase):
    def test_match_uses_catalog_and_never_raises(self):
        import worker
        ex = {"materials": [{"item": "grab bar"}]}
        with mock.patch.object(worker, "list_catalog_items", return_value=CATALOG) as lc, \
                mock.patch.object(worker, "match_catalog_items", return_value=1) as mc:
            worker._match_materials("job_1", ex)
        lc.assert_called_once_with()
        mc.assert_called_once_with(ex, CATALOG)
        for ex in ({}, {"materials": []}, None, {"materials": None}):
            with mock.patch.object(worker, "list_catalog_items") as lc:
                worker._match_materials("job_1", ex)
            lc.assert_not_called()
        with mock.patch.object(worker, "list_catalog_items", side_effect=RuntimeError("x")), \
                mock.patch.object(worker, "report") as rep:
            worker._match_materials("job_1", {"materials": [{"item": "a"}]})
        rep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
