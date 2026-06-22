"""Writes extracted intake data to the mock FieldPulse instance.

Exposes `write_intake_to_fieldpulse(extraction)`, which takes the dict from
extractor.extract_intake() and creates a customer, a job, a single note, and
one subtask per next_action. Raises WriterError on any fatal API failure.
"""

import logging
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent

# --- Logging ----------------------------------------------------------------

logger = logging.getLogger("voice_intake.fieldpulse_writer")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


class WriterError(Exception):
    """Raised when a fatal FieldPulse write fails (customer or job creation)."""


# --- Configuration ----------------------------------------------------------

load_dotenv(_HERE / ".env")

BASE_URL = os.environ.get("FIELDPULSE_BASE_URL", "http://localhost:5000").rstrip("/")
API_KEY = os.environ.get("FIELDPULSE_API_KEY", "dev-test-key-12345")
_HEADERS = {"x-api-key": API_KEY}
_SOURCE = "voice_intake_ai"
_TIMEOUT = 10

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
)
_WEEKDAY_RE = re.compile(r"\b(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE)
_SOON_HINTS = ("this week", "within", "few days", "couple days", "couple of days", "next few")


# --- HTTP helper ------------------------------------------------------------

def _post(path, body):
    """POST to the mock FieldPulse API and return the `data` object.

    Raises WriterError on connection failure, non-2xx status, or a response
    that doesn't contain a `data.id`.
    """
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.post(url, json=body, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise WriterError(f"POST {path} failed (is the server running at {BASE_URL}?): {exc}")

    if resp.status_code not in (200, 201):
        raise WriterError(f"POST {path} returned {resp.status_code}: {resp.text}")

    try:
        payload = resp.json()
    except ValueError:
        raise WriterError(f"POST {path} returned non-JSON body: {resp.text}")

    data = payload.get("data")
    if not isinstance(data, dict) or "id" not in data:
        raise WriterError(f"POST {path} response missing data.id: {resp.text}")
    return data


# --- Mapping helpers --------------------------------------------------------

def _split_name(name):
    """Split a full name into (first, last) on the last space.

    'Karen Whitfield' -> ('Karen', 'Whitfield')
    'Cher'            -> ('Cher', '')
    'Mary Jane Watson'-> ('Mary Jane', 'Watson')
    """
    name = (name or "").strip()
    if " " in name:
        first, last = name.rsplit(" ", 1)
        return first.strip(), last.strip()
    return name, ""


def _build_custom_fields(extraction):
    """Collect custom fields from the extraction, skipping null values."""
    home = extraction.get("home") or {}
    project = extraction.get("project") or {}
    lead_source = extraction.get("lead_source") or {}

    candidates = {
        "year_built": home.get("year_built"),
        "lead_source": lead_source.get("channel"),
        "lead_source_details": lead_source.get("details"),
        "time_frame": project.get("time_frame"),
        "years_at_residence": home.get("years_at_residence"),
    }
    return {k: v for k, v in candidates.items() if v is not None}


def _parse_due_date(hint):
    """Resolve a due_date_hint into (due_date_or_None, weekday_flag_or_None).

    - exact YYYY-MM-DD            -> used directly
    - 'today' / 'tomorrow'        -> resolved relative to today
    - mentions a weekday          -> None, plus a flag (so the rep can confirm)
    - 'this week' / 'within ...'  -> today + 3 days
    - anything else / missing     -> None
    """
    if not hint:
        return None, None
    raw = hint.strip()
    low = raw.lower()
    today = date.today()

    if _DATE_RE.match(raw):
        return raw, None
    if low == "today":
        return today.isoformat(), None
    if low == "tomorrow":
        return (today + timedelta(days=1)).isoformat(), None
    if _WEEKDAY_RE.search(low):
        return None, f"subtask due date references a weekday and needs manual scheduling: \"{raw}\""
    if any(token in low for token in _SOON_HINTS):
        return (today + timedelta(days=3)).isoformat(), None
    return None, None


def _build_note_body(extraction, extra_flags, original_transcript=None):
    """Assemble the single intake note: summary, FLAGS, RAW QUOTES.

    If original_transcript is provided, append it in a labeled section so the
    full call can always be traced back from the note.
    """
    summary = extraction.get("notes_for_fieldpulse") or ""
    flags = list(extraction.get("flags") or [])
    flags.extend(extra_flags)
    quotes = extraction.get("raw_quotes") or []

    lines = [summary]
    if flags:
        lines.append("")
        lines.append("FLAGS:")
        lines.extend(f"- {flag}" for flag in flags)
    if quotes:
        lines.append("")
        lines.append("RAW QUOTES:")
        lines.extend(f"> {q}" for q in quotes)
    if original_transcript:
        lines.append("")
        lines.append("--- ORIGINAL TRANSCRIPT ---")
        lines.append(original_transcript)
    return "\n".join(lines)


# --- Public API -------------------------------------------------------------

def write_intake_to_fieldpulse(extraction: dict, original_transcript: str = None) -> dict:
    """Write a complete intake (customer + job + note + subtasks) to FieldPulse.

    If original_transcript is provided, it's appended to the note in a labeled
    section so the full call can be traced back. Returns a dict with
    customer_id, job_id, note_ids, subtask_ids, and fieldpulse_url. Raises
    WriterError if customer or job creation fails. Note/subtask failures are
    logged and surfaced via a "partial_failure" key, but do not abort the write.
    """
    prospect = extraction.get("prospect") or {}
    project = extraction.get("project") or {}
    consultation = extraction.get("consultation") or {}

    first_name, last_name = _split_name(prospect.get("name"))
    display_name = f"{last_name}, {first_name}" if last_name else (first_name or "New Customer")

    # --- Customer (fatal on failure) ---
    customer_body = {
        "display_name": display_name,
        "first_name": first_name,
        "last_name": last_name,
        "email": prospect.get("email"),
        "phone": prospect.get("phone"),
        "address": {
            "street": prospect.get("street_address"),
            "city": prospect.get("city_or_part_of_town"),
            "state": None,
            "postal_code": None,
        },
    }
    try:
        customer = _post("/customers", customer_body)
    except WriterError as exc:
        logger.error("Customer creation failed — aborting, no job created: %s", exc)
        raise
    customer_id = customer["id"]
    logger.info("Customer created: %s (%s)", customer_id, display_name)

    # --- Job (fatal on failure; orphaned customer is acceptable for v1) ---
    job_type = (project.get("job_type") or "unknown").strip() or "unknown"
    if job_type == "unknown":
        title = "New Intake"
    else:
        name_prefix = last_name or first_name or "Lead"
        title = f"{name_prefix} — {job_type.title()} Remodel"
    status = "design" if consultation.get("scheduled") is True else "new_lead"

    job_body = {
        "customer_id": customer_id,
        "title": title,
        "status": status,
        "custom_fields": _build_custom_fields(extraction),
    }
    try:
        job = _post("/jobs", job_body)
    except WriterError as exc:
        logger.error(
            "Job creation FAILED after customer %s was created. "
            "Orphaned customer left in FieldPulse (acceptable for v1 — delete "
            "manually if needed). Error: %s",
            customer_id,
            exc,
        )
        raise
    job_id = job["id"]
    logger.info("Job created: %s (title=%r, status=%s)", job_id, title, status)

    result = {
        "customer_id": customer_id,
        "job_id": job_id,
        "note_ids": [],
        "subtask_ids": [],
        "fieldpulse_url": f"{BASE_URL}/admin/jobs/{job_id}",
    }
    partial_failures = []

    # --- Parse subtask due dates first, so weekday flags land in the note ---
    next_actions = extraction.get("next_actions") or []
    parsed_actions = []
    weekday_flags = []
    for action in next_actions:
        due_date, weekday_flag = _parse_due_date(action.get("due_date_hint"))
        if weekday_flag:
            weekday_flags.append(weekday_flag)
        parsed_actions.append((action, due_date))

    # --- Note (non-fatal on failure) ---
    note_body = _build_note_body(extraction, weekday_flags, original_transcript)
    try:
        note = _post(f"/jobs/{job_id}/notes", {"body": note_body, "source": _SOURCE})
        result["note_ids"].append(note["id"])
        logger.info("Note added: %s", note["id"])
    except WriterError as exc:
        logger.error("Note creation failed (continuing): %s", exc)
        partial_failures.append(f"note failed: {exc}")

    # --- Subtasks (non-fatal on failure) ---
    for i, (action, due_date) in enumerate(parsed_actions, start=1):
        subtask_body = {
            "title": action.get("title", ""),
            "due_date": due_date,
            "priority": action.get("priority", "med"),
            "source": _SOURCE,
        }
        try:
            subtask = _post(f"/jobs/{job_id}/subtasks", subtask_body)
            result["subtask_ids"].append(subtask["id"])
            logger.info(
                "Subtask %d added: %s (%r, due=%s)",
                i, subtask["id"], action.get("title"), due_date,
            )
        except WriterError as exc:
            logger.error("Subtask %d creation failed (continuing): %s", i, exc)
            partial_failures.append(f"subtask {i} failed: {exc}")

    if partial_failures:
        result["partial_failure"] = "; ".join(partial_failures)

    logger.info(
        "Intake write complete: customer=%s job=%s notes=%d subtasks=%d",
        customer_id, job_id, len(result["note_ids"]), len(result["subtask_ids"]),
    )
    return result
