"""Read-only customer list for the app's customer picker.

One public function, `list_customers()`, returns every customer as a small
dict (id, names, phone, email, company_name, address, created_at), sorted newest-added first, with
customers that have no usable date at the end.

Where it reads from:
  - FIELDPULSE_READ_BASE_URL + FIELDPULSE_READ_API_KEY when set (real
    FieldPulse, read-only).
  - Otherwise FIELDPULSE_BASE_URL + FIELDPULSE_API_KEY (the mock), exactly as
    /app/customers did before, so local dev and the current deploy keep working.

Safety: this module can only issue HTTP GETs. Its one network call,
`_http_get`, hard-codes the verb "GET" (stdlib http.client); there is no
helper that takes a method. The writers (consultation_writer.py,
fieldpulse_writer.py) never read FIELDPULSE_READ_*.

Response shapes handled:
  - Mock:  {"data": [...], "meta": {"total_pages": N, ...}}
  - Real:  {"error": false, "response": [...]} or
           {"error": false, "response": {"<list key>": [...], ...pagination}}
    Real pagination and the real created-date field name are UNVERIFIED
    (docs/FIELDPULSE_API.md). Run probe_fieldpulse_customers.py first.

Completeness (never a partial list as success):
  - Recognised paging metadata (has_more / next* / total_pages|last_page /
    total|total_count, ints or digit strings) is honoured. If any signal says
    "more", we fetch the next page.
  - With NO recognised metadata we keep fetching until a verified terminal
    page: an empty page, or a page that adds no new ids (which is also what an
    API that ignores `page` looks like).
  - An empty page while metadata says more remain, a repeated page while
    metadata exists, a reported current_page that isn't the one we asked for,
    or more than _MAX_PAGES pages -> CustomerSourceError.

Errors: messages are fixed, status-based text. Upstream bodies, the base URL
and its credentials/query are never echoed (to the browser or to logs).
Key names in shape errors come only from an allowlist (`safe_keys`).

Field types: display_name / first_name / last_name / phone / email /
company_name / address are normalised to str or None. `address` is one line
built here from the real flat keys or the mock's nested dict (`_address`). Numbers become strings; booleans, objects and
arrays become None (the record is kept, the bad field is dropped).

Dates: created_at is "YYYY-MM-DDTHH:MM:SSZ" for instants, "YYYY-MM-DD" for
date-only values (calendar precision kept, no timezone shift), or None.

Time budget (review-log F-2 / D-2 / F-14): one wall-clock deadline,
_TOTAL_BUDGET_S after list_customers() starts, shared by every page and retry.
`_http_get` gives EVERY blocking socket operation only the time left before
that deadline, and starts none after it: TCP connect (per address), TLS
handshake (CPython applies the socket timeout to the whole handshake), the
request send, and every recv of the status line, headers, chunk framing and
body (through `_DeadlineReader`, however slowly the bytes trickle and whatever
the framing: Content-Length, chunked, or Connection: close). So:

    elapsed <= _TOTAL_BUDGET_S + DNS lookup time + JSON parse/sort CPU

The one phase NOT bounded here is DNS (socket.getaddrinfo, the OS resolver's
own timeouts; glibc default 5s x 2 attempts per nameserver if DNS is down).
No threads, no signals. With the 20s budget this stays under Gunicorn's 30s
default worker timeout unless DNS itself hangs, so a slow upstream returns a
readable 504 (with CORS headers) instead of a worker kill. HTTP(S)_PROXY env
vars are NOT honoured (none on Render).
"""

import functools
import http.client
import io
import json
import logging
import math
import os
import re
import socket
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

try:                                    # same CA bundle requests used
    import certifi
    _CA_FILE = certifi.where()
except ImportError:                     # fall back to the system store
    _CA_FILE = None

logger = logging.getLogger("voice_intake.customer_source")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


class CustomerSourceError(Exception):
    """The customer list couldn't be produced. `status` is a short machine
    label and `http_status` is what the route should return. The message is
    always fixed text, safe to show and log."""

    def __init__(self, message, status="fieldpulse_error", http_status=502):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


# --- Tunables ---------------------------------------------------------------

_PER_PAGE = 200
_MAX_PAGES = 50
# Leaves ~10s of the 30s Gunicorn default for Clerk verification and the rest
# of the request.
_TOTAL_BUDGET_S = 20.0
_CONNECT_TIMEOUT_S = 5.0        # per address, and never more than the time left
_READ_TIMEOUT_S = 10.0          # max silence per recv (docs §1), never more than the time left
_MIN_ATTEMPT_S = 1.0            # don't start a request with less time than this left
_RETRY_MIN_REMAINING_S = 2.0    # only retry a timeout if this much time is left
_READ_CHUNK = 65536
_MAX_BODY_BYTES = 25 * 1024 * 1024

# Where the customer list may sit inside a real {"response": {...}} dict.
LIST_KEYS = ("data", "customers", "items", "results", "records", "rows")
# Created-date field names to try, in order. Only created_at is confirmed (mock,
# and estimates in the real API); the rest are guesses for the real customer.
DATE_FIELDS = (
    "created_at", "createdAt", "created", "date_created",
    "created_on", "creation_date", "date_added",
)
_TOTAL_PAGES_KEYS = ("total_pages", "last_page", "page_count", "totalPages", "lastPage")
_TOTAL_COUNT_KEYS = ("total", "total_count", "totalCount", "total_records")
_HAS_MORE_KEYS = ("has_more", "hasMore", "has_next", "hasNext")
_NEXT_KEYS = ("next_page_url", "next", "next_page", "nextPage")
_CURRENT_PAGE_KEYS = ("current_page", "currentPage")
_META_SUBKEYS = ("meta", "pagination", "paging", "links")
_TEXT_FIELDS = ("display_name", "first_name", "last_name", "email")

# The ONLY key names diagnostics may print (shape errors here, and the probe).
# Anything else is counted, never named: a key can itself be personal or
# secret text (review-log F-21). Extend this list deliberately, by hand.
KNOWN_KEYS = frozenset(
    LIST_KEYS + DATE_FIELDS + _TOTAL_PAGES_KEYS + _TOTAL_COUNT_KEYS + _HAS_MORE_KEYS
    + _NEXT_KEYS + _CURRENT_PAGE_KEYS + _META_SUBKEYS + _TEXT_FIELDS + (
        # wrapper / errors
        "error", "response", "message", "errors", "status", "success",
        # paging (Laravel-style and common variants)
        "page", "per_page", "perPage", "limit", "offset", "count", "from", "to",
        "first_page_url", "last_page_url", "prev_page_url", "prev", "previous",
        "path", "cursor", "next_cursor", "prev_cursor", "self", "first", "last",
        # customer fields from docs/FIELDPULSE_API.md §7 + common ones
        "id", "phone", "phone_e164", "notes", "address", "address_1", "address_2",
        "city", "state", "zip_code", "country", "company_name", "pipeline_status_id",
        "lead_source_id", "lead_source", "customer_type", "type", "tags",
        "billing_address_1", "billing_address_2", "billing_city", "billing_state",
        "billing_zip_code",
        # other date-like names worth recognising in the probe
        "updated_at", "updatedAt", "deleted_at", "createdDate", "created_date",
        "dateCreated", "modified_at", "last_activity_at",
    )
)


def safe_keys(d, limit=20):
    """Key names of a dict for diagnostics, allowlist only: names in
    KNOWN_KEYS are listed (sorted, up to `limit`), every other key is only
    counted. Shared with probe_fieldpulse_customers.py."""
    names = {str(k) for k in d.keys()}
    out = sorted(names & KNOWN_KEYS)[:limit]
    hidden = len(names) - len(out)
    if hidden:
        out.append(f"(+{hidden} unlisted key{'s' if hidden != 1 else ''})")
    return out


# --- Public API -------------------------------------------------------------

def list_customers():
    """Return all customers, newest-added first. Raises CustomerSourceError."""
    base, key, label = _config()
    deadline = time.monotonic() + _TOTAL_BUDGET_S

    records = []
    seen_ids = set()
    page = 1
    while True:
        payload = _fetch_page(base, key, label, page, deadline)
        items, containers = _extract_items(payload)
        _check_reported_page(containers, page)

        new_count = 0
        for pos, item in enumerate(items):
            canon = _canonical_id(item, page, pos)
            if canon in seen_ids:
                continue
            seen_ids.add(canon)
            records.append(item)
            new_count += 1

        more = _more_signal(containers, page, len(records))
        if not items:
            if more:
                raise CustomerSourceError(
                    f"FieldPulse returned an empty page {page} but its paging info says "
                    f"more customers remain. Refusing to return a partial list.",
                )
            break                       # verified terminal: empty page
        if new_count == 0:
            if more is None:
                break                   # verified terminal: no metadata, nothing new
            raise CustomerSourceError(
                f"FieldPulse pagination did not advance: page {page} returned only "
                f"customers already seen. The paging parameters may be wrong; "
                f"run probe_fieldpulse_customers.py."
            )
        if more is False:
            break
        # more is True, or unknown (keep going until a terminal page).
        if page >= _MAX_PAGES:
            raise CustomerSourceError(
                f"FieldPulse customer list still had more pages after {_MAX_PAGES} "
                f"pages ({len(records)} customers). Refusing to return a partial list."
            )
        page += 1

    customers = _sorted_simplified(records)
    logger.info(
        "customer list: source=%s pages=%d customers=%d", label, page, len(customers)
    )
    return customers


# --- Config -----------------------------------------------------------------

def _config():
    """(base_url, api_key, label). Read at call time, never at import."""
    read_base = (os.environ.get("FIELDPULSE_READ_BASE_URL") or "").strip()
    read_key = (os.environ.get("FIELDPULSE_READ_API_KEY") or "").strip()
    if read_base and not read_key:
        raise CustomerSourceError(
            "FIELDPULSE_READ_BASE_URL is set but FIELDPULSE_READ_API_KEY is not.",
            status="config_error", http_status=500,
        )
    if read_key and not read_base:
        raise CustomerSourceError(
            "FIELDPULSE_READ_API_KEY is set but FIELDPULSE_READ_BASE_URL is not.",
            status="config_error", http_status=500,
        )
    if read_base:
        return read_base.rstrip("/"), read_key, "fieldpulse-read"
    # Unchanged fallback: same defaults the old /app/customers route used.
    base = os.environ.get("FIELDPULSE_BASE_URL", "http://localhost:5000").rstrip("/")
    key = os.environ.get("FIELDPULSE_API_KEY", "dev-test-key-12345")
    return base, key, "mock"


# --- Transport (GET only) ---------------------------------------------------

def _remaining(deadline):
    return deadline - time.monotonic()


def _check_deadline(deadline, label, page):
    if _remaining(deadline) <= 0:
        raise _deadline_error(label, page)


def _fetch_page(base, key, label, page, deadline):
    """GET one page and parse it, inside the shared deadline. One retry on a
    timeout/connection error if time allows (still inside the deadline)."""
    url = f"{base}/customers"
    params = {"page": page, "per_page": _PER_PAGE, "limit": _PER_PAGE}
    headers = {"x-api-key": key, "Accept": "application/json",
               "Accept-Encoding": "identity"}

    attempt = 0
    while True:
        attempt += 1
        if _remaining(deadline) < _MIN_ATTEMPT_S:
            raise _deadline_error(label, page)
        try:
            status, body = _http_get(url, params=params, headers=headers, deadline=deadline)
            break
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # OSError covers socket.timeout, refused/reset, DNS and TLS errors.
            if attempt == 1 and _remaining(deadline) >= _RETRY_MIN_REMAINING_S:
                logger.warning(
                    "GET %s /customers page %d failed (%s); retrying once",
                    label, page, type(exc).__name__,
                )
                continue
            if isinstance(exc, socket.timeout) or _remaining(deadline) <= 0:
                raise _deadline_error(label, page)
            raise CustomerSourceError(
                f"Could not reach FieldPulse ({label}): {type(exc).__name__}",
                status="fieldpulse_unreachable",
            )

    _check_deadline(deadline, label, page)
    if status != 200:
        raise _status_error(status, body)
    try:
        payload = json.loads(body)
    except ValueError:
        raise CustomerSourceError("FieldPulse returned a non-JSON body for /customers")
    _check_deadline(deadline, label, page)
    return payload


def _http_get(url, params, headers, deadline):
    """The module's only network call: one HTTP GET (verb fixed below).
    Returns (status, body bytes). Every blocking socket step gets only the
    time left before `deadline` (see the module docstring for the bound).
    Raises OSError (incl. socket.timeout) / http.client.HTTPException /
    ValueError on transport problems; CustomerSourceError for a bad base URL
    or an oversized body. Exception text may contain the host, so callers
    report only the exception class."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CustomerSourceError(
            "The FieldPulse base URL is not a valid http(s) URL.",
            status="config_error", http_status=500,
        )
    netloc = parts.netloc.rpartition("@")[2]        # never send URL userinfo
    https = parts.scheme == "https"
    ctx = ssl.create_default_context(cafile=_CA_FILE) if https else None
    conn = (http.client.HTTPSConnection(netloc, context=ctx) if https
            else http.client.HTTPConnection(netloc))
    conn.response_class = functools.partial(_DeadlineResponse, deadline=deadline)
    query = "&".join(q for q in (parts.query, urlencode(params)) if q)
    target = (parts.path or "/") + (f"?{query}" if query else "")
    resp = None
    try:
        conn.sock = _connect(conn.host, conn.port, deadline)
        if https:
            conn.sock.settimeout(_time_left(deadline))
            conn.sock = ctx.wrap_socket(conn.sock, server_hostname=conn.host)
        conn.sock.settimeout(_time_left(deadline))
        conn.request("GET", target, headers=headers)
        resp = conn.getresponse()
        chunks, size = [], 0
        while True:
            chunk = resp.read(_READ_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_BODY_BYTES:
                raise CustomerSourceError("FieldPulse /customers response was too large")
            chunks.append(chunk)
        return resp.status, b"".join(chunks)
    finally:
        if resp is not None:
            resp.close()                    # releases the socket reference
        conn.close()


def _time_left(deadline, cap=None):
    """Seconds left (optionally capped) for the next socket step; raises
    socket.timeout once the deadline has passed, so no step starts late."""
    left = _remaining(deadline)
    if left <= 0:
        raise socket.timeout("customer list deadline reached")
    return min(left, cap) if cap else left


def _connect(host, port, deadline):
    """TCP connect, trying each resolved address with only the time left."""
    last = None
    for family, kind, proto, _, addr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        timeout = _time_left(deadline, _CONNECT_TIMEOUT_S)
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(addr)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise last or OSError("no address to connect to")


class _DeadlineReader(io.RawIOBase):
    """Raw stream over the socket used for the WHOLE response (status line,
    headers, chunk sizes, body). Each recv waits at most min(_READ_TIMEOUT_S,
    time left) and none starts after the deadline, so a byte-at-a-time
    trickle can't extend the request past it."""

    def __init__(self, sock, deadline, socket_ref):
        super().__init__()
        self._sock = sock
        self._deadline = deadline
        # http.client's own makefile() reader. Unused for reading, but it holds
        # the socket's io reference: on a Connection: close response
        # http.client closes the connection's socket right away and relies on
        # that reference to keep the fd open until the body is read.
        self._socket_ref = socket_ref

    def readable(self):
        return True

    def readinto(self, buf):
        self._sock.settimeout(_time_left(self._deadline, _READ_TIMEOUT_S))
        return self._sock.recv_into(buf)

    def close(self):
        if not self.closed:
            self._socket_ref.close()
        super().close()


class _DeadlineResponse(http.client.HTTPResponse):
    """http.client response that reads through _DeadlineReader."""

    def __init__(self, sock, *args, deadline, **kwargs):
        super().__init__(sock, *args, **kwargs)
        self.fp = io.BufferedReader(_DeadlineReader(sock, deadline, self.fp))


def _status_error(status, body):
    """Fixed, status-based message. The upstream body is never echoed; it is
    only compared against the exact API Gateway wrong-path message."""
    if status == 401:
        msg = "FieldPulse rejected the read API key (HTTP 401)."
    elif status == 403 and _is_missing_auth_token(body):
        msg = ("FieldPulse path not found ('Missing Authentication Token', HTTP 403). "
               "Check FIELDPULSE_READ_BASE_URL; see docs/FIELDPULSE_API.md §10.")
    elif status == 403:
        msg = "FieldPulse refused the read API key (HTTP 403): bad, revoked, or not permitted."
    elif status == 404:
        msg = "FieldPulse /customers was not found (HTTP 404). Check the base URL."
    elif status == 429:
        msg = "FieldPulse rate-limited the customer list (HTTP 429). Try again shortly."
    elif 500 <= status <= 599:
        msg = f"FieldPulse had a server error (HTTP {int(status)}). Try again shortly."
    else:
        msg = f"FieldPulse returned HTTP {int(status)} for /customers."
    return CustomerSourceError(msg)


def _is_missing_auth_token(body):
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and data.get("message") == "Missing Authentication Token"


def _deadline_error(label, page):
    return CustomerSourceError(
        f"FieldPulse ({label}) did not return the customer list within "
        f"{int(_TOTAL_BUDGET_S)}s (stopped at page {page}). Try again; if it keeps "
        f"happening the service may be cold-starting or slow.",
        status="fieldpulse_timeout", http_status=504,
    )


# --- Shape handling ---------------------------------------------------------

def _shape_hint(payload):
    if isinstance(payload, dict):
        return f"object with keys {safe_keys(payload)}"
    return type(payload).__name__


def _extract_items(payload):
    """Return (items, containers). `containers` are the dicts that may carry
    pagination fields. Raises CustomerSourceError on any unknown shape."""
    if isinstance(payload, list):
        return payload, []
    if not isinstance(payload, dict):
        raise CustomerSourceError(
            f"FieldPulse /customers returned an unrecognized shape ({_shape_hint(payload)})"
        )

    err = payload.get("error")
    if err not in (None, False, 0, ""):
        raise CustomerSourceError(
            "FieldPulse /customers reported an error in a 200 response "
            f"({_shape_hint(payload)})"
        )

    if "response" in payload:                       # real FieldPulse wrapper
        resp = payload["response"]
        if isinstance(resp, list):
            return resp, [payload]
        if isinstance(resp, dict):
            for k in LIST_KEYS:
                if isinstance(resp.get(k), list):
                    return resp[k], [payload, resp]
        raise CustomerSourceError(
            "FieldPulse /customers 'response' holds no customer list "
            f"(response is {_shape_hint(resp)})"
        )

    if isinstance(payload.get("data"), list):       # mock
        return payload["data"], [payload]

    raise CustomerSourceError(
        f"FieldPulse /customers returned an unrecognized shape ({_shape_hint(payload)})"
    )


def _canonical_id(item, page, pos):
    """Canonical string form of the record's id, used for dedup (42 and "42"
    are the same customer). The outward id keeps its original type."""
    if not isinstance(item, dict):
        raise CustomerSourceError(
            f"FieldPulse customer record at page {page} position {pos} is not an object"
        )
    cid = item.get("id")
    if isinstance(cid, int) and not isinstance(cid, bool):
        return str(cid)
    if isinstance(cid, str) and cid.strip():
        return cid.strip()
    raise CustomerSourceError(
        f"FieldPulse customer record at page {page} position {pos} has no usable id"
    )


def _as_count(v):
    """Non-negative int from an int, an integral float, or a digit string;
    None if the value isn't a recognisable count."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float):
        return int(v) if math.isfinite(v) and v >= 0 and v.is_integer() else None
    if isinstance(v, str) and re.fullmatch(r"\s*\d{1,12}\s*", v):
        return int(v)
    return None


def _meta_dicts(containers):
    dicts = []
    for c in containers:
        dicts.append(c)
        for sub in _META_SUBKEYS:
            if isinstance(c.get(sub), dict):
                dicts.append(c[sub])
    return dicts


def _more_signal(containers, page, fetched):
    """True if any recognised paging field says more pages remain, False if
    recognised fields exist and all say done, None if there are none."""
    votes = []
    for d in _meta_dicts(containers):
        for k in _HAS_MORE_KEYS:
            if isinstance(d.get(k), bool):
                votes.append(d[k])
        for k in _NEXT_KEYS:
            if k not in d:
                continue
            v = d[k]
            if v is None or v is False or v == "":
                votes.append(False)
            elif v is True or isinstance(v, str):
                votes.append(True)
            elif _as_count(v) is not None:
                votes.append(_as_count(v) > 0)
            # dict/list/other: not a recognised signal
        for k in _TOTAL_PAGES_KEYS:
            n = _as_count(d.get(k))
            if n is not None:
                votes.append(page < n)
        for k in _TOTAL_COUNT_KEYS:
            n = _as_count(d.get(k))
            if n is not None:
                votes.append(fetched < n)
    if any(votes):
        return True
    return False if votes else None


def _check_reported_page(containers, page):
    for d in _meta_dicts(containers):
        for k in _CURRENT_PAGE_KEYS:
            n = _as_count(d.get(k))
            if n is not None and n != page:
                raise CustomerSourceError(
                    f"FieldPulse answered page {n} when page {page} was requested; the "
                    f"paging parameters may be wrong. Run probe_fieldpulse_customers.py."
                )


# --- Field normalisation ----------------------------------------------------

def _text(v):
    """str or None. Numbers become strings; bools/objects/arrays become None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        return str(int(v)) if v.is_integer() else str(v)
    return None


def _address(item):
    """One display line, or None. Real FieldPulse sends flat address_1,
    address_2, city, state, zip_code; the mock sends a nested
    "address": {street, city, state, postal_code}. A plain-string "address"
    is used as-is. Empty parts are skipped:
    "12 Main St, Apt 2, O'Fallon, IL 62269"."""
    flat = item
    nested = item.get("address")
    if isinstance(nested, str):
        return _text(nested)
    if isinstance(nested, dict) and not any(
        _text(item.get(k)) for k in ("address_1", "address_2", "city", "state", "zip_code")
    ):
        flat = {
            "address_1": nested.get("street") or nested.get("address_1"),
            "address_2": nested.get("street_2") or nested.get("address_2"),
            "city": nested.get("city"),
            "state": nested.get("state"),
            "zip_code": nested.get("postal_code") or nested.get("zip_code") or nested.get("zip"),
        }
    line1, line2, city, state, zip_code = (
        _text(flat.get(k)) for k in ("address_1", "address_2", "city", "state", "zip_code")
    )
    # docs/FIELDPULSE_API.md §3: a ZIP with no street is stored in address_1 too.
    if line1 and line1 == zip_code:
        line1 = None
    region = " ".join(p for p in (state, zip_code) if p) or None
    return ", ".join(p for p in (line1, line2, city, region) if p) or None


# --- Dates + sorting --------------------------------------------------------

_DATE_ONLY_FORMATS = ("%Y-%m-%d", "%m/%d/%Y")
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
)
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def _from_epoch(v):
    if v > 1e11:            # milliseconds
        v = v / 1000.0
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _zone(name):
    """tzinfo for a Carbon-style timezone name/offset, or None if unsupported."""
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    m = _OFFSET_RE.match(name)
    if m:
        hours, minutes = int(m.group(2)), int(m.group(3))
        if hours > 23 or minutes > 59:          # e.g. "+24:00", "-99:00" (F-23)
            return None
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    if name.upper() in ("UTC", "Z", "GMT"):
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):   # ZoneInfoNotFoundError is a KeyError
        return None


def _parse_date(value, tz=None):
    """A UTC-aware datetime (an instant), a date (calendar date only), or
    None. Naive timestamps use `tz` when given, else are treated as UTC
    (UNVERIFIED for the real API)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(value) if math.isfinite(value) and value > 0 else None
    if isinstance(value, dict):             # Laravel/Carbon {"date": "...", "timezone": "..."}
        if "timezone" in value:
            zone = _zone(value.get("timezone"))
            if zone is None:
                return None                 # explicit but unsupported zone: don't guess
            return _parse_date(value.get("date"), tz=zone)
        return _parse_date(value.get("date"), tz=tz)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        num = float(s)
    except ValueError:
        num = None
    if num is not None:
        return _from_epoch(num) if math.isfinite(num) and num > 0 else None

    for fmt in _DATE_ONLY_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    dt = None
    iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        for fmt in _DATETIME_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or timezone.utc)
    return dt.astimezone(timezone.utc)


def _created(item):
    """First usable created date, or None. This is the normalisation
    boundary: any conversion error from one odd value (bad offset, out of
    range year after a zone shift, ...) makes that value null (sorts last)
    instead of failing the whole customer list (F-23)."""
    for field in DATE_FIELDS:
        try:
            parsed = _parse_date(item.get(field))
        except (ValueError, OverflowError, OSError, TypeError):
            parsed = None
        if parsed is not None:
            return parsed
    return None


def _sort_instant(parsed):
    """Sort key for mixed precision. A date-only value sorts as 12:00 UTC of
    that day, which is the same calendar day for any offset within +/-12h,
    so it orders correctly against any timestamp on a different local day."""
    if isinstance(parsed, datetime):
        return parsed.timestamp()
    return datetime(parsed.year, parsed.month, parsed.day, 12, tzinfo=timezone.utc).timestamp()


def _format_created(parsed):
    if parsed is None:
        return None
    if isinstance(parsed, datetime):
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    return parsed.isoformat()               # "YYYY-MM-DD"


def _sorted_simplified(records):
    decorated = []
    for idx, c in enumerate(records):
        parsed = _created(c)
        cid = c["id"]
        decorated.append((
            (0, -_sort_instant(parsed), idx) if parsed is not None else (1, 0.0, idx),
            {
                "id": cid.strip() if isinstance(cid, str) else cid,
                **{f: _text(c.get(f)) for f in _TEXT_FIELDS},
                "phone": _text(c.get("phone")) or _text(c.get("phone_e164")),
                "company_name": _text(c.get("company_name")),
                "address": _address(c),
                "created_at": _format_created(parsed),
            },
        ))
    decorated.sort(key=lambda pair: pair[0])
    return [simple for _, simple in decorated]

