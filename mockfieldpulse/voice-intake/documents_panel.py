"""Vendor documents (stage P1b): the request side of "Vendor documents" in the
app. Upload a vendor PDF for a customer, list/read the results, delete one.
OUR Postgres (jobs.vendor_documents) + R2 (storage, vendor-docs/) only: no
FieldPulse call of any kind (P2 will send lists). The PDF is read by the RQ
worker (worker.process_vendor_document), never inside a request.

Entry points, each returning (http_status, json_body) so the routes in
orchestrator.py stay one-liners (the route parses the customer with the same
helpers as /consultation/submit):

    upload_url(filename, size)
    submit(file_key, filename, customer_id, customer)
    list_documents(customer_id, offset, limit)
    document_detail(doc_id)
    delete_document(doc_id, user_id)

Hidden in here: filename/size/key rules (PDF only, <= 20 MB, our vendor-docs/
prefix), the R2 size check, one row per upload, the compact list shape,
scrubbing every response (admin_panel.scrub) and the delete order (row first,
then the PDF, best effort).
"""

from __future__ import annotations

import logging
import os
import re
import sys

import jobs
import queue_setup
import storage
from admin_panel import scrub
from observability import report
from vendor_documents import MAX_PDF_BYTES   # 20 MB, the reader's own limit

logger = logging.getLogger("voice_intake.documents_panel")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

MAX_PAGE = 100
DEFAULT_PAGE = 50
_MAX_FILENAME = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def customer_display_name(customer):
    """Name to show for a customer snapshot (display_name, else first + last), or None.
    Also what the worker hands the reader for the job-name check."""
    c = customer if isinstance(customer, dict) else {}
    name = c.get("display_name")
    if isinstance(name, str) and name.strip():
        return name.strip()[:200]
    parts = [c.get(k).strip() for k in ("first_name", "last_name")
             if isinstance(c.get(k), str) and c.get(k).strip()]
    return " ".join(parts)[:200] or None


def _filename(raw):
    """Clean PDF filename (no path, no control chars, <= 200 chars) or None."""
    if not isinstance(raw, str):
        return None
    name = _CONTROL.sub("", os.path.basename(raw.replace("\\", "/"))).strip()
    if not name.lower().endswith(".pdf") or len(name) <= 4:
        return None
    if len(name) > _MAX_FILENAME:
        name = name[:_MAX_FILENAME - 4].rstrip() + ".pdf"
    return name


def _too_big():
    return 413, {"error": "The PDF is larger than 20 MB."}


# --- Upload ------------------------------------------------------------------------

def upload_url(filename, size):
    """Presigned PUT for a new vendor-docs/<uuid>.pdf key. The browser PUTs the
    file with Content-Type: application/pdf, then calls submit()."""
    if _filename(filename) is None:
        return 400, {"error": "Choose a PDF file (the name must end in .pdf)."}
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return 400, {"error": "'size' must be the file size in bytes."}
    if size > MAX_PDF_BYTES:
        return _too_big()
    try:
        up = storage.vendor_document_upload()
    except storage.StorageError as exc:
        logger.error("Vendor document upload URL failed: %s", exc)
        report(exc, "R2 upload URL generation failed (vendor document)")
        return 502, {"error": "Could not prepare the upload right now. Try again shortly."}
    logger.info("Issued vendor document upload URL: %s (%d bytes)", up["key"], size)
    return 200, {"key": up["key"], "url": up["url"],
                 "content_type": storage.VENDOR_DOC_CONTENT_TYPE}


def submit(file_key, filename, customer_id, customer):
    """After the PUT: check the object is ours, there and <= 20 MB, create the
    row, queue the read -> 202 {document_id, status}. Nothing is read here."""
    if not isinstance(file_key, str) or not storage.VENDOR_DOC_KEY_RE.match(file_key):
        return 400, {"error": "'file_key' is not a vendor document upload key."}
    name = _filename(filename)
    if name is None:
        return 400, {"error": "'filename' must be a PDF file name."}
    if not customer_id or not isinstance(customer, dict):
        return 400, {"error": "'customer' is required (pick the customer first)."}
    try:
        size = storage.object_size(file_key)
    except storage.StorageError as exc:
        logger.error("Vendor document HEAD check failed: %s", exc)
        report(exc, "R2 HEAD check on vendor document submit failed")
        return 502, {"error": "Could not check the upload right now. Try again shortly."}
    if size is None:
        return 404, {"error": "The upload was not found. Upload the PDF again."}
    if size == 0:
        return 400, {"error": "The uploaded file is empty."}
    if size > MAX_PDF_BYTES:
        _remove_pdf(None, file_key)
        return _too_big()
    try:
        doc = jobs.create_vendor_document(file_key, filename=name, file_size=size,
                                          customer_id=customer_id, customer=customer)
    except Exception as exc:  # noqa: BLE001 — UniqueViolation: this upload was already submitted
        if type(exc).__name__ == "UniqueViolation":
            return 409, {"error": "This upload was already submitted."}
        raise
    try:
        queue_setup.enqueue_vendor_document(doc["id"])
    except Exception as exc:  # noqa: BLE001 — Redis down etc.
        logger.error("[%s] could not queue vendor document: %s", doc["id"], exc)
        report(exc, "Could not queue vendor document", job_id=doc["id"])
        jobs.update_vendor_document(doc["id"], "failed",
                                    error="Could not queue the document for reading. "
                                          "Delete it and upload it again.")
        return 503, {"error": "Could not queue the document. Try again shortly.",
                     "document_id": doc["id"]}
    logger.info("[%s] vendor document queued (customer_id=%s)", doc["id"], customer_id)
    return 202, {"document_id": doc["id"], "status": doc["status"]}


# --- Read ----------------------------------------------------------------------------

def _int(v, default):
    if v is None or v == "":
        return default
    return int(v)


def list_documents(customer_id=None, offset=0, limit=DEFAULT_PAGE):
    """Newest first; compact rows only (no lines/checks)."""
    try:
        offset = max(0, _int(offset, 0))
        limit = max(1, min(_int(limit, DEFAULT_PAGE), MAX_PAGE))
    except (TypeError, ValueError):
        return 400, {"error": "'offset' and 'limit' must be integers"}
    page = jobs.list_vendor_documents(customer_id=customer_id, offset=offset, limit=limit)
    items = [{
        "id": r["id"],
        "created_at": r.get("created_at"),
        "status": r.get("status"),
        "customer_id": r.get("customer_id"),
        "customer_name": customer_display_name(r.get("customer")),
        "filename": r.get("filename"),
        "vendor": r.get("vendor"),
        "document_type": r.get("document_type"),
        "line_count": r.get("line_count"),
        "warn_count": r.get("warn_count"),
        "error": r.get("error"),
        "stuck": bool(r.get("stuck")),
    } for r in page["items"]]
    return 200, scrub({"items": items, "total": page["total"], "offset": offset,
                       "limit": limit})


def _can_delete(doc):
    return doc.get("status") not in ("queued", "processing") or bool(doc.get("stuck"))


def document_detail(doc_id):
    """The full row (result = the reader's output), scrubbed; no storage keys/links."""
    doc = jobs.get_vendor_document(doc_id)
    if doc is None:
        return 404, {"error": f"Document not found: {doc_id}"}
    result = doc.get("result") if isinstance(doc.get("result"), dict) else None
    return 200, scrub({
        "id": doc["id"],
        "status": doc.get("status"),
        "error": doc.get("error"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "customer_id": doc.get("customer_id"),
        "customer": doc.get("customer"),
        "customer_name": customer_display_name(doc.get("customer")),
        "filename": doc.get("filename"),
        "file_size": doc.get("file_size"),
        "stuck": bool(doc.get("stuck")),
        "can_delete": _can_delete(doc),
        "result": result,
    })


# --- Delete --------------------------------------------------------------------------

def _remove_pdf(doc_id, key):
    """Best-effort R2 delete; logged + reported on failure. 'deleted' | 'failed'."""
    try:
        storage.delete_audio(key)          # generic R2 object delete (bounded client)
        return "deleted"
    except Exception as exc:  # noqa: BLE001 — never undo what already happened
        logger.error("[%s] vendor PDF %s was not removed from R2: %s", doc_id or "-", key, exc)
        report(exc, "R2 vendor document delete failed", job_id=doc_id)
        return "failed"


def delete_document(doc_id, user_id=None):
    """Delete the row (refused while queued/processing, unless stuck), then the
    PDF in R2 (best effort). Never FieldPulse."""
    out = jobs.delete_vendor_document(doc_id)
    if out is None:
        return 404, {"error": f"Document not found: {doc_id}"}
    if not out["deleted"]:
        return 409, {"error": out["reason"]}
    key = out["document"].get("file_key")
    file_status = _remove_pdf(doc_id, key) if key else "none"
    logger.info("[%s] vendor document deleted by %s (file %s)", doc_id,
                (user_id or "?")[:64] if isinstance(user_id, str) else "?", file_status)
    return 200, {"deleted": True, "id": doc_id, "file": file_status}
