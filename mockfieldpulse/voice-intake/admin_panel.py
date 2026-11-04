"""Admin panel data for Nick and Mike (stage 3): every consultation, in full,
from OUR Postgres only (consultation_jobs + fieldpulse_writes). Never reads
the mock or FieldPulse, and never talks to FieldPulse: the one action
("Send to FieldPulse" / "Retry") only queues a worker job.

Four entry points, each returning (http_status, json_body) so the routes in
orchestrator.py stay one-liners:

    list_consultations(search, fp_filter, offset, limit)
    consultation_detail(job_id)
    request_manual_send(job_id, user_id, user_name)
    audio_url(job_id)

Hidden in here: the FieldPulse status of a consultation (one label per row,
computed from the write rows + the stored sync summary, legacy mock jobs
included), the record/upload/paste source, display names, search and
filter, and scrubbing anything secret-looking out of every response.

Who may use it: every signed-in Clerk user (only Nick and Mike have accounts
today). Deliberate stage-3 decision; add a role check here when that changes.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

import jobs
import queue_setup
import storage

logger = logging.getLogger("voice_intake.admin_panel")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

SCAN_LIMIT = 2000           # newest rows searched/filtered by the list (trial volume)
MAX_PAGE = 100
DEFAULT_PAGE = 50
_SEND_DEDUPE = timedelta(minutes=10)   # a queued send younger than this is reused

# FieldPulse status per consultation (one value per row in the list).
FP_LABELS = {
    "succeeded": "Sent to FieldPulse",
    "partial": "Partly sent",
    "failed": "Failed",
    "sent": "Sent, no answer",
    "held": "Held (send by hand)",
    "dry_run": "Dry run (not sent)",
    "not_sent": "Not sent",
    "pending": "Pending",
    "not_attempted": "Not attempted",
    "legacy_mock": "Legacy (mock only)",
}
# "Needs attention": something Mike should look at in FieldPulse or retry.
# `sent` = the request went out but no answer came back (may or may not exist).
ATTENTION = ("failed", "partial", "held", "sent")
_FILTERS = set(FP_LABELS) | {"attention", ""}


# --- Small shaping helpers ----------------------------------------------------

def _dict(v):
    return v if isinstance(v, dict) else {}


def _text(v, limit=200):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v[:limit] if v else None


def _customer_name(customer, client_name=None, customer_id=None):
    c = _dict(customer)
    name = _text(c.get("display_name"))
    if not name:
        name = " ".join(p for p in (_text(c.get("first_name")), _text(c.get("last_name"))) if p)
    if name:
        return name
    if _text(client_name):
        return f"{_text(client_name)} (name heard in the consultation)"
    if _text(customer_id):
        return f"Customer {_text(customer_id)}"
    return None


def source_kind(source, has_audio, has_paste, input_=None):
    """record | upload | paste | audio (old jobs: record or upload, not stored) | unknown."""
    s = (source or "").lower() if isinstance(source, str) else ""
    if has_paste or input_ == "paste" or s.endswith("-paste"):
        return "paste"
    if s.endswith("-upload"):
        return "upload"
    if s.endswith("-record"):
        return "record"
    if has_audio or input_ == "audio":
        return "audio"
    return "unknown"


def _close_score(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return v if 1 <= v <= 10 else None


def fieldpulse_status(job_status, sync, writes, legacy_mock):
    """One FieldPulse status for a consultation. The write rows (source of
    truth for what went out) win; then the stored sync summary; then legacy."""
    rows = [w for w in (writes or []) if isinstance(w, dict)]
    active = {w.get("status") for w in rows if w.get("status") != "skipped"}
    if "sent" in active:
        return "sent"
    if "succeeded" in active:
        return "partial" if active - {"succeeded"} else "succeeded"
    if "failed" in active:
        return "failed"
    sync = _dict(sync)
    s = sync.get("status")
    steps = [st for st in (sync.get("steps") or []) if isinstance(st, dict)]
    if s == "blocked" and any(st.get("status") == "held" for st in steps):
        return "held"
    if s in ("succeeded", "partial", "failed", "dry_run"):
        return s
    if s in ("off", "skipped", "blocked"):
        return "not_sent"
    if "planned" in active:
        return "dry_run" if any(w.get("mode") == "dry_run" for w in rows) else "not_sent"
    if s == "pending" or job_status in ("queued", "processing"):
        return "pending"
    if legacy_mock:
        return "legacy_mock"
    return "not_attempted"


def _needs_attention(job_status, fp):
    return fp in ATTENTION or job_status == "failed"


# --- Secret scrubbing (applied to every admin response) -----------------------

_SECRET_KEY = re.compile(
    r"(^|[_-])(api[_-]?key|apikey|authorization|secret|password|passwd|token|"
    r"access[_-]?key|cookie|headers?|credentials?)$", re.I)
_PRESIGNED = re.compile(r"https?://\S*X-Amz-(Signature|Credential)=\S*", re.I)


def scrub(value):
    """Drop secret-looking keys and blank out presigned URLs, recursively."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()
                if not (isinstance(k, str) and _SECRET_KEY.search(k))}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str) and "X-Amz-" in value:
        return _PRESIGNED.sub("[signed link removed]", value)
    return value


# --- List ------------------------------------------------------------------------

def _list_item(r):
    fp = fieldpulse_status(r.get("status"), r.get("sync"), r.get("writes"), r.get("legacy_mock"))
    customer = _dict(r.get("customer"))
    last = _dict(r.get("last_manual_send"))
    return {
        "id": r["id"],
        "created_at": r.get("created_at"),
        "status": r.get("status"),
        "error": r.get("error"),
        "customer_id": r.get("customer_id"),
        "customer_name": _customer_name(customer, r.get("client_name"), r.get("customer_id")),
        "source": source_kind(r.get("source"), r.get("has_audio"), r.get("has_paste"),
                              r.get("input")),
        "project_type": _text(r.get("project_type"), 40),
        "close_score": _close_score(r.get("close_score")),
        "fieldpulse_status": fp,
        "fieldpulse_label": FP_LABELS[fp],
        "needs_attention": _needs_attention(r.get("status"), fp),
        "manual_send": ({"status": _text(last.get("status"), 20),
                         "requested_at": _text(last.get("requested_at"), 40)}
                        if last else None),
        "_search": " ".join(x for x in (
            _text(customer.get("display_name")), _text(customer.get("first_name")),
            _text(customer.get("last_name")), _text(r.get("client_name")),
            _text(r.get("customer_id")), r["id"]) if x).lower(),
    }


def list_consultations(search="", fp_filter="", offset=0, limit=DEFAULT_PAGE):
    """Newest first. Summaries only (never transcripts / comment text)."""
    fp_filter = (fp_filter or "").strip().lower()
    if fp_filter not in _FILTERS:
        return 400, {"error": f"Unknown filter {fp_filter!r}"}
    try:
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), MAX_PAGE))
    except (TypeError, ValueError):
        return 400, {"error": "'offset' and 'limit' must be integers"}
    needle = (search or "").strip().lower()[:100]

    rows = jobs.list_jobs_for_admin(SCAN_LIMIT)
    items = [_list_item(r) for r in rows]
    counts = {"all": len(items), "attention": sum(1 for i in items if i["needs_attention"])}
    for i in items:
        counts[i["fieldpulse_status"]] = counts.get(i["fieldpulse_status"], 0) + 1
    if needle:
        items = [i for i in items if needle in i["_search"]]
    if fp_filter == "attention":
        items = [i for i in items if i["needs_attention"]]
    elif fp_filter:
        items = [i for i in items if i["fieldpulse_status"] == fp_filter]
    page = items[offset:offset + limit]
    for i in page:
        i.pop("_search", None)
    return 200, scrub({
        "items": page, "total": len(items), "offset": offset, "limit": limit,
        "counts": counts, "labels": FP_LABELS,
        "scan_limit": SCAN_LIMIT, "truncated": len(rows) >= SCAN_LIMIT,
    })


# --- Detail ----------------------------------------------------------------------

def consultation_detail(job_id):
    """Everything we hold about one consultation."""
    job = jobs.get_job(job_id)
    if job is None:
        return 404, {"error": f"Consultation not found: {job_id}"}
    result = _dict(job.get("result"))
    writes = jobs.list_fieldpulse_writes(job_id)
    sync = result.get("fieldpulse_sync") if isinstance(result.get("fieldpulse_sync"), dict) else None
    legacy = result.get("fieldpulse") if isinstance(result.get("fieldpulse"), dict) else None
    extraction = result.get("extraction") if isinstance(result.get("extraction"), dict) else None
    transcript = result.get("transcript") if isinstance(result.get("transcript"), str) else None
    if transcript is None and isinstance(job.get("transcript"), str):
        transcript = job["transcript"]           # pasted, not processed yet / failed
    customer = _dict(job.get("customer"))
    fp = fieldpulse_status(job.get("status"), sync, writes, legacy is not None)
    sends = result.get("fieldpulse_manual_sends")
    sends = [s for s in sends if isinstance(s, dict)] if isinstance(sends, list) else []

    # Last-resort raw view: the whole row, but long texts only once.
    raw = dict(job)
    if isinstance(raw.get("transcript"), str):
        raw["transcript"] = f"[pasted transcript, {len(raw['transcript']):,} characters: shown above]"
    if isinstance(raw.get("result"), dict) and isinstance(raw["result"].get("transcript"), str):
        raw["result"] = {**raw["result"], "transcript":
                         f"[{len(raw['result']['transcript']):,} characters: shown above]"}

    client = _dict(_dict(extraction).get("client"))
    return 200, scrub({
        "id": job["id"],
        "status": job.get("status"),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "source": source_kind(job.get("source"), bool(job.get("audio_key")),
                              bool(job.get("transcript")), result.get("input")),
        "source_raw": job.get("source"),
        "customer_id": job.get("customer_id"),
        "customer": customer or None,
        "customer_name": _customer_name(customer, client.get("name"), job.get("customer_id")),
        "has_audio": bool(job.get("audio_key")),
        "fieldpulse_status": fp,
        "fieldpulse_label": FP_LABELS[fp],
        "needs_attention": _needs_attention(job.get("status"), fp),
        "can_send": job.get("status") == "done" and extraction is not None,
        "fieldpulse_sync": sync,
        "fieldpulse_writes": writes,
        "manual_sends": sends,
        "legacy_fieldpulse": legacy,
        "extraction": extraction,
        "transcript": transcript,
        "transcription_seconds": result.get("transcription_seconds"),
        "raw": raw,
    })


# --- Actions ---------------------------------------------------------------------

def _parse_iso(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def request_manual_send(job_id, user_id=None, user_name=None):
    """Queue an explicit manual FieldPulse send. Never sends anything here:
    the worker runs fieldpulse_sync(manual=True) under ITS write mode."""
    job = jobs.get_job(job_id)
    if job is None:
        return 404, {"error": f"Consultation not found: {job_id}"}
    result = _dict(job.get("result"))
    if job.get("status") != "done" or not isinstance(result.get("extraction"), dict):
        return 409, {"error": "Only a finished consultation with extracted details can be "
                              "sent to FieldPulse."}
    now = datetime.now(timezone.utc)
    sends = result.get("fieldpulse_manual_sends")
    last = sends[-1] if isinstance(sends, list) and sends and isinstance(sends[-1], dict) else {}
    when = _parse_iso(last.get("requested_at"))
    if last.get("status") == "queued" and when and now - when < _SEND_DEDUPE and last.get("id"):
        return 202, {"request_id": last["id"], "status": "queued", "already_queued": True}

    entry = {"id": f"ms_{uuid.uuid4().hex[:12]}", "status": "queued",
             "requested_at": now.isoformat(),
             "requested_by": _text(user_name, 120), "requested_by_user_id": _text(user_id, 64)}
    if not jobs.save_manual_send(job_id, entry):
        return 404, {"error": f"Consultation not found: {job_id}"}
    try:
        queue_setup.enqueue_manual_send(job_id, entry["id"])
    except Exception as exc:  # noqa: BLE001 — Redis down etc.
        logger.error("[%s] could not queue manual send: %s", job_id, exc)
        jobs.save_manual_send(job_id, {"id": entry["id"], "status": "error",
                                       "finished_at": datetime.now(timezone.utc).isoformat(),
                                       "message": "Could not queue the send (the job queue is "
                                                  "unreachable). Nothing was sent. Try again."})
        return 503, {"error": "Could not queue the send. Nothing was sent. Try again shortly."}
    logger.info("[%s] manual FieldPulse send queued (%s) by %s", job_id, entry["id"],
                entry["requested_by_user_id"] or "?")
    return 202, {"request_id": entry["id"], "status": "queued"}


def audio_url(job_id):
    """Short-lived (15 min) presigned GET link to the recording, if any."""
    job = jobs.get_job(job_id)
    if job is None:
        return 404, {"error": f"Consultation not found: {job_id}"}
    key = job.get("audio_key")
    if not key:
        return 404, {"error": "This consultation has no audio (it was pasted)."}
    try:
        url = storage.generate_download_url(key, expires_in=900)
    except storage.StorageError as exc:
        logger.error("[%s] audio url failed: %s", job_id, exc)
        return 502, {"error": "Could not create an audio link right now."}
    return 200, {"url": url, "expires_in": 900}
