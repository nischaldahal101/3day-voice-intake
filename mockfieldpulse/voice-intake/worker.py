"""RQ worker entry-point for consultation jobs.

For each job on the `consultations` queue:
  1. Look it up in Postgres (get_job). Missing -> JobError, RQ records failure.
  2. Mark status='processing'.
  3. Get the transcript:
       - recorded/uploaded audio (audio_key): download from R2, transcribe;
       - pasted transcript (jobs.transcript): use it as-is, no transcription.
  4. Extract (Claude, null-flagged), told the consultation's date so a
     relative return appointment ("next Thursday") can be resolved.
  5. SAVE FIRST: transcript + extraction go on the row (still 'processing')
     before anything touches FieldPulse (docs/FIELDPULSE_API.md §11).
  6. fieldpulse_sync.sync_consultation: real FieldPulse write, gated by
     FIELDPULSE_WRITE_MODE (default dry_run = nothing sent). It never raises
     and never fails the job; its summary is stored as result.fieldpulse_sync.
  7. status='done'. Any failure in steps 3-5: status='failed' with a clear
     error, then re-raise so RQ also records it. Job never stays 'processing'.
  8. Always clean up the temp audio file.

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

from extractor import ExtractionError, extract_consultation
from fieldpulse_sync import consultation_reference, sync_consultation
from jobs import JobError, get_job, update_job_status
from pipeline import transcribe
from storage import StorageError, download_audio
from transcriber import TranscriptionError

logger = logging.getLogger("voice_intake.worker")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


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
        sync = {"status": "failed", "message": "FieldPulse sync hit an internal error. "
                "The consultation is saved.", "error": f"{type(exc).__name__}", "steps": []}
    summary["fieldpulse_sync"] = sync

    try:
        update_job_status(job_id, "done", result=summary)
    except Exception as exc:  # noqa: BLE001
        _fail(job_id, f"Could not save the finished job: {type(exc).__name__}: {exc}")
        raise
    logger.info("[%s] DONE (fieldpulse_sync=%s)", job_id, sync.get("status"))


def _fail(job_id, message):
    """Mark a job as failed. Never raises — we're already in an error path."""
    logger.error("[%s] FAILED: %s", job_id, message)
    try:
        update_job_status(job_id, "failed", error=message)
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] additionally failed to record failure: %s", job_id, exc)
