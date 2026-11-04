"""Consultation pipeline stages.

  - transcribe(audio_path)   used by worker.py (the live async flow). The
                             worker then extracts and calls fieldpulse_sync.
  - run_from_transcript(...) DEPRECATED: only the old synchronous
                             /process-consultation and /test-consultation-sample
                             routes use it. It still writes to the MOCK via
                             consultation_writer. The app no longer calls those
                             routes (stage 6 removes them with the mock).

Each stage raises its own module's exception (TranscriptionError,
ExtractionError, WriterError). On WriterError the extraction is attached as
`.extraction` so the deprecated route can still show it.
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


def run_from_transcript(transcript, customer_id=None, source=None, customer=None):
    """DEPRECATED sync route only. Extract, then write to the MOCK FieldPulse.

    `customer` is the selected customer's identity snapshot (or None); the
    writer uses it only to create/link a mock customer for an external id.

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
            customer=customer,
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


def transcribe(audio_path):
    """Transcribe one audio file. Returns (transcript, duration_seconds).
    Raises TranscriptionError, including for an empty transcript."""
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
    return transcript, duration
