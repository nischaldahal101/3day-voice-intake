"""Read a vendor PDF (Ferguson portfolio / quote, Onyx order preview, Floor & Decor
receipt, ...) into product lines with Claude. Stage P1b: called only by
worker.process_vendor_document (the app's "Vendor documents" screen); no FieldPulse call.

One entry point:

    read_vendor_document(pdf_bytes, customer_name=None) -> dict
        {vendor, document_type, document_number, document_date, job_name,
         lines: [{part_number, name, details, notes, quantity, unit, unit_price, line_total}],
         totals: {subtotal, tax, freight, total},
         checks: [{level: ok|warn|info, code, message}]}

Flag, don't fabricate: the model's JSON is never trusted raw. Prices and quantities are
kept only when printed (the prompt says so and the normaliser drops anything that isn't a
clean non-negative number). The only derived value is a missing line_total when quantity
and unit_price are both printed (reported as an info check); unit_price is never derived.
Checks are plain arithmetic, done here, not by the model.

Private info (payment, contact, addresses) is left out by the prompt first, then by a
conservative label-based scrub over every emitted text field (_scrub): the text after a
label such as "Ship to", "Tel", "Email", "Visa", "Approval code", "Payment" is replaced up
to the next ";" or "|" ("Card", "Acct", "Account", "Address" only in contact/payment syntax:
followed by ":", "#", "to" or a number), plus emails, separated phone numbers, card tails and
spaced 13-19 digit card numbers (those two kept when labeled SKU/Item/Model/Part/Mfr/UPC). This is best effort, NOT a guarantee: an unlabeled street
address or an unlabeled bare 10-digit phone number is kept (it can't be told apart from a
SKU), and a product description that happens to use a label word loses that span.

Raises ValueError with one of the fixed _ERR_* messages (bad input, Claude API error,
unreadable output). One attempt only (max_retries=0, 120 s per network operation): an RQ
JobTimeoutException raised during the call is never swallowed by an SDK retry; it comes
back wrapped in an APIConnectionError and is re-raised unwrapped so a future worker job
keeps its deadline. Retrying a failed read is the job's decision (worker.py retries once
when is_transient(exc)), not the SDK's.

CLI (for trying real PDFs locally; real PDFs are never committed):
    python vendor_documents.py <file.pdf> [--customer NAME]
"""

import base64
import json
import logging
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import anthropic

import extractor   # reuses MODEL, the shared client and its JSON helpers; loads .env

logger = logging.getLogger("voice_intake.vendor_documents")

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_LINES = 200
_SCAN_LINES = 1000            # model entries looked at before the cap (drops don't count)
_TIMEOUT_S = 120
_MAX_RETRIES = 0              # see the module docstring (RQ deadline vs SDK retry)
_MAX_TOKENS = 8000            # ~200 short lines; more would not finish inside the timeout
_MAX_NUMBER = 10_000_000      # anything bigger is a misread (e.g. a SKU in a price field)

DOCUMENT_TYPES = ("selection", "quote", "order", "receipt", "other")
_TEXT_LIMITS = {"vendor": 80, "document_number": 80, "document_date": 40, "job_name": 120,
                "part_number": 80, "name": 255, "details": 1000, "notes": 500, "unit": 20}
_LINE_TEXT = ("part_number", "name", "details", "notes")
_TOTAL_KEYS = ("subtotal", "tax", "freight", "total")

# Fixed messages (also in observability._SAFE_VALUES so they stay readable in Sentry).
_ERR_NOT_PDF = "Vendor document is not a PDF"
_ERR_TOO_BIG = "Vendor document is larger than 20 MB"
_ERR_API = "Vendor document read failed: Claude API error"
_ERR_CUT_OFF = "Vendor document read failed: Claude output was cut off"
_ERR_MALFORMED = "Vendor document read failed: Claude returned malformed output"
INPUT_ERRORS = frozenset((_ERR_NOT_PDF, _ERR_TOO_BIG))   # the file itself; retrying won't help
# What the app shows for each (saved on the failed vendor_documents row by worker.py).
USER_MESSAGES = {
    _ERR_NOT_PDF: "This file is not a PDF.",
    _ERR_TOO_BIG: "This PDF is larger than 20 MB.",
    # worker.py picks busy-vs-rejected wording for this one (it owns the retry).
    _ERR_API: "Claude could not read this PDF request. Upload the PDF again later.",
    _ERR_CUT_OFF: "This document is too long to read in one go (too many lines).",
    _ERR_MALFORMED: "The document could not be read (Claude's answer was not usable). "
                    "Upload it again to retry.",
}

# The contractor (buyer) is never the vendor and never the homeowner's job name.
_BUYER = re.compile(r"3\s*day\s*kitchen|3\s*dkb\b|definitive\s+home", re.I)

# Defence in depth for "never output private info" (the prompt is the first line). Best
# effort, see the module docstring. A labeled span runs to the next ";" or "|" (or the end).
_SPAN = r"[^;|]*"
_PRIVATE = [
    # Labels whose following text is always private.
    re.compile(r"(?<!\w)(?:ship(?:ping)?\s*to|bill(?:ing)?\s*to|sold\s*to|deliver(?:y)?\s*to"
               r"|billing|(?:street|mailing|delivery|billing|shipping)\s+address"
               r"|tel(?:ephone)?|phone|ph(?=\s*[:#.])|fax|(?:cell|mobile)(?=\s*[:#])|e-?mail"
               r"|visa|master\s*card|amex|american\s+express"
               r"|approval\s*(?:code|num(?:ber)?|no\b\.?|#)"
               r"|auth(?:orization)?\s*(?:code|num(?:ber)?|no\b\.?|#)"
               r"|payments?|paid\s+(?:with|by))(?:(?<=\w)(?!\w)|(?<!\w))" + _SPAN, re.I),
    # Generic labels: private only in contact/payment syntax, i.e. the label (optionally
    # "number"/"no."/"#") followed by ":" / "#" / "to" or directly by a number:
    # "Address: 77 ...", "Card number 4242 ...", "Acct 9988" go; "Card holder shelf 12 in"
    # and "Address light 12V" stay (F-72).
    re.compile(r"(?<!\w)(?:card|acct|account|address)(?:\s*(?:number|num|no\b\.?|#))?"
               r"(?:\s*[:#]|\s+to\b|\s*(?=\d))" + _SPAN, re.I),
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),                                 # email
    re.compile(r"(?:\*{2,}|\bx{2,}|\bending(?:\s+in)?|\blast\s*(?:4|four))[\s:#-]*\d{4}\b",
               re.I),                                                                 # card tail
]
# Number shapes removed unless explicitly labeled as a product code (_PRODUCT_LABEL):
# separated phone numbers, and spaced/dashed card numbers (4111 1111 1111 1111,
# 3782 822463 10005; 13-19 digits).
_PHONE = re.compile(r"(?<!\w)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\w)")
_CARD_NUMBER = re.compile(r"(?<![\w-])\d{4}(?:[ -]\d{3,7}){2,4}(?![\w-])")
# "SKU 1234-5678-90123", "Item #: ...", "Model no. ..." right before the number (F-72).
_PRODUCT_LABEL = re.compile(r"(?<!\w)(?:sku|item|model|part|mfr|upc)"
                            r"(?:\s*(?:number|num|no\b\.?|#))?\s*[:#]?\s*$", re.I)
_REMOVED = "[removed]"

_PROMPT_PATH = Path(__file__).resolve().parent / "vendor_document_prompt.txt"
SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_USER_TEXT = "Read this vendor document and return the JSON described in your instructions."


# --- Public API ------------------------------------------------------------------

def read_vendor_document(pdf_bytes: bytes, customer_name: str | None = None) -> dict:
    """PDF bytes -> normalised lines, totals and checks (see module docstring)."""
    _validate(pdf_bytes)
    raw = _call_claude(bytes(pdf_bytes))
    return _normalize(raw, customer_name)


def is_transient(exc) -> bool:
    """True for a read that failed only because Claude couldn't be reached or was
    busy (connection/timeout, 408/409/429, 5xx/overloaded): worth one retry by the
    job. False for bad input, unreadable output and other 4xx (e.g. a PDF the API
    refuses)."""
    if not (isinstance(exc, ValueError) and str(exc) == _ERR_API):
        return False
    cause = exc.__cause__
    if isinstance(cause, anthropic.APIConnectionError):     # includes APITimeoutError
        return True
    status = getattr(cause, "status_code", None)
    return isinstance(status, int) and (status in (408, 409, 429) or status >= 500)


# --- Input + Claude call -----------------------------------------------------------

def _validate(pdf_bytes):
    if not isinstance(pdf_bytes, (bytes, bytearray)) or not bytes(pdf_bytes[:4]) == b"%PDF":
        raise ValueError(_ERR_NOT_PDF)
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError(_ERR_TOO_BIG)


def _job_timeout_in(exc):
    """RQ's deadline exception, if the SDK wrapped it (it catches transport errors)."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if type(exc).__name__ == "JobTimeoutException":
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def _call_claude(pdf):
    """One Claude call with the PDF as a document block -> parsed JSON object."""
    content = [
        {"type": "document",
         "source": {"type": "base64", "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf).decode("ascii")}},
        {"type": "text", "text": _USER_TEXT},
    ]
    try:
        response = extractor._client.with_options(
            timeout=_TIMEOUT_S, max_retries=_MAX_RETRIES).messages.create(
            model=extractor.MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=extractor.TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as exc:
        deadline = _job_timeout_in(exc)
        if deadline is not None:
            raise deadline from None   # the job's deadline must reach the worker
        logger.error("Vendor document read failed: %s", type(exc).__name__)
        raise ValueError(_ERR_API) from exc
    if getattr(response, "stop_reason", None) == "max_tokens":
        logger.error("Vendor document read failed: output hit max_tokens")
        raise ValueError(_ERR_CUT_OFF)
    try:
        data = json.loads(extractor._strip_code_fences(extractor._response_text(response)))
    except (ValueError, TypeError, AttributeError, RecursionError):
        data = None
    if not isinstance(data, dict):
        logger.error("Vendor document read failed: output is not a JSON object")
        raise ValueError(_ERR_MALFORMED)
    return data


# --- Normalisation (plain code; the model's JSON is never trusted raw) -----------------

def _scrub(text):
    for p in _PRIVATE:
        text = p.sub(_REMOVED, text)

    def labeled(m):
        return bool(_PRODUCT_LABEL.search(m.string[:m.start()]))

    text = _PHONE.sub(lambda m: m.group() if labeled(m) else _REMOVED, text)
    return _CARD_NUMBER.sub(
        lambda m: _REMOVED if (13 <= sum(c.isdigit() for c in m.group()) <= 19
                               and not labeled(m)) else m.group(),
        text)


def _text(value, key):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)              # json.loads already caps ints at ~4300 digits
        if len(value) > _TEXT_LIMITS[key]:
            return None                 # a cut-off number would be wrong data
    elif isinstance(value, float) and math.isfinite(value):
        value = str(int(value)) if value.is_integer() else str(value)
    if not isinstance(value, str):
        return None
    value = _scrub(" ".join(value.split()))[:_TEXT_LIMITS[key]].strip()
    return value or None


_NUM_STR = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?|\.\d+)", re.ASCII)


def _number(value):
    """Clean non-negative finite number, or None. Accepts "$1,203.57"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        m = _NUM_STR.fullmatch(value.strip())
        if not m or (m.group(1).count(",") and not re.fullmatch(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?",
                                                              m.group(1))):
            return None
        value = float(m.group(1).replace(",", ""))
    if not isinstance(value, (int, float)):
        return None
    if isinstance(value, int) and abs(value) > _MAX_NUMBER:
        return None                     # also avoids OverflowError on 10**400
    value = float(value)
    if not math.isfinite(value) or value < 0 or value > _MAX_NUMBER:
        return None
    return value


def _quantity(value):
    q = _number(value)
    return int(q) if q is not None and q.is_integer() else q


def _money(x):
    return f"${x:,.2f}"


def _dec(x):
    """Float as its printed decimal (10.05 -> Decimal("10.05")), so the cent tolerances are exact."""
    return Decimal(repr(x))


def _normalize(raw, customer_name):
    checks = []
    out = {k: _text(raw.get(k), k) for k in ("vendor", "document_number", "document_date",
                                             "job_name")}
    doc_type = raw.get("document_type")
    doc_type = doc_type.strip().lower() if isinstance(doc_type, str) else None
    out["document_type"] = doc_type if doc_type in DOCUMENT_TYPES else "other"
    for key, what in (("vendor", "vendor"), ("job_name", "job name")):
        if out[key] and _BUYER.search(out[key]):
            out[key] = None
            checks.append(_check("warn", f"{key}_is_buyer",
                                 f"The {what} read was the contractor's own company; left empty."))

    lines, computed, dropped, seen = [], 0, 0, 0
    raw_lines = raw.get("lines") if isinstance(raw.get("lines"), list) else []
    for item in raw_lines[:_SCAN_LINES]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        line = {k: _text(item.get(k), k) for k in _LINE_TEXT}
        if not line["name"] and not line["part_number"]:
            dropped += 1
            continue
        seen += 1
        if len(lines) >= MAX_LINES:
            continue
        line["quantity"] = _quantity(item.get("quantity"))
        line["unit"] = _text(item.get("unit"), "unit")
        line["unit_price"] = _number(item.get("unit_price"))
        line["line_total"] = _number(item.get("line_total"))
        line["_printed_total"] = line["line_total"] is not None
        if (line["line_total"] is None and line["quantity"] is not None
                and line["unit_price"] is not None):
            line["line_total"] = float((_dec(line["quantity"]) * _dec(line["unit_price"]))
                                       .quantize(Decimal("0.01"), ROUND_HALF_UP))
            computed += 1
        lines.append(line)
    if len(raw_lines) > _SCAN_LINES:
        seen += len(raw_lines) - _SCAN_LINES     # not inspected; counted as over the cap
    if seen > MAX_LINES:
        checks.append(_check("warn", "lines_truncated",
                             f"Only the first {MAX_LINES} of {seen} lines were kept."))
    if dropped:
        checks.append(_check("info", "lines_dropped",
                             f"{dropped} line(s) without a name or part number were left out."))
    if computed:
        checks.append(_check("info", "line_totals_computed",
                             f"{computed} line total(s) computed from quantity x unit price."))

    raw_totals = raw.get("totals") if isinstance(raw.get("totals"), dict) else {}
    totals = {k: _number(raw_totals.get(k)) for k in _TOTAL_KEYS}

    checks += _math_checks(lines, totals)
    checks += _job_name_checks(out["job_name"], customer_name)

    for line in lines:
        line.pop("_printed_total")
    return {
        "vendor": out["vendor"],
        "document_type": out["document_type"],
        "document_number": out["document_number"],
        "document_date": out["document_date"],
        "job_name": out["job_name"],
        "lines": lines,
        "totals": totals,
        "checks": checks,
    }


# --- Deterministic checks ------------------------------------------------------------

def _check(level, code, message):
    return {"level": level, "code": code, "message": message}


def _label(i, line):
    name = line["name"] or line["part_number"]
    return f"Line {i} ({name[:60]})"


def _math_checks(lines, totals):
    checks = []
    if not lines:
        return [_check("warn", "no_lines", "No product lines were found in this document.")]

    bad = 0
    tested = 0
    for i, line in enumerate(lines, 1):
        q, p, t = line["quantity"], line["unit_price"], line["line_total"]
        if q is None or p is None or t is None or not line["_printed_total"]:
            continue
        tested += 1
        expected, t = _dec(q) * _dec(p), _dec(t)
        if abs(expected - t) > max(Decimal("0.02"), Decimal("0.005") * abs(t)):
            bad += 1
            checks.append(_check("warn", "line_math",
                                 f"{_label(i, line)}: quantity x unit price is "
                                 f"{_money(expected)} but the printed line total is {_money(t)}."))
    if tested and not bad:
        checks.append(_check("ok", "line_math", "Every line's quantity x unit price matches its total."))

    priced = [l["line_total"] for l in lines if l["line_total"] is not None]
    if priced:
        line_sum = sum((_dec(x) for x in priced), Decimal(0))
        if totals["subtotal"] is not None:
            target, what = _dec(totals["subtotal"]), "subtotal"
        elif totals["total"] is not None:
            target = (_dec(totals["total"]) - _dec(totals["tax"] or 0.0)
                      - _dec(totals["freight"] or 0.0))
            what = "total" + (" (less tax and freight)" if totals["tax"] or totals["freight"] else "")
        else:
            target = None
        if target is not None:
            if abs(line_sum - target) > Decimal("0.05"):
                checks.append(_check("warn", "lines_vs_totals",
                                     f"The line totals add up to {_money(line_sum)} but the "
                                     f"document's {what} is {_money(target)}."))
            else:
                checks.append(_check("ok", "lines_vs_totals",
                                     f"The line totals add up to the document's {what}."))

    if totals["subtotal"] is not None and totals["total"] is not None:
        expected = (_dec(totals["subtotal"]) + _dec(totals["tax"] or 0.0)
                    + _dec(totals["freight"] or 0.0))
        if abs(expected - _dec(totals["total"])) > Decimal("0.05"):
            checks.append(_check("warn", "totals_math",
                                 f"Subtotal + tax + freight is {_money(expected)} but the "
                                 f"printed total is {_money(totals['total'])}."))
        else:
            checks.append(_check("ok", "totals_math", "Subtotal + tax + freight matches the total."))

    unpriced = sum(1 for l in lines if l["unit_price"] is None and l["line_total"] is None)
    if unpriced:
        checks.append(_check("info", "unpriced_lines",
                             f"{unpriced} line(s) have no price (they stay $0)."))
    return checks


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _name_words(text):
    """Lowercase name tokens: "O’Brien-Smith's Bath" -> ["obrien-smith", "bath"]."""
    words = re.split(r"[^\w'’-]+|_", text.lower())
    words = [re.sub(r"['’]s$", "", w) for w in words]            # possessive
    return [w for w in (re.sub(r"['’]", "", w).strip("-") for w in words) if w]


def _last_name(customer_name):
    if not isinstance(customer_name, str):
        return None
    words = _name_words(customer_name)
    while len(words) > 1 and words[-1].strip(".") in _SUFFIXES:
        words.pop()
    return words[-1] if words else None


def _name_parts(words):
    """Whole tokens plus the parts of hyphenated ones (Smith-Jones -> smith, jones)."""
    return set(words) | {p for w in words for p in w.split("-") if p}


def _job_name_checks(job_name, customer_name):
    last = _last_name(customer_name)
    if not last:
        return []
    if not job_name:
        return [_check("info", "job_name_missing",
                       "No job name was found on the document to compare with the customer.")]
    if not _name_parts([last]) & _name_parts(_name_words(job_name)):  # whole words, not substrings
        return [_check("warn", "job_name_mismatch",
                       "The document's job name does not contain the customer's last name. "
                       "Check that this document belongs to this customer.")]
    return [_check("ok", "job_name_match", "The document's job name matches the customer.")]


# --- CLI ------------------------------------------------------------------------------

def _main(argv=None):
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Read a vendor PDF into product lines (prints JSON).")
    ap.add_argument("pdf", help="path to the PDF")
    ap.add_argument("--customer", help="customer name, to check the document's job name")
    args = ap.parse_args(argv)
    try:
        result = read_vendor_document(Path(args.pdf).read_bytes(), args.customer)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
