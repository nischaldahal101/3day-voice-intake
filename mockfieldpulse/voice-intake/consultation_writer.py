"""Writes consultation data to FieldPulse by UPDATING an existing customer's job.

Companion to fieldpulse_writer.py. Where the intake writer creates a fresh
customer + job, this one resolves the right existing customer (by provided id,
phone, email, or name), finds their job, and PATCHes consultation fields onto
it — so intake data isn't overwritten. Only creates new records when no match
exists.
"""

import logging
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent

# --- Logging ----------------------------------------------------------------

logger = logging.getLogger("voice_intake.consultation_writer")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


class WriterError(Exception):
    """Raised when a fatal FieldPulse write fails (customer or job determination)."""


# --- Configuration ----------------------------------------------------------

load_dotenv(_HERE / ".env")

BASE_URL = os.environ.get("FIELDPULSE_BASE_URL", "http://localhost:5000").rstrip("/")
API_KEY = os.environ.get("FIELDPULSE_API_KEY", "dev-test-key-12345")
_HEADERS = {"x-api-key": API_KEY}
_SOURCE = "consultation_ai"
# 60s rather than 10s so a cold-starting hosted mock (Render free-tier can
# take 30–60s to wake) has time to respond on the first call after idle.
_TIMEOUT = 60
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --- HTTP helpers -----------------------------------------------------------

def _request(method, path, body=None, params=None, allow_404=False):
    """Shared transport. Returns parsed JSON payload, or None if allow_404 and
    the server returns 404. Raises WriterError on any other failure."""
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.request(
            method, url, json=body, params=params, headers=_HEADERS, timeout=_TIMEOUT
        )
    except requests.RequestException as exc:
        raise WriterError(
            f"{method} {path} failed (is the server running at {BASE_URL}?): {exc}"
        )

    if resp.status_code == 404 and allow_404:
        return None
    if resp.status_code not in (200, 201):
        raise WriterError(f"{method} {path} returned {resp.status_code}: {resp.text}")
    try:
        return resp.json()
    except ValueError:
        raise WriterError(f"{method} {path} returned non-JSON body: {resp.text}")


def _get(path, params=None, allow_404=False):
    """GET with a single retry on timeout — gives a cold-starting hosted
    mock a second chance after the first call woke it up. Only GETs are
    retried; POST/PATCH stay single-attempt to avoid duplicate writes."""
    try:
        return _request("GET", path, params=params, allow_404=allow_404)
    except WriterError as exc:
        msg = str(exc).lower()
        if "timed out" not in msg and "timeout" not in msg:
            raise
        logger.warning("GET %s timed out — retrying once after brief sleep", path)
        time.sleep(1)
        return _request("GET", path, params=params, allow_404=allow_404)


def _post(path, body):
    payload = _request("POST", path, body=body)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or "id" not in data:
        raise WriterError(f"POST {path} response missing data.id: {payload}")
    return data


def _patch(path, body):
    payload = _request("PATCH", path, body=body)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or "id" not in data:
        raise WriterError(f"PATCH {path} response missing data.id: {payload}")
    return data


# --- Customer matching ------------------------------------------------------

def _list_all_customers():
    """Page through /customers and return every record."""
    customers = []
    page = 1
    while True:
        payload = _get("/customers", params={"page": page, "per_page": 200})
        batch = (payload or {}).get("data") or []
        customers.extend(batch)
        meta = (payload or {}).get("meta") or {}
        total_pages = meta.get("total_pages") or 1
        if page >= total_pages or not batch:
            break
        page += 1
    return customers


def _norm(value):
    return (value or "").strip().lower()


def _match_customer(customers, client):
    """Apply phone -> email -> name in priority order.

    Returns (criterion, hits). Stops at the first criterion that yields ≥1
    match, so callers can distinguish single-match vs ambiguous-match cases.
    Returns (None, []) if nothing matched on any criterion.
    """
    phone = (client.get("phone") or "").strip()
    email = _norm(client.get("email"))
    name = _norm(client.get("name"))

    if phone:
        hits = [c for c in customers if (c.get("phone") or "").strip() == phone]
        if hits:
            return "phone", hits
    if email:
        hits = [c for c in customers if _norm(c.get("email")) == email]
        if hits:
            return "email", hits
    if name:
        hits = []
        for c in customers:
            display = _norm(c.get("display_name"))
            first_last = _norm(
                f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
            )
            if display == name or first_last == name:
                hits.append(c)
        if hits:
            return "name", hits
    return None, []


def _create_customer_from_client(client):
    """Mirror the intake writer's name-split + display_name conventions."""
    name = (client.get("name") or "").strip()
    if " " in name:
        first, last = name.rsplit(" ", 1)
    else:
        first, last = name, ""
    display = f"{last}, {first}" if last else (first or "New Customer")
    address = client.get("address") or {}
    body = {
        "display_name": display,
        "first_name": first,
        "last_name": last,
        "email": client.get("email"),
        "phone": client.get("phone"),
        "address": {
            "street": address.get("street") or client.get("street_address"),
            "city": address.get("city") or client.get("city") or client.get("city_or_part_of_town"),
            "state": address.get("state"),
            "postal_code": address.get("postal_code"),
        },
    }
    return _post("/customers", body)


# --- Job target -------------------------------------------------------------

def _create_job_for_consultation(customer_id, client, extraction):
    """Used only when the matched/created customer has no existing job."""
    name = (client.get("name") or "").strip()
    last = name.rsplit(" ", 1)[-1] if " " in name else (name or "Lead")
    project_type = extraction.get("project_type")
    if isinstance(project_type, str) and project_type.strip() and project_type != "unknown":
        title = f"{last} — {project_type.title()} Consultation"
    else:
        title = f"{last} — Consultation"
    return _post("/jobs", {
        "customer_id": customer_id,
        "title": title,
        "status": "consultation_complete",
    })


# --- Mapping ----------------------------------------------------------------

def _build_consultation_custom_fields(extraction):
    """Pull consultation values into a flat custom_fields dict with the
    consult_ prefix. Drops None and 'unknown' so they don't bury the real data.

    Reads from the actual consultation extraction schema:
      - project_type (top-level)
      - scope.{project_vision, structural_changes/_details, plumbing_*, ...}
      - scope.appliances.{refrigerator, range, dishwasher, microwave, hood, other}
      - readiness.{time_frame, thinking_duration, work_done_so_far}
      - sales.{budget_raw, budget_low, budget_high, likelihood_percent,
               close_score_1_10, waiting_on_financing, concerns,
               process_presentation_shown}
    """
    scope = extraction.get("scope") or {}
    readiness = extraction.get("readiness") or {}
    sales = extraction.get("sales") or {}
    appliances = scope.get("appliances") or {}

    fields = {
        "consult_project_type": extraction.get("project_type"),
        "consult_project_vision": scope.get("project_vision"),
        "consult_structural": scope.get("structural_changes"),
        "consult_structural_details": scope.get("structural_details"),
        "consult_plumbing": scope.get("plumbing_changes"),
        "consult_plumbing_details": scope.get("plumbing_details"),
        "consult_electrical": scope.get("electrical_changes"),
        "consult_electrical_details": scope.get("electrical_details"),
        "consult_lighting": scope.get("lighting_changes"),
        "consult_lighting_details": scope.get("lighting_details"),
        "consult_style": scope.get("style"),
        "consult_appliance_refrigerator": appliances.get("refrigerator"),
        "consult_appliance_range": appliances.get("range"),
        "consult_appliance_dishwasher": appliances.get("dishwasher"),
        "consult_appliance_microwave": appliances.get("microwave"),
        "consult_appliance_hood": appliances.get("hood"),
        "consult_appliance_other": appliances.get("other"),
        "consult_time_frame": readiness.get("time_frame"),
        "consult_thinking_duration": readiness.get("thinking_duration"),
        "consult_work_done": readiness.get("work_done_so_far"),
        "consult_budget": sales.get("budget_raw"),
        "consult_budget_low": sales.get("budget_low"),
        "consult_budget_high": sales.get("budget_high"),
        "consult_likelihood_percent": sales.get("likelihood_percent"),
        "consult_close_score": sales.get("close_score_1_10"),
        "consult_waiting_financing": sales.get("waiting_on_financing"),
        "consult_concerns": sales.get("concerns"),
        "consult_presentation_shown": sales.get("process_presentation_shown"),
    }
    return {k: v for k, v in fields.items() if v is not None and v != "unknown" and v != ""}


def _build_consult_note_body(extraction, original_transcript):
    """Same shape as the intake note: summary -> FLAGS -> RAW QUOTES -> optional transcript."""
    summary = extraction.get("notes_summary") or ""
    flags = list(extraction.get("flags") or [])
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


def _build_return_appointment_subtask(return_appointment):
    """Return a subtask body, or None if no return appointment was scheduled.

    The consultation schema gives return_appointment.{scheduled, details} —
    details is a free-text string like "Thursday at 6 PM" or "2026-07-08 7:00 PM".
    We try to extract a YYYY-MM-DD for due_date; otherwise leave it null and
    surface the details in the title for the rep to act on.
    """
    if not return_appointment or not return_appointment.get("scheduled"):
        return None
    details = (return_appointment.get("details") or "").strip()
    title = "Return presentation appointment"
    if details:
        title = f"{title} ({details})"
    due_date = None
    if details:
        m = re.search(r"\b\d{4}-\d{2}-\d{2}\b", details)
        if m:
            due_date = m.group(0)
    return {
        "title": title,
        "due_date": due_date,
        "priority": "high",
        "source": _SOURCE,
    }


# --- Public API -------------------------------------------------------------

def write_consultation_to_fieldpulse(
    extraction: dict,
    customer_id: str = None,
    original_transcript: str = None,
) -> dict:
    """Find or create the right customer + job, then write consultation data to it.

    See module docstring. Raises WriterError only on fatal failures (customer
    resolution or job PATCH). Note/subtask failures are non-fatal: they go into
    partial_failure and the function returns normally.
    """
    client = extraction.get("client") or {}
    flags = []
    ambiguous_candidates = None
    match_path = None
    customer = None

    # Step 1 — explicit customer_id (falls through if the id isn't found).
    if customer_id:
        payload = _get(f"/customers/{customer_id}", allow_404=True)
        if payload and isinstance(payload.get("data"), dict):
            customer = payload["data"]
            match_path = "matched_by_id"
            flags.append(f"matched existing customer by provided id ({customer_id})")
            logger.info("Customer matched by provided id: %s", customer_id)
        else:
            flags.append(f"provided customer_id {customer_id} not found — falling back to search")
            logger.warning("Provided customer_id %s not found; falling back to search", customer_id)

    # Step 2 — phone / email / name search.
    if customer is None:
        all_customers = _list_all_customers()
        criterion, hits = _match_customer(all_customers, client)
        if criterion is None:
            try:
                customer = _create_customer_from_client(client)
            except WriterError as exc:
                logger.error("Customer creation failed: %s", exc)
                raise
            match_path = "created_new_no_match"
            flags.append("created new customer — no match found")
            logger.info("Created new customer (no match): %s", customer["id"])
        elif len(hits) == 1:
            customer = hits[0]
            match_path = f"matched_by_{criterion}"
            flags.append(f"matched existing customer by {criterion}")
            logger.info("Customer matched by %s: %s", criterion, customer["id"])
        else:
            # Multiple matches — don't guess. Create a new record so the
            # consultation data isn't lost, and surface the candidates.
            ambiguous_candidates = [h["id"] for h in hits]
            match_path = "ambiguous_match"
            flags.append(
                f"AMBIGUOUS — {len(hits)} customers matched by {criterion} "
                f"({ambiguous_candidates}); created a new customer to avoid clobbering"
            )
            logger.warning(
                "Ambiguous match by %s across %s — creating new customer instead",
                criterion, ambiguous_candidates,
            )
            try:
                customer = _create_customer_from_client(client)
            except WriterError as exc:
                logger.error("Customer creation (after ambiguous match) failed: %s", exc)
                raise

    cid = customer["id"]

    # Step 3 — locate or create the target job for this customer.
    jobs_payload = _get("/jobs", params={"customer_id": cid})
    existing_jobs = (jobs_payload or {}).get("data") or []
    if len(existing_jobs) == 1:
        job = existing_jobs[0]
        logger.info("Found existing job: %s", job["id"])
    elif len(existing_jobs) > 1:
        # Most recently created wins, but flag the decision.
        job = sorted(
            existing_jobs, key=lambda j: j.get("created_at") or "", reverse=True
        )[0]
        flags.append(
            f"customer had {len(existing_jobs)} jobs, used most recently created ({job['id']})"
        )
        logger.info("Multiple jobs (%d); using most recent: %s", len(existing_jobs), job["id"])
    else:
        try:
            job = _create_job_for_consultation(cid, client, extraction)
        except WriterError as exc:
            logger.error("Job creation failed for customer %s: %s", cid, exc)
            raise
        flags.append(f"no existing job — created new job ({job['id']})")
        logger.info("Created new job: %s", job["id"])

    jid = job["id"]

    # Step 4 — PATCH consultation fields + status onto the target job. The mock
    # merges custom_fields, so prior intake fields are preserved.
    custom_fields = _build_consultation_custom_fields(extraction)
    try:
        _patch(f"/jobs/{jid}", {
            "status": "consultation_complete",
            "custom_fields": custom_fields,
        })
        logger.info(
            "Job updated: %s (status=consultation_complete, %d consult_* fields merged)",
            jid, len(custom_fields),
        )
    except WriterError as exc:
        logger.error("Job PATCH failed for %s: %s", jid, exc)
        raise

    result = {
        "match_path": match_path,
        "customer_id": cid,
        "job_id": jid,
        "note_ids": [],
        "subtask_ids": [],
        "fieldpulse_url": f"{BASE_URL}/admin/jobs/{jid}",
        "flags": flags,
    }
    if ambiguous_candidates:
        result["ambiguous_candidates"] = ambiguous_candidates

    partial_failures = []

    # Step 5 — consultation note.
    note_body = _build_consult_note_body(extraction, original_transcript)
    try:
        note = _post(f"/jobs/{jid}/notes", {"body": note_body, "source": _SOURCE})
        result["note_ids"].append(note["id"])
        logger.info("Consultation note added: %s", note["id"])
    except WriterError as exc:
        logger.error("Note creation failed (continuing): %s", exc)
        partial_failures.append(f"note failed: {exc}")

    # Step 6 — return-appointment subtask, if one was scheduled.
    subtask_body = _build_return_appointment_subtask(extraction.get("return_appointment"))
    if subtask_body:
        try:
            sub = _post(f"/jobs/{jid}/subtasks", subtask_body)
            result["subtask_ids"].append(sub["id"])
            logger.info(
                "Return-appointment subtask added: %s (due=%s)",
                sub["id"], subtask_body.get("due_date"),
            )
        except WriterError as exc:
            logger.error("Return-appointment subtask creation failed (continuing): %s", exc)
            partial_failures.append(f"return-appointment subtask failed: {exc}")

    if partial_failures:
        result["partial_failure"] = "; ".join(partial_failures)

    logger.info(
        "Consultation write complete: match=%s customer=%s job=%s notes=%d subtasks=%d",
        match_path, cid, jid, len(result["note_ids"]), len(result["subtask_ids"]),
    )
    return result
