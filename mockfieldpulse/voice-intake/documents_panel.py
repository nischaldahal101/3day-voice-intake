"""Vendor documents (stages P1b + P2): the request side of "Vendor documents" in
the app. Upload a vendor PDF for a customer, list/read the results, delete one;
per customer: the documents, the combined material list preview and its
FieldPulse status, and the manual "Send to FieldPulse".
OUR Postgres (jobs.vendor_documents, jobs.vendor_material_lists) + R2 (storage,
vendor-docs/) only: NO FieldPulse call of any kind here. The PDF is read and the
list is sent by the RQ worker (worker.process_vendor_document,
worker.sync_vendor_materials_job -> fieldpulse_sync), never inside a request.

Entry points, each returning (http_status, json_body) so the routes in
orchestrator.py stay one-liners (the route parses the customer with the same
helpers as /consultation/submit):

    upload_url(filename, size)
    submit(file_key, filename, customer_id, customer)
    list_documents(customer_id, offset, limit)
    document_detail(doc_id)
    delete_document(doc_id, user_id)          (+ queues the customer's list rebuild)
    list_customers()                          customers with documents + list status
    customer_detail(customer_id)              documents, preview, FieldPulse record
    request_send(customer_id)                 queue the manual send -> 202

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
import vendor_materials
from fieldpulse_sync import VENDOR_LIST_PREFIX, customer_name, vendor_list_notes  # pure text
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
    # Rebuild the customer's combined list without it (worker; best effort).
    queued = _queue_list(out["document"].get("customer_id"), False,
                         out["document"].get("customer"), doc_id)
    return 200, {"deleted": True, "id": doc_id, "file": file_status,
                 "materials": "queued" if queued else "not_queued"}


# --- Per customer: combined vendor list (stage P2) ----------------------------------------

_ATTENTION = ("failed", "unknown", "needs_config")
_STALE_SENDING_S = 300
_SYNC_PENDING_S = 300        # a done document newer than the list record: sync on its way
LIST_LABELS = {"not_sent": "Not sent", "dry_run": "Dry run", "held": "Held (dry run)",
               "sent": "Sent", "attention": "Needs attention", "sending": "Sending…"}


def _age_s(iso):
    from datetime import datetime, timezone
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def list_status(status, updated_at=None, create_pending=False):
    """The FieldPulse list status for the app: not_sent | dry_run | held | sent |
    attention | sending. A fresh 'sending' wins; then an unresolved create
    (create_pending) is always attention, whatever the last run recorded; 'sending'
    older than 5 min = the worker died mid-send -> attention."""
    if status == "sending":
        age = _age_s(updated_at)
        return "sending" if age is not None and age <= _STALE_SENDING_S else "attention"
    if create_pending or status in _ATTENTION:
        return "attention"
    if status in ("sent", "dry_run", "held"):
        return status
    return "not_sent"


def sync_pending(last_done_at, list_updated_at):
    """True while a document finished recently (5 min) and the list record is older:
    its automatic sync is queued or running, so the app keeps refreshing (F-80)."""
    age = _age_s(last_done_at)
    if age is None or age > _SYNC_PENDING_S:
        return False
    return list_updated_at is None or (_age_s(list_updated_at) or 0) > age


def _queue_list(customer_id, manual, customer, ref):
    if not customer_id:
        return False
    try:
        queue_setup.enqueue_vendor_materials(customer_id, manual, customer)
        return True
    except Exception as exc:  # noqa: BLE001 — Redis down etc.
        logger.error("[%s] could not queue the vendor material list update: %s",
                     ref or customer_id, type(exc).__name__)
        report(exc, "Could not queue vendor material sync", job_id=ref)
        return False


def list_customers():
    """Customers that have vendor documents, newest upload first."""
    rows = jobs.list_vendor_document_customers()
    items = []
    for r in rows:
        key = list_status(r.get("list_status"), r.get("list_updated_at"),
                          r.get("create_pending"))
        items.append({
            "customer_id": r["customer_id"],
            "customer_name": customer_display_name(r.get("customer")),
            "document_count": r.get("document_count"),
            "last_upload_at": r.get("last_upload_at"),
            "processing_count": r.get("processing_count"),
            "warn_count": r.get("warn_count"),
            "fieldpulse_status": key,
            "fieldpulse_label": LIST_LABELS[key],
            "fieldpulse_list_id": r.get("fieldpulse_list_id"),
            "sync_pending": sync_pending(r.get("last_done_at"), r.get("list_updated_at")),
        })
    return 200, scrub({"items": items, "total": len(items)})


def _compact(doc):
    r = doc.get("result") if isinstance(doc.get("result"), dict) else {}
    lines, checks = r.get("lines"), r.get("checks")
    return {
        "id": doc["id"], "created_at": doc.get("created_at"), "status": doc.get("status"),
        "filename": doc.get("filename"), "vendor": r.get("vendor"),
        "document_type": r.get("document_type"),
        "line_count": len(lines) if isinstance(lines, list) else None,
        "warn_count": sum(1 for c in checks if isinstance(c, dict) and c.get("level") == "warn")
        if isinstance(checks, list) else None,
        "error": (doc.get("error") or "")[:300] or None, "stuck": bool(doc.get("stuck")),
    }


def customer_detail(customer_id):
    """The customer page: snapshot, documents (newest first), the combined preview
    (vendor_materials.build_material_list = exactly the lines that will be sent) and
    the FieldPulse list record. 404 when the customer has neither."""
    docs = jobs.list_customer_vendor_documents(customer_id)
    record = jobs.get_vendor_material_list(customer_id)
    if not docs and record is None:
        return 404, {"error": f"No vendor documents for customer {customer_id}"}
    customer = next((d["customer"] for d in reversed(docs) if isinstance(d.get("customer"), dict)),
                    None)
    name = customer_display_name(customer)
    built = vendor_materials.build_material_list(docs)
    rec = record or {}
    key = list_status(rec.get("status"), rec.get("updated_at"), rec.get("create_pending"))
    done_at = max((d.get("updated_at") or "" for d in docs if d.get("status") == "done"),
                  default="") or None
    return 200, scrub({
        "customer_id": customer_id,
        "customer": customer,
        "customer_name": name,
        "documents": [_compact(d) for d in reversed(docs)],
        "processing_count": sum(1 for d in docs if d.get("status") in ("queued", "processing")
                                and not d.get("stuck")),
        "sync_pending": sync_pending(done_at, rec.get("updated_at")),
        "preview": {
            **built,
            # The list's title + notes text exactly as sent (built["notes"] are inside it).
            "title": (VENDOR_LIST_PREFIX + customer_name({"customer": customer,
                                                          "customer_id": customer_id}))[:255],
            "notes": vendor_list_notes(built["document_count"], built["notes"]),
        },
        "fieldpulse": {
            "status": key, "label": LIST_LABELS[key], "raw_status": rec.get("status"),
            "list_id": rec.get("fieldpulse_list_id"), "mode": rec.get("mode"),
            "create_pending": bool(rec.get("create_pending")), "held": bool(rec.get("held")),
            "last_error": rec.get("last_error"), "updated_at": rec.get("updated_at"),
            "last_request": rec.get("last_request"), "last_response": rec.get("last_response"),
            "history": rec.get("history") or [],
        },
    })


def request_send(customer_id, user_id=None):
    """Queue the manual "Send to FieldPulse" for this customer's list -> 202. The
    worker's FIELDPULSE_WRITE_MODE still decides whether anything is sent."""
    docs = jobs.list_customer_vendor_documents(customer_id, limit=1)
    if not docs and jobs.get_vendor_material_list(customer_id) is None:
        return 404, {"error": f"No vendor documents for customer {customer_id}"}
    if not _queue_list(customer_id, True, None, None):
        return 503, {"error": "Could not queue the send. Try again shortly."}
    logger.info("[customer %s] manual vendor list send queued by %s", customer_id,
                (user_id or "?")[:64] if isinstance(user_id, str) else "?")
    return 202, {"status": "queued", "customer_id": customer_id}
