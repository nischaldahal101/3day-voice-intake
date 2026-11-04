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

from clerk_backend_api import AuthenticateRequestOptions, Clerk
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

from consultation_writer import WriterError as ConsultWriterError
from customer_source import CustomerSourceError, list_customers
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

# --- Clerk auth ------------------------------------------------------------
#
# Verifies incoming session tokens against Clerk before any protected route
# runs. Networkless once Clerk's JWKS is cached — one round-trip on first
# request, then all subsequent verifies are local RSA checks.
#
# Endpoints that must have this on:
#   /consultation/upload-url, /consultation/submit, /job-status/<id>,
#   /jobs/recent, /app/customers (GET only), /process-consultation.
# Public: /health only.
#
# We used to require x-intake-key on every request. That's retired here —
# Clerk sessions are the real gate now.
_CLERK_SECRET = os.environ.get("CLERK_SECRET_KEY")
if not _CLERK_SECRET:
    # Loud failure at boot: an unprotected orchestrator is worse than one
    # that won't start. Local dev must set CLERK_SECRET_KEY in .env.
    raise RuntimeError(
        "CLERK_SECRET_KEY is not set. Add it to voice-intake/.env "
        "(Clerk dashboard → API Keys → 'Secret key' — starts with sk_test_ or sk_live_)."
    )
_clerk_sdk = Clerk(bearer_auth=_CLERK_SECRET)

# Origins Clerk should accept tokens from — used to defend against a token
# issued for a different app being replayed here. Comma-separated env var
# lets ops list every host the app runs on (localhost:5173 for dev; the
# deployed app URL in prod).
_AUTHORIZED_PARTIES = [
    p.strip() for p in os.environ.get(
        "CLERK_AUTHORIZED_PARTIES",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if p.strip()
]

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
# Authorization must be in allow_headers so the browser is willing to send
# the Clerk Bearer token on cross-origin requests.
CORS(
    app,
    origins="*",
    allow_headers=["Content-Type", "Authorization"],
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

# Routes that do NOT require a Clerk session token. Everything else must have
# a valid Bearer token on the Authorization header. Keep this list tight —
# add here only if a caller genuinely can't send a token (health checks,
# public probes). All new app-facing endpoints should stay off this list.
_PUBLIC_PATHS = frozenset(["/health"])


@app.before_request
def _require_clerk_session():
    """Verify the Clerk session token on every non-public request.

    Flow:
      1. Skip OPTIONS (CORS preflights don't carry auth headers and are
         answered by Flask-CORS with the right Access-Control-Allow-* set).
      2. Skip anything in _PUBLIC_PATHS (currently just /health).
      3. Call Clerk's authenticate_request against the incoming request.
         Networkless after the first JWKS fetch.
      4. On not-signed-in, return 401 with a JSON error body.
      5. On success, stash the Clerk user id on flask.g in case a route
         wants to attribute writes to a specific user (Chunk C will use
         this for org-scoping).
    """
    if request.method == "OPTIONS" or request.path in _PUBLIC_PATHS:
        return
    try:
        state = _clerk_sdk.authenticate_request(
            request,
            AuthenticateRequestOptions(authorized_parties=_AUTHORIZED_PARTIES),
        )
    except Exception as exc:  # noqa: BLE001 — never want SDK bugs to 500 auth
        logger.warning("Clerk auth threw on %s: %s", request.path, exc)
        return jsonify({
            "status": "unauthorized",
            "error": "Session verification failed.",
        }), 401
    if not state.is_signed_in:
        reason = state.message or "Not signed in."
        logger.info("Rejected request to %s: %s", request.path, reason)
        return jsonify({
            "status": "unauthorized",
            "error": reason,
        }), 401
    # Stash for later routes / logging. Clerk session tokens put the user id
    # in the 'sub' claim.
    from flask import g
    payload = state.payload or {}
    g.clerk_user_id = payload.get("sub")
    g.clerk_session_id = payload.get("sid")


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

    DEPRECATED sync path only (the live async flow is worker.py +
    fieldpulse_sync). Delegates to pipeline.run_from_transcript (mock writer)
    and maps stage-specific exceptions to HTTP status codes.
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
    """DEPRECATED — synchronous consultation endpoint. The app no longer
    calls it (paste now goes through /consultation/submit with a
    `transcript`). Kept server-side until stage 6; it still writes to the
    MOCK FieldPulse via pipeline.run_from_transcript, never to real FieldPulse.

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
        customer_id, customer_id_error = _parse_customer_id(request.form.get("customer_id"))
        if customer_id_error:
            return jsonify({"status": "bad_request", "error": customer_id_error}), 400
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
        customer_id, customer_id_error = _parse_customer_id(body.get("customer_id"))
        if customer_id_error:
            return jsonify({"status": "bad_request", "error": customer_id_error}), 400
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
# Replaces /process-consultation. The app calls these in order (paste skips 1-2
# and sends {"transcript"} to submit):
#   1. POST /consultation/upload-url   -> get a presigned R2 PUT URL + audio_key
#   2. (browser PUTs the audio directly to R2 — bypasses the orchestrator)
#   3. POST /consultation/submit       -> creates the job + enqueues; returns
#                                         job_id and HTTP 202
#   4. GET  /job-status/<job_id>       -> polled until status=='done'|'failed'

# Anything under audio/* is accepted; `video/webm` is included because
# browser MediaRecorder often labels audio-only WebM as video/webm.
_ALLOWED_CONTENT_TYPE_EXTRAS = {"video/webm"}
# Mock ids look like "cust_ab12cd34"; real FieldPulse ids are integers (they
# reach us as JSON numbers from the picker, or as digit strings).
_CUSTOMER_ID_RE = re.compile(r"^(cust_[A-Za-z0-9]+|[0-9]{1,20})$")


def _parse_customer_id(raw):
    """(customer_id_str_or_None, error_message_or_None). Accepts a mock
    "cust_..." id, a real integer id, or nothing. Stored as TEXT."""
    if raw is None or raw == "":
        return None, None
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None, "customer_id must be a string or an integer"
    value = str(raw).strip()
    if not value:
        return None, None
    if not _CUSTOMER_ID_RE.match(value):
        return None, (
            f"customer_id '{value[:40]}' has invalid format "
            f"(expected cust_... or a numeric FieldPulse id)"
        )
    return value, None


_IDENTITY_FIELDS = ("display_name", "first_name", "last_name", "phone", "email")


def _identity_text(v):
    """str (trimmed, max 200 chars) or None. Numbers -> str; anything else -> None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        v = str(v)
    if not isinstance(v, str):
        return None
    v = v.strip()[:200]
    return v or None


def _parse_customer_identity(raw, customer_id):
    """(identity_dict_or_None, error_message_or_None). The picker's selected
    customer, stored on the job so the worker can create/link a mock customer
    for a real FieldPulse id without guessing by name. Its id must be the
    same customer_id the request sends.

    UNVERIFIED client input (review-log F-24): the id check is a consistency
    check, not authentication of the names/phone/email. Any signed-in user
    can submit any fields for an id. Acceptable mid-trial (only Clerk-signed-in
    Mike/Nischal, and it only shapes the MOCK customer the writer creates).
    Deferred: server-verified identity and a link-correction path, required
    before multi-tenant or before writing to real FieldPulse."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "customer must be an object"
    ident_id, err = _parse_customer_id(raw.get("id"))
    if err or ident_id is None or ident_id != customer_id:
        return None, "customer.id must match customer_id"
    identity = {"id": ident_id}
    identity.update({f: _identity_text(raw.get(f)) for f in _IDENTITY_FIELDS})
    return identity, None


_AUDIO_KEY_RE = re.compile(r"^consultations/[A-Za-z0-9\-]+\.[A-Za-z0-9]{1,10}$")
# Pasted transcripts (paste path of /consultation/submit). A 2-hour
# consultation is roughly 120-150k characters; 400k leaves headroom.
_MIN_TRANSCRIPT_CHARS = 20
_MAX_TRANSCRIPT_CHARS = 400_000


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
        { "filename": "...", "content_type": "audio/...", "customer_id"?: "cust_..." | 12345 }

    Returns { "upload_url", "audio_key" }. The app then PUTs the audio
    directly to `upload_url`, sending the SAME `Content-Type` header it
    passed here (the value is baked into the SigV4 signature; R2 will
    reject the PUT otherwise).
    """
    body = request.get_json(silent=True) or {}
    filename = (body.get("filename") or "").strip()
    content_type = (body.get("content_type") or "").strip()
    customer_id, customer_id_error = _parse_customer_id(body.get("customer_id"))

    if not filename:
        return _bad_request("'filename' is required")
    if not content_type:
        return _bad_request("'content_type' is required")
    if not _is_allowed_content_type(content_type):
        return _bad_request(
            f"content_type '{content_type}' is not an allowed audio type "
            f"(accepted: audio/*, video/webm)"
        )
    if customer_id_error:
        return _bad_request(customer_id_error)

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
    """Step 2 of the async flow. Body, with EXACTLY ONE input:
        { "audio_key": "consultations/..."      (recorded/uploaded audio in R2)
          | "transcript": "..."                  (pasted text; worker skips transcription),
          "customer_id"?: "...", "source"?: "...",
          "customer"?: {id, display_name, first_name, last_name, phone, email} }

    Audio: verifies the R2 object exists (the client PUT succeeded). Then
    creates a consultation_jobs row, enqueues it for the worker, and returns
    { "job_id", "status": "queued" } with HTTP 202. No extraction or
    FieldPulse work happens here.
    """
    body = request.get_json(silent=True) or {}
    raw_key = body.get("audio_key")
    audio_key = raw_key.strip() if isinstance(raw_key, str) else ""
    raw_transcript = body.get("transcript")
    customer_id, customer_id_error = _parse_customer_id(body.get("customer_id"))
    raw_source = body.get("source")
    source = (raw_source.strip() if isinstance(raw_source, str) else "")[:64] or "3day-app"
    customer, customer_error = (None, None)
    if not customer_id_error:
        customer, customer_error = _parse_customer_identity(body.get("customer"), customer_id)

    transcript = None
    if raw_transcript is not None:
        if audio_key:
            return _bad_request("Send either 'audio_key' or 'transcript', not both")
        if not isinstance(raw_transcript, str) or not raw_transcript.strip():
            return _bad_request("'transcript' must be a non-empty string")
        transcript = raw_transcript.strip()
        if len(transcript) < _MIN_TRANSCRIPT_CHARS:
            return _bad_request(
                f"'transcript' is too short ({len(transcript)} characters; "
                f"need at least {_MIN_TRANSCRIPT_CHARS})"
            )
        if len(transcript) > _MAX_TRANSCRIPT_CHARS:
            return _bad_request(
                f"'transcript' is too long ({len(transcript):,} characters; "
                f"max {_MAX_TRANSCRIPT_CHARS:,}, about 4 hours of talk)",
                code=413,
            )
    elif not audio_key:
        return _bad_request("'audio_key' or 'transcript' is required")
    elif not _AUDIO_KEY_RE.match(audio_key):
        return _bad_request(
            f"audio_key '{audio_key[:80]}' has invalid format "
            f"(expected consultations/<uuid>.<ext>)"
        )
    if customer_id_error:
        return _bad_request(customer_id_error)
    if customer_error:
        return _bad_request(customer_error)

    if audio_key:
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
        audio_key=audio_key or None, transcript=transcript,
        customer_id=customer_id, source=source, customer=customer,
    )
    enqueue_consultation(job["id"])
    logger.info(
        "Enqueued consultation job %s (input=%s customer_id=%s source=%s)",
        job["id"], "audio" if audio_key else f"paste {len(transcript)} chars",
        customer_id or "—", source,
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
    """Customer list for the app's picker, newest-added first.

    Read-only. customer_source.list_customers() reads real FieldPulse when
    FIELDPULSE_READ_* is set, otherwise the mock. Customers are created in
    FieldPulse, never by this app (there is no POST route).
    """
    try:
        customers = list_customers()
    except CustomerSourceError as exc:
        logger.error("/app/customers failed (%s): %s", exc.status, exc)
        return jsonify({"status": exc.status, "error": str(exc)}), exc.http_status
    logger.info("/app/customers -> %d customers", len(customers))
    return jsonify({"customers": customers})


# --- Startup ----------------------------------------------------------------

if __name__ == "__main__":
    bar = "─" * 64
    print(bar)
    print("Voice Intake Orchestrator")
    print(f"  Local URL:    http://localhost:{PORT}")
    print(f"  Bound to:     0.0.0.0:{PORT}  (reachable via ngrok)")
    print("  Endpoints:")
    print(f"    GET  /health                    (no auth)")
    print(f"    POST /process-transcript        (Clerk-protected — JSON)")
    print(f"    POST /test-with-sample          (Clerk-protected — smoke test)")
    print(f"    POST /process-consultation      (Clerk-protected — DEPRECATED, mock only)")
    print(f"    POST /test-consultation-sample  (Clerk-protected — smoke test)")
    print(f"    POST /consultation/upload-url   (Clerk-protected — presigned R2)")
    print(f"    POST /consultation/submit       (Clerk-protected — enqueue audio or paste job)")
    print(f"    GET  /job-status/<job_id>       (Clerk-protected — poll)")
    print(f"    GET  /jobs/recent               (Clerk-protected — history)")
    print(f"    GET  /app/customers             (Clerk-protected — picker list)")
    print("  CORS: allow_origins=* (dev only — lock down for prod)")
    print(f"  Auth:         Authorization: Bearer <clerk-session-token>")
    print(f"  Authorized parties: {', '.join(_AUTHORIZED_PARTIES) or '(none — set CLERK_AUTHORIZED_PARTIES)'}")
    print("  Note: mock FieldPulse must be running on http://localhost:5000")
    print(bar)
    # debug=False → no auto-reloader re-running the pipeline during testing.
    app.run(host="0.0.0.0", port=PORT, debug=False)
