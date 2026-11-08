"""Combine one customer's read vendor documents into ONE material list (stage P2).
Pure: no I/O, no env, no FieldPulse. The preview on the customer page and the body
fieldpulse_sync.sync_vendor_materials sends are both built from this.

One entry point:

    build_material_list(documents) -> {
        sections: [{vendor, lines: [{title, sku, description, quantity, unit_price,
                                     sources, flags}]}],
        notes: [str],
        document_count: int,       # DONE documents used
        line_count: int,
    }

`documents` are vendor_documents rows (jobs.py shape: id, status, created_at,
filename, result = vendor_documents.read_vendor_document()'s dict). Only status
'done' with a dict result is used; the rest is ignored.

Rules (flag, don't fabricate):
  - One section per canonical vendor (alias map, case-insensitive; null -> "Other"),
    in first-upload order.
  - Within a vendor, lines from DIFFERENT documents merge by part number
    (_same_part). A merged line never takes two lines from the same document.
    Lines without a usable part number never merge.
  - Price only as printed: unit_price from the latest-uploaded document that printed
    one, else 0 with the flag "No price yet". Never derived from line totals.
  - Quantity from that priced document if printed, else from any document, else 1
    with the flag "Quantity not printed".
  - Tax / freight are never lines: per-document notes (totals, and fee-only lines).
  - Caps: title 255, description 1000, 200 lines in total (the rest counted in notes).
"""

from __future__ import annotations

import math
import re

MAX_LINES = 200
TITLE_CHARS = 255
DESC_CHARS = 1000
OTHER_VENDOR = "Other"
_MIN_PART = 3            # normalized part numbers shorter than this are "no part number"
# Suffix matches ("MOE6410" ~ "6410"): the shorter part must have at least this many
# characters. The brief said >= 5 but also listed MOE6410 ~ 6410 (4) as a match; 4
# keeps that real case and the leftover prefix must still be 1-3 letters.
_MIN_SUFFIX_PART = 4

FLAG_NO_PRICE = "No price yet"
FLAG_NO_QTY = "Quantity not printed"
FLAG_NOT_ON_QUOTE = "Not on the quote"
FLAG_NOT_IN_PORTFOLIO = "Not in the portfolio"

_VENDOR_ALIASES = {
    "ferguson": "Ferguson",
    "ferguson home": "Ferguson",
    "ferguson enterprises": "Ferguson",
    "onyx": "Onyx",
    "onyx collection": "Onyx",
    "the onyx collection": "Onyx",
}

# A line that is only a fee ("Sales tax", "Freight", "Delivery charge"): never a material.
_FEE_LINE = re.compile(
    r"(?:sales\s+)?tax(?:es)?|(?:estimated\s+)?(?:freight|shipping|delivery)"
    r"(?:\s*(?:&|and)\s*handling)?(?:\s+(?:charge|charges|fee|fees|cost|costs))?",
    re.I)


# --- Small helpers ----------------------------------------------------------------------

def _str(v):
    return " ".join(v.split()) if isinstance(v, str) and v.strip() else None


def _num(v):
    """A clean non-negative finite number as stored by the reader, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not math.isfinite(v) or v < 0:
        return None
    return int(v) if float(v).is_integer() else v


def canonical_vendor(name):
    """Section name: the alias map's name, the vendor as printed, or "Other"."""
    s = _str(name)
    if not s:
        return OTHER_VENDOR
    key = re.sub(r"[^\w&]+", " ", s.casefold()).strip()
    return _VENDOR_ALIASES.get(key, s[:TITLE_CHARS])


def _vendor_key(name):
    return re.sub(r"[^\w&]+", " ", name.casefold()).strip()


def normalize_part(part):
    """Uppercase, alphanumerics only; None when too short to compare."""
    s = re.sub(r"[^A-Za-z0-9]", "", part).upper() if isinstance(part, str) else ""
    return s if len(s) >= _MIN_PART else None


def same_part(a, b):
    """True if two NORMALIZED part numbers name the same product: equal, or the
    longer is the shorter with a 1-3 letter brand prefix (MOE6410 ~ 6410)."""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < _MIN_SUFFIX_PART or not long_.endswith(short):
        return False
    return bool(re.fullmatch(r"[A-Z]{1,3}", long_[:len(long_) - len(short)]))


def _money(x):
    return f"${x:,.2f}"


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _sentence(text):
    return text if text[-1] in ".!?…" else text + "."


# --- Build --------------------------------------------------------------------------------

def _done_documents(documents):
    docs = [d for d in (documents or []) if isinstance(d, dict) and d.get("status") == "done"
            and isinstance(d.get("result"), dict)]
    # First-upload order; created_at is an ISO string (same tz) from jobs.py; id breaks ties.
    return sorted(docs, key=lambda d: (str(d.get("created_at") or ""), str(d.get("id") or "")))


def _doc_label(doc):
    """"Ferguson quote Q-1" (or the file name when nothing identifies it)."""
    r = doc["result"]
    number = _str(r.get("document_number"))
    if not number and not _str(r.get("vendor")):
        return _str(doc.get("filename")) or "Document"
    kind = _str(r.get("document_type")) or "document"
    return f"{canonical_vendor(r.get('vendor'))} {kind}" + (f" {number}" if number else "")


def _source(doc):
    r = doc["result"]
    return {"document_id": doc.get("id"), "filename": _str(doc.get("filename")),
            "document_type": _str(r.get("document_type")),
            "document_number": _str(r.get("document_number"))}


def build_material_list(documents):
    """See the module docstring."""
    docs = _done_documents(documents)
    notes = []
    vendors = {}                    # key -> {"name", "docs": [(order, doc)], "clusters": []}
    for order, doc in enumerate(docs):
        r = doc["result"]
        name = canonical_vendor(r.get("vendor"))
        v = vendors.setdefault(_vendor_key(name), {"name": name, "docs": [], "clusters": []})
        v["docs"].append((order, doc))
        totals = r.get("totals") if isinstance(r.get("totals"), dict) else {}
        fees = [f"{label} {_money(_num(totals.get(k)))}" for k, label in
                (("tax", "tax"), ("freight", "freight")) if _num(totals.get(k))]
        for line in r.get("lines") if isinstance(r.get("lines"), list) else []:
            if not isinstance(line, dict):
                continue
            title = _str(line.get("name"))
            if title and _FEE_LINE.fullmatch(title):
                amount = _num(line.get("line_total"))
                if amount is None:
                    amount = _num(line.get("unit_price"))
                fees.append(f"{title.lower()} line" + (f" {_money(amount)}" if amount else ""))
                continue
            if not title and not _str(line.get("part_number")):
                continue
            _add_line(v["clusters"], order, doc, line)
        if fees:
            notes.append(f"{_doc_label(doc)}: " + ", ".join(fees) + " (not in the list).")

    sections, total_lines, kept = [], 0, 0
    for v in vendors.values():
        has_priced = any(_num(l.get("unit_price")) is not None
                         for _, d in v["docs"] for l in _lines(d))
        has_selection = any(d["result"].get("document_type") == "selection" for _, d in v["docs"])
        lines = []
        for cluster in v["clusters"]:
            total_lines += 1
            if kept >= MAX_LINES:
                continue
            lines.append(_merged_line(cluster, has_priced, has_selection))
            kept += 1
        if lines:
            sections.append({"vendor": v["name"], "lines": lines})
    if total_lines > kept:
        notes.append(f"Only the first {kept} of {total_lines} lines are listed.")
    return {"sections": sections, "notes": notes, "document_count": len(docs),
            "line_count": kept}


def _lines(doc):
    lines = doc["result"].get("lines")
    return [l for l in lines if isinstance(l, dict)] if isinstance(lines, list) else []


def _add_line(clusters, order, doc, line):
    part = normalize_part(line.get("part_number"))
    entry = {"order": order, "doc": doc, "line": line, "part": part}
    if part:
        for c in clusters:
            if any(e["order"] == order for e in c):
                continue            # never two lines of the same document in one line
            if any(same_part(part, e["part"]) for e in c):
                c.append(entry)
                return
    clusters.append([entry])


def _merged_line(cluster, vendor_has_priced, vendor_has_selection):
    latest_first = sorted(cluster, key=lambda e: e["order"], reverse=True)
    selection = next((e for e in cluster if e["doc"]["result"].get("document_type")
                      == "selection" and _str(e["line"].get("name"))), None)
    titled = selection or next((e for e in latest_first if _str(e["line"].get("name"))), None)
    priced = next((e for e in latest_first if _num(e["line"].get("unit_price")) is not None),
                  None)
    flags = []

    if priced and _num(priced["line"].get("quantity")) is not None:
        qty_entry = priced
    else:
        qty_entry = next((e for e in latest_first if _num(e["line"].get("quantity")) is not None),
                         None)
    quantity = _num(qty_entry["line"].get("quantity")) if qty_entry else None
    if quantity is None:
        quantity = 1
        flags.append(FLAG_NO_QTY)

    unit_price = _num(priced["line"].get("unit_price")) if priced else None
    has_selection_line = any(e["doc"]["result"].get("document_type") == "selection"
                             for e in cluster)
    if unit_price is None:
        if has_selection_line and vendor_has_priced:
            flags.append(FLAG_NOT_ON_QUOTE)
        flags.append(FLAG_NO_PRICE)
        unit_price = 0
    elif vendor_has_selection and not has_selection_line:
        flags.append(FLAG_NOT_IN_PORTFOLIO)

    sku_entry = priced if priced and _str(priced["line"].get("part_number")) else \
        next((e for e in cluster if _str(e["line"].get("part_number"))), None)
    sku = _str(sku_entry["line"].get("part_number")) if sku_entry else None
    title = (_str(titled["line"].get("name")) if titled else None) or sku or "Item"

    # Mandatory first (F-79): the flags and the unit always survive the 1000-char cap;
    # only the descriptive text after them is clipped.
    head = []
    if flags:
        head.append("; ".join(flags))
    unit = _str(qty_entry["line"].get("unit")) if qty_entry else None
    if unit:
        head.append(f"Unit: {unit}")
    parts = []
    for e in (titled, priced):
        if not e:
            continue
        for key in ("details", "notes"):
            t = _str(e["line"].get(key))
            if t and t not in parts:
                parts.append(t)
    description = " ".join(_sentence(p) for p in head + parts)

    return {
        "title": _clip(title, TITLE_CHARS),
        "sku": _clip(sku, TITLE_CHARS) if sku else None,
        "description": _clip(description, DESC_CHARS) if description else None,
        "quantity": quantity,
        "unit_price": unit_price,
        "sources": [_source(e["doc"]) for e in sorted(cluster, key=lambda e: e["order"])],
        "flags": flags,
    }
