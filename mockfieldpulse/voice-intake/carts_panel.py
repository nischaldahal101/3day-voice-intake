"""Ferguson carts (stage E2 + E3 + E4): per-user inbound cart addresses, the inbound email
webhook, each user's own cart records, (E3) linking a cart to a FieldPulse customer and
(E4) queueing its send. NO FieldPulse WRITE of any kind here: E4 sending runs in the worker
(fieldpulse_sync.sync_cart); this module only queues it and shows its record. The only FieldPulse
call is the READ-ONLY customer list through customer_source.list_customers()
(FIELDPULSE_READ_*, GET only): in the worker for the auto-match, and in the manual
link request to check the picked customer exists (same bounded read as /app/customers).

Entry points, each returning (http_status, json_body) so the routes in
orchestrator.py stay one-liners:

    receive_inbound(secret, content_length, read_body)   POST /inbound/cart (public)
    cart_address(user_id)                                GET  /carts/address
    list_carts(user_id, offset, limit)                   GET  /carts
    cart_detail(user_id, cart_id)                        GET  /carts/<id>
    delete_cart(user_id, cart_id)                        DELETE /carts/<id>
    request_match(user_id, cart_id)                      POST /carts/<id>/match (E3, -> 202)
    link_customer(user_id, cart_id, body)                POST /carts/<id>/customer (E3)
    request_send(user_id, cart_id)                       POST /carts/<id>/send (E4, -> 202)
    match_cart(user_id, cart_id)                         the worker's auto-match (E3)

Customer match (E3): exactly ONE customer whose display name equals the cart's
project name after normalize_name (case, extra spaces, punctuation) -> 'linked',
match "auto". Zero or several -> stays 'needs_customer' with a plain reason. Never
fuzzy. A search error -> 'needs_customer', "Couldn't search FieldPulse", Retry.
A manual link / relink is allowed only while status is in RELINKABLE.

Hidden in here: the shared-secret check, size caps, the alias rules (from the
Clerk first name, else the email local part; reserved words; collisions -> 2, 3,
...), the two accept paths (accepted_sender: Ferguson direct = header From
@fergusonhome.com + DMARC pass for exactly that domain in our receiver's topmost
Authentication-Results, envelope ignored; user forward = envelope sender is one of
the user's VERIFIED Clerk emails, cached briefly, + sender_authenticated; header
shape verified 2026-11-12 on a real Ferguson email), the duplicate-forward rule
(same raw bytes for the same user = the existing cart), storing the raw .eml in
R2 (ferguson-carts/<user>/<uuid>.eml) and parsing it with the pure
ferguson_cart.parse_cart_email (fast, in-request; any exception -> stored with
ok=false). Every cart query is scoped to the signed-in user.

Inbound drops (decision, E2): unknown alias, wrong domain, sender not allowed and
failed authentication all answer 202 {"status": "dropped", "reason": <code>} and
store nothing. Only our Cloudflare Email Worker (it holds the secret) sees that
answer; it must drop silently, never bounce, so a stranger can't probe which
aliases exist. Logs carry the fixed reason code only: no addresses, no content.
"""

from __future__ import annotations

import base64
import binascii
import email.parser
import email.policy
import email.utils
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import uuid
from decimal import Decimal

import customer_source
import jobs
import queue_setup
import storage
from admin_panel import scrub
from documents_panel import customer_display_name
from ferguson_cart import normalize_name, parse_cart_email
from observability import report

logger = logging.getLogger("voice_intake.carts_panel")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

FERGUSON_DOMAIN = "fergusonhome.com"     # exact; never a parent/child/look-alike
DEFAULT_DOMAIN = "voiceconsult.app"
DEFAULT_AUTHSERV_ID = "mx.cloudflare.net"
MAX_RAW_BYTES = 5 * 1024 * 1024                    # the decoded email
MAX_BODY_BYTES = MAX_RAW_BYTES * 4 // 3 + 64 * 1024  # base64 + the JSON around it
RESERVED_ALIASES = frozenset((
    "admin", "support", "postmaster", "abuse", "noreply", "no-reply", "root", "info",
    "carts", "mail"))
ALIAS_RE = re.compile(r"^[a-z0-9.-]{1,40}\Z")
CANT_READ = "Couldn't read this email."
MAX_PAGE = 100
DEFAULT_PAGE = 50
_ALIAS_TRIES = 99
_PROFILE_TTL_S = 300
_ADDRESS_RE = re.compile(r"^[^@\s<>\"]{1,64}@[a-z0-9.-]{1,253}\Z")
_CART_ID_RE = re.compile(r"^cart_[0-9a-f]{12}\Z")


class ProfileError(Exception):
    """The Clerk user could not be read (network, Clerk down, unknown user)."""


# --- config (read at call time) ---------------------------------------------------

def _env(name, default=None):
    v = (os.environ.get(name) or "").strip()
    return v or default


def inbound_domain():
    return _env("INBOUND_EMAIL_DOMAIN", DEFAULT_DOMAIN).lower()


# --- Clerk profile (first name + verified emails), cached briefly -----------------

_profiles = {}
_profiles_lock = threading.Lock()


def _load_clerk_profile(user_id):
    """{"first_name", "primary_email", "emails": [verified, lowercased]} from Clerk.
    Same SDK and secret the orchestrator verifies sessions with; bounded (5 s)."""
    from clerk_backend_api import Clerk
    secret = _env("CLERK_SECRET_KEY")
    if not secret:
        raise ProfileError("CLERK_SECRET_KEY is not set")
    try:
        user = Clerk(bearer_auth=secret, timeout_ms=5000).users.get(user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - any SDK/network error = profile unavailable
        raise ProfileError(type(exc).__name__) from exc
    emails, primary = [], None
    for e in getattr(user, "email_addresses", None) or []:
        addr = getattr(e, "email_address", None)
        if not isinstance(addr, str) or not addr.strip():
            continue
        addr = addr.strip().lower()
        if getattr(e, "id", None) and e.id == getattr(user, "primary_email_address_id", None):
            primary = addr
        status = getattr(getattr(e, "verification", None), "status", None)
        status = getattr(status, "value", status)
        if isinstance(status, str) and status.lower() == "verified":
            emails.append(addr)
    first = getattr(user, "first_name", None)
    return {"first_name": first if isinstance(first, str) else None,
            "primary_email": primary or (emails[0] if emails else None),
            "emails": emails}


def _profile(user_id):
    now = time.monotonic()
    with _profiles_lock:
        hit = _profiles.get(user_id)
        if hit and hit[0] > now:
            return hit[1]
    prof = _load_clerk_profile(user_id)
    with _profiles_lock:
        _profiles[user_id] = (now + _PROFILE_TTL_S, prof)
    return prof


# --- aliases -------------------------------------------------------------------------

def alias_base(text):
    """'José Smith' -> 'josesmith'; only [a-z0-9.-], no leading/trailing/double
    dots or hyphens, <= 40 chars. '' when nothing usable is left."""
    if not isinstance(text, str):
        return ""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9.-]+", "", t)
    t = re.sub(r"[.-]{2,}", lambda m: m.group(0)[0], t)
    return t.strip(".-")[:40].strip(".-")


def alias_candidates(first_name, email_addr):
    """base, base2, base3, ... (never a reserved word, always ALIAS_RE)."""
    base = alias_base(first_name)
    if not base and isinstance(email_addr, str) and "@" in email_addr:
        base = alias_base(email_addr.rsplit("@", 1)[0])
    base = base or "user"
    for n in range(1, _ALIAS_TRIES + 1):
        if n == 1:
            alias = base
        else:
            suffix = str(n)
            alias = (base[:40 - len(suffix)].rstrip(".-") or "user") + suffix
        if alias not in RESERVED_ALIASES and ALIAS_RE.match(alias):
            yield alias


def cart_address(user_id):
    """The user's inbound address; the alias is created on first use."""
    if not user_id:
        return 401, {"error": "Not signed in."}
    alias = jobs.get_cart_alias(user_id)
    if alias is None:
        try:
            prof = _profile(user_id)
        except ProfileError as exc:
            logger.error("Cart address: Clerk profile unavailable (%s)", exc)
            report(exc, "Clerk profile lookup failed (cart address)")
            return 503, {"error": "Couldn't create your cart address right now. "
                                  "Try again shortly."}
        alias = jobs.ensure_cart_alias(
            user_id, alias_candidates(prof.get("first_name"), prof.get("primary_email")))
    return 200, {"address": f"{alias}@{inbound_domain()}"}


# --- sender authentication (header shape verified 2026-11-12, real Ferguson email) ---

def _strip_comments(text):
    for _ in range(3):                      # RFC 5322 comments may nest
        text = re.sub(r"\([^()]*\)", " ", text)
    return text


def _parse_auth_results(value):
    """'mx.example; spf=pass smtp.mailfrom=a@b.c; dkim=pass header.d=b.c' ->
    ('mx.example', [('spf', 'pass', {'smtp.mailfrom': 'a@b.c'}), ...]).
    Comments in parentheses are removed first (Cloudflare's hold '@' and ':'), the
    same method may appear more than once (Cloudflare writes two spf= results: helo
    and mailfrom), values may hold '+', '=' and '/' (only the first '=' splits)."""
    text = " ".join(_strip_comments(str(value)).split())
    parts = [p.strip() for p in text.split(";")]
    head = parts[0].split()
    servid = head[0].lower() if head else ""
    results = []
    for p in parts[1:]:
        toks = p.split()
        if not toks or "=" not in toks[0]:
            continue
        method, _, result = toks[0].partition("=")
        props = {}
        for t in toks[1:]:
            k, eq, v = t.partition("=")
            if eq:
                props[k.lower()] = v.strip('"').lower()
        results.append((method.lower(), result.lower(), props))
    return servid, results


def _domain_of(value):
    d = (value or "").rsplit("@", 1)[-1].strip().strip(".").lower()
    return d if "." in d else ""


def _trusted_results(headers, authserv_id):
    """The results of the TOPMOST Authentication-Results header written by our
    receiver (authserv-id == `authserv_id`), or [] when there is none. Lower
    headers with the same id are ignored (a sender can write those)."""
    trusted = (authserv_id or "").strip().lower()
    for value in headers.get_all("Authentication-Results") or []:
        servid, results = _parse_auth_results(value)
        if servid == trusted:
            return results
    return []


def sender_authenticated(headers, sender, authserv_id):
    """True when our receiver's topmost Authentication-Results header shows
    spf=pass for the sender's domain (smtp.mailfrom), dkim=pass for it (header.d /
    header.i, the domain or a parent of it) or dmarc=pass for it (header.from).
    Header shape verified 2026-11-12 on a real Ferguson email via Cloudflare."""
    sender_domain = _domain_of(sender)
    if not sender_domain:
        return False
    for method, result, props in _trusted_results(headers, authserv_id):
        if result != "pass":
            continue
        if method == "spf" and _domain_of(props.get("smtp.mailfrom")) == sender_domain:
            return True
        if method in ("dkim", "dmarc"):
            keys = ("header.d", "header.i") if method == "dkim" else ("header.from",)
            for k in keys:
                d = _domain_of(props.get(k))
                if d and (sender_domain == d or sender_domain.endswith("." + d)):
                    return True
    return False


def _header_from(headers):
    """The ONE address in the message's From: header (lowercased), else None
    (missing, several From headers, several addresses, unparseable)."""
    values = headers.get_all("From") or []
    if len(values) != 1:
        return None
    pairs = email.utils.getaddresses([str(values[0])])
    if len(pairs) != 1:
        return None
    return _address(pairs[0][1])


def ferguson_direct(headers, authserv_id):
    """The header From address when the email comes straight from Ferguson, else None:
    From is @fergusonhome.com (exact domain) AND our receiver's topmost
    Authentication-Results has dmarc results for header.from=fergusonhome.com, all
    pass (DMARC pass binds the visible From to an authenticated domain). The envelope
    sender is ignored (Ferguson's is a bounces+...@em9668.fergusonhome.com address)."""
    sender = _header_from(headers)
    if sender is None or sender.rsplit("@", 1)[1] != FERGUSON_DOMAIN:
        return None
    dmarc = [r for m, r, props in _trusted_results(headers, authserv_id)
             if m == "dmarc" and props.get("header.from") == FERGUSON_DOMAIN]
    return sender if dmarc and all(r == "pass" for r in dmarc) else None


# --- inbound webhook -------------------------------------------------------------------

def _address(value):
    """'a@b.c' (lowercased) or None. Accepts 'Name <a@b.c>' too."""
    if not isinstance(value, str) or len(value) > 512:
        return None
    addr = email.utils.parseaddr(value)[1].strip().lower()
    return addr if _ADDRESS_RE.match(addr) else None


def _drop(code):
    logger.info("Inbound cart email dropped: %s", code)
    return 202, {"status": "dropped", "reason": code}


def _jsonable(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if v is None or isinstance(v, (str, bool, int, float)):
        return v
    return str(v)


def _read_cart(raw):
    """parse_cart_email (never raises by contract; guarded anyway) -> JSON-safe dict."""
    try:
        parsed = parse_cart_email(raw)
        if not isinstance(parsed, dict):
            raise TypeError("parser returned a non-dict")
        return _jsonable(parsed)
    except Exception as exc:  # noqa: BLE001 - untrusted input never fails the request
        logger.error("Cart email parse raised %s", type(exc).__name__)
        return {"project_name": None, "pricing_expires": None, "subtotal": None,
                "items": [], "checks": [CANT_READ], "ok": False}


def accepted_sender(user_id, envelope_sender, headers):
    """(sender_to_store, None) when accepted, else (None, drop_code). Two paths:
    1. Ferguson direct (ferguson_direct): no Clerk lookup, envelope ignored.
    2. User forward: envelope sender is one of the user's VERIFIED Clerk emails AND
       sender_authenticated for it.
    Drop codes: 'sender_not_allowed' (not Ferguson, not one of the user's emails) or
    'auth_failed' (one of the user's emails but not authenticated, or the From
    claims fergusonhome.com but DMARC didn't pass). ProfileError when the user's
    emails can't be read."""
    authserv = _env("INBOUND_AUTHSERV_ID", DEFAULT_AUTHSERV_ID)
    direct = ferguson_direct(headers, authserv)
    if direct:
        return direct, None
    if envelope_sender in _profile(user_id)["emails"]:
        if sender_authenticated(headers, envelope_sender, authserv):
            return envelope_sender, None
        return None, "auth_failed"
    claimed = _header_from(headers)
    if claimed and claimed.rsplit("@", 1)[1] == FERGUSON_DOMAIN:
        return None, "auth_failed"
    return None, "sender_not_allowed"


def receive_inbound(secret, content_length, read_body):
    """POST /inbound/cart, called only by our Cloudflare Email Worker (E5).
    `read_body()` returns the request body bytes (called only after the secret
    and size checks)."""
    expected = _env("INBOUND_EMAIL_SECRET")
    if not expected:
        return 503, {"error": "Inbound email is not configured."}
    if not isinstance(secret, str) or not hmac.compare_digest(
            secret.encode("utf-8"), expected.encode("utf-8")):
        logger.warning("Inbound cart email refused: bad secret")
        return 401, {"error": "unauthorized"}
    if content_length is None:
        return 411, {"error": "Content-Length is required."}
    if content_length > MAX_BODY_BYTES:
        return 413, {"error": "The email is larger than 5 MB."}

    try:
        body = json.loads(read_body() or b"")
    except (ValueError, UnicodeDecodeError):
        return 400, {"error": "The body must be JSON."}
    if not isinstance(body, dict) or not all(
            isinstance(body.get(k), str) for k in ("to", "from", "raw_b64")):
        return 400, {"error": "'to', 'from' and 'raw_b64' are required strings."}
    try:
        raw = base64.b64decode(body["raw_b64"], validate=True)
    except (binascii.Error, ValueError):
        return 400, {"error": "'raw_b64' is not base64."}
    if len(raw) > MAX_RAW_BYTES:
        return 413, {"error": "The email is larger than 5 MB."}
    if not raw:
        return 400, {"error": "The email is empty."}

    to, sender = _address(body["to"]), _address(body["from"])
    if to is None or sender is None:
        return _drop("bad_address")
    local, _, domain = to.rpartition("@")
    if domain != inbound_domain():
        return _drop("wrong_domain")
    user_id = jobs.get_cart_alias_user(local) if ALIAS_RE.match(local) else None
    if user_id is None:
        return _drop("unknown_alias")
    headers = email.parser.BytesHeaderParser(policy=email.policy.compat32).parsebytes(raw)
    try:
        sender, refused = accepted_sender(user_id, sender, headers)
    except ProfileError as exc:
        logger.error("Inbound cart email: Clerk profile unavailable (%s)", exc)
        report(exc, "Clerk profile lookup failed (inbound cart)")
        return 503, {"status": "retry", "reason": "user_lookup_failed"}
    if refused:
        return _drop(refused)

    sha = hashlib.sha256(raw).hexdigest()
    existing = jobs.find_cart_by_sha(user_id, sha)
    if existing:
        logger.info("[%s] inbound cart email is a duplicate", existing["id"])
        return 202, {"status": "duplicate", "cart_id": existing["id"]}

    parsed = _read_cart(raw)
    message_id = headers.get("Message-ID")
    message_id = re.sub(r"[\x00-\x1f\x7f]", "", str(message_id))[:300] if message_id else None
    owner = re.sub(r"[^A-Za-z0-9_-]", "_", user_id)[:64]
    key = f"{storage.CART_PREFIX}{owner}/{uuid.uuid4()}.eml"
    try:
        storage.put_bytes(key, raw, "message/rfc822")
    except storage.StorageError as exc:
        logger.error("Inbound cart email: R2 put failed")
        report(exc, "R2 put failed (inbound cart)")
        return 503, {"status": "retry", "reason": "storage_unavailable"}

    warnings = [str(c)[:500] for c in parsed.get("checks") or []][:100]
    project = parsed.get("project_name")
    cart, created = jobs.create_ferguson_cart(
        user_id, local, sender, message_id, key, sha, parsed, bool(parsed.get("ok")),
        warnings, project[:255] if isinstance(project, str) else None)
    if not created:                          # the same email raced in: keep the first
        _remove_raw(cart["id"] if cart else None, key)
        return 202, {"status": "duplicate", "cart_id": cart["id"] if cart else None}
    logger.info("[%s] inbound cart email stored (ok=%s)", cart["id"], cart["ok"])
    try:                                     # E3: match in the worker, never in here
        _start_match(user_id, cart["id"])
    except Exception as exc:  # noqa: BLE001 - the cart is stored; Retry match recovers
        logger.error("[%s] could not start the customer match (%s)", cart["id"],
                     type(exc).__name__)
        report(exc, "Could not start cart match (inbound)", job_id=cart["id"])
    return 202, {"status": "stored", "cart_id": cart["id"]}


# --- the user's carts (Clerk routes) ---------------------------------------------------

def _int(v, default):
    if v is None or v == "":
        return default
    return int(v)


def list_carts(user_id, offset=0, limit=DEFAULT_PAGE):
    """This user's carts, newest first, compact."""
    if not user_id:
        return 401, {"error": "Not signed in."}
    try:
        offset = max(0, _int(offset, 0))
        limit = max(1, min(_int(limit, DEFAULT_PAGE), MAX_PAGE))
    except (TypeError, ValueError):
        return 400, {"error": "'offset' and 'limit' must be integers"}
    page = jobs.list_ferguson_carts(user_id, offset=offset, limit=limit)
    items = [{
        "id": r["id"], "received_at": r.get("received_at"),
        "project_name": r.get("project_name"), "item_count": r.get("item_count"),
        "subtotal": r.get("subtotal"), "ok": bool(r.get("ok")),
        "warning_count": r.get("warning_count") or 0, "status": r.get("status"),
        "customer_name": r.get("customer_name"), "match_state": r.get("match_state"),
        "fieldpulse_state": fp_state(r.get("fieldpulse_status"), r.get("fieldpulse_at"),
                                     r.get("superseded_by")),
        "superseded_by": r.get("superseded_by"),
    } for r in page["items"]]
    return 200, scrub({"items": items, "total": page["total"], "offset": offset,
                       "limit": limit})


def _not_found():
    return 404, {"error": "Cart not found."}


def cart_detail(user_id, cart_id):
    """One cart of this user: the parsed cart, warnings, status. No R2 key, no sender."""
    if not user_id:
        return 401, {"error": "Not signed in."}
    if not isinstance(cart_id, str) or not _CART_ID_RE.match(cart_id):
        return _not_found()
    cart = jobs.get_ferguson_cart(user_id, cart_id)
    if cart is None:
        return _not_found()
    return 200, _detail(cart)


def _detail(cart):
    p = cart.get("parsed") or {}
    items = [it for it in p.get("items") or [] if isinstance(it, dict)]
    m = cart.get("customer_match") if isinstance(cart.get("customer_match"), dict) else {}
    c = cart.get("customer") if isinstance(cart.get("customer"), dict) else None
    status = cart.get("status")
    return scrub({
        "id": cart["id"], "received_at": cart.get("received_at"),
        "status": status, "ok": cart.get("ok"),
        "warnings": cart.get("warnings") or [],
        "project_name": cart.get("project_name"),
        "customer": {k: c.get(k) for k in _SNAPSHOT_KEYS} if c else None,
        "match": m.get("match"), "match_state": m.get("state"),
        "match_reason": m.get("reason"), "match_at": m.get("at"),
        "can_link": status in RELINKABLE,
        "can_match": status == "needs_customer",
        "relink_note": SENT_NO_RELINK if status == "sent" else None,
        "superseded_by": cart.get("superseded_by"),
        "fieldpulse": _fp_view(cart),
        "cart": {"pricing_expires": p.get("pricing_expires"), "subtotal": p.get("subtotal"),
                 "items": [{k: it.get(k) for k in (
                     "room", "brand", "model", "finish", "quantity", "unit_price",
                     "product_url", "image_url")} for it in items]},
    })


def _remove_raw(cart_id, key):
    """Best-effort R2 delete of the raw email: 'deleted' | 'failed'."""
    try:
        storage.delete_audio(key)          # generic R2 object delete (bounded client)
        return "deleted"
    except Exception as exc:  # noqa: BLE001 - never undo the DB delete
        logger.error("[%s] raw cart email was not removed from R2 (%s)", cart_id or "-",
                     type(exc).__name__)
        report(exc, "R2 cart email delete failed", job_id=cart_id)
        return "failed"


def delete_cart(user_id, cart_id):
    """Delete the row, then the raw email in R2 (best effort). Never FieldPulse."""
    if not user_id:
        return 401, {"error": "Not signed in."}
    if not isinstance(cart_id, str) or not _CART_ID_RE.match(cart_id):
        return _not_found()
    cart = jobs.delete_ferguson_cart(user_id, cart_id)
    if cart is None:
        return _not_found()
    file_status = _remove_raw(cart_id, cart["raw_key"]) if cart.get("raw_key") else "none"
    logger.info("[%s] cart deleted (file %s)", cart_id, file_status)
    return 200, {"deleted": True, "id": cart_id, "file": file_status}


# --- customer match + link (stage E3; reads only, never a FieldPulse write) -------------

# Statuses a cart's customer may be (re)linked from. E4 adds sending | sent | failed and
# decides which of those may still be relinked; set_cart_customer never touches others.
RELINKABLE = ("needs_customer", "linked")
_SNAPSHOT_KEYS = ("id", "display_name", "first_name", "last_name", "phone", "email",
                  "company_name", "address")
_CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
SEARCH_FAILED = "Couldn't search FieldPulse. Try matching again."
QUEUE_FAILED = "Couldn't start matching. Try matching again."
PENDING = "Looking for a FieldPulse customer with this project's name..."
NO_PROJECT = "This cart has no project name to match. Link a customer by hand."
LINKED_BY_HAND = "Linked by hand."
NOT_READ_NOTE = " This cart couldn't be read completely: check it before it is sent."


def _snapshot(c):
    """The picked/matched customer as stored on the cart (customer_source fields)."""
    snap = {k: c.get(k) for k in _SNAPSHOT_KEYS}
    snap["display_name"] = customer_display_name(c)
    return snap


def _quote(name):
    name = " ".join(str(name).split())
    return "\u201c" + (name[:120] + "..." if len(name) > 120 else name) + "\u201d"


def find_by_name(customers, project_name):
    """(customer or None, reason). Exactly one display-name match after normalize_name
    -> that customer; zero or several -> None with a plain reason. Never fuzzy."""
    key = normalize_name(project_name)
    if not key:
        return None, NO_PROJECT
    hits = [c for c in customers or [] if isinstance(c, dict) and c.get("id") is not None
            and normalize_name(customer_display_name(c)) == key]
    if len(hits) == 1:
        return hits[0], f"Matched by name: {_quote(project_name)}."
    if not hits:
        return None, f"No FieldPulse customer named {_quote(project_name)}."
    return None, (f"{len(hits)} customers match {_quote(project_name)}. "
                  "Link the right one by hand.")


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _match_info(how, state, reason):
    return {"match": how, "state": state, "reason": reason, "at": _now()}


def _start_match(user_id, cart_id):
    """Mark the cart 'pending' and queue the worker match. True if queued; a queue
    failure leaves the cart needs_customer with QUEUE_FAILED (Retry recovers).
    None when the cart isn't (any more) waiting for a customer."""
    if jobs.set_cart_customer(user_id, cart_id, None, _match_info(None, "pending", PENDING),
                              only_from=("needs_customer",)) is None:
        return None
    try:
        queue_setup.enqueue_cart_match(user_id, cart_id)
        return True
    except Exception as exc:  # noqa: BLE001 - Redis down etc.
        logger.error("[%s] could not queue the customer match (%s)", cart_id,
                     type(exc).__name__)
        report(exc, "Could not queue cart match", job_id=cart_id)
        jobs.set_cart_customer(user_id, cart_id, None,
                               _match_info(None, "error", QUEUE_FAILED),
                               only_from=("needs_customer",))
        return False


def match_cart(user_id, cart_id):
    """The worker's auto-match (worker.match_cart_customer). One read of the customer
    list, then one conditional write that applies only while the cart still needs a
    customer (a manual link made meanwhile wins). Returns a short outcome code:
    'linked' | 'needs_customer' | 'search_failed' | 'skipped' (gone / already linked)."""
    cart = jobs.get_ferguson_cart(user_id, cart_id)
    if cart is None or cart.get("status") != "needs_customer":
        return "skipped"
    note = "" if cart.get("ok") else NOT_READ_NOTE
    project = cart.get("project_name")
    if not normalize_name(project):
        found, reason, state = None, NO_PROJECT, "done"
    else:
        try:
            customers = customer_source.list_customers()
        except Exception as exc:  # noqa: BLE001 - any read failure = "couldn't search"
            logger.error("[%s] customer search failed (%s)", cart_id, type(exc).__name__)
            report(exc, "Cart match: customer search failed", job_id=cart_id)
            found, reason, state = None, SEARCH_FAILED, "error"
        else:
            found, reason = find_by_name(customers, project)
            state = "done"
    saved = jobs.set_cart_customer(
        user_id, cart_id, _snapshot(found) if found else None,
        _match_info("auto" if found else None, state, reason + note),
        only_from=("needs_customer",))
    if saved is None:
        return "skipped"
    outcome = "linked" if found else ("search_failed" if state == "error" else "needs_customer")
    logger.info("[%s] cart match: %s", cart_id, outcome)
    if found:
        _queue_send(user_id, cart_id, manual=False)    # E4: the worker sends (mode-gated)
    return outcome


def _own_cart(user_id, cart_id):
    if not isinstance(cart_id, str) or not _CART_ID_RE.match(cart_id):
        return None
    return jobs.get_ferguson_cart(user_id, cart_id)


def request_match(user_id, cart_id):
    """POST /carts/<id>/match: queue the auto-match again (worker; no FieldPulse I/O here)."""
    if not user_id:
        return 401, {"error": "Not signed in."}
    cart = _own_cart(user_id, cart_id)
    if cart is None:
        return _not_found()
    if cart.get("status") != "needs_customer":
        return 409, {"error": "This cart is already linked. Use Change customer instead."}
    queued = _start_match(user_id, cart_id)
    if queued is None:
        return 409, {"error": "This cart is already linked. Use Change customer instead."}
    if not queued:
        return 503, {"error": "Couldn't start matching right now. Try again shortly."}
    return 202, {"status": "queued", "id": cart_id}


def link_customer(user_id, cart_id, body):
    """POST /carts/<id>/customer {customer_id}: link (or relink) by hand. The id is
    checked against the read-only customer list; the stored snapshot comes from
    FieldPulse, never from the browser."""
    if not user_id:
        return 401, {"error": "Not signed in."}
    cart = _own_cart(user_id, cart_id)
    if cart is None:
        return _not_found()
    if cart.get("status") == "sent":
        return 409, {"error": SENT_NO_RELINK}
    if cart.get("status") not in RELINKABLE:
        return 409, {"error": "This cart can't be relinked any more."}
    raw = body.get("customer_id") if isinstance(body, dict) else None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        raw = str(raw)
    if not isinstance(raw, str) or not _CUSTOMER_ID_RE.match(raw.strip()):
        return 400, {"error": "'customer_id' must be a FieldPulse customer id."}
    wanted = raw.strip()
    try:
        customers = customer_source.list_customers()
    except Exception as exc:  # noqa: BLE001 - shaped, never echoed
        logger.error("[%s] customer check failed (%s)", cart_id, type(exc).__name__)
        report(exc, "Cart link: customer check failed", job_id=cart_id)
        return 502, {"error": "Couldn't check the customer in FieldPulse. Try again shortly."}
    found = next((c for c in customers or [] if isinstance(c, dict)
                  and c.get("id") is not None and str(c["id"]).strip() == wanted), None)
    if found is None:
        return 400, {"error": "That FieldPulse customer wasn't found."}
    saved = jobs.set_cart_customer(
        user_id, cart_id, _snapshot(found),
        _match_info("manual", "done",
                    LINKED_BY_HAND + ("" if cart.get("ok") else NOT_READ_NOTE)),
        only_from=RELINKABLE)
    if saved is None:
        now = _own_cart(user_id, cart_id)
        if now is None:
            return _not_found()
        return 409, {"error": SENT_NO_RELINK if now.get("status") == "sent"
                     else "This cart can't be relinked any more."}
    logger.info("[%s] cart linked by hand", cart_id)
    _queue_send(user_id, cart_id, manual=False)        # E4: the worker sends (mode-gated)
    return 200, _detail(saved)


# --- send to FieldPulse (stage E4; the worker does the sending, never a request) --------
#
# fieldpulse_sync.sync_cart writes ONE draft estimate + ONE material list per (user,
# customer, project) through the FIELDPULSE_WRITE_MODE gate. Here: queue it (after a link,
# and on Send/Retry) and show its record. A 'sent' cart (a request went out) can't be
# relinked: the estimate and list would stay on the first customer.

SENT_NO_RELINK = ("This cart was already sent to FieldPulse, so its customer can't be "
                  "changed: the draft estimate and material list would stay on the first "
                  "customer. Delete them in FieldPulse if they are on the wrong customer.")
_STALE_SENDING_S = 300
FP_LABELS = {"not_sent": "Not sent", "queued": "Queued", "sending": "Sending…",
             "dry_run": "Dry run", "held": "Held (dry run)", "sent": "Sent",
             "attention": "Needs attention", "refused": "Needs fixing",
             "superseded": "Replaced by a newer cart"}
_FP_ATTENTION = ("failed", "unknown", "needs_config")


def _age_s(iso):
    from datetime import datetime, timezone
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except (TypeError, ValueError):
        return None


def fp_state(raw, at=None, superseded_by=None):
    """The app's FieldPulse state for a cart: not_sent | queued | sending | dry_run | held |
    sent | attention | refused | superseded. A 'sending' older than 5 min = the worker
    died mid-send -> attention."""
    if superseded_by:
        return "superseded"
    if raw == "sending":
        age = _age_s(at)
        return "sending" if age is not None and age <= _STALE_SENDING_S else "attention"
    if raw in _FP_ATTENTION:
        return "attention"
    if raw in ("queued", "dry_run", "held", "sent", "refused", "superseded"):
        return raw
    return "not_sent"


def _fp_view(cart):
    rec = cart.get("fieldpulse") if isinstance(cart.get("fieldpulse"), dict) else {}
    state = fp_state(rec.get("status"), rec.get("at"), cart.get("superseded_by"))
    steps = rec.get("steps") if isinstance(rec.get("steps"), dict) else {}
    problem = None
    if cart.get("status") in ("linked", "sent"):
        from fieldpulse_sync import cart_send_problem
        problem = cart_send_problem(cart)
    return {
        "state": state, "label": FP_LABELS[state], "raw_status": rec.get("status"),
        "message": rec.get("message"), "error": rec.get("error"), "mode": rec.get("mode"),
        "updated_at": rec.get("at"), "estimate_id": rec.get("estimate_id"),
        "list_id": rec.get("list_id"), "replaces": rec.get("replaces"),
        "replaced_by": cart.get("superseded_by") or rec.get("replaced_by"),
        "steps": {k: v for k, v in steps.items() if isinstance(v, dict)},
        "history": cart.get("fieldpulse_history") or [],
        "problem": problem,
        "can_send": (cart.get("status") in ("linked", "sent") and not problem
                     and not cart.get("superseded_by") and state not in ("sending", "sent")),
    }


def _queue_send(user_id, cart_id, manual):
    """Best effort: a queue failure is logged; the user's Send button recovers."""
    try:
        queue_setup.enqueue_cart_send(user_id, cart_id, manual)
        return True
    except Exception as exc:  # noqa: BLE001 - Redis down etc.
        logger.error("[%s] could not queue the cart send (%s)", cart_id, type(exc).__name__)
        report(exc, "Could not queue cart send", job_id=cart_id)
        return False


def request_send(user_id, cart_id):
    """POST /carts/<id>/send: queue a manual Send/Retry (202). No FieldPulse I/O here."""
    if not user_id:
        return 401, {"error": "Not signed in."}
    cart = _own_cart(user_id, cart_id)
    if cart is None:
        return _not_found()
    if cart.get("status") not in ("linked", "sent"):
        return 409, {"error": "Link this cart to a FieldPulse customer first."}
    if cart.get("superseded_by"):
        return 409, {"error": "This cart was replaced by a newer cart for the same project. "
                              "Send the newer cart instead."}
    from fieldpulse_sync import cart_send_problem
    problem = cart_send_problem(cart)
    if problem:
        return 409, {"error": problem}
    if not _queue_send(user_id, cart_id, manual=True):
        return 503, {"error": "Couldn't queue the send right now. Try again shortly."}
    return 202, {"status": "queued", "id": cart_id}
