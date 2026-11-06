"""Write a finished consultation to REAL FieldPulse, in staged modes.

One entry point:

    sync_consultation(job, extraction, manual=False) -> summary dict   (never raises)

The worker calls it after the transcript + extraction are already saved on the
job row (docs/FIELDPULSE_API.md §11 "save first"). Everything else is hidden
here: building the requests, the comment text, the return-appointment date
parsing, the mode gate, the HTTP call, and the durable per-step record
(Postgres `fieldpulse_writes`, via jobs.py).

Plan (Nick's decisions, stage 2):
  - `comment`      always: POST /comments on the customer (summary, close score,
                   likelihood, budget, scope, timeline, return appointment,
                   what was not mentioned).
  - `return_task`  only when the extraction has a usable return appointment
                   date AND time (validated, see below): POST /jobs type "task",
                   UNASSIGNED (no `assignments`). Missing/vague date -> the step
                   is `skipped` and the comment says why (flag, don't fabricate).
  - `material_list` (stage 1a, DRY-RUN build) only when the extraction lists
                   materials: POST /material-list, a separate DRAFT list
                   ("Consultation materials (draft): <name>") grouped by area,
                   every line at price 0 / cost 0 and quantity as said (else 1,
                   with "Quantity not mentioned"). No materials, or an older
                   extraction without a `materials` key -> `skipped`.
  No pipeline moves, no custom fields, no consultation job.

Mode (env FIELDPULSE_WRITE_MODE, read on every call):
  dry_run             DEFAULT, and the fallback for any unknown value. Builds and
                      records the exact requests (status `planned`). Sends nothing.
  off                 Does nothing at all (no rows, no requests).
  test_customer_only  Sends ONLY when the job's customer id equals
                      FIELDPULSE_TEST_CUSTOMER_ID. Any other customer -> `skipped`.
  live                Sends for every customer.
Sending also needs FIELDPULSE_WRITE_BASE_URL + FIELDPULSE_WRITE_API_KEY. These
are NEW names on purpose: the old mock FIELDPULSE_BASE_URL can never become a
real write target, and this module never reads FIELDPULSE_READ_* (the picker's
read-only key).

Idempotency: one row per (job_id, step). A `succeeded` step is never sent
again. A `sent` step (request went out, no answer: timeout or dropped
connection) is never re-sent automatically either, because FieldPulse may have
created it and its list filters are ignored (we can't check by reading back).
Only `planned` and `failed` steps are (re)tried, up to _MAX_ATTEMPTS.
A step first recorded in dry_run (row mode = "dry_run") is NEVER sent or
rewritten automatically after the mode changes (reported as "held"); only an
explicit manual send may release it. jobs.py enforces the same rule in SQL.

Manual send (stage 3 admin "Send to FieldPulse"; the worker calls
sync_consultation(..., manual=True)): the SAME mode gate decides. Not allowed
(dry_run / off / test_customer_only for another customer / no write config)
-> nothing is sent and the message says "Not sent: <why>". Allowed -> held
dry-run rows are released (jobs.release_held_fieldpulse_writes) and the plan
is recorded + sent like a normal run, with a higher attempt cap for failed
steps. `succeeded` and `sent` steps are never re-sent, manual or not.

Return appointment: the worker tells the extractor the consultation date
(consultation_reference), so the AI can fill resolved_date / resolved_time.
parse_return_appointment re-checks them deterministically against that date
and the spoken phrase (after the consultation, within 180 days, weekday and
relative words consistent, time actually said); any doubt -> no task, and the
comment says why. Without resolved fields it falls back to explicit dates in
the phrase.

UNVERIFIED against real FieldPulse (stage 4 confirms on the test customer):
  - POST /comments body: {"commentable_type", "commentable_id", "text"}.
    commentable_type value for customers (env FIELDPULSE_COMMENT_COMMENTABLE_TYPE,
    default "customer"; may need a class name like "App\\Customer").
  - POST /jobs type "task" without `assignments` (doc §5 always sent one).
  - `tags` write shape ([<tag id>]); only sent when FIELDPULSE_AI_JOB_TAG_ID is set.
  - POST /material-list body (from the public API reference, never tried on
    our account): {customer_id, is_template: false, title, notes, groupings:
    [{order_number, title, type "group", component_visibility, price_visibility,
    line_items: [{item_id null, title, description, quantity, unit_price,
    unit_cost, type "product", unit_measure}]}]}. status_id is omitted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from observability import report

logger = logging.getLogger("voice_intake.fieldpulse_sync")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

MODES = ("off", "dry_run", "test_customer_only", "live")
SEND_MODES = ("test_customer_only", "live")
DEFAULT_MODE = "dry_run"
DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_COMMENTABLE_TYPE = "customer"   # UNVERIFIED, see module docstring

# FieldPulse rejects longer comments: HTTP 400 "The text may not be greater than 4000
# characters." (confirmed on the real API 2026-11-05; Laravel max:4000 counts characters).
# 3900 leaves a margin; the body is trimmed at a line break and the footer is kept.
MAX_COMMENT_CHARS = 3900
_MAX_LINK_CHARS = 500         # admin link line in the comment footer (longer -> plain fallback)
_FIELD_CHARS = 600            # one extracted text field inside the comment
_MAX_ATTEMPTS = 3
_MANUAL_MAX_ATTEMPTS = 10     # manual "Retry" may go past the automatic cap
_RESPONSE_CHARS = 4000        # stored response body cap
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 15
_TASK_MINUTES = 60
_TASK_JOB_TYPE = "Return Presentation Appointment"   # free text (doc §4)
_MATERIAL_STEP = "material_list"
_MATERIAL_TITLE_CHARS = 255       # FieldPulse max for list/grouping/line titles
_MATERIAL_NOTES_CHARS = 10000     # FieldPulse max for list notes
_MATERIAL_DESC_CHARS = 1000       # our cap for one line description (API max unknown)
_MATERIAL_UNIT_CHARS = 255
_MAX_MATERIAL_LINES = 60          # a draft list; the rest are named in the notes count
_MAX_MATERIAL_GROUPS = 20
_OTHER_AREA = "Other"
_NO_MATERIALS = "No materials mentioned"
_MATERIALS_NOT_CAPTURED = ("Materials were not captured for this consultation "
                           "(processed before material lists were added)")
_HELD_REASON = ("planned during a dry run, so it is never sent automatically; "
                "send it by hand from the admin page")
# Part-of-day windows from doc §5 (local time, hours).
_DAY_PARTS = {"morning": (9, 12), "afternoon": (12, 15), "evening": (15, 17)}


# --- Config -------------------------------------------------------------------

def _config():
    """Read env on every call (so a Render env change + restart is all it takes,
    and tests can patch os.environ). Never reads FIELDPULSE_READ_* or the mock's
    FIELDPULSE_BASE_URL / FIELDPULSE_API_KEY."""
    raw_mode = (os.environ.get("FIELDPULSE_WRITE_MODE") or "").strip().lower()
    mode = raw_mode if raw_mode in MODES else DEFAULT_MODE
    if raw_mode and raw_mode not in MODES:
        logger.warning("Unknown FIELDPULSE_WRITE_MODE %r -> %s", raw_mode, DEFAULT_MODE)
    tz_name = (os.environ.get("FIELDPULSE_TIMEZONE") or "").strip() or DEFAULT_TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — bad tz name must not break the sync
        logger.warning("Unknown FIELDPULSE_TIMEZONE %r -> %s", tz_name, DEFAULT_TIMEZONE)
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    return {
        "mode": mode,
        "base_url": (os.environ.get("FIELDPULSE_WRITE_BASE_URL") or "").strip().rstrip("/"),
        "api_key": (os.environ.get("FIELDPULSE_WRITE_API_KEY") or "").strip(),
        "test_customer_id": _int_id(os.environ.get("FIELDPULSE_TEST_CUSTOMER_ID")),
        "commentable_type": (os.environ.get("FIELDPULSE_COMMENT_COMMENTABLE_TYPE") or "").strip()
                            or DEFAULT_COMMENTABLE_TYPE,
        "tag_id": _int_id(os.environ.get("FIELDPULSE_AI_JOB_TAG_ID")),
        # FieldPulse refuses a material-list line without an item_id (NOT NULL in its DB),
        # so every draft line links to one $0 placeholder item in Mike's item list.
        "material_item_id": _int_id(os.environ.get("FIELDPULSE_MATERIAL_ITEM_ID")),
        "admin_base_url": (os.environ.get("ADMIN_BASE_URL") or "").strip().rstrip("/"),
        "tz": tz,
    }


def _int_id(value):
    """A positive integer id from an int or a digit string, else None.
    Mock ids ("cust_...") and anything else -> None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    s = str(value).strip()
    if re.fullmatch(r"[0-9]{1,18}", s) and int(s) > 0:
        return int(s)
    return None


def _send_gate(cfg, customer_id):
    """(allowed, reason). The ONLY place that decides whether anything is sent."""
    if cfg["mode"] not in SEND_MODES:
        return False, f"mode is {cfg['mode']}"
    if not cfg["base_url"] or not cfg["api_key"]:
        return False, "FIELDPULSE_WRITE_BASE_URL / FIELDPULSE_WRITE_API_KEY are not set"
    if cfg["mode"] == "test_customer_only":
        if cfg["test_customer_id"] is None:
            return False, "FIELDPULSE_TEST_CUSTOMER_ID is not set"
        if customer_id != cfg["test_customer_id"]:
            return False, "test_customer_only mode and this is not the test customer"
    return True, None


# --- Small text helpers ---------------------------------------------------------

def _txt(value, limit=_FIELD_CHARS):
    """Trimmed one-paragraph string, or None. Non-strings (except numbers) -> None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = re.sub(r"[ \t]+", " ", value).strip()
    if not value:
        return None
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def _num(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < lo or value > hi:   # NaN / out of range
        return None
    return int(value) if float(value).is_integer() else value


def _money(value):
    n = _num(value, 0, 10_000_000)
    return f"${n:,.0f}" if n is not None else None


def _yes_no(value):
    v = value.strip().lower() if isinstance(value, str) else None
    return v if v in ("yes", "no") else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _fmt_day(d):
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"


def _fmt_time(dt):
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _fmt_window(start, end):
    return f"{_fmt_day(start.date())}, {_fmt_time(start)}–{_fmt_time(end)}"


def _consultation_day(job, tz):
    """The consultation's local date: the job's created_at in the branch tz."""
    raw = job.get("created_at")
    dt = None
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date()


def consultation_reference(job):
    """(date, timezone name) of the consultation: job created_at in
    FIELDPULSE_TIMEZONE. The worker hands this to the extractor so relative
    return dates can be resolved; build_plan validates against the same day.
    Never raises."""
    try:
        tz = _config()["tz"]
        return _consultation_day(job, tz), getattr(tz, "key", None) or DEFAULT_TIMEZONE
    except Exception as exc:  # noqa: BLE001 — a reference date is optional
        logger.exception("consultation_reference failed")
        report(exc, "consultation_reference failed", job_id=_dict(job).get("id"))
        return None, None


def customer_name(job):
    """Display name from the job's customer snapshot (display only, never sent)."""
    c = _dict(job.get("customer"))
    name = _txt(c.get("display_name"), 120)
    if not name:
        name = _txt(" ".join(p for p in (_txt(c.get("first_name"), 60),
                                         _txt(c.get("last_name"), 60)) if p), 120)
    if name:
        return name
    cid = _int_id(job.get("customer_id"))
    return f"customer #{cid}" if cid else "the customer"


# --- Return appointment parsing ------------------------------------------------

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
             r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_MONTH_DAY = re.compile(r"\b" + _MONTH_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4})\b)?")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_RE
                        + r"\b\.?(?:,?\s+(\d{4})\b)?")
_NUM_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b")
_MERIDIEM = r"(a\.?\s?m\.?|p\.?\s?m\.?)(?![a-z])"
_TIME_RANGE = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*(?:" + _MERIDIEM + r")?\s*(?:-|–|to)\s*"
                         r"(\d{1,2})(?::([0-5]\d))?\s*" + _MERIDIEM)
_TIME_12H = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*" + _MERIDIEM)
_TIME_24H = re.compile(r"\b(1[3-9]|2[0-3]):([0-5]\d)\b")
_NOON = re.compile(r"\bnoon\b")
# "6 or 7 pm", "6/7pm": a choice, not a time.
_TIME_CHOICE = re.compile(r"\b\d{1,2}(?::[0-5]\d)?\s*(?:" + _MERIDIEM + r")?\s*(?:or|/)\s*"
                          r"\d{1,2}(?::[0-5]\d)?\s*" + _MERIDIEM)
_IMPLICIT_YEAR_DAYS = 180    # "November 12" (no year) must be within ~6 months
_RESOLVED_MAX_DAYS = 180     # an AI-resolved return date must be within ~6 months
_ISO_DAY = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_WORD_HOUR = r"(" + "|".join(_NUM_WORDS) + r")"
# Spoken minutes right after a word hour ("five-thirty", "seven forty-five"); hyphen or one
# space. Only the quarter-hours: "five ten" / "five fifty" are too often a height or a price.
_MINUTE_WORDS = {"fifteen": 15, "thirty": 30, "forty five": 45}
_WORD_MIN = r"(fifteen|thirty|forty[ -]five)"
_UNIT_AFTER = r"\s*(?:days?|weeks?|months?|years?)\b"
# A word hour's minutes: ":30" digits or a spoken quarter-hour.
# The lookahead: a longer spoken minute ("thirty-five") isn't read as "thirty"; the match
# falls back to the bare hour, exactly as before word minutes were read (F-61).
_WORD_HOUR_MIN = r"(?::([0-5]\d)|[ -]" + _WORD_MIN + r"\b(?![\s\-‐-―−]+(?:one|two|three|four|five|six|seven|eight|nine)\b))?"
# A bare hour with no am/pm: "at 6", "around 7:30", "around five-thirty", "six o'clock".
# Groups: (1,2) "at 6[:30]"; (3,4,5) "at six[:30| thirty]"; (6,7) "6[:30] o'clock";
# (8,9,10) "six[:30| thirty] o'clock". The lookahead right after a word hour rejects
# "at five thirty days" so backtracking can't fall back to a bare "at five".
_BARE_HOUR = re.compile(r"\b(?:at|around|about|by)\s+(?:(\d{1,2})(?::([0-5]\d))?\b"
                        r"|" + _WORD_HOUR + r"\b(?![ -](?:" + _WORD_MIN[1:] + _UNIT_AFTER + r")"
                        + _WORD_HOUR_MIN + r")"
                        r"(?!" + _UNIT_AFTER + r")"
                        r"|\b(?:(\d{1,2})(?::([0-5]\d))?|" + _WORD_HOUR + _WORD_HOUR_MIN
                        + r")\s*o'?\s?clock\b")
_PM_WORDS = re.compile(r"\b(?:afternoon|evening|tonight|night)\b")
_AM_WORDS = re.compile(r"\bmorning\b")


def _to_24h(hour, minute, meridiem):
    hour = int(hour)
    minute = int(minute or 0)
    if not 1 <= hour <= 12:
        return None
    pm = meridiem.replace(".", "").replace(" ", "").startswith("p")
    if hour == 12:
        hour = 12 if pm else 0
    elif pm:
        hour += 12
    return hour, minute


def _find_dates(text, ref):
    """All explicit calendar dates in text -> (set of dates, error or None, spans).
    A missing year resolves to the first such date on/after the consultation."""
    found = set()
    spans = []
    implicit = []

    def add(y, m, d, span):
        spans.append(span)
        try:
            if y is None:
                cand = date(ref.year, m, d)
                if cand < ref:
                    cand = date(ref.year + 1, m, d)
                implicit.append(cand)
            else:
                y = int(y)
                if y < 100:
                    y += 2000
                cand = date(y, m, d)
        except ValueError:
            return "a date in it is not a real calendar date"
        found.add(cand)
        return None

    errors = []
    for m in _ISO_DATE.finditer(text):
        errors.append(add(m.group(1), int(m.group(2)), int(m.group(3)), m.span()))
    for m in _MONTH_DAY.finditer(text):
        errors.append(add(m.group(3), _MONTHS[m.group(1)[:3]], int(m.group(2)), m.span()))
    for m in _DAY_MONTH.finditer(text):
        if any(s[0] <= m.start() < s[1] for s in spans):
            continue
        errors.append(add(m.group(3), _MONTHS[m.group(2)[:3]], int(m.group(1)), m.span()))
    for m in _NUM_DATE.finditer(text):
        errors.append(add(m.group(3), int(m.group(1)), int(m.group(2)), m.span()))
    err = next((e for e in errors if e), None)
    if not err and any(d > ref + timedelta(days=_IMPLICIT_YEAR_DAYS) for d in implicit):
        err = "the date has no year and is months away (unclear)"
    return found, err, spans


def parse_return_appointment(return_appointment, reference_day, tz):
    """-> (window, reason). window = (start, end) aware datetimes in tz, or None
    with a plain-language reason.

    Two sources, both deterministic checks (on any doubt: no window):
      - the AI's resolved_date / resolved_time (it was told the consultation
        date), validated against the spoken phrase in `details`;
      - otherwise the spoken phrase itself, explicit dates and times only
        ("next Thursday at six" alone is vague: no task)."""
    ra = _dict(return_appointment)
    scheduled = ra.get("scheduled")
    if scheduled not in (True, "true", "True"):
        if scheduled in (False, "false", "False"):
            return None, "no return appointment was scheduled"
        return None, "return appointment was not mentioned"
    details = _txt(ra.get("details"), 300)
    if ra.get("resolved_date") not in (None, ""):
        return _check_resolved(ra.get("resolved_date"), ra.get("resolved_time"),
                               details, reference_day, tz)
    if not details:
        return None, "it was scheduled but no date or time was captured"
    return _parse_explicit(details.lower(), reference_day, tz)


def _mask(text, spans):
    """Blank out the date spans so "11/12" or "2026" can't be read as a time."""
    masked = list(text)
    for a, b in spans:
        masked[a:b] = " " * (b - a)
    return "".join(masked)


def _parse_explicit(text, reference_day, tz):
    """The spoken phrase alone: exactly one explicit calendar date + time."""
    dates, err, spans = _find_dates(text, reference_day)
    if err:
        return None, err
    if not dates:
        return None, "no exact calendar date was given"
    if len(dates) > 1:
        return None, "more than one date was mentioned"
    day = next(iter(dates))
    if day < reference_day:
        return None, "the date is before the consultation"
    if day > reference_day + timedelta(days=400):
        return None, "the date is more than a year out"
    named = [w for w in _WEEKDAYS if re.search(rf"\b{w}\b", text)]
    if named and (len(named) > 1 or named[0] != _WEEKDAYS[day.weekday()]):
        return None, "the weekday doesn't match the date"
    window, reason, _kind, _bare = _parse_time(_mask(text, spans), day, tz)
    return window, reason


def _hour_value(token):
    return _NUM_WORDS[token] if token in _NUM_WORDS else int(token)


def _parse_time(masked, day, tz):
    """Time of day from the (date-masked) phrase.
    -> (window, reason, kind, bare). kind: "exact" (am/pm, 24h, noon, range,
    or a bare hour + morning/evening), "daypart" (morning/afternoon/evening
    only), "bare" (an hour with no am/pm: window None, bare = (h, m)),
    "none" (no time at all) or "bad" (unclear / several times)."""
    def at(hm, minutes=_TASK_MINUTES):
        start = datetime(day.year, day.month, day.day, *hm, tzinfo=tz)
        return start, start + timedelta(minutes=minutes)

    if _TIME_CHOICE.search(masked):
        return None, "more than one time was mentioned", "bad", None
    m = _TIME_RANGE.search(masked)
    if m:
        end_hm = _to_24h(m.group(4), m.group(5), m.group(6))
        start_hm = _to_24h(m.group(1), m.group(2), m.group(3) or m.group(6))
        if not start_hm or not end_hm:
            return None, "the time is not a real clock time", "bad", None
        start = datetime(day.year, day.month, day.day, *start_hm, tzinfo=tz)
        end = datetime(day.year, day.month, day.day, *end_hm, tzinfo=tz)
        if end <= start:
            return None, "the time range is unclear", "bad", None
        return (start, end), None, "exact", None
    times = set()
    for t in _TIME_12H.finditer(masked):
        hm = _to_24h(t.group(1), t.group(2), t.group(3))
        if not hm:
            return None, "the time is not a real clock time", "bad", None
        times.add(hm)
    for t in _TIME_24H.finditer(masked):
        times.add((int(t.group(1)), int(t.group(2))))
    if _NOON.search(masked):
        times.add((12, 0))
    if len(times) > 1:
        return None, "more than one time was mentioned", "bad", None
    if times:
        return at(next(iter(times))), None, "exact", None

    bares = set()
    for b in _BARE_HOUR.finditer(masked):
        g = b.groups()
        tok, minute = next((g[i], g[i + 1] or (g[i + 2] if i in (2, 7) else None))
                           for i in (0, 2, 5, 7) if g[i])
        hour = _hour_value(tok)
        if not 1 <= hour <= 12:
            return None, "the time is not a real clock time", "bad", None
        if minute and not minute.isdigit():
            minute = _MINUTE_WORDS[re.sub(r"[ -]", " ", minute)]
        bares.add((hour, int(minute or 0)))
    if len(bares) > 1:
        return None, "more than one time was mentioned", "bad", None
    pm, am = bool(_PM_WORDS.search(masked)), bool(_AM_WORDS.search(masked))
    if bares:
        bare = next(iter(bares))
        if pm != am:                       # "6 in the evening", "at 9 in the morning"
            return at(_to_24h(bare[0], bare[1], "pm" if pm else "am")), None, "exact", None
        return None, "no exact time was given (needs am/pm)", "bare", bare

    parts = [p for p in _DAY_PARTS if re.search(rf"\b{p}\b", masked)]
    if len(parts) == 1:
        h0, h1 = _DAY_PARTS[parts[0]]
        return ((datetime(day.year, day.month, day.day, h0, tzinfo=tz),
                 datetime(day.year, day.month, day.day, h1, tzinfo=tz)), None, "daypart", None)
    if parts:
        return None, "no exact time was given (needs am/pm)", "bad", None
    return None, "no exact time was given (needs am/pm)", "none", None


def _week_index(day, ref, first_weekday):
    """Calendar weeks between ref's week and day's week (weeks start on
    first_weekday: 0 = Monday, 6 = Sunday)."""
    def start(d):
        return d - timedelta(days=(d.weekday() - first_weekday) % 7)
    return (start(day) - start(ref)).days // 7


def _relative_mismatch(text, day, ref):
    """Cheap consistency checks between relative words in the phrase and the
    resolved day. -> reason or None."""
    delta = (day - ref).days
    if re.search(r"\bday after tomorrow\b", text):
        return None if delta == 2 else "it says 'the day after tomorrow'"
    if re.search(r"\btomorrow\b", text):
        return None if delta == 1 else "it says 'tomorrow'"
    if re.search(r"\b(?:today|tonight)\b", text):
        return "it says 'today' or 'tonight'"
    if re.search(r"\bnext week\b", text):
        if 1 not in (_week_index(day, ref, 0), _week_index(day, ref, 6)):
            return "it says 'next week'"
    if re.search(r"\bthis week\b", text):
        if 0 not in (_week_index(day, ref, 0), _week_index(day, ref, 6)):
            return "it says 'this week'"
    if re.search(r"\bnext month\b", text):
        nxt = (ref.year + (ref.month == 12), ref.month % 12 + 1)
        if (day.year, day.month) != nxt:
            return "it says 'next month'"
    wd = r"(?:" + "|".join(_WEEKDAYS) + r")"
    if re.search(r"\bnext\s+" + wd + r"\b", text) and not 1 <= delta <= 14:
        return "it says 'next <weekday>' but the date is not in the next two weeks"
    if re.search(r"\b(?:this|this coming|coming)\s+" + wd + r"\b", text) and not 1 <= delta <= 7:
        return "it says 'this <weekday>' but the date is not in the coming week"
    return None


def _check_resolved(raw_date, raw_time, details, ref, tz):
    """Validate the AI's resolved_date / resolved_time against the consultation
    date and the spoken phrase. -> (window, reason)."""
    m = _ISO_DAY.fullmatch(raw_date.strip()) if isinstance(raw_date, str) else None
    try:
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except ValueError:
        day = None
    if day is None:
        return None, "the AI's resolved date is not a valid date"
    seen = f"the AI read it as {_fmt_day(day)}"
    if day <= ref:
        return None, f"{seen}, which is not after the consultation"
    if day > ref + timedelta(days=_RESOLVED_MAX_DAYS):
        return None, f"{seen}, which is more than {_RESOLVED_MAX_DAYS} days out"
    if not details:
        return None, f"{seen}, but what was said wasn't captured, so it can't be checked"
    text = details.lower()

    named = [w for w in _WEEKDAYS if re.search(rf"\b{w}\b", text)]
    if len(named) > 1:
        return None, f"{seen}, but more than one weekday was said"
    if named and named[0] != _WEEKDAYS[day.weekday()]:
        return None, f"{seen}, but the weekday said ({named[0].capitalize()}) doesn't match"
    dates, err, spans = _find_dates(text, ref)
    if err:
        return None, f"{seen}, but {err}"
    if len(dates) > 1:
        return None, f"{seen}, but more than one date was said"
    if dates and next(iter(dates)) != day:
        return None, f"{seen}, but that doesn't match the date said"
    rel = _relative_mismatch(text, day, ref)
    if rel:
        return None, f"{seen}, but {rel}"

    window, reason, kind, bare = _parse_time(_mask(text, spans), day, tz)
    if kind == "bad":
        return None, reason
    if raw_time in (None, ""):
        if window:
            return window, None
        return None, "no exact time was given" + (" (needs am/pm)" if kind == "bare" else "")
    t = _HHMM.fullmatch(raw_time.strip()) if isinstance(raw_time, str) else None
    if not t:
        return None, "the AI's resolved time is not a valid time"
    hm = (int(t.group(1)), int(t.group(2)))
    start = datetime(day.year, day.month, day.day, *hm, tzinfo=tz)
    seen_t = f"the AI read the time as {_fmt_time(start)}"
    if kind == "exact":
        if (window[0].hour, window[0].minute) != hm:
            return None, f"{seen_t}, which doesn't match the time said"
        return window, None
    if kind == "daypart":
        if not window[0] <= start < window[1]:
            return None, f"{seen_t}, which is outside the part of the day said"
        return (start, start + timedelta(minutes=_TASK_MINUTES)), None
    if kind == "bare":
        if (hm[0] % 12, hm[1]) != (bare[0] % 12, bare[1]):
            return None, f"{seen_t}, which doesn't match the hour said"
        return (start, start + timedelta(minutes=_TASK_MINUTES)), None
    return None, f"{seen_t}, but no time was said"


# --- Comment text ----------------------------------------------------------------

def _not_mentioned(extraction):
    """Checklist items the extraction left null/unknown: flag, don't fabricate."""
    sales = _dict(extraction.get("sales"))
    scope = _dict(extraction.get("scope"))
    ready = _dict(extraction.get("readiness"))
    ra = _dict(extraction.get("return_appointment"))
    out = []
    if _num(sales.get("close_score_1_10"), 1, 10) is None:
        out.append("Close score (1-10)")
    if _num(sales.get("likelihood_percent"), 0, 100) is None:
        out.append("Likelihood %")
    if not (_txt(sales.get("budget_raw")) or _money(sales.get("budget_low"))
            or _money(sales.get("budget_high"))):
        out.append("Budget")
    if _yes_no(sales.get("waiting_on_financing")) is None:
        out.append("Financing")
    if not _txt(sales.get("concerns")):
        out.append("Concerns")
    if not _txt(scope.get("project_vision")):
        out.append("Project vision")
    for key, label in (("structural_changes", "Structural changes"),
                       ("plumbing_changes", "Plumbing"), ("electrical_changes", "Electrical"),
                       ("lighting_changes", "Lighting")):
        if _yes_no(scope.get(key)) is None:
            out.append(label)
    if not _txt(scope.get("style")):
        out.append("Style")
    if extraction.get("project_type") == "kitchen":
        appl = _dict(scope.get("appliances"))
        unknown = [k for k in ("refrigerator", "range", "dishwasher", "microwave", "hood")
                   if appl.get(k) not in ("new", "reinstall", "none")]
        if unknown:
            out.append("Appliances: " + ", ".join(unknown))
    if not _txt(ready.get("time_frame")):
        out.append("Time frame")
    if not _txt(ready.get("thinking_duration")):
        out.append("How long they've been thinking about it")
    if not _txt(ready.get("work_done_so_far")):
        out.append("Work done so far (plans, products)")
    if ra.get("scheduled") not in (True, False, "true", "false", "True", "False"):
        out.append("Return appointment")
    return out


def _admin_link_line(job, cfg):
    """"Full transcript and details: <ADMIN_BASE_URL>/#admin/<id>", or None when
    ADMIN_BASE_URL is unset. Bounded by _MAX_LINK_CHARS (review-log F-45: an
    oversized ADMIN_BASE_URL / id must not push a FieldPulse text past its
    limit; a cut URL would be a broken link, so it falls back to plain text)."""
    if not cfg["admin_base_url"]:
        return None
    job_id = str(job.get("id") or "")
    link = f"Full transcript and details: {cfg['admin_base_url']}/#admin/{job_id}"
    if len(link) > _MAX_LINK_CHARS:
        ref = f" (consultation {job_id})" if job_id and len(job_id) <= 64 else ""
        link = f"Full transcript and details: in the consultation tool{ref}."
    return link


def build_comment_text(job, extraction, task_window, task_reason, cfg):
    """Plain text for Mike's FieldPulse Comments tab. Capped at MAX_COMMENT_CHARS
    (the body is trimmed at a line break, with a note; the footer is kept)."""
    ex = _dict(extraction)
    tz = cfg["tz"]
    sales = _dict(ex.get("sales"))
    scope = _dict(ex.get("scope"))
    ready = _dict(ex.get("readiness"))
    ra = _dict(ex.get("return_appointment"))
    project = ex.get("project_type") if ex.get("project_type") in ("kitchen", "bath") else None

    lines = [f"AI consultation summary — {_fmt_day(_consultation_day(job, tz))}"]
    lines.append(f"{project.capitalize()} consultation, captured by Voice Consultation Capture."
                 if project else "Captured by Voice Consultation Capture.")
    lines += ["", "SUMMARY", _txt(ex.get("notes_summary"), 2000) or "(No summary was produced.)"]

    sales_lines = []
    score = _num(sales.get("close_score_1_10"), 1, 10)
    if score is not None:
        sales_lines.append(f"- Close score: {score}/10")
    pct = _num(sales.get("likelihood_percent"), 0, 100)
    if pct is not None:
        sales_lines.append(f"- Likelihood: {pct}%")
    lo, hi = _money(sales.get("budget_low")), _money(sales.get("budget_high"))
    rng = f"{lo}–{hi}" if lo and hi else (f"from {lo}" if lo else (f"up to {hi}" if hi else None))
    raw = _txt(sales.get("budget_raw"), 300)
    if rng or raw:
        sales_lines.append("- Budget: " + " ".join(
            p for p in (rng, f'("{raw}")' if rng and raw else raw) if p))
    fin = _yes_no(sales.get("waiting_on_financing"))
    if fin:
        detail = _txt(sales.get("financing_details"), 300)
        sales_lines.append(("- Waiting on financing" if fin == "yes" else
                            "- Not waiting on financing") + (f": {detail}" if detail else ""))
    v = _txt(sales.get("concerns"))
    if v:
        sales_lines.append(f"- Concerns: {v}")
    if sales_lines:
        lines += ["", "SALES READ"] + sales_lines

    scope_lines = []
    v = _txt(scope.get("project_vision"))
    if v:
        scope_lines.append(f"- Vision: {v}")
    for key, det, label in (("structural_changes", "structural_details", "Structural"),
                            ("plumbing_changes", "plumbing_details", "Plumbing"),
                            ("electrical_changes", "electrical_details", "Electrical"),
                            ("lighting_changes", "lighting_details", "Lighting")):
        yn = _yes_no(scope.get(key))
        if yn:
            d = _txt(scope.get(det), 300)
            scope_lines.append(f"- {label}: {'changes wanted' if yn == 'yes' else 'no changes'}"
                               + (f" ({d})" if d else ""))
    appl = _dict(scope.get("appliances")) if project == "kitchen" else {}
    new = [k for k in ("refrigerator", "range", "dishwasher", "microwave", "hood")
           if appl.get(k) == "new"]
    keep = [k for k in ("refrigerator", "range", "dishwasher", "microwave", "hood")
            if appl.get(k) == "reinstall"]
    if new or keep:
        scope_lines.append("- Appliances: " + "; ".join(
            p for p in (("new " + ", ".join(new)) if new else None,
                        ("reinstall " + ", ".join(keep)) if keep else None) if p))
    other = _txt(appl.get("other"), 200)
    if other:
        scope_lines.append(f"- Other appliances: {other}")
    v = _txt(scope.get("style"))
    if v:
        scope_lines.append(f"- Style: {v}")
    if scope_lines:
        lines += ["", "SCOPE HIGHLIGHTS"] + scope_lines

    time_lines = []
    for key, label in (("time_frame", "Time frame"),
                       ("thinking_duration", "Thinking about it for"),
                       ("work_done_so_far", "Done so far")):
        v = _txt(ready.get(key))
        if v:
            time_lines.append(f"- {label}: {v}")
    if time_lines:
        lines += ["", "TIMELINE"] + time_lines

    lines += ["", "RETURN APPOINTMENT"]
    said = _txt(ra.get("details"), 300)
    resolved = ra.get("resolved_date") not in (None, "")
    if task_window:
        how = ("worked out from what was said and the consultation date; "
               if resolved else "")
        lines.append(f"- {_fmt_window(*task_window)} ({how}a FieldPulse task is created for it)")
        if said:
            lines.append(f'- As said: "{said}"')
    elif ra.get("scheduled") in (True, "true", "True"):
        lines.append(f'- Scheduled: "{said}"' if said else "- Scheduled, but no date or time was captured.")
        lines.append(f"- No FieldPulse task was created: {task_reason}. Please add it by hand.")
    elif ra.get("scheduled") in (False, "false", "False"):
        lines.append("- Not scheduled.")
    else:
        lines.append("- Not mentioned.")

    missing = _not_mentioned(ex)
    if missing:
        lines += ["", "NOT MENTIONED IN THE CONSULTATION"] + [f"- {m}" for m in missing]
    raw_flags = ex.get("flags") if isinstance(ex.get("flags"), list) else []
    flags = [f for f in (_txt(x, 300) for x in raw_flags) if f]
    if flags:
        lines += ["", "AI FLAGS"] + [f"- {f}" for f in flags[:20]]

    footer = ["", "Written by AI from the consultation. Please double-check before acting on it."]
    link = _admin_link_line(job, cfg)
    if link:
        footer.append(link)
    footer_text = "\n" + "\n".join(footer)        # blank line before the footer

    body = "\n".join(lines)
    budget = MAX_COMMENT_CHARS - len(footer_text)
    if len(body) > budget:
        note = "\n\n[Trimmed to fit. The full summary is kept in the consultation tool.]"
        cut = body[: max(0, budget - len(note))]
        if "\n" in cut:
            cut = cut[: cut.rfind("\n")]
        body = cut.rstrip() + note
    # The footer is bounded above, so this never cuts in practice; it makes the
    # FieldPulse length limit a guarantee for every input.
    return (body + footer_text)[:MAX_COMMENT_CHARS]


# --- Material list (draft) --------------------------------------------------------

def _material_quantity(value):
    """The quantity as said: a positive number, else None (never guessed)."""
    return _num(value, 1e-9, 1_000_000)


def _material_entries(extraction):
    """-> (entries, skip_reason). entries: [{area, item, details, quantity, unit}]
    with a usable item name. Old extractions (no `materials` key) and anything
    that isn't a list are 'not captured'; nothing usable -> 'No materials mentioned'."""
    raw = extraction.get("materials")
    if not isinstance(raw, list):
        return [], _MATERIALS_NOT_CAPTURED
    out = []
    for m in raw:
        m = _dict(m)
        item = _txt(m.get("item"), _MATERIAL_TITLE_CHARS)
        if not item:
            continue
        qty = _material_quantity(m.get("quantity"))
        out.append({
            "area": _txt(m.get("area"), _MATERIAL_TITLE_CHARS),
            "item": item,
            "details": _txt(m.get("details"), _MATERIAL_DESC_CHARS),
            "quantity": qty,
            "unit": _txt(m.get("unit"), _MATERIAL_UNIT_CHARS) if qty is not None else None,
        })
    if not out:
        return [], _NO_MATERIALS
    return out, None


def _material_line(entry, item_id):
    desc = " ".join(p for p in (
        entry["details"] and (entry["details"] if entry["details"][-1] in ".!?…"
                              else entry["details"] + "."),
        "Quantity not mentioned." if entry["quantity"] is None else None) if p)
    # "id": None on lines and groupings: FieldPulse reads $x["id"] without a default
    # (HTTP 400 'Undefined array key "id"' when absent); null means "create new".
    line = {
        "id": None,
        "item_id": item_id,
        "title": entry["item"],
        "quantity": entry["quantity"] if entry["quantity"] is not None else 1,
        "unit_price": 0,
        "unit_cost": 0,
        "type": "product",
    }
    if desc:
        line["description"] = desc[:_MATERIAL_DESC_CHARS]
    if entry["unit"]:
        # Only with a said quantity: "1 sq ft" from a default 1 would be invented.
        line["unit_measure"] = entry["unit"]
    return line


def build_material_list(job, extraction, cfg):
    """-> (request body, item count) for a DRAFT material list, or (None, reason).
    One grouping per area in the order first mentioned ("Other" last), prices 0,
    capped at _MAX_MATERIAL_LINES lines / _MAX_MATERIAL_GROUPS groups."""
    entries, reason = _material_entries(_dict(extraction))
    if reason:
        return None, reason
    groups = {}                                     # area key -> (title, [lines])
    kept = 0
    for e in entries:
        if kept >= _MAX_MATERIAL_LINES:
            break
        title = e["area"] or _OTHER_AREA
        key = title.casefold()
        if key not in groups:
            if len(groups) >= _MAX_MATERIAL_GROUPS:
                title, key = _OTHER_AREA, _OTHER_AREA.casefold()
            groups.setdefault(key, (title, []))
        groups[key][1].append(_material_line(e, cfg.get("material_item_id")))
        kept += 1
    other = groups.pop(_OTHER_AREA.casefold(), None)
    ordered = list(groups.values()) + ([other] if other else [])
    groupings = [{
        "id": None,
        "order_number": i,
        "title": title[:_MATERIAL_TITLE_CHARS],
        "type": "group",
        "component_visibility": "all",
        "price_visibility": "all",
        "line_items": lines,
    } for i, (title, lines) in enumerate(ordered, start=1)]

    notes = (f"Draft from the consultation recording on "
             f"{_fmt_day(_consultation_day(job, cfg['tz']))}. Materials as discussed; "
             f"prices and quantities to be confirmed by the office.")
    if len(entries) > kept:
        notes += (f" Only the first {kept} of {len(entries)} materials are listed; "
                  f"the rest are in the consultation tool.")
    link = _admin_link_line(job, cfg)
    if link:
        notes += "\n" + link
    body = {
        "customer_id": _int_id(job.get("customer_id")),
        "is_template": False,
        "title": f"Consultation materials (draft): {customer_name(job)}"[:_MATERIAL_TITLE_CHARS],
        "notes": notes[:_MATERIAL_NOTES_CHARS],
        "groupings": groupings,
    }
    return body, kept


# --- Plan ----------------------------------------------------------------------------

def build_plan(job, extraction, cfg):
    """The exact requests for this consultation, as a list of steps:
    {"step", "request": {method, path, body} | None, "skip_reason", "description",
     "window"}. Pure: no I/O."""
    cid = _int_id(job.get("customer_id"))
    tz = cfg["tz"]
    ex = _dict(extraction)
    ref = _consultation_day(job, tz)
    window, reason = parse_return_appointment(ex.get("return_appointment"), ref, tz)
    name = customer_name(job)

    text = build_comment_text(job, ex, window, reason, cfg)
    steps = [{
        "step": "comment",
        "request": {"method": "POST", "path": "/comments", "body": {
            "commentable_type": cfg["commentable_type"],
            "commentable_id": cid,
            "text": text,
        }},
        "skip_reason": None,
        "description": f"Add a comment to {name}",
        "window": None,
    }]

    if window:
        start, end = window
        said = _txt(_dict(ex.get("return_appointment")).get("details"), 300)
        project = ex.get("project_type") if ex.get("project_type") in ("kitchen", "bath") else None
        notes = (f"Return presentation appointment set during the consultation on "
                 f"{_fmt_day(ref)}: {_fmt_window(start, end)}."
                 + (f' Customer said: "{said}".' if said else "")
                 + " Created by Voice Consultation Capture (AI). Unassigned.")
        body = {
            "customer_id": cid,
            "type": "task",
            "job_type": _TASK_JOB_TYPE,
            "subtitle": (f"{project.capitalize()} — return appointment" if project
                         else "Return appointment"),
            "status": 1,
            "billing": 1,
            "start_time": int(start.timestamp()),
            "end_time": int(end.timestamp()),
            "notes": notes,
        }
        if cfg["tag_id"] is not None:
            body["tags"] = [cfg["tag_id"]]          # UNVERIFIED write shape
        steps.append({
            "step": "return_task",
            "request": {"method": "POST", "path": "/jobs", "body": body},
            "skip_reason": None,
            "description": f"Create a task for {_fmt_window(start, end)}",
            "window": window,
        })
    else:
        steps.append({
            "step": "return_task",
            "request": None,
            "skip_reason": reason,
            "description": f"No task: {reason}",
            "window": None,
        })

    material, info = build_material_list(job, ex, cfg)
    if material and not cfg.get("material_item_id"):
        material, info = None, ("FIELDPULSE_MATERIAL_ITEM_ID is not set (FieldPulse needs a "
                                "placeholder item for material-list lines)")
    if material:
        n_groups = len(material["groupings"])
        steps.append({
            "step": _MATERIAL_STEP,
            # Plural: the docs say /material-list, but the API only routes /material-lists
            # (the singular path answers 403 "Missing Authentication Token").
            "request": {"method": "POST", "path": "/material-lists", "body": material},
            "skip_reason": None,
            "description": (f"Create a draft material list for {name} ({info} item"
                            f"{'' if info == 1 else 's'} in {n_groups} group"
                            f"{'' if n_groups == 1 else 's'})"),
            "window": None,
            "items": info,
        })
    else:
        steps.append({
            "step": _MATERIAL_STEP,
            "request": None,
            "skip_reason": info,
            "description": f"No material list: {info}",
            "window": None,
            "items": 0,
        })
    return steps


# --- Transport --------------------------------------------------------------------------

class _NotDelivered(Exception):
    """The request certainly did not reach FieldPulse (safe to retry)."""


class _OutcomeUnknown(Exception):
    """The request may have reached FieldPulse (never auto-retry)."""


def _send(request, cfg):
    """POST one request to the real FieldPulse write target. Returns
    (http_status, parsed JSON or text). Only reachable through the send gate."""
    if cfg["mode"] not in SEND_MODES or not cfg["base_url"] or not cfg["api_key"]:
        raise _NotDelivered("send refused: not in a send mode")   # belt and braces
    if request["method"] != "POST":
        raise _NotDelivered("only POST is used")
    import requests  # local import: dry_run/off never touch the HTTP stack
    try:
        resp = requests.post(
            cfg["base_url"] + request["path"],
            json=request["body"],
            headers={"x-api-key": cfg["api_key"], "Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            allow_redirects=False,
        )
    except requests.ConnectTimeout as exc:
        raise _NotDelivered(f"connect timeout: {type(exc).__name__}") from None
    except requests.RequestException as exc:
        # Could have been sent (read timeout, reset mid-response): never re-send.
        raise _OutcomeUnknown(f"{type(exc).__name__}: no response") from None
    try:
        payload = resp.json()
    except ValueError:
        payload = resp.text[:_RESPONSE_CHARS]
    return resp.status_code, payload


def _record_id(payload):
    """New record id from the wrapper, read defensively (doc §1)."""
    if not isinstance(payload, dict):
        return None
    for holder in (payload.get("response"), payload, payload.get("data")):
        if isinstance(holder, dict) and holder.get("id") not in (None, "", False):
            return str(holder["id"])
    return None


def _clip_response(status, payload):
    text = json.dumps(payload, default=str)
    if len(text) > _RESPONSE_CHARS:
        return {"http_status": status, "body_truncated": text[:_RESPONSE_CHARS]}
    return {"http_status": status, "body": payload}


def _error_text(status, payload):
    msg = None
    if isinstance(payload, dict):
        msg = payload.get("message") or payload.get("error_message") or payload.get("errors")
        if msg is None and payload.get("error") not in (None, False):
            msg = payload.get("error")
    elif isinstance(payload, str):
        msg = payload
    msg = json.dumps(msg, default=str) if not isinstance(msg, str) and msg is not None else msg
    return f"HTTP {status}" + (f": {msg[:300]}" if msg else "")


# --- Store (jobs.py owns the SQL) --------------------------------------------------------

_STORE = None   # tests inject a fake with the same four functions


def _store():
    if _STORE is not None:
        return _STORE
    import jobs
    return jobs


# --- Entry point ----------------------------------------------------------------------------

def sync_consultation(job, extraction, manual=False):
    """Plan, record, and (only when the env allows) send this consultation's
    FieldPulse writes. NEVER raises: any problem becomes status "failed" in the
    returned summary, and the consultation itself stays saved. `manual=True`
    is the admin's explicit send: it may release held dry-run steps, but only
    if the mode gate allows sending (see module docstring)."""
    try:
        out = _sync(job, extraction, manual)
    except Exception as exc:  # noqa: BLE001 — the write must never fail the job
        logger.exception("[%s] FieldPulse sync crashed", job.get("id"))
        report(exc, "FieldPulse sync crashed", job_id=job.get("id"), manual=manual)
        out = {"status": "failed", "mode": None,
                "message": "FieldPulse sync hit an internal error. Nothing more was sent. "
                           "The consultation is saved.",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}", "steps": []}
    if manual:
        out["manual"] = True
    return out


def _not_sent_reason(cfg, why):
    """Plain reason for a manual send that the gate refused."""
    if cfg["mode"] not in SEND_MODES:
        return f"write mode is {cfg['mode']}"
    return why


def _sync(job, extraction, manual=False):
    cfg = _config()
    job_id = job["id"]
    name = customer_name(job)
    base = {"mode": cfg["mode"], "customer_id": _int_id(job.get("customer_id")),
            "customer_name": name,
            "synced_at": datetime.now(timezone.utc).isoformat()}

    if cfg["mode"] == "off":
        logger.info("[%s] FieldPulse sync off", job_id)
        return {**base, "status": "off", "steps": [],
                "message": ("Not sent: write mode is off. Nothing was sent to FieldPulse."
                            if manual else
                            "FieldPulse sync is off. Nothing was sent to FieldPulse.")}

    cid = base["customer_id"]
    if cid is None:
        logger.info("[%s] no real FieldPulse customer id (%r): nothing planned",
                    job_id, job.get("customer_id"))
        return {**base, "status": "skipped", "steps": [],
                "message": "Nothing was sent to FieldPulse: this consultation isn't linked "
                           "to a FieldPulse customer."}

    plan = build_plan(job, extraction, cfg)
    allowed, why = _send_gate(cfg, cid)
    store = _store()
    if manual and allowed:
        # The explicit manual action: held dry-run steps may now be sent.
        store.release_held_fieldpulse_writes(job_id, cfg["mode"])
    max_attempts = _MANUAL_MAX_ATTEMPTS if manual else _MAX_ATTEMPTS
    existing = {r["step"]: r for r in store.list_fieldpulse_writes(job_id)}

    for step in plan:
        name_ = step["step"]
        prior = existing.get(name_)
        if prior and prior["status"] in ("succeeded", "sent"):
            continue                                 # never re-send
        if prior and prior.get("mode") == "dry_run" and cfg["mode"] != "dry_run":
            # Planned during a dry run: never sent (or rewritten) automatically
            # after the mode changes. Only an explicit manual send (stage 3
            # admin) may release it. jobs.py enforces the same rule in SQL.
            continue
        if step["skip_reason"]:
            store.record_fieldpulse_write(job_id, name_, "skipped", cfg["mode"],
                                          request=None, error=step["skip_reason"])
            continue
        if not allowed:
            status = "planned" if cfg["mode"] == "dry_run" else "skipped"
            store.record_fieldpulse_write(job_id, name_, status, cfg["mode"],
                                          request=step["request"],
                                          error=None if status == "planned" else why)
            continue
        store.record_fieldpulse_write(job_id, name_, "planned", cfg["mode"],
                                      request=step["request"], error=None)
        if not store.claim_fieldpulse_write(job_id, name_, max_attempts):
            continue                                 # someone else has it / out of attempts
        try:
            status_code, payload = _send(step["request"], cfg)
        except _NotDelivered as exc:
            store.finish_fieldpulse_write(job_id, name_, "failed", error=str(exc))
            report(message=f"FieldPulse {name_} not delivered", level="warning",
                   job_id=job_id, step=name_, mode=cfg["mode"], manual=manual)
            continue
        except _OutcomeUnknown as exc:
            report(message=f"FieldPulse {name_} sent, outcome unknown", level="warning",
                   job_id=job_id, step=name_, mode=cfg["mode"], manual=manual)
            store.finish_fieldpulse_write(
                job_id, name_, "sent",
                error=f"{exc}. It may or may not have been created; check FieldPulse "
                      f"before retrying (it will not be re-sent automatically).")
            continue
        ok = 200 <= status_code < 300 and not (isinstance(payload, dict)
                                               and payload.get("error") not in (None, False))
        rec = _record_id(payload) if ok else None
        store.finish_fieldpulse_write(
            job_id, name_, "succeeded" if ok else "failed",
            response=_clip_response(status_code, payload), record_id=rec,
            error=None if ok else _error_text(status_code, payload))
        logger.info("[%s] FieldPulse %s -> HTTP %s (%s)", job_id, name_, status_code,
                    "ok" if ok else "failed")
        if not ok:
            # Fixed text + tags only: FieldPulse's error body may echo customer data.
            report(message=f"FieldPulse {name_} failed (HTTP {status_code})", level="warning",
                   job_id=job_id, step=name_, http_status=status_code, mode=cfg["mode"],
                   manual=manual)

    rows = {r["step"]: r for r in store.list_fieldpulse_writes(job_id)}
    out = _summarize(base, cfg, plan, rows, allowed, why)
    if manual and not allowed:
        out["message"] = (f"Not sent: {_not_sent_reason(cfg, why)}. "
                          "Nothing was sent to FieldPulse.")
    return out


def _summarize(base, cfg, plan, rows, allowed, why):
    name = base["customer_name"]
    steps = []
    for step in plan:
        row = rows.get(step["step"]) or {}
        status = row.get("status") or ("skipped" if step["skip_reason"] else "planned")
        error = row.get("error")
        if (cfg["mode"] != "dry_run" and row.get("mode") == "dry_run"
                and status in ("planned", "failed")):
            status, error = "held", _HELD_REASON
        steps.append({
            "step": step["step"],
            "status": status,
            "description": step["description"],
            "record_id": row.get("fieldpulse_record_id"),
            "error": error,
            "attempts": row.get("attempts", 0),
        })
    comment = next(s for s in plan if s["step"] == "comment")
    task = next(s for s in plan if s["step"] == "return_task")
    when = _fmt_window(*task["window"]) if task["window"] else None
    no_task = f" No task: {task['skip_reason']}." if task["skip_reason"] else ""
    mat = next((s for s in plan if s["step"] == _MATERIAL_STEP), None)
    n_mat = mat["items"] if mat and mat["request"] else 0
    mat_text = f"draft material list ({n_mat} item{'' if n_mat == 1 else 's'})"

    active = [s for s in steps if s["status"] != "skipped" or s["step"] == "comment"]
    if cfg["mode"] == "dry_run":
        status = "dry_run"
        message = (f"Dry run: nothing sent to FieldPulse yet. Would add a comment to {name}"
                   + (f" and create a task for {when}." if when else ".") + no_task
                   + (f" Would also create a {mat_text}." if n_mat else ""))
    elif not allowed:
        status = "blocked"
        message = f"Nothing was sent to FieldPulse ({why})."
    else:
        done = [s for s in active if s["status"] == "succeeded"]
        held = [s for s in active if s["status"] == "held"]
        if held and not done and len(held) == len(active):
            status = "blocked"
            message = f"Nothing was sent to FieldPulse: {_HELD_REASON}."
        elif len(done) == len(active):
            status = "succeeded"
            message = (f"Added a comment to {name}"
                       + (f" and created a task for {when}" if when else "")
                       + " in FieldPulse." + no_task
                       + (f" Created a {mat_text}." if n_mat else ""))
        else:
            status = "partial" if done else "failed"
            bad = "; ".join(f"{s['step'].replace('_', ' ')}: {s['status']}"
                            + (f" ({s['error']})" if s["error"] else "")
                            for s in active if s["status"] != "succeeded")
            message = ("FieldPulse update " + ("partly failed" if done else "failed")
                       + f": {bad}. The consultation itself is saved.")
    return {**base, "status": status, "message": message, "steps": steps,
            "comment_text": comment["request"]["body"]["text"],
            "task_skipped_reason": task["skip_reason"]}
