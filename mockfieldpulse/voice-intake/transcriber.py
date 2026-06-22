"""Audio transcription via the Deepgram REST API.

Exposes `transcribe_audio(audio_path)` returning {"transcript", "duration_seconds",
"raw"} and `TranscriptionError`. Calls Deepgram directly over HTTP — no SDK
dependency to keep upgrade pain low.
"""

import logging
import mimetypes
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

# --- Logging ----------------------------------------------------------------

logger = logging.getLogger("voice_intake.transcriber")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


class TranscriptionError(Exception):
    """Raised when audio transcription fails."""


# --- Configuration ----------------------------------------------------------

API_KEY = os.environ.get("DEEPGRAM_API_KEY", "").strip()
_URL = "https://api.deepgram.com/v1/listen"
# Defaults tuned for consultation calls: latest model, smart formatting,
# punctuation, plus diarization so we can label speakers in the transcript.
_PARAMS = {
    "model": "nova-3",
    "smart_format": "true",
    "diarize": "true",
    "utterances": "true",
    "punctuate": "true",
}
_TIMEOUT = 300  # generous — audio uploads can take a while


# --- Helpers ----------------------------------------------------------------

def _diarized_text(response_json):
    """Build a 'Speaker N: ...' transcript from utterances when present.

    The consultation prompt expects either REP/PROSPECT labels or speaker
    numbers — it identifies the rep by context, not by label. Fall back to
    the flat transcript if utterances aren't in the response.
    """
    results = response_json.get("results") or {}
    utterances = results.get("utterances") or []
    if utterances:
        lines = []
        for u in utterances:
            speaker = u.get("speaker")
            text = (u.get("transcript") or "").strip()
            if not text:
                continue
            lines.append(f"Speaker {speaker}: {text}" if speaker is not None else text)
        if lines:
            return "\n".join(lines)
    try:
        return ((results.get("channels") or [])[0].get("alternatives") or [])[0].get("transcript", "")
    except (IndexError, AttributeError, TypeError):
        return ""


# --- Public API -------------------------------------------------------------

def transcribe_audio(audio_path):
    """Transcribe an audio file via Deepgram.

    Returns {"transcript": str, "duration_seconds": float|None, "raw": dict}.
    Raises TranscriptionError on any failure (missing key, missing file,
    network error, non-200 response, or non-JSON body).
    """
    if not API_KEY:
        raise TranscriptionError("DEEPGRAM_API_KEY is not set in .env")
    if not os.path.exists(audio_path):
        raise TranscriptionError(f"Audio file not found: {audio_path}")

    mime, _ = mimetypes.guess_type(audio_path)
    if not mime:
        mime = "audio/wav"
    logger.info("Transcribing %s (%s)", audio_path, mime)

    try:
        with open(audio_path, "rb") as f:
            resp = requests.post(
                _URL,
                params=_PARAMS,
                headers={
                    "Authorization": f"Token {API_KEY}",
                    "Content-Type": mime,
                },
                data=f,
                timeout=_TIMEOUT,
            )
    except requests.RequestException as exc:
        raise TranscriptionError(f"Deepgram request failed: {exc}")

    if resp.status_code != 200:
        raise TranscriptionError(
            f"Deepgram returned {resp.status_code}: {resp.text[:500]}"
        )

    try:
        body = resp.json()
    except ValueError:
        raise TranscriptionError(f"Deepgram returned non-JSON body: {resp.text[:500]}")

    duration = (body.get("metadata") or {}).get("duration")
    transcript = _diarized_text(body)
    logger.info(
        "Transcription done: %.1fs audio -> %d chars",
        float(duration or 0.0), len(transcript),
    )

    return {
        "transcript": transcript,
        "duration_seconds": duration,
        "raw": body,
    }
