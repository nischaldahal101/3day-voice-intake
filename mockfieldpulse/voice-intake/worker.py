"""RQ worker entry-point for consultation jobs.

For each job on the `consultations` queue:
  1. Look it up in Postgres (get_job). Missing -> JobError, RQ records failure.
  2. Mark status='processing'.
  3. Download the audio from R2 to a temp file.
  4. Run pipeline.run_from_audio: transcribe -> extract -> write.
  5. On success: status='done', result=<summary JSONB>.
  6. On any failure: log, mark status='failed' with a clear error string,
     then re-raise so RQ also records the failure. Job never stays in
     'processing'.
  7. Always clean up the temp audio file (finally block).

Start the worker with:
    cd voice-intake/
    OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES venv/bin/rq worker consultations
(the env-var prefix is macOS-only; on Linux/Render the bare command works.)
"""

import logging
import os
import sys
import tempfile

from consultation_writer import WriterError as ConsultWriterError
from extractor import ExtractionError
from jobs import JobError, get_job, update_job_status
from pipeline import run_from_audio
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
    customer_id = job.get("customer_id")
    source = job.get("source") or "async-worker"
    if not audio_key:
        _fail(job_id, "job has no audio_key")
        raise JobError(f"Job {job_id} has no audio_key")

    logger.info(
        "[%s] audio_key=%s customer_id=%s source=%s",
        job_id, audio_key, customer_id or "—", source,
    )
    update_job_status(job_id, "processing")

    # Preserve the R2 key's extension so mimetypes.guess_type() in the
    # transcriber sends Deepgram the right Content-Type.
    ext = os.path.splitext(audio_key)[1] or ".bin"
    fd, tmp_path = tempfile.mkstemp(prefix="consult_", suffix=ext)
    os.close(fd)

    try:
        logger.info("[%s] downloading r2 -> %s", job_id, tmp_path)
        download_audio(audio_key, tmp_path)

        logger.info("[%s] running pipeline (transcribe + extract + write)", job_id)
        result = run_from_audio(
            tmp_path, customer_id=customer_id, source=source,
            customer=job.get("customer"),
        )

        # Store enough for the app's confirmation: extraction, the fieldpulse
        # write result (customer/job ids, note/subtask ids, match_path,
        # flags, fieldpulse_url), how long transcription took, and the full
        # transcript itself so the Confirmation screen (and reopening a
        # completed job from the Recent list) can show it without any
        # secondary fetch. Deepgram transcripts even for 60-minute consults
        # sit under ~60KB — trivial for JSONB.
        summary = {
            "extraction": result.get("extraction"),
            "fieldpulse": result.get("fieldpulse"),
            "transcription_seconds": result.get("transcription_seconds"),
            "transcript_chars": len(result.get("transcript") or ""),
            "transcript": result.get("transcript"),
        }
        update_job_status(job_id, "done", result=summary)
        fp = summary["fieldpulse"] or {}
        logger.info(
            "[%s] DONE (match_path=%s customer=%s job=%s fieldpulse_url=%s)",
            job_id,
            fp.get("match_path"),
            fp.get("customer_id"),
            fp.get("job_id"),
            fp.get("fieldpulse_url"),
        )

    except StorageError as exc:
        _fail(job_id, f"R2 download failed: {exc}")
        raise
    except TranscriptionError as exc:
        _fail(job_id, f"Transcription failed: {exc}")
        raise
    except ExtractionError as exc:
        _fail(job_id, f"Extraction failed: {exc}")
        raise
    except ConsultWriterError as exc:
        # pipeline.run_from_transcript attaches the extraction to the
        # WriterError, but we don't need it for the failure record — just
        # a clear message so a human can debug.
        _fail(job_id, f"FieldPulse write failed: {exc}")
        raise
    except Exception as exc:  # noqa: BLE001 — job must never stay 'processing'
        _fail(job_id, f"Unexpected {type(exc).__name__}: {exc}")
        raise
    finally:
        try:
            os.unlink(tmp_path)
            logger.info("[%s] cleaned up %s", job_id, tmp_path)
        except OSError:
            pass


def _fail(job_id, message):
    """Mark a job as failed. Never raises — we're already in an error path."""
    logger.error("[%s] FAILED: %s", job_id, message)
    try:
        update_job_status(job_id, "failed", error=message)
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] additionally failed to record failure: %s", job_id, exc)
