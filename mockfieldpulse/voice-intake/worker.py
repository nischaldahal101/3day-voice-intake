"""RQ worker entry-point for consultation jobs.

For each job on the `consultations` queue:
  1. Look it up in Postgres (get_job). Missing -> JobError, RQ records failure.
  2. Mark status='processing'.
  3. Get the transcript:
       - recorded/uploaded audio (audio_key): download from R2, transcribe;
       - pasted transcript (jobs.transcript): use it as-is, no transcription.
  4. Extract (Claude, null-flagged), told the consultation's date so a
     relative return appointment ("next Thursday") can be resolved. Then
     match the extracted materials to Mike's FieldPulse item list (optional,
     never fails the job; stored on the extraction).
  5. SAVE FIRST: transcript + extraction go on the row (still 'processing')
     before anything touches FieldPulse (docs/FIELDPULSE_API.md §11).
  6. fieldpulse_sync.sync_consultation: real FieldPulse write, gated by
     FIELDPULSE_WRITE_MODE (default dry_run = nothing sent). It never raises
     and never fails the job; its summary is stored as result.fieldpulse_sync.
  7. status='done'. Any failure in steps 3-5: status='failed' with a clear
     error, then re-raise so RQ also records it. Job never stays 'processing'.
  8. Always clean up the temp audio file.

Also runs the admin panel's manual "Send to FieldPulse" for a finished job
(send_consultation_to_fieldpulse, queued by admin_panel.request_manual_send),
reads uploaded vendor PDFs (process_vendor_document, stage P1b, queued by
documents_panel.submit; no FieldPulse call), and rebuilds each customer's combined
vendor material list (sync_vendor_materials_job, stage P2: queued after a document is
read, after one is deleted, and by the manual send; fieldpulse_sync does the write),
and matches Ferguson carts to FieldPulse customers by name (match_cart_customer, stage
E3, queued by carts_panel; a read only).

The worker no longer calls the mock writer (consultation_writer); that module
stays only for the deprecated sync route until stage 6 removes the mock.

Start the worker with:
    cd voice-intake/
    OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES venv/bin/rq worker consultations
(the env-var prefix is macOS-only; on Linux/Render the bare command works.)
"""

import logging
import os
import sys
import tempfile
import time

import queue_setup
import vendor_documents
from customer_source import list_catalog_items
from documents_panel import customer_display_name
from extractor import ExtractionError, extract_consultation, match_catalog_items
from fieldpulse_sync import (consultation_reference, sync_cart, sync_consultation,
                             sync_vendor_materials)
from jobs import (JobError, get_job, get_vendor_document, save_manual_send,
                  update_job_status, update_vendor_document)
from observability import init_sentry, report, traced_job
from pipeline import transcribe
from storage import StorageError, download_audio, download_bytes
from transcriber import TranscriptionError

logger = logging.getLogger("voice_intake.worker")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

# Sentry: no-op unless SENTRY_DSN is set. RQ imports this module inside the
# forked work-horse, so this runs per job; traced_job flushes before the horse
# exits (see observability.py). No start-command change needed.
init_sentry("worker")


@traced_job
def process_consultation_job(job_id):
    """Full pipeline for one queued consultation job."""
    logger.info("[%s] picking up job", job_id)

    job = get_job(job_id)
    if job is None:
        raise JobError(f"Job not found: {job_id}")

    audio_key = job.get("audio_key")
    pasted = job.get("transcript")
    pasted = pasted.strip() if isinstance(pasted, str) else ""
    if not audio_key and not pasted:
        _fail(job_id, "job has no audio_key and no transcript")
        raise JobError(f"Job {job_id} has no audio_key and no transcript")

    logger.info(
        "[%s] input=%s customer_id=%s source=%s",
        job_id, "audio" if audio_key else "paste",
        job.get("customer_id") or "—", job.get("source") or "—",
    )
    update_job_status(job_id, "processing")

    tmp_path = None
    try:
        if audio_key:
            # Keep the R2 key's extension so the transcriber sends Deepgram
            # the right Content-Type.
            ext = os.path.splitext(audio_key)[1] or ".bin"
            fd, tmp_path = tempfile.mkstemp(prefix="consult_", suffix=ext)
            os.close(fd)
            logger.info("[%s] downloading r2 -> %s", job_id, tmp_path)
            download_audio(audio_key, tmp_path)
            transcript, transcription_seconds = transcribe(tmp_path)
        else:
            transcript, transcription_seconds = pasted, None

        # The consultation's own date (job created_at in FIELDPULSE_TIMEZONE),
        # the same day fieldpulse_sync later validates the return date against.
        ref_day, ref_tz = consultation_reference(job)
        extraction = extract_consultation(transcript, reference_date=ref_day,
                                          reference_tz=ref_tz)
        _match_materials(job_id, extraction)

        # Save first: everything the app (and the stage-3 admin panel) needs
        # is on the row before FieldPulse is touched.
        summary = {
            "input": "audio" if audio_key else "paste",
            "extraction": extraction,
            "transcription_seconds": transcription_seconds,
            "transcript_chars": len(transcript),
            "transcript": transcript,
            "fieldpulse_sync": {"status": "pending"},
        }
        update_job_status(job_id, "processing", result=summary)

    except StorageError as exc:
        _fail(job_id, f"R2 download failed: {exc}")
        raise
    except TranscriptionError as exc:
        _fail(job_id, f"Transcription failed: {exc}")
        raise
    except ExtractionError as exc:
        _fail(job_id, f"Extraction failed: {exc}")
        raise
    except Exception as exc:  # noqa: BLE001 — job must never stay 'processing'
        _fail(job_id, f"Unexpected {type(exc).__name__}: {exc}")
        raise
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
                logger.info("[%s] cleaned up %s", job_id, tmp_path)
            except OSError:
                pass

    # FieldPulse never fails the consultation (sync_consultation doesn't raise;
    # the guard below is belt and braces).
    try:
        sync = sync_consultation(job, extraction)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] sync_consultation raised", job_id)
        report(exc, "sync_consultation raised", job_id=job_id)
        sync = {"status": "failed", "message": "FieldPulse sync hit an internal error. "
                "The consultation is saved.", "error": f"{type(exc).__name__}", "steps": []}
    summary["fieldpulse_sync"] = sync

    try:
        update_job_status(job_id, "done", result=summary)
    except Exception as exc:  # noqa: BLE001
        _fail(job_id, f"Could not save the finished job: {type(exc).__name__}: {exc}")
        raise
    logger.info("[%s] DONE (fieldpulse_sync=%s)", job_id, sync.get("status"))


@traced_job
def send_consultation_to_fieldpulse(job_id, request_id):
    """Admin "Send to FieldPulse" / "Retry" (queued by admin_panel). Runs
    fieldpulse_sync for the saved extraction as an explicit MANUAL send: the
    worker's FIELDPULSE_WRITE_MODE still decides whether anything goes out.
    Records the outcome on the request's entry in result.fieldpulse_manual_sends
    and the new summary as result.fieldpulse_sync. Never raises (a crash is
    recorded on the entry), so RQ never retries it on its own."""
    from datetime import datetime, timezone

    def now():
        return datetime.now(timezone.utc).isoformat()

    entry = {"id": request_id, "started_at": now()}
    try:
        job = get_job(job_id)
        if job is None:
            logger.error("[%s] manual send: job not found", job_id)
            return
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        extraction = result.get("extraction")
        if job.get("status") != "done" or not isinstance(extraction, dict):
            save_manual_send(job_id, {**entry, "status": "error", "finished_at": now(),
                                      "message": "Not sent: this consultation has no finished "
                                                 "extraction to send."})
            return
        logger.info("[%s] manual FieldPulse send (request %s)", job_id, request_id)
        # If the admin deleted the consultation meanwhile, every claim fails
        # (jobs.claim_fieldpulse_write checks the row under a lock, F-28), so
        # nothing is sent, and save_manual_send finds no row: stop quietly.
        sync = sync_consultation(job, extraction, manual=True)
        if not save_manual_send(job_id, {**entry, "status": "finished", "finished_at": now(),
                                         "outcome": sync.get("status"), "mode": sync.get("mode"),
                                         "message": sync.get("message")}, sync=sync):
            logger.info("[%s] manual send: consultation was deleted meanwhile; nothing recorded",
                        job_id)
            return
        logger.info("[%s] manual send done: %s", job_id, sync.get("status"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] manual send crashed", job_id)
        report(exc, "Manual FieldPulse send crashed", job_id=job_id)
        try:
            save_manual_send(job_id, {**entry, "status": "error", "finished_at": now(),
                                      "message": f"The send hit an internal error "
                                                 f"({type(exc).__name__}). Check the "
                                                 f"FieldPulse steps below before retrying."})
        except Exception as exc2:  # noqa: BLE001
            logger.exception("[%s] could not record the manual send error", job_id)
            report(exc2, "Could not record manual send error", job_id=job_id)


def _match_materials(job_id, extraction):
    """Stage 1b: link each extracted material to Mike's FieldPulse item list
    (read-only GET /items via customer_source, so fieldpulse_sync never reads the read-only key).
    Stored on the extraction (materials[].catalog_item), so Retry reuses it.
    Optional: never raises, never fails the job; no match -> placeholder item."""
    try:
        mats = extraction.get("materials") if isinstance(extraction, dict) else None
        if not isinstance(mats, list) or not mats:
            return
        n = match_catalog_items(extraction, list_catalog_items())
        logger.info("[%s] materials matched to the item list: %d of %d", job_id, n, len(mats))
    except Exception as exc:  # noqa: BLE001 — both callees already never raise
        logger.warning("[%s] material matching skipped (%s)", job_id, type(exc).__name__)
        report(exc, "Material catalog matching crashed", job_id=job_id)


# --- Vendor documents (stage P1b) ---------------------------------------------------

_DOC_RETRY_PAUSE_S = 5
# Fixed, readable messages saved on a failed vendor_documents row (shown in the app);
# other reader errors use vendor_documents.USER_MESSAGES. Never upstream text.
DOC_ERR_MISSING = "The uploaded PDF was not found in storage. Upload it again."
DOC_ERR_STORAGE = "Could not download the PDF from storage. Upload it again later."
DOC_ERR_TOO_BIG = "This PDF is larger than 20 MB."   # R2 object grew after submit
DOC_ERR_TIMEOUT = "Reading the document took too long and was stopped."
DOC_ERR_INTERNAL = "Reading the document hit an internal error."
# Claude API error (F-74): after the one retry for a transient failure, vs a request
# Claude rejected (not retried).
DOC_ERR_CLAUDE_BUSY = ("Claude was busy or unreachable (tried twice). Upload the PDF "
                       "again later.")
DOC_ERR_CLAUDE_REJECTED = ("Claude could not read this PDF request (it was rejected). "
                           "Check that it is a normal, unprotected PDF, then upload it again.")


def _is_job_timeout(exc):
    return type(exc).__name__ == "JobTimeoutException"   # rq.timeouts (any rq version)


def _doc_failure(exc):
    """(fixed message, quiet) for a failed read. quiet = the user's own file
    (not a PDF / too big): nothing to alert on, not re-raised."""
    if _is_job_timeout(exc):
        return DOC_ERR_TIMEOUT, False
    if isinstance(exc, StorageError):
        msg = str(exc)
        if msg.startswith("R2 object is larger"):
            return DOC_ERR_TOO_BIG, True
        return (DOC_ERR_MISSING if msg.startswith("R2 key not found") else DOC_ERR_STORAGE), False
    if isinstance(exc, ValueError):
        if str(exc) == vendor_documents._ERR_API:
            return (DOC_ERR_CLAUDE_BUSY if vendor_documents.is_transient(exc)
                    else DOC_ERR_CLAUDE_REJECTED), False
        if str(exc) in vendor_documents.USER_MESSAGES:
            return (vendor_documents.USER_MESSAGES[str(exc)],
                    str(exc) in vendor_documents.INPUT_ERRORS)
    return DOC_ERR_INTERNAL, False


@traced_job
def process_vendor_document(doc_id):
    """Read one uploaded vendor PDF: processing -> R2 download -> Claude read
    (vendor_documents.read_vendor_document, one retry for a transient Claude
    failure) -> done with the result. Any failure: status 'failed' with a fixed
    readable message (_doc_failure; never left 'processing' when the write can
    be made). Bad input (not a PDF / too big) whose failure was saved is not
    re-raised; everything else is, after the row is marked, so RQ and Sentry
    see it. RQ's JobTimeoutException is never swallowed, including one that
    lands during a status write (F-73)."""
    doc = get_vendor_document(doc_id)
    if doc is None:
        logger.info("[%s] vendor document no longer exists; nothing to do", doc_id)
        return
    try:
        if update_vendor_document(doc_id, "processing") is None:
            return
        logger.info("[%s] reading vendor document (%s bytes)", doc_id, doc.get("file_size"))
        data = download_bytes(doc["file_key"], vendor_documents.MAX_PDF_BYTES)
        result = _read_vendor_pdf(doc_id, data, customer_display_name(doc.get("customer")))
        saved = update_vendor_document(doc_id, "done", result=result)
    except BaseException as exc:  # noqa: BLE001 — row must never stay 'processing'; re-raised
        message, quiet = _doc_failure(exc)
        # _fail_document re-raises a deadline that interrupts its own write.
        if _fail_document(doc_id, message) and quiet:
            logger.info("[%s] not readable as a vendor PDF: %s", doc_id, message)
            return                                  # the user's file; failure saved
        raise
    if saved is None:
        logger.info("[%s] vendor document was deleted while it was read", doc_id)
        return
    logger.info("[%s] vendor document DONE (%d lines, %d warnings)", doc_id,
                len(result.get("lines") or []),
                sum(1 for c in result.get("checks") or [] if c.get("level") == "warn"))
    # Rebuild this customer's combined vendor list (its own RQ job; never changes
    # this document's status). The customer snapshot is read from the documents.
    queue_vendor_materials(saved.get("customer_id"), ref=doc_id)


def queue_vendor_materials(customer_id, manual=False, customer=None, ref=None):
    """Queue the customer's vendor list rebuild (stage P2). Best effort: a queue
    failure is logged + reported and never changes a document's status. True if queued."""
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


@traced_job
def sync_vendor_materials_job(customer_id, manual=False, customer=None):
    """Rebuild + send one customer's combined vendor material list
    (fieldpulse_sync.sync_vendor_materials; never raises, mode-gated). Documents are
    loaded under the per-customer lease, so the newest finished ones are included."""
    out = sync_vendor_materials(customer_id, customer=customer, documents=None, manual=manual)
    logger.info("[customer %s] vendor material list: %s (%s)", customer_id,
                out.get("status"), "manual" if manual else "automatic")
    if out.get("follow_up"):
        # Another update was asked for while this one ran (F-78): one more run. The
        # request stays flagged in the DB (and a flagged manual request is honoured by
        # whichever run claims it), so a failed enqueue only delays it to the next event.
        logger.info("[customer %s] vendor material list: queueing the follow-up run",
                    customer_id)
        queue_vendor_materials(customer_id)
    return out.get("status")


@traced_job
def match_cart_customer(user_id, cart_id):
    """Stage E3: match one Ferguson cart to a FieldPulse customer by its project name
    (carts_panel.match_cart: one READ-ONLY customer list through customer_source, then
    one conditional write that never overrides a manual link). A search failure leaves
    the cart needs_customer with a readable reason; only DB errors raise."""
    import carts_panel   # here, not at import: the worker boots without the cart chain
    return carts_panel.match_cart(user_id, cart_id)


def queue_cart_send(user_id, cart_id, manual=False):
    """Queue one cart's FieldPulse send (stage E4). Best effort: True if queued."""
    try:
        queue_setup.enqueue_cart_send(user_id, cart_id, manual)
        return True
    except Exception as exc:  # noqa: BLE001 — Redis down etc.
        logger.error("[%s] could not queue the cart send: %s", cart_id, type(exc).__name__)
        report(exc, "Could not queue cart send", job_id=cart_id)
        return False


@traced_job
def send_cart_to_fieldpulse(user_id, cart_id, manual=False):
    """Stage E4: send one linked Ferguson cart as a DRAFT estimate + a material list
    (fieldpulse_sync.sync_cart; never raises, FIELDPULSE_WRITE_MODE-gated). A run that
    found the project's slot busy is picked up here as ONE follow-up run for the newest
    cart that asked (its request stays flagged in the DB until a run claims it)."""
    import carts_panel   # here, not at import: the worker boots without the cart chain
    # Product types for the "<Type> - <Room>" titles: decided ONCE per cart (one Claude call,
    # stored on the cart) BEFORE anything is built, so every send/PUT of this cart uses the
    # same titles. AI trouble -> null types, the send still goes ahead; only a DB error
    # raises here (nothing sent; the same DB error would stop the send anyway).
    carts_panel.ensure_item_types(user_id, cart_id)
    out = sync_cart(user_id, cart_id, manual=manual)
    logger.info("[%s] cart FieldPulse send: %s (%s)", cart_id, out.get("status"),
                "manual" if manual else "automatic")
    follow = out.get("follow_up")
    if follow:
        queue_cart_send(user_id, follow.get("cart_id") or cart_id, bool(follow.get("manual")))
    return out.get("status")


def _read_vendor_pdf(doc_id, data, customer_name):
    """The reader, retried once after a short pause when Claude was only
    unreachable/busy (vendor_documents.is_transient). The reader itself makes a
    single attempt and re-raises RQ's deadline, which passes straight through."""
    try:
        return vendor_documents.read_vendor_document(data, customer_name)
    except ValueError as exc:
        if not vendor_documents.is_transient(exc):
            raise
        logger.warning("[%s] Claude request failed (%s); retrying once in %ds", doc_id,
                       type(exc.__cause__).__name__, _DOC_RETRY_PAUSE_S)
    time.sleep(_DOC_RETRY_PAUSE_S)
    return vendor_documents.read_vendor_document(data, customer_name)


def _fail_document(doc_id, message):
    """Mark a vendor document failed. True if the failed status was saved (or the
    row is gone), False if an ordinary error stopped the write (logged + reported).
    An RQ deadline that interrupts the write is NEVER swallowed (F-73): one more
    best-effort write (the interrupted transaction has been unwound by then, and
    RQ's alarm fires only once), then the deadline is re-raised."""
    logger.error("[%s] vendor document FAILED: %s", doc_id, message)
    try:
        update_vendor_document(doc_id, "failed", error=message)
        return True
    except Exception as exc:  # noqa: BLE001
        if _is_job_timeout(exc):
            try:
                update_vendor_document(doc_id, "failed", error=DOC_ERR_TIMEOUT)
            except Exception as exc2:  # noqa: BLE001
                logger.error("[%s] could not record the failure after the deadline: %s",
                             doc_id, type(exc2).__name__)
                report(exc2, "Could not mark vendor document failed", job_id=doc_id)
            raise
        logger.error("[%s] additionally failed to record failure: %s", doc_id,
                     type(exc).__name__)
        report(exc, "Could not mark vendor document failed", job_id=doc_id)
        return False


def _fail(job_id, message):
    """Mark a job as failed. Never raises — we're already in an error path."""
    logger.error("[%s] FAILED: %s", job_id, message)
    try:
        update_job_status(job_id, "failed", error=message)
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] additionally failed to record failure: %s", job_id, exc)
        # The row may now be stuck in 'processing': worth an alert of its own.
        report(exc, "Could not mark job failed", job_id=job_id)
