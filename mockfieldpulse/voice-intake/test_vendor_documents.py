"""Stage P1a: vendor document reader. Offline: the Claude client is faked, no network.
All fixture data is MADE UP (no real customers, addresses or vendor codes).

Run: /tmp/vi-py314/bin/python -m unittest test_vendor_documents
"""

import base64
import json
import unittest
from unittest import mock

# Shared env scrub (dummy ANTHROPIC key, no dotenv) and network block.
import test_fieldpulse_sync  # noqa: F401

import anthropic  # noqa: E402
import httpx  # noqa: E402

import extractor  # noqa: E402
import observability  # noqa: E402
import vendor_documents as vd  # noqa: E402

PDF = b"%PDF-1.7\n% made-up test bytes\n%%EOF\n"


class FakeResponse:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason


def doc(lines=(), totals=None, **kw):
    d = {"vendor": "Ferguson", "document_type": "quote", "document_number": "Q-000123",
         "document_date": "10/30/2026", "job_name": "Testerson", "lines": list(lines),
         "totals": totals or {"subtotal": None, "tax": None, "freight": None, "total": None}}
    d.update(kw)
    return d


def ln(**kw):
    d = {"part_number": None, "name": None, "details": None, "notes": None, "quantity": None,
         "unit": None, "unit_price": None, "line_total": None}
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.options = []
        self.reply = None

    def read(self, reply, customer=None, pdf=PDF):
        self.reply = reply

        def create(**kw):
            self.calls.append(kw)
            r = self.reply
            if isinstance(r, BaseException):
                raise r
            if isinstance(r, FakeResponse):
                return r
            return FakeResponse(r if isinstance(r, str) else json.dumps(r))

        def with_options(**kw):
            self.options.append(kw)
            return mock.Mock(messages=mock.Mock(create=create))
        with mock.patch.object(extractor._client, "with_options", with_options):
            return vd.read_vendor_document(pdf, customer)

    def codes(self, result, level=None):
        return [c["code"] for c in result["checks"] if level is None or c["level"] == level]


class TestRequest(Base):
    def test_document_block_model_prompt_and_options(self):
        self.read(doc([ln(name="Widget")]))
        self.assertEqual(self.options, [{"timeout": 120, "max_retries": 0}])
        (call,) = self.calls
        self.assertEqual(call["model"], extractor.MODEL)
        self.assertEqual(call["system"], vd.SYSTEM_PROMPT)
        block = call["messages"][0]["content"][0]
        self.assertEqual(block["type"], "document")
        self.assertEqual(block["source"]["media_type"], "application/pdf")
        self.assertEqual(base64.standard_b64decode(block["source"]["data"]), PDF)

    def test_prompt_has_the_rules(self):
        p = vd.SYSTEM_PROMPT
        for needle in ("Part of Kit", "CAN GET IN NEXT DAY", "Never assume 1",
                       "Never estimate", "Definitive Home and Design", "NEVER OUTPUT"):
            self.assertIn(needle, p)


class TestInput(Base):
    def test_bad_pdf_bytes(self):
        for bad in (b"", b"PK\x03\x04", b"%PD", "%PDF-1.4", None, 123):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as ctx:
                self.read(doc(), pdf=bad)
            self.assertEqual(str(ctx.exception), vd._ERR_NOT_PDF)
        self.assertEqual(self.calls, [])

    def test_oversized(self):
        big = b"%PDF" + b"\0" * vd.MAX_PDF_BYTES
        with self.assertRaises(ValueError) as ctx:
            self.read(doc(), pdf=big)
        self.assertEqual(str(ctx.exception), vd._ERR_TOO_BIG)
        self.assertEqual(self.calls, [])
        ok = b"%PDF" + b"\0" * (vd.MAX_PDF_BYTES - 4)
        self.read(doc([ln(name="x")]), pdf=ok)       # exactly 20 MB is allowed

    def test_bytearray_ok(self):
        self.assertEqual(len(self.read(doc([ln(name="x")]), pdf=bytearray(PDF))["lines"]), 1)


class TestOutputErrors(Base):
    def test_malformed_outputs(self):
        for reply in ("not json", "", "[1,2]", "null", '"str"', "{\"lines\": [", "```\n```",
                      "[" * 100000):
            with self.subTest(reply=reply[:20]), self.assertRaises(ValueError) as ctx:
                self.read(reply)
            self.assertEqual(str(ctx.exception), vd._ERR_MALFORMED)

    def test_code_fences_ok(self):
        r = self.read("```json\n" + json.dumps(doc([ln(name="Faucet")])) + "\n```")
        self.assertEqual(r["lines"][0]["name"], "Faucet")

    def test_cut_off(self):
        with self.assertRaises(ValueError) as ctx:
            self.read(FakeResponse('{"lines": [', stop_reason="max_tokens"))
        self.assertEqual(str(ctx.exception), vd._ERR_CUT_OFF)

    def test_api_error_fixed_message(self):
        req = httpx.Request("POST", "https://api.invalid/v1/messages")
        with self.assertRaises(ValueError) as ctx:
            self.read(anthropic.APIConnectionError(request=req))
        self.assertEqual(str(ctx.exception), vd._ERR_API)

    def test_job_timeout_reraised_direct_and_wrapped(self):
        JobTimeoutException = type("JobTimeoutException", (Exception,), {})
        with self.assertRaises(JobTimeoutException):
            self.read(JobTimeoutException("deadline"))
        req = httpx.Request("POST", "https://api.invalid/v1/messages")
        wrapped = anthropic.APIConnectionError(request=req)
        wrapped.__cause__ = JobTimeoutException("deadline")
        with self.assertRaises(JobTimeoutException):
            self.read(wrapped)

    def test_messages_safe_in_sentry(self):
        for msg in (vd._ERR_NOT_PDF, vd._ERR_TOO_BIG, vd._ERR_API, vd._ERR_CUT_OFF,
                    vd._ERR_MALFORMED):
            self.assertEqual(observability._safe_value(msg, "ValueError", "builtins", None), msg)


class TestRealSdkOneAttempt(unittest.TestCase):
    """F-68: the real Anthropic client over httpx.MockTransport (no network). An RQ deadline
    in the first transport call must propagate, and nothing may be retried."""

    def run_reader(self, first):
        from rq.timeouts import JobTimeoutException  # noqa: F401  (imported by callers)
        attempts = []

        def handler(request):
            attempts.append(request)
            if isinstance(first, BaseException):
                raise first
            return first

        client = anthropic.Anthropic(api_key="test-key", base_url="https://api.invalid",
                                     http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(client.close)
        with mock.patch.object(extractor, "_client", client):
            try:
                return vd.read_vendor_document(PDF), attempts
            except BaseException as exc:
                exc.attempts = attempts
                raise

    def test_job_timeout_in_transport_propagates_after_one_attempt(self):
        from rq.timeouts import JobTimeoutException
        with self.assertRaises(JobTimeoutException) as ctx:
            self.run_reader(JobTimeoutException("deadline"))
        self.assertEqual(len(ctx.exception.attempts), 1)
        self.assertIsNone(ctx.exception.__cause__)

    def test_server_error_not_retried(self):
        with self.assertRaises(ValueError) as ctx:
            self.run_reader(httpx.Response(500, json={"type": "error", "error": {
                "type": "api_error", "message": "made-up"}}))
        self.assertEqual(str(ctx.exception), vd._ERR_API)
        self.assertEqual(len(ctx.exception.attempts), 1)

    def test_success_through_real_client(self):
        body = {"id": "msg_test", "type": "message", "role": "assistant", "model": extractor.MODEL,
                "content": [{"type": "text", "text": json.dumps(doc([ln(name="Widget")]))}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1}}
        result, attempts = self.run_reader(httpx.Response(200, json=body))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(result["lines"][0]["name"], "Widget")


class TestPrivateScrub(Base):
    """F-67: labeled contact/address/payment spans are removed from every text field."""

    def test_backtester_repro(self):
        notes = ("Ship to 123 Example Street; Tel: 2025550199; Visa 4111 1111 1111 1111; "
                 "Approval code: ABCDEF")
        r = self.read(doc([ln(name="Widget", notes=notes)]))
        dumped = json.dumps(r)
        for secret in ("123 Example", "Example Street", "2025550199", "4111", "ABCDEF"):
            self.assertNotIn(secret, dumped)
        self.assertEqual(r["lines"][0]["name"], "Widget")

    def test_labels_in_every_text_field(self):
        cases = {
            "Bill to: Pat Testerson, 9 Sample Rd": "Sample Rd",
            "Billing address 9 Sample Rd": "Sample Rd",
            "Address: 77 Madeup Ave": "Madeup Ave",
            "Phone 6185550123": "6185550123",
            "Ph: 6185550123": "6185550123",
            "Cell: 6185550123": "6185550123",
            "Email: pat at example dot test": "example dot",
            "MasterCard 5500-0000-0000-0004": "5500",
            "AMEX 3782 822463 10005": "822463",
            "Paid with VISA x1234": "1234",
            "Payment: Visa debit": "debit",
            "Auth num 778899": "778899",
            "Authorization #QWERTY": "QWERTY",
            "Approval #ZXCVB": "ZXCVB",
            "Card number 4242 4242 4242 4242": "4242",
            "Acct 99887766": "99887766",
        }
        for text, secret in cases.items():
            for field in ("notes", "details", "name"):
                with self.subTest(text=text, field=field):
                    r = self.read(doc([ln(part_number="P-1", **{field: text})]))
                    self.assertNotIn(secret, json.dumps(r))
        r = self.read(doc([ln(name="x")], document_number="Tel: 6185550123",
                          document_date="Visa 4111", job_name="Testerson; Ship to 1 Fake Ln",
                          vendor="Ferguson | Phone 6185550123"))
        self.assertNotIn("6185550123", json.dumps(r))
        self.assertNotIn("4111", json.dumps(r))
        self.assertNotIn("Fake Ln", json.dumps(r))
        self.assertEqual(r["job_name"], "Testerson; [removed]")      # text before the label kept
        self.assertEqual(r["vendor"], "Ferguson | [removed]")

    def test_unlabeled_card_numbers(self):
        for card in ("4111 1111 1111 1111", "4111-1111-1111-1111", "3782 822463 10005",
                     "6011 0000 0000 0004 123"):
            with self.subTest(card=card):
                r = self.read(doc([ln(name="x", notes=f"ref {card} thanks")]))
                self.assertEqual(r["lines"][0]["notes"], "ref [removed] thanks")

    def test_product_and_sku_text_survives(self):
        keep = ["K-8304-KS-NA", "100999952", "#26916080", "SKU 1000123456", "2025550199",
                "Approval pending", "Authentic finish", "Discover series", "Card holder shelf",
                "Single cell shade", "Headphone niche", "Telescoping rod", "Shower Kit 60x32",
                "Ending trim", "Model 1234 5678", "Address light", "Item 12-345-678"]
        for text in keep:
            with self.subTest(text=text):
                r = self.read(doc([ln(part_number=text, name=text, details=text, notes=text)]))
                line = r["lines"][0]
                self.assertEqual((line["part_number"], line["name"], line["details"],
                                  line["notes"]), (text[:80], text, text, text))

    def test_f72_dimensions_and_labeled_skus_survive(self):
        keep = ["Card holder shelf 12 in", "Address light 12V", "SKU 1234-5678-90123",
                "Item #: 4111-1111-1111-1111", "Model no. 618-555-0123", "Part: 1234 5678 9012 3",
                "Account manager desk 2 drawer", "Card table 36x36"]
        for text in keep:
            for field in ("part_number", "name", "details", "notes"):
                with self.subTest(text=text, field=field):
                    r = self.read(doc([ln(**{"part_number": "P-1", "name": "Widget", field: text})]))
                    self.assertEqual(r["lines"][0][field], text[:80])

    def test_f72_generic_labels_still_removed_in_payment_syntax(self):
        cases = {"Address: 77 Madeup Ave": "Madeup", "Address 77 Madeup Ave": "Madeup",
                 "Card number 4242 4242 4242 4242": "4242", "Card # 4242": "4242",
                 "Card: Visa debit": "debit", "Acct 99887766": "99887766",
                 "Account no. 5544332211": "5544332211", "Address to Pat Sampleton": "Sampleton",
                 "ref 4111 1111 1111 1111": "4111", "call 618-555-0123": "555"}
        for text, secret in cases.items():
            for field in ("name", "notes"):
                with self.subTest(text=text, field=field):
                    r = self.read(doc([ln(part_number="P-1", **{field: text})]))
                    self.assertNotIn(secret, json.dumps(r))


class TestLayouts(Base):
    def test_portfolio_no_prices(self):
        r = self.read(doc([
            ln(part_number="Acme 1234-BN", name="Single-handle faucet",
               details="Finish: Brushed Nickel", quantity=1),
            ln(part_number="Acme 77-W", name="Toilet", details="Finish: White", quantity=2),
            ln(part_number="Acme 9", name="Towel bar"),
        ], document_type="selection", vendor="Ferguson Home", job_name="Testerson Bath"),
            customer="Pat Testerson")
        self.assertEqual(r["document_type"], "selection")
        for line in r["lines"]:
            self.assertIsNone(line["unit_price"])
            self.assertIsNone(line["line_total"])
        self.assertIsNone(r["lines"][2]["quantity"])           # not printed -> stays null
        self.assertIn("3 line(s) have no price (they stay $0).",
                      [c["message"] for c in r["checks"]])
        self.assertIn("job_name_match", self.codes(r, "ok"))
        self.assertNotIn("warn", [c["level"] for c in r["checks"]])

    def test_quote_three_decimals_tax_freight_notes(self):
        r = self.read(doc([
            ln(part_number="ZZ100", name="Shower valve", quantity=2, unit="EA",
               unit_price=123.456, line_total=246.91, notes="CAN GET IN NEXT DAY"),
            ln(part_number="ZZ200", name="Trim kit", quantity=3, unit="EA",
               unit_price=10.333, line_total=31.0),
        ], totals={"subtotal": "$277.91", "tax": 22.93, "freight": "15.00", "total": 315.84}))
        self.assertEqual(r["lines"][0]["unit_price"], 123.456)
        self.assertEqual(r["lines"][0]["notes"], "CAN GET IN NEXT DAY")
        self.assertEqual(r["totals"], {"subtotal": 277.91, "tax": 22.93, "freight": 15.0,
                                       "total": 315.84})
        self.assertEqual(self.codes(r, "warn"), [])
        self.assertEqual(sorted(self.codes(r, "ok")), ["line_math", "lines_vs_totals", "totals_math"])

    def test_onyx_kit_with_prices(self):
        r = self.read(doc([
            ln(part_number="#1", name="Standard Shower Kit", quantity=1,
               details="Kit includes: base 60x32; back panel; side panel; drain; silicone",
               line_total="$1,111.11"),
            ln(part_number="#2", name="Corner shelf", quantity=1, unit_price="Cost: $12.00"),
            ln(part_number="#3", name="Grab bar", quantity=2, unit_price=33.0),
        ], vendor="Onyx", document_type="order", job_name="Testerson",
            totals={"subtotal": None, "tax": None, "freight": None, "total": "$1,177.11"}),
            customer="Testerson")
        kit = r["lines"][0]
        self.assertEqual(kit["line_total"], 1111.11)
        self.assertIsNone(kit["unit_price"])                    # never derived
        self.assertIsNone(r["lines"][1]["unit_price"])          # "Cost: $12.00" is not a clean number
        self.assertEqual(r["lines"][2]["line_total"], 66.0)     # computed qty x price
        self.assertIn("line_totals_computed", self.codes(r, "info"))
        self.assertIn("lines_vs_totals", self.codes(r, "ok"))   # 1111.11 + 66 = 1177.11
        self.assertIn("unpriced_lines", self.codes(r, "info"))

    def test_onyx_kit_without_prices(self):
        r = self.read(doc([ln(part_number="#1", name="Shower Kit", quantity=1),
                           ln(part_number="#2", name="Niche", quantity=1)],
                          vendor="Onyx", document_type="order"))
        self.assertEqual([c["code"] for c in r["checks"]], ["unpriced_lines"])
        self.assertEqual(r["totals"], {"subtotal": None, "tax": None, "freight": None, "total": None})

    def test_retail_receipt_and_private_text_removed(self):
        r = self.read(doc([
            ln(part_number="SKU 900000001", name="Porcelain tile 12x24",
               details="Size: 12 x 24 in; Color: Grey", quantity=40, unit="PC",
               unit_price=2.49, line_total=99.6,
               notes="Card ending in 4242, call (555) 010-0199 or fake@example.test"),
        ], vendor="Floor & Decor", document_type="receipt", document_number="Auth code: A1B2C3",
            totals={"subtotal": 99.6, "tax": 8.22, "freight": 0, "total": 107.82}))
        dumped = json.dumps(r)
        for secret in ("4242", "010-0199", "fake@example.test", "A1B2C3"):
            self.assertNotIn(secret, dumped)
        self.assertEqual(r["lines"][0]["part_number"], "SKU 900000001")   # SKU kept
        self.assertEqual(self.codes(r, "warn"), [])

    def test_scrub_keeps_ordinary_words(self):
        r = self.read(doc([ln(name="Authentic finish trim", details="Approval pending; Discover series")]))
        self.assertEqual(r["lines"][0]["name"], "Authentic finish trim")
        self.assertEqual(r["lines"][0]["details"], "Approval pending; Discover series")


class TestNormalize(Base):
    def test_numbers(self):
        cases = [("$1,203.57", 1203.57), ("$ 12.345", 12.345), ("12", 12.0), (".5", 0.5),
                 (3, 3.0), (0, 0.0), ("-5", None), (-5, None), (True, None), (False, None),
                 ("NaN", None), (float("nan"), None), (float("inf"), None), ("12,34", None),
                 ("1.2.3", None), ("abc", None), (None, None), ([1], None), ({"x": 1}, None),
                 (1e12, None), ("(5.00)", None), ("", None), ("12 EA", None)]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(vd._number(raw), want)

    def test_oversized_ints_become_null(self):          # F-70
        big = "1" + "0" * 400
        r = self.read('{"vendor": ' + big + ', "lines": [{"name": "Widget", "part_number": '
                      + big + ', "quantity": ' + big + ', "unit_price": ' + big
                      + ', "line_total": -' + big + '}], "totals": {"total": ' + big + '}}')
        line = r["lines"][0]
        self.assertEqual((line["part_number"], line["quantity"], line["unit_price"],
                          line["line_total"]), (None, None, None, None))
        self.assertIsNone(r["vendor"])
        self.assertIsNone(r["totals"]["total"])
        self.assertEqual(self.read(doc([ln(name="x", part_number=10 ** 30)]))["lines"][0]
                         ["part_number"], "1" + "0" * 30)        # within the 80-char cap

    def test_nan_inf_from_model_json(self):
        r = self.read('{"lines": [{"name": "x", "quantity": NaN, "unit_price": Infinity, '
                      '"line_total": -1}], "totals": {"total": NaN}}')
        line = r["lines"][0]
        self.assertEqual((line["quantity"], line["unit_price"], line["line_total"]),
                         (None, None, None))
        self.assertIsNone(r["totals"]["total"])

    def test_quantity_int_when_whole(self):
        r = self.read(doc([ln(name="a", quantity="2"), ln(name="b", quantity=1.5)]))
        self.assertEqual([l["quantity"] for l in r["lines"]], [2, 1.5])
        self.assertIsInstance(r["lines"][0]["quantity"], int)

    def test_missing_names_dropped_and_shapes(self):
        r = self.read(doc([ln(), ln(details="only details"), "str", 5, None,
                           ln(part_number=12345), ln(name="  Spaced\n  name  ")]))
        self.assertEqual([(l["part_number"], l["name"]) for l in r["lines"]],
                         [("12345", None), (None, "Spaced name")])
        self.assertIn("lines_dropped", self.codes(r, "info"))

    def test_text_caps_and_types(self):
        r = self.read(doc([ln(name="n" * 999, details="d" * 5000, notes="x" * 5000,
                              unit="u" * 99, part_number=True)],
                          vendor="v" * 500, document_type="  QUOTE "))
        line = r["lines"][0]
        self.assertEqual(len(line["name"]), 255)
        self.assertEqual(len(line["details"]), 1000)
        self.assertEqual(len(line["notes"]), 500)
        self.assertEqual(len(line["unit"]), 20)
        self.assertIsNone(line["part_number"])
        self.assertEqual(len(r["vendor"]), 80)
        self.assertEqual(r["document_type"], "quote")

    def test_unknown_document_type(self):
        for t in ("invoice", None, 5, ""):
            with self.subTest(t=t):
                self.assertEqual(self.read(doc([ln(name="x")], document_type=t))["document_type"],
                                 "other")

    def test_missing_keys_and_bad_containers(self):
        r = self.read({"lines": {"name": "x"}, "totals": [1, 2]})
        self.assertEqual(r["lines"], [])
        self.assertEqual(r["totals"], {"subtotal": None, "tax": None, "freight": None, "total": None})
        self.assertEqual(r["document_type"], "other")
        self.assertEqual(self.codes(r, "warn"), ["no_lines"])
        self.assertEqual(set(r), {"vendor", "document_type", "document_number", "document_date",
                                  "job_name", "lines", "totals", "checks"})
        self.assertEqual(set(r["checks"][0]), {"level", "code", "message"})

    def test_over_200_lines(self):
        r = self.read(doc([ln(name=f"Item {i}") for i in range(250)]))
        self.assertEqual(len(r["lines"]), 200)
        self.assertEqual(r["lines"][-1]["name"], "Item 199")
        self.assertIn("lines_truncated", self.codes(r, "warn"))
        r = self.read(doc([ln(name=f"Item {i}") for i in range(200)] + [ln()] * 10))
        self.assertNotIn("lines_truncated", self.codes(r))

    def test_extra_model_keys_dropped(self):
        r = self.read(doc([dict(ln(name="x"), card_last4="4242", email="a@b.test")],
                          payment="Visa 4242"))
        self.assertNotIn("4242", json.dumps(r))
        self.assertEqual(set(r["lines"][0]), {"part_number", "name", "details", "notes",
                                              "quantity", "unit", "unit_price", "line_total"})

    def test_buyer_never_vendor_or_job_name(self):
        r = self.read(doc([ln(name="x")], vendor="3 Day Kitchen & Bath",
                          job_name="Definitive Home and Design"))
        self.assertIsNone(r["vendor"])
        self.assertIsNone(r["job_name"])
        self.assertIn("vendor_is_buyer", self.codes(r, "warn"))
        self.assertIn("job_name_is_buyer", self.codes(r, "warn"))


class TestChecks(Base):
    def test_line_math_mismatch(self):
        r = self.read(doc([ln(name="Valve", quantity=2, unit_price=10.0, line_total=25.0)]))
        (c,) = [c for c in r["checks"] if c["code"] == "line_math"]
        self.assertEqual(c["level"], "warn")
        self.assertIn("$20.00", c["message"])
        self.assertIn("$25.00", c["message"])

    def test_line_math_tolerance(self):
        # 0.5% of 1000 = 5.00 -> 1004 ok; 0.02 floor for small lines
        r = self.read(doc([ln(name="a", quantity=1, unit_price=1000.0, line_total=1004.0),
                           ln(name="b", quantity=3, unit_price=0.333, line_total=1.0)]))
        self.assertIn("line_math", self.codes(r, "ok"))
        r = self.read(doc([ln(name="a", quantity=3, unit_price=0.333, line_total=1.03)]))
        self.assertIn("line_math", self.codes(r, "warn"))

    def test_lines_vs_subtotal_mismatch(self):
        r = self.read(doc([ln(name="a", line_total=10.0), ln(name="b", line_total=5.0)],
                          totals={"subtotal": 20.0, "tax": None, "freight": None, "total": 20.0}))
        self.assertIn("lines_vs_totals", self.codes(r, "warn"))
        self.assertIn("totals_math", self.codes(r, "ok"))

    def test_lines_vs_total_without_subtotal(self):
        r = self.read(doc([ln(name="a", line_total=100.0)],
                          totals={"subtotal": None, "tax": 8.0, "freight": None, "total": 108.0}))
        self.assertIn("lines_vs_totals", self.codes(r, "ok"))
        r = self.read(doc([ln(name="a", line_total=100.0)],
                          totals={"subtotal": None, "tax": None, "freight": None, "total": 100.06}))
        self.assertIn("lines_vs_totals", self.codes(r, "warn"))

    def test_totals_math_mismatch(self):
        r = self.read(doc([ln(name="a", line_total=100.0)],
                          totals={"subtotal": 100.0, "tax": 7.0, "freight": 5.0, "total": 113.0}))
        self.assertIn("totals_math", self.codes(r, "warn"))

    def test_cent_boundaries_exact(self):               # F-69 (each fails with plain floats)
        def level(r, code):
            return [c["level"] for c in r["checks"] if c["code"] == code]
        sub = {"tax": None, "freight": None, "total": None}
        # lines_vs_totals: exactly $0.05 off is ok, $0.06 warns
        r = self.read(doc([ln(name="a", line_total=1.0)], totals=dict(sub, subtotal=1.05)))
        self.assertEqual(level(r, "lines_vs_totals"), ["ok"])
        r = self.read(doc([ln(name="a", line_total=1.0)], totals=dict(sub, subtotal=1.06)))
        self.assertEqual(level(r, "lines_vs_totals"), ["warn"])
        r = self.read(doc([ln(name="a", line_total=1.0)],
                          totals={"subtotal": None, "tax": 0.1, "freight": 0.2, "total": 1.35}))
        self.assertEqual(level(r, "lines_vs_totals"), ["ok"])
        r = self.read(doc([ln(name="a", line_total=1.0)],
                          totals={"subtotal": None, "tax": 0.1, "freight": 0.2, "total": 1.36}))
        self.assertEqual(level(r, "lines_vs_totals"), ["warn"])
        # totals_math: 1.13 + 1.00 vs 2.18 is exactly $0.05
        r = self.read(doc([ln(name="a")], totals={"subtotal": 1.13, "tax": 1.0, "freight": None,
                                                  "total": 2.18}))
        self.assertEqual(level(r, "totals_math"), ["ok"])
        r = self.read(doc([ln(name="a")], totals={"subtotal": 1.13, "tax": 1.0, "freight": None,
                                                  "total": 2.19}))
        self.assertEqual(level(r, "totals_math"), ["warn"])
        # line_math: $0.02 floor, 1 x 1.00 vs 1.02 is exactly at it
        r = self.read(doc([ln(name="a", quantity=1, unit_price=1.0, line_total=1.02)]))
        self.assertEqual(level(r, "line_math"), ["ok"])
        r = self.read(doc([ln(name="a", quantity=1, unit_price=1.0, line_total=1.03)]))
        self.assertEqual(level(r, "line_math"), ["warn"])
        # 0.5% of 1000.00 = 5.00 exactly
        r = self.read(doc([ln(name="a", quantity=1, unit_price=995.0, line_total=1000.0)]))
        self.assertEqual(level(r, "line_math"), ["ok"])
        r = self.read(doc([ln(name="a", quantity=1, unit_price=994.99, line_total=1000.0)]))
        self.assertEqual(level(r, "line_math"), ["warn"])

    def test_computed_line_total_rounds_half_up(self):
        r = self.read(doc([ln(name="a", quantity=3, unit_price=0.335)]))   # 1.005 -> 1.01
        self.assertEqual(r["lines"][0]["line_total"], 1.01)

    def test_zero_lines(self):
        r = self.read(doc([]))
        self.assertEqual(self.codes(r), ["no_lines"])

    def test_job_name_checks(self):
        lines = [ln(name="x")]
        r = self.read(doc(lines, job_name="SAMPLESON KITCHEN"), customer="Robin Sampleson Jr.")
        self.assertIn("job_name_match", self.codes(r, "ok"))
        r = self.read(doc(lines, job_name="Otherperson"), customer="Robin Sampleson")
        self.assertIn("job_name_mismatch", self.codes(r, "warn"))
        self.assertNotIn("Sampleson", json.dumps(r["checks"]))      # no customer name in checks
        r = self.read(doc(lines, job_name=None), customer="Robin Sampleson")
        self.assertIn("job_name_missing", self.codes(r, "info"))
        for customer in (None, "", "   ", 5):
            r = self.read(doc(lines, job_name="Otherperson"), customer=customer)
            self.assertFalse([c for c in self.codes(r) if c.startswith("job_name")])

    def test_job_name_whole_words(self):                # F-71
        lines = [ln(name="x")]
        cases = [("Alex Li", "Williams Bathroom", "warn"), ("Alex Li", "Li Bathroom", "ok"),
                 ("Sam Ng", "Kingston", "warn"), ("Sam Ng", "NG/Kitchen", "ok"),
                 ("Pat O'Brien", "O'BRIEN KITCHEN", "ok"), ("Pat O’Brien", "OBrien Bath", "ok"),
                 ("Pat O'Brien", "Brien Bath", "warn"),
                 ("Lee Smith-Jones", "Smith-Jones Bath", "ok"), ("Lee Smith-Jones", "Jones", "ok"),
                 ("Lee Jones", "Smith-Jones remodel", "ok"), ("Lee Jones", "Jonesboro", "warn"),
                 ("Robin Testerson", "Testerson's Kitchen", "ok"),
                 ("Robin Testerson", "Testerson_Kitchen", "ok"),
                 ("Robin Testerson III", "TESTERSON", "ok")]
        for customer, job, want in cases:
            with self.subTest(customer=customer, job=job):
                r = self.read(doc(lines, job_name=job), customer=customer)
                got = [c["level"] for c in r["checks"] if c["code"].startswith("job_name")]
                self.assertEqual(got, [want])


class TestCli(Base):
    def test_cli_prints_json_and_errors(self):
        import tempfile, io, contextlib, os
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(PDF)
        self.addCleanup(os.unlink, f.name)
        out = io.StringIO()
        with mock.patch.object(vd, "read_vendor_document",
                               return_value={"vendor": "X"}) as m, contextlib.redirect_stdout(out):
            self.assertEqual(vd._main([f.name, "--customer", "Pat Testerson"]), 0)
        m.assert_called_once_with(PDF, "Pat Testerson")
        self.assertEqual(json.loads(out.getvalue()), {"vendor": "X"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(vd._main(["/nonexistent/none.pdf"]), 1)


if __name__ == "__main__":
    unittest.main()
