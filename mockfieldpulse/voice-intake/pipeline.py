"""Shared transcribe -> extract -> write consultation pipeline.

Called by both:
  - orchestrator.py's /process-consultation route (synchronous)
  - worker.py's process_consultation_job (async, via RQ)

Each stage raises its own module's exception (TranscriptionError,
ExtractionError, WriterError), so callers can map failures to whatever
they care about (HTTP status codes on the endpoint, `status='failed'`
on the async job).

On WriterError specifically the extraction is attached to the exception
as `.extraction`, so the /process-consultation route can still expose
what Claude figured out even when the FieldPulse write fails.
"""

import logging
import sys

from consultation_writer import WriterError, write_consultation_to_fieldpulse
from extractor import ExtractionError, extract_consultation
from transcriber import TranscriptionError, transcribe_audio

logger = logging.getLogger("voice_intake.pipeline")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def run_from_transcript(transcript, customer_id=None, source=None):
    """Extract structured consultation data and write it to FieldPulse.

    Returns {"transcript", "extraction", "fieldpulse"}.
    Raises ExtractionError (from extract_consultation) or WriterError (from
    write_consultation_to_fieldpulse). On WriterError, the completed
    extraction is attached to the exception as `.extraction` so callers
    who want to surface Claude's output on failure can.
    """
    logger.info(
        "Extract stage (transcript=%d chars, customer_id=%s, source=%s)",
        len(transcript), customer_id or "—", source or "—",
    )
    extraction = extract_consultation(transcript)
    logger.info(
        "Extract done (project_type=%s)", extraction.get("project_type")
    )

    logger.info("Write stage")
    try:
        fieldpulse = write_consultation_to_fieldpulse(
            extraction,
            customer_id=customer_id,
            original_transcript=transcript,
        )
    except WriterError as exc:
        # Preserve Claude's output for callers that want to display it on failure.
        exc.extraction = extraction
        raise
    logger.info(
        "Write done (match_path=%s customer=%s job=%s)",
        fieldpulse.get("match_path"),
        fieldpulse.get("customer_id"),
        fieldpulse.get("job_id"),
    )

    return {
        "transcript": transcript,
        "extraction": extraction,
        "fieldpulse": fieldpulse,
    }


def run_from_audio(audio_path, customer_id=None, source=None):
    """Transcribe audio, then extract + write.

    Returns {"transcript", "transcription_seconds", "extraction", "fieldpulse"}.
    Raises TranscriptionError (transcribe stage), ExtractionError, or WriterError.
    """
    logger.info("Transcribe stage (audio=%s)", audio_path)
    tr = transcribe_audio(audio_path)
    transcript = (tr.get("transcript") or "").strip()
    duration = tr.get("duration_seconds")
    logger.info(
        "Transcribe done (%.1fs audio -> %d chars)",
        float(duration or 0.0), len(transcript),
    )
    if not transcript:
        raise TranscriptionError("Transcription returned an empty transcript")

    result = run_from_transcript(
        transcript, customer_id=customer_id, source=source
    )
    result["transcription_seconds"] = duration
    return result
