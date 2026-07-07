"""Flask orchestrator that ties the extractor and writer together.

Exposes an HTTP endpoint that takes a call transcript, runs it through
extract_intake() (Claude), then write_intake_to_fieldpulse() (mock FieldPulse),
and returns the combined result. Intended to be reached by an iPhone Shortcut
(via ngrok) that POSTs transcripts. Audio→text happens elsewhere.
"""

import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

from consultation_writer import WriterError as ConsultWriterError
from extractor import ExtractionError, extract_intake
from fieldpulse_writer import WriterError, write_intake_to_fieldpulse
from jobs import create_job, get_job, list_recent_jobs
from pipeline import run_from_transcript as _run_consult_from_transcript
from queue_setup import enqueue_consultation
from storage import (
    StorageError,
    audio_exists,
    generate_consultation_key,
    generate_upload_url,
)
from transcriber import TranscriptionError, transcribe_audio

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

# Render sets $PORT; default to 5100 for local dev so existing scripts /
# curl commands keep working. (Under gunicorn on Render, gunicorn binds
# via --bind 0.0.0.0:$PORT and this app.run() never executes.)
PORT = int(os.environ.get("PORT", 5100))
DEFAULT_INTAKE_KEY = "dev-intake-key"

# --- Logging ----------------------------------------------------------------

logger = logging.getLogger("voice_intake.orchestrator")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

app = Flask(__name__)

# CORS: allow all origins during development so the React app (e.g. on
# localhost:5173) can hit this orchestrator from a browser. For production,
# REPLACE origins="*" with the deployed app's actual domain — e.g.
#   CORS(app, origins=["https://app.example.com"], supports_credentials=True)
# Also: keep allow_headers including "x-intake-key" so the browser can send
# the existing auth header on cross-origin requests.
CORS(
    app,
    origins="*",
    allow_headers=["Content-Type", "x-intake-key"],
    methods=["GET", "POST", "OPTIONS"],
)


# --- A realistic baked-in sample for /test-with-sample ----------------------

SAMPLE_TRANSCRIPT = """\
REP: Hi, this is Mike with 3 Day Kitchen and Bath, returning your call. Is now an okay time?
PROSPECT: Yeah, now's good. Thanks for getting back to me.
REP: Of course. So tell me a little about what you're thinking of doing.
PROSPECT: We want to redo our kitchen. It's the original from when we bought the place and it's just really dated.
REP: A full kitchen remodel, got it. And I didn't catch your name?
PROSPECT: It's Greg Halloran.
REP: Greg, great. What part of town are you in?
PROSPECT: We're over on the west side, near Lincoln Elementary.
REP: And do you know when the home was built?
PROSPECT: I want to say early 2000s. 2003 maybe?
REP: Okay. How long have you all lived there?
PROSPECT: About six years now.
REP: Are you planning to move, or is this a stay-put project?
PROSPECT: Oh no, we're staying put. We love the neighborhood.
REP: Do you have any products picked out yet, or any plans drawn up?
PROSPECT: Not really. My wife's been pinning stuff but nothing decided. No drawings or anything.
REP: Got it. Any time frame in mind?
PROSPECT: We'd like to get going this fall if we can.
REP: And how'd you hear about us?
PROSPECT: You guys did my coworker's bathroom, Dana Pruitt. She passed along your number.
REP: Love a referral. Now are you married? What's your wife's name?
PROSPECT: Yep, my wife is Sandra.
REP: Perfect. There's no cost for the consultation, but I'd want both of you there. Does sometime next week work?
PROSPECT: Probably, yeah. Let me check with Sandra and get back to you on the exact day.
REP: Sounds good. Can I grab your address and an email for the pre-meeting info?
PROSPECT: Sure. 1714 Brookhaven Drive. And email is greg.halloran at gmail dot com.
REP: Got it. I'll send that over today.
"""


# --- Baked-in consultation sample for /test-consultation-sample ------------

SAMPLE_CONSULTATION_TRANSCRIPT = """\
REP: Thanks for having me in. Walk me through what you're imagining for the kitchen.
HOMEOWNER: We've been here twelve years. Kitchen is original from '92 — really tired. We want to open up the half-wall between the kitchen and the dining room.
REP: A structural change, got it. Plumbing — keeping the sink where it is?
HOMEOWNER: Sink stays, no real plumbing moves planned.
REP: Electrical changes? Any panel concerns?
HOMEOWNER: A few new outlets along the new island. The panel's about ten years old, so we're fine there.
REP: Lighting?
HOMEOWNER: Recessed over the island, plus under-cabinet LEDs.
REP: Appliances — what's staying, what's new?
HOMEOWNER: Keep the fridge for now. New gas range, new dishwasher, and a drawer microwave under the island.
REP: Hood?
HOMEOWNER: New vented hood, ducted to the outside.
REP: Style and feeling for the space?
HOMEOWNER: Bright and open. White shaker cabinets, quartz counters, brushed nickel hardware.
REP: Budget range you're comfortable with?
HOMEOWNER: We're thinking 45 to 55 thousand, hoping closer to 45.
REP: Time frame in mind?
HOMEOWNER: We'd love to start in September and have it usable by Thanksgiving. We've been talking about it for over a year.
REP: Any plans drawn or products picked out already?
HOMEOWNER: No, just Pinterest boards and a couple of showroom visits.
REP: Chances you actually move forward — 50, 75 percent?
HOMEOWNER: Honestly, 85 percent. We're ready.
REP: I want to walk you through our process before we wrap up — the UFC presentation.
HOMEOWNER: Sure.
REP: If I come back with a plan you like that's reasonably on budget, how would you score that on a 1 to 10?
HOMEOWNER: I'd say a nine. If the design works, we're going.
REP: Anything you're worried about?
HOMEOWNER: Mostly the timeline around the holidays. I don't want to be eating off paper plates in November.
REP: Any financing — or could you do this if you wanted to?
HOMEOWNER: No financing, we'd pay out of pocket.
REP: I'd like to come back next Thursday at six to walk you through the design.
HOMEOWNER: That works. Thursday at six.
"""


# --- Auth -------------------------------------------------------------------

@app.before_request
def _require_intake_key():
    """Every endpoint except /health requires a valid x-intake-key header.

    OPTIONS (CORS preflight) requests are also exempt — browsers don't send
    custom headers on preflights, so blocking them here would break CORS.
    Flask-CORS responds with the appropriate Access-Control-Allow-* headers.
    """
    if request.method == "OPTIONS" or request.path == "/health":
        return
    expected = os.environ.get("INTAKE_API_KEY", DEFAULT_INTAKE_KEY)
    provided = request.headers.get("x-intake-key")
    if not provided or provided != expected:
        logger.warning("Rejected request to %s: missing/invalid x-intake-key", request.path)
        return jsonify({
            "status": "unauthorized",
            "error": "Missing or invalid x-intake-key header",
        }), 401


# --- Core pipeline ----------------------------------------------------------

def _run_pipeline(transcript, source):
    """Run extract -> write. Returns (response_dict, http_status_code).

    Handles the two expected failure modes (extraction, writer). Unexpected
    exceptions are allowed to propagate to the route's catch-all (500).
    """
    start = time.monotonic()
    logger.info(
        "Request received (source=%s, transcript_len=%d chars)",
        source or "—", len(transcript),
    )

    # --- Extraction (Claude) ---
    try:
        extraction = extract_intake(transcript)
    except ExtractionError as exc:
        elapsed = round(time.monotonic() - start, 1)
        logger.warning("Extraction failed after %.1fs: %s", elapsed, exc)
        return {
            "status": "extraction_failed",
            "error": str(exc),
            "elapsed_seconds": elapsed,
        }, 422

    # --- Write (mock FieldPulse) ---
    try:
        fieldpulse = write_intake_to_fieldpulse(extraction, transcript)
    except WriterError as exc:
        elapsed = round(time.monotonic() - start, 1)
        logger.error("Writer failed after %.1fs: %s", elapsed, exc)
        return {
            "status": "writer_failed",
            "error": str(exc),
            "extraction": extraction,  # surface what Claude figured out
            "elapsed_seconds": elapsed,
        }, 502

    elapsed = round(time.monotonic() - start, 1)
    logger.info(
        "Success in %.1fs: customer=%s job=%s",
        elapsed, fieldpulse.get("customer_id"), fieldpulse.get("job_id"),
    )
    return {
        "status": "success",
        "extraction": extraction,
        "fieldpulse": fieldpulse,
        "elapsed_seconds": elapsed,
    }, 200


def _handle(transcript, source):
    """Route wrapper: run the pipeline, converting anything unexpected to 500."""
    try:
        result, code = _run_pipeline(transcript, source)
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all for 500
        logger.exception("Unexpected error during pipeline")
        return jsonify({
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }), 500
    return jsonify(result), code


# --- Endpoints --------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "voice-intake-orchestrator"})


@app.post("/process-transcript")
def process_transcript():
    body = request.get_json(silent=True) or {}
    transcript = body.get("transcript")
    source = body.get("source")
    if not isinstance(transcript, str) or not transcript.strip():
        return jsonify({
            "status": "bad_request",
            "error": "Request body must include a non-empty 'transcript' string",
        }), 400
    return _handle(transcript, source)


@app.post("/test-with-sample")
def test_with_sample():
    return _handle(SAMPLE_TRANSCRIPT, "test-with-sample")


# --- Consultation pipeline --------------------------------------------------

def _run_consultation_pipeline(transcript, customer_id, source, transcription_seconds=None):
    """Extract -> write consultation. Returns (response_dict, http_status_code).

    Delegates the actual extract+write work to pipeline.run_from_transcript
    (which is also what the async worker uses via pipeline.run_from_audio),
    so there's one source of truth for the consultation flow. This function
    just maps stage-specific exceptions to HTTP status codes.
    """
    start = time.monotonic()
    logger.info(
        "Consultation request received (source=%s, transcript_len=%d chars, customer_id=%s)",
        source or "—", len(transcript), customer_id or "—",
    )

    try:
        result = _run_consult_from_transcript(
            transcript, customer_id=customer_id, source=source,
        )
    except ExtractionError as exc:
        elapsed = round(time.monotonic() - start, 1)
        logger.warning("Consultation extraction failed after %.1fs: %s", elapsed, exc)
        return {
            "status": "extraction_failed",
            "error": str(exc),
            "elapsed_seconds": elapsed,
        }, 422
    except ConsultWriterError as exc:
        elapsed = round(time.monotonic() - start, 1)
        logger.error("Consultation writer failed after %.1fs: %s", elapsed, exc)
        # pipeline attaches the completed extraction to the WriterError so
        # we can still show Claude's output when the FieldPulse write fails.
        return {
            "status": "writer_failed",
            "error": str(exc),
            "extraction": getattr(exc, "extraction", None),
            "elapsed_seconds": elapsed,
        }, 502

    elapsed = round(time.monotonic() - start, 1)
    response = {
        "status": "success",
        "transcript": result["transcript"],
        "extraction": result["extraction"],
        "fieldpulse": result["fieldpulse"],
        "elapsed_seconds": elapsed,
    }
    if transcription_seconds is not None:
        response["transcription_seconds"] = transcription_seconds
    return response, 200


def _handle_consultation(transcript, customer_id, source, transcription_seconds=None):
    """Route wrapper for consultation calls — wraps unexpected exceptions as 500."""
    try:
        result, code = _run_consultation_pipeline(
            transcript, customer_id, source, transcription_seconds
        )
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all for 500
        logger.exception("Unexpected error during consultation pipeline")
        return jsonify({
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }), 500
    return jsonify(result), code


@app.post("/process-consultation")
def process_consultation():
    """DEPRECATED — synchronous consultation endpoint. Kept working until
    the app is rewired to the async flow below (upload-url / submit /
    job-status). Prefer the async flow for new callers.

    End-to-end consultation: audio file -> transcribe -> extract -> write.

    Accepts EITHER:
      - multipart/form-data with field `audio` (audio file) and optional
        form fields `customer_id`, `source`. Transcribes via Deepgram.
      - JSON body {"transcript": "...", "customer_id": "...", "source": "..."}.
        Skips transcription and uses the transcript directly.
    """
    transcript = None
    customer_id = None
    source = None
    transcription_seconds = None

    content_type = request.content_type or ""
    if content_type.startswith("multipart/form-data"):
        audio = request.files.get("audio")
        if not audio or not audio.filename:
            return jsonify({
                "status": "bad_request",
                "error": "multipart request must include 'audio' file",
            }), 400
        customer_id = (request.form.get("customer_id") or "").strip() or None
        source = (request.form.get("source") or "").strip() or None

        # Save to temp, transcribe, then clean up — guaranteed via try/finally.
        suffix = os.path.splitext(audio.filename)[1] or ".wav"
        fd, tmp_path = tempfile.mkstemp(prefix="consult_", suffix=suffix)
        os.close(fd)
        try:
            audio.save(tmp_path)
            t_start = time.monotonic()
            logger.info(
                "Consultation transcription started (file=%s, customer_id=%s)",
                audio.filename, customer_id or "—",
            )
            try:
                tr = transcribe_audio(tmp_path)
            except TranscriptionError as exc:
                t_elapsed = round(time.monotonic() - t_start, 1)
                logger.warning(
                    "Consultation transcription failed after %.1fs: %s", t_elapsed, exc
                )
                return jsonify({
                    "status": "transcription_failed",
                    "error": str(exc),
                    "elapsed_seconds": t_elapsed,
                }), 422
            transcript = tr.get("transcript") or ""
            transcription_seconds = round(time.monotonic() - t_start, 1)
            logger.info(
                "Consultation transcription done (%.1fs, audio %.1fs, %d chars)",
                transcription_seconds,
                float(tr.get("duration_seconds") or 0.0),
                len(transcript),
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        if not transcript.strip():
            return jsonify({
                "status": "transcription_failed",
                "error": "Transcription returned an empty transcript",
            }), 422

    else:
        body = request.get_json(silent=True) or {}
        transcript = body.get("transcript")
        customer_id = (body.get("customer_id") or "").strip() or None if isinstance(body.get("customer_id"), str) else body.get("customer_id")
        source = body.get("source")
        if not isinstance(transcript, str) or not transcript.strip():
            return jsonify({
                "status": "bad_request",
                "error": (
                    "Send either multipart/form-data with an 'audio' file, "
                    "or JSON with a non-empty 'transcript' string."
                ),
            }), 400

    return _handle_consultation(transcript, customer_id, source, transcription_seconds)


@app.post("/test-consultation-sample")
def test_consultation_sample():
    """Smoke-test the consultation extract+write wiring with a baked-in transcript."""
    return _handle_consultation(
        SAMPLE_CONSULTATION_TRANSCRIPT, customer_id=None, source="test-consultation-sample"
    )


# --- Async consultation flow (direct-to-R2 upload) -------------------------
# Replaces /process-consultation. The app calls these three in order:
#   1. POST /consultation/upload-url   -> get a presigned R2 PUT URL + audio_key
#   2. (browser PUTs the audio directly to R2 — bypasses the orchestrator)
#   3. POST /consultation/submit       -> creates the job + enqueues; returns
#                                         job_id and HTTP 202
#   4. GET  /job-status/<job_id>       -> polled until status=='done'|'failed'

# Anything under audio/* is accepted; `video/webm` is included because
# browser MediaRecorder often labels audio-only WebM as video/webm.
_ALLOWED_CONTENT_TYPE_EXTRAS = {"video/webm"}
_CUSTOMER_ID_RE = re.compile(r"^cust_[A-Za-z0-9]+$")
_AUDIO_KEY_RE = re.compile(r"^consultations/[A-Za-z0-9\-]+\.[A-Za-z0-9]{1,10}$")


def _is_allowed_content_type(ct):
    if not isinstance(ct, str) or not ct:
        return False
    # Strip codec params, e.g. "audio/webm;codecs=opus" -> "audio/webm"
    base = ct.split(";", 1)[0].strip().lower()
    return base.startswith("audio/") or base in _ALLOWED_CONTENT_TYPE_EXTRAS


def _bad_request(message, code=400):
    return jsonify({"error": message}), code


@app.post("/consultation/upload-url")
def consultation_upload_url():
    """Step 1 of the async flow. Body:
        { "filename": "...", "content_type": "audio/...", "customer_id"?: "cust_..." }

    Returns { "upload_url", "audio_key" }. The app then PUTs the audio
    directly to `upload_url`, sending the SAME `Content-Type` header it
    passed here (the value is baked into the SigV4 signature; R2 will
    reject the PUT otherwise).
    """
    body = request.get_json(silent=True) or {}
    filename = (body.get("filename") or "").strip()
    content_type = (body.get("content_type") or "").strip()
    customer_id = (body.get("customer_id") or "").strip() or None

    if not filename:
        return _bad_request("'filename' is required")
    if not content_type:
        return _bad_request("'content_type' is required")
    if not _is_allowed_content_type(content_type):
        return _bad_request(
            f"content_type '{content_type}' is not an allowed audio type "
            f"(accepted: audio/*, video/webm)"
        )
    if customer_id and not _CUSTOMER_ID_RE.match(customer_id):
        return _bad_request(
            f"customer_id '{customer_id}' has invalid format (expected cust_...)"
        )

    try:
        audio_key = generate_consultation_key(filename)
        upload_url = generate_upload_url(audio_key, content_type)
    except StorageError as exc:
        logger.error("Upload URL generation failed: %s", exc)
        return jsonify({"error": str(exc)}), 502

    logger.info(
        "Issued upload URL: audio_key=%s content_type=%s customer_id=%s",
        audio_key, content_type, customer_id or "—",
    )
    return jsonify({
        "upload_url": upload_url,
        "audio_key": audio_key,
    })


@app.post("/consultation/submit")
def consultation_submit():
    """Step 2 of the async flow. Body:
        { "audio_key": "consultations/...", "customer_id"?: "...", "source"?: "..." }

    Verifies the R2 object actually exists (means the client PUT succeeded),
    creates a consultation_jobs row, enqueues it for the worker, and returns
    { "job_id", "status": "queued" } with HTTP 202.
    """
    body = request.get_json(silent=True) or {}
    audio_key = (body.get("audio_key") or "").strip()
    customer_id = (body.get("customer_id") or "").strip() or None
    source = (body.get("source") or "3day-app").strip() or "3day-app"

    if not audio_key:
        return _bad_request("'audio_key' is required")
    if not _AUDIO_KEY_RE.match(audio_key):
        return _bad_request(
            f"audio_key '{audio_key}' has invalid format "
            f"(expected consultations/<uuid>.<ext>)"
        )
    if customer_id and not _CUSTOMER_ID_RE.match(customer_id):
        return _bad_request(
            f"customer_id '{customer_id}' has invalid format (expected cust_...)"
        )

    try:
        exists = audio_exists(audio_key)
    except StorageError as exc:
        logger.error("Failed to verify R2 key: %s", exc)
        return jsonify({"error": f"Could not verify R2 upload: {exc}"}), 502

    if not exists:
        return _bad_request(
            f"audio_key '{audio_key}' does not exist in R2 — the upload may "
            f"have failed. Request a fresh upload URL and try again.",
            code=404,
        )

    job = create_job(
        audio_key=audio_key, customer_id=customer_id, source=source
    )
    enqueue_consultation(job["id"])
    logger.info(
        "Enqueued consultation job %s (audio_key=%s customer_id=%s source=%s)",
        job["id"], audio_key, customer_id or "—", source,
    )
    return jsonify({
        "job_id": job["id"],
        "status": job["status"],
    }), 202


@app.get("/jobs/recent")
def jobs_recent():
    """List recent consultation jobs newest-first for the app's history view.
    Optional ?limit= (default 20, hard-capped at 50)."""
    raw = request.args.get("limit", "20")
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return _bad_request(f"'limit' must be an integer (got {raw!r})")
    limit = max(1, min(limit, 50))
    return jsonify({"jobs": list_recent_jobs(limit)})


@app.get("/job-status/<job_id>")
def consultation_job_status(job_id):
    """Poll a consultation job's status. Returns the current record; on
    done the `result` JSONB is included; on failed the `error` field is."""
    job = get_job(job_id)
    if job is None:
        return jsonify({"error": f"Job not found: {job_id}"}), 404

    response = {
        "job_id": job["id"],
        "status": job["status"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }
    if job["status"] == "done" and job.get("result") is not None:
        response["result"] = job["result"]
    if job["status"] == "failed" and job.get("error"):
        response["error"] = job["error"]
    return jsonify(response)


# --- App-facing endpoints (read-only, used by the React app) ---------------

@app.get("/app/customers")
def app_customers():
    """Return a simplified customer list for the app's picker UI.

    Proxies GET /customers on the configured FieldPulse instance (using the
    base URL + API key from .env — same as the writers use) and returns just
    the fields the picker needs. Pages through results so the app gets all
    customers in one response.
    """
    base = os.environ.get("FIELDPULSE_BASE_URL", "http://localhost:5000").rstrip("/")
    key = os.environ.get("FIELDPULSE_API_KEY", "dev-test-key-12345")

    customers = []
    page = 1
    try:
        while True:
            # 60s timeout + single retry on Timeout — handles the Render
            # free-tier cold-start case where the first call takes 30–60s
            # to wake the mock service. In practice the retry only fires
            # on page 1; once the mock is warm, subsequent pages are fast.
            try:
                resp = requests.get(
                    f"{base}/customers",
                    headers={"x-api-key": key},
                    params={"page": page, "per_page": 200},
                    timeout=60,
                )
            except requests.Timeout:
                logger.warning(
                    "FieldPulse /customers timed out — retrying once after brief sleep"
                )
                time.sleep(1)
                resp = requests.get(
                    f"{base}/customers",
                    headers={"x-api-key": key},
                    params={"page": page, "per_page": 200},
                    timeout=60,
                )
            if resp.status_code != 200:
                logger.error(
                    "FieldPulse /customers returned %d: %s",
                    resp.status_code, resp.text[:200],
                )
                return jsonify({
                    "status": "fieldpulse_error",
                    "error": f"FieldPulse returned HTTP {resp.status_code}",
                    "detail": resp.text[:300],
                }), 502
            payload = resp.json()
            batch = payload.get("data") or []
            customers.extend(batch)
            meta = payload.get("meta") or {}
            total_pages = meta.get("total_pages") or 1
            if page >= total_pages or not batch:
                break
            page += 1
    except requests.RequestException as exc:
        logger.error("FieldPulse unreachable at %s: %s", base, exc)
        return jsonify({
            "status": "fieldpulse_unreachable",
            "error": f"Could not reach FieldPulse at {base}: {exc}",
        }), 502
    except ValueError as exc:
        logger.error("FieldPulse returned non-JSON: %s", exc)
        return jsonify({
            "status": "fieldpulse_error",
            "error": f"FieldPulse returned non-JSON body: {exc}",
        }), 502

    simplified = [
        {
            "id": c.get("id"),
            "display_name": c.get("display_name"),
            "first_name": c.get("first_name"),
            "last_name": c.get("last_name"),
            "phone": c.get("phone"),
            "email": c.get("email"),
        }
        for c in customers
    ]
    logger.info("/app/customers -> %d customers", len(simplified))
    return jsonify({"customers": simplified})


@app.post("/app/customers")
def app_create_customer():
    """Create a customer on the configured FieldPulse instance.

    Accepts JSON {first_name, last_name, phone?, email?}. Builds
    display_name = "{last_name}, {first_name}" to match how the rest of the
    system formats names. Returns the simplified customer the same way
    GET /app/customers does — wrapped under a singular 'customer' key.
    """
    body = request.get_json(silent=True) or {}
    first_name = (body.get("first_name") or "").strip()
    last_name = (body.get("last_name") or "").strip()
    phone = (body.get("phone") or "").strip() or None
    email = (body.get("email") or "").strip() or None

    if not first_name or not last_name:
        return jsonify({
            "status": "bad_request",
            "error": "Request must include non-empty 'first_name' and 'last_name'",
        }), 400

    display_name = f"{last_name}, {first_name}"
    base = os.environ.get("FIELDPULSE_BASE_URL", "http://localhost:5000").rstrip("/")
    key = os.environ.get("FIELDPULSE_API_KEY", "dev-test-key-12345")

    body_out = {
        "display_name": display_name,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone,
        "email": email,
    }

    try:
        resp = requests.post(
            f"{base}/customers",
            headers={"x-api-key": key},
            json=body_out,
            # 60s for cold-start tolerance. No retry on this POST because
            # the mock could create a customer on the first attempt even
            # if the response is slow; retrying would risk a duplicate.
            timeout=60,
        )
        if resp.status_code not in (200, 201):
            logger.error(
                "FieldPulse POST /customers returned %d: %s",
                resp.status_code, resp.text[:200],
            )
            return jsonify({
                "status": "fieldpulse_error",
                "error": f"FieldPulse returned HTTP {resp.status_code}",
                "detail": resp.text[:300],
            }), 502
        payload = resp.json()
    except requests.RequestException as exc:
        logger.error("FieldPulse unreachable at %s: %s", base, exc)
        return jsonify({
            "status": "fieldpulse_unreachable",
            "error": f"Could not reach FieldPulse at {base}: {exc}",
        }), 502
    except ValueError as exc:
        logger.error("FieldPulse returned non-JSON: %s", exc)
        return jsonify({
            "status": "fieldpulse_error",
            "error": f"FieldPulse returned non-JSON body: {exc}",
        }), 502

    cust = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(cust, dict) or not cust.get("id"):
        logger.error("FieldPulse create-customer response missing data.id: %s", payload)
        return jsonify({
            "status": "fieldpulse_error",
            "error": "FieldPulse response missing data.id",
        }), 502

    simplified = {
        "id": cust.get("id"),
        "display_name": cust.get("display_name"),
        "first_name": cust.get("first_name"),
        "last_name": cust.get("last_name"),
        "phone": cust.get("phone"),
        "email": cust.get("email"),
    }
    logger.info(
        "created customer %s (%s)", simplified["id"], simplified["display_name"]
    )
    return jsonify({"customer": simplified}), 201


# --- Startup ----------------------------------------------------------------

if __name__ == "__main__":
    intake_key = os.environ.get("INTAKE_API_KEY", DEFAULT_INTAKE_KEY)
    bar = "─" * 64
    print(bar)
    print("Voice Intake Orchestrator")
    print(f"  Local URL:    http://localhost:{PORT}")
    print(f"  Bound to:     0.0.0.0:{PORT}  (reachable via ngrok)")
    print("  Endpoints:")
    print(f"    GET  /health                    (no auth)")
    print(f"    POST /process-transcript        (intake — JSON)")
    print(f"    POST /test-with-sample          (intake smoke test)")
    print(f"    POST /process-consultation      (consultation — audio or JSON)")
    print(f"    POST /test-consultation-sample  (consultation smoke test)")
    print(f"    GET  /app/customers             (proxy: customer picker list)")
    print(f"    POST /app/customers             (proxy: create customer)")
    print("  CORS: allow_origins=* (dev only — lock down for prod)")
    print(f"  Auth header:  x-intake-key: {intake_key}")
    print("  Note: mock FieldPulse must be running on http://localhost:5000")
    print(bar)
    # debug=False → no auto-reloader re-running the pipeline during testing.
    app.run(host="0.0.0.0", port=PORT, debug=False)
