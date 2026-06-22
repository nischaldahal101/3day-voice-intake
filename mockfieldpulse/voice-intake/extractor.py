"""Claude integration for extracting structured intake data from call transcripts.

Exposes a single entry point, `extract_intake(transcript)`, which sends the
transcript to Claude using the locked-in system prompt and returns the parsed
JSON extraction as a dict. Raises `ExtractionError` on any failure.
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

# --- Configuration ----------------------------------------------------------

# Current Sonnet model. The request asked for "claude-sonnet-4-5 (or whatever
# the current Sonnet version is)"; the installed SDK's current Sonnet is 4.6.
MODEL = "claude-sonnet-4-6"
TEMPERATURE = 0.0
MAX_TOKENS = 2000

_HERE = Path(__file__).resolve().parent
_PROMPT_PATH = _HERE / "extraction_prompt.txt"

# --- Logging ----------------------------------------------------------------

logger = logging.getLogger("voice_intake.extractor")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


class ExtractionError(Exception):
    """Raised when extraction fails (API error or unparseable response).

    The raw model response (if any) is preserved on `.raw_response` for
    debugging malformed-JSON cases.
    """

    def __init__(self, message, raw_response=None):
        super().__init__(message)
        self.raw_response = raw_response


# --- Module load: API key + cached system prompt ----------------------------

load_dotenv(_HERE / ".env")

_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not _API_KEY:
    raise ExtractionError(
        "ANTHROPIC_API_KEY not found. Set it in voice-intake/.env"
    )

try:
    # Read and cache the system prompts once, at import time. The intake prompt
    # remains exposed as SYSTEM_PROMPT for backward compatibility; the
    # consultation prompt is loaded alongside it.
    SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")
except OSError as exc:
    raise ExtractionError(f"Could not read system prompt at {_PROMPT_PATH}: {exc}")

_CONSULT_PROMPT_PATH = _HERE / "consultation_prompt.txt"
try:
    SYSTEM_PROMPT_CONSULT = _CONSULT_PROMPT_PATH.read_text(encoding="utf-8")
except OSError as exc:
    raise ExtractionError(
        f"Could not read consultation prompt at {_CONSULT_PROMPT_PATH}: {exc}"
    )

_client = anthropic.Anthropic(api_key=_API_KEY)


# --- Helpers ----------------------------------------------------------------

def _inject_date(prompt):
    """Replace {TODAY} and {DAY_OF_WEEK} placeholders with today's values.

    If the placeholders aren't present, str.replace is a no-op — graceful
    skip, exactly as intended.
    """
    now = datetime.now()
    return (
        prompt
        .replace("{TODAY}", now.strftime("%Y-%m-%d"))
        .replace("{DAY_OF_WEEK}", now.strftime("%A"))
    )


def _response_text(response):
    """Concatenate the text content blocks of a Claude response."""
    return "".join(
        block.text for block in response.content if block.type == "text"
    )


def _strip_code_fences(text):
    """Remove a wrapping ```json ... ``` (or ``` ... ```) fence if present."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


# --- Internal: shared Claude call + JSON parse ------------------------------

def _extract(system_prompt_template: str, transcript: str, label: str) -> dict:
    """Send the transcript to Claude with the given system prompt and parse JSON.

    Used by both extract_intake() and extract_consultation(). The behavior
    matches the original extract_intake exactly so existing callers are
    unchanged.
    """
    logger.info(
        "%s extraction started (transcript length: %d chars)", label, len(transcript)
    )

    system_prompt = _inject_date(system_prompt_template)

    try:
        response = _client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    # Cache the (large, stable) system prompt across calls.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": transcript}],
        )
    except anthropic.APIError as exc:
        logger.error("%s extraction failed: API error: %s", label, exc)
        raise ExtractionError(f"Claude API error: {exc}")

    raw_text = _response_text(response)

    try:
        result = json.loads(_strip_code_fences(raw_text))
    except json.JSONDecodeError as exc:
        logger.error(
            "%s extraction failed: could not parse JSON response: %s", label, exc
        )
        raise ExtractionError(
            f"Claude returned malformed JSON: {exc}", raw_response=raw_text
        )

    if not isinstance(result, dict):
        logger.error("%s extraction failed: parsed response is not a JSON object", label)
        raise ExtractionError(
            "Parsed response is not a JSON object", raw_response=raw_text
        )

    return result


# --- Public API -------------------------------------------------------------

def extract_intake(transcript: str) -> dict:
    """Extract structured intake data from a call transcript.

    Takes a call transcript as a string. Returns Claude's structured
    extraction as a Python dict. Raises ExtractionError on failure.
    """
    result = _extract(SYSTEM_PROMPT, transcript, "Intake")

    logger.info(
        "Extraction succeeded (match_confidence: %s)",
        result.get("match_confidence"),
    )
    return result


def extract_consultation(transcript: str) -> dict:
    """Extract structured consultation data from a transcript.

    Mirrors extract_intake() but uses the consultation prompt and returns the
    consultation schema (project_type, scope, readiness, sales, ...).
    """
    result = _extract(SYSTEM_PROMPT_CONSULT, transcript, "Consultation")

    logger.info(
        "Consultation extraction succeeded (project_type: %s, match_confidence: %s)",
        result.get("project_type"),
        result.get("match_confidence"),
    )
    return result
