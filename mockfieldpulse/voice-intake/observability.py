"""Sentry error monitoring + tracing for the orchestrator and the RQ worker.

Interface (everything else is internal):
    init_sentry(component)  once per process: "orchestrator" | "worker".
                            Silent no-op when SENTRY_DSN is unset (local dev, tests).
    traced_job(fn)          decorator for RQ job functions (worker.py).
    report(exc=None, message=None, level="error", **tags)
                            report a HANDLED problem (one we turn into a failed job,
                            a 5xx, or a FieldPulse step failure). No-op when disabled.

Privacy decisions live here, not in callers (F-31..F-37):
  * send_default_pii=False, request bodies never sent, no stack-frame locals,
    no profiling, no Sentry logs, no AI prompt capture.
  * Every event/transaction is REBUILT from an allowlist (_rebuild_event): each
    kept field has a value rule (enum, id/number shape, URL without userinfo/
    query/fragment, or a fixed template); a field without a rule is dropped.
    No free text survives except report()'s fixed message/note and log
    TEMPLATES ("[%s] FAILED: %s", never the formatted values).
  * Exception text is redacted: the value is kept only when it matches one of
    our known-safe messages (_SAFE_VALUES), else only a known-safe prefix
    survives ("Deepgram returned 400: [redacted]"), else "[redacted]". psycopg
    errors add their SQLSTATE. Type, module and frames are kept (grouping).
  * Contexts: trace (ids/op/status/data by rule, DSC rebuilt), runtime
    name/version, and voice_intake.note from report() only. Tags: _TAG_RULES.
  * URLs (F-38): an incoming request is named by its Flask route TEMPLATE
    ("/job-status/<job_id>", "[unmatched route]" for 404s), never its path or
    Host; outgoing URLs keep scheme+host only (path only for _UPSTREAM_PATHS).
  * Data keys are an exact list (_DATA_RULES, F-39); numbers only for counts,
    durations and status codes, bounded. Content-Type -> fixed set, Origin only
    when it exactly matches an app origin (CLERK_AUTHORIZED_PARTIES).
  * Sessions are rebuilt from a strict schema with our own release/environment
    (F-40); `sdk` is rebuilt from the installed SDK's constants (F-41).
  * The transport is the final boundary (SafeTransport): only event /
    transaction / session(s) / client_report items leave, and the envelope
    `trace` header (incoming baggage!) keeps only trace_id / sample_rate /
    sample_rand / sampled, with our own public_key / environment / release.
  * No trace headers on outgoing HTTP (trace_propagation_targets=[]).
  * psycopg.pool WARNING+ log records (F-82) -> one report() message per 60 s with
    FIXED text + the exception class name only (the record text can hold the DB host/user).
  * New fixed exception messages that should stay readable: _SAFE_VALUES /
    _SAFE_PREFIXES. New report() tags: _TAG_RULES.

RQ: `rq worker consultations` forks a work-horse per job, and the parent never
imports worker.py, so init happens in the horse when RQ imports worker.py for
the job. By then RQ is already inside perform_job, so the RqIntegration can't
wrap that call (no transaction, and RQ's os._exit skips Sentry's flush).
traced_job fills that gap: it starts the transaction (continuing the trace the
orchestrator put on the RQ job), captures an escaping exception, and flushes
before the horse exits. If something upstream already opened a transaction
(e.g. a future `rq worker -c` settings module), it only captures and flushes.
"""

import functools
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
logger = logging.getLogger("voice_intake.observability")

_enabled = False

_JOB_ID = r"job_[0-9a-f]{6,32}"
_DOC_ID = r"doc_[0-9a-f]{12}"          # vendor_documents ids (stage P1b)
# Exception values (whole message) known to carry no customer data: our own
# fixed texts, optionally with a job id / internal status word.
_SAFE_VALUES = [re.compile(p) for p in (
    rf"Job not found: {_JOB_ID}",
    rf"Job {_JOB_ID} has no audio_key and no transcript",
    r"create_job needs exactly one of audio_key or transcript",
    r"save_manual_send needs an entry with an 'id'",
    r"(?:record|finish)_fieldpulse_write: bad status '[a-z_]{1,32}'",
    r"release_held_fieldpulse_writes: '[a-z_]{1,32}' is not a send mode",
    r"Transcription returned an empty transcript",
    r"DEEPGRAM_API_KEY is not set in \.env",
    r"ANTHROPIC_API_KEY not found\. Set it in voice-intake/\.env",
    r"Parsed response is not a JSON object",
    r"Not a consultation recording key",
    r"R2 is not configured — missing in \.env: [A-Z0-9_, ]{1,120}",
    r"R2 key not found: consultations/[0-9a-f-]{36}\.[a-z0-9]{1,10}",
    r"Vendor document is not a PDF",
    r"Vendor document is larger than 20 MB",
    r"Vendor document read failed: Claude (?:API error|output was cut off"
    r"|returned malformed output)",
    r"R2 key not found: vendor-docs/[0-9a-f-]{36}\.pdf",
    r"R2 object is larger than \d{1,12} bytes",
    r"create_vendor_document needs a file_key",
    r"update_vendor_document: bad status '[a-z_]{1,32}'",
    r"couldn't get a connection after \d{1,4}\.\d{2} sec",   # psycopg_pool.PoolTimeout (F-82)
)]
# Known-safe leading text; whatever follows it is replaced by [redacted].
_SAFE_PREFIXES = [re.compile(p) for p in (
    r"Deepgram returned \d{3}",
    r"Deepgram returned non-JSON body",
    r"Deepgram request failed",
    r"Audio file not found",
    r"Claude API error",
    r"Claude returned malformed JSON",
    r"Could not read (?:system|consultation) prompt",
    r"Failed to (?:upload|download|delete|check|generate upload URL for"
    r"|generate download URL for)",
    r"Error code: \d{3}",                                    # anthropic.APIStatusError
    r"\d{3} (?:Client|Server) Error",                        # requests.HTTPError
    r"An error occurred \([A-Za-z0-9.]{1,64}\) when calling the [A-Za-z]{1,64} operation",  # boto
)]
# Our exception types whose messages are documented as fixed text.
_SAFE_TYPES = {("customer_source", "CustomerSourceError")}

_FLUSH_TIMEOUT_S = 5
_REDACTED = "[redacted]"
_REDACTED_MESSAGE = "[redacted message]"
_LOG_OMITTED = "[log message omitted]"
_REPORT_HINT = "voice_intake_report"   # hint key: report()'s fixed text (message or note)
_MAX_TEXT = 300

# --- value rules (F-37: every kept field has one; a field without a rule is dropped) ------

_HTTP_METHODS = frozenset("GET POST PUT PATCH DELETE HEAD OPTIONS".split())
_LEVELS = frozenset("fatal error warning info debug".split())
_SPAN_STATUS = frozenset((
    "ok cancelled unknown invalid_argument deadline_exceeded not_found already_exists "
    "permission_denied resource_exhausted failed_precondition aborted out_of_range "
    "unimplemented internal_error unavailable data_loss unauthenticated").split())
_SOURCES = frozenset(("custom", "url", "route", "view", "component", "task"))
_OP = re.compile(r"(?:http|db|cache|queue|function|gen_ai|middleware|view|template|rq|"
                 r"subprocess|file|socket|serialize|default)"
                 r"(?:\.[a-z_]{1,24}(?:-[a-z_]{1,24})?){0,4}")
_ORIGIN = re.compile(r"(?:auto|manual)(?:\.[a-z_]{1,24}){0,5}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX16 = re.compile(r"[0-9a-f]{16}")
_HOST = re.compile(r"[a-z0-9.-]{1,253}(?::\d{1,5})?", re.I)
_UUID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.I)
# Names that come from our code (Flask endpoint, worker.<function>, module/function names).
_CODE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,63}(?:\.[a-z_][a-z0-9_]{0,63}){0,5}")
_REDIS_COMMANDS = frozenset((
    "GET SET SETEX SETNX DEL EXISTS EXPIRE PEXPIRE PERSIST TTL PTTL TYPE KEYS SCAN INCR INCRBY "
    "HGET HSET HMSET HGETALL HMGET HDEL HEXISTS HINCRBY HKEYS HLEN "
    "LPUSH RPUSH LPOP RPOP BLPOP BRPOP BLMOVE LMOVE LRANGE LREM LLEN LINDEX LTRIM "
    "SADD SREM SMEMBERS SISMEMBER SCARD "
    "ZADD ZREM ZRANGE ZRANGEBYSCORE ZREVRANGEBYSCORE ZREMRANGEBYSCORE ZCARD ZSCORE ZPOPMIN "
    "MULTI EXEC WATCH UNWATCH DISCARD PING INFO CLIENT EVAL EVALSHA SCRIPT PUBLISH "
    "SUBSCRIBE UNSUBSCRIBE TIME").split())
# report() tag values. A new tag needs a rule here or it is not sent.
_TAG_RULES = {
    "component": {"orchestrator", "worker"},
    "job_id": re.compile(rf"{_JOB_ID}|{_DOC_ID}"),     # traced_job tags its first arg
    "step": {"comment", "return_task", "material_list", "vendor_list"},
    "mode": {"dry_run", "off", "test_customer_only", "live"},
    "manual": {"True", "False"},
    "status": {"fieldpulse_error", "config_error", "fieldpulse_unreachable", "fieldpulse_timeout"},
    "http_status": re.compile(r"\d{3}"),
    "route": lambda v: _known_route(v),                 # Flask URL rule template, not the path
    # DB pool health (F-82): numeric psycopg_pool counters, a flag, an exception class name.
    "pool_replaced": {"True", "False"},
    "exc_class": re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}"),
    **{k: re.compile(r"\d{1,9}") for k in (
        "pool_min", "pool_max", "pool_size", "pool_available", "requests_waiting",
        "requests_num", "requests_queued", "requests_errors", "returns_bad",
        "connections_num", "connections_errors", "connections_lost")},
}

# --- incoming routes / outgoing URLs (F-38) -----------------------------------------------

_UNMATCHED_ROUTE = "[unmatched route]"
_flask_app = None          # remembered from Flask's request_started signal (orchestrator)
_JOB_NAMES = set()         # "worker.<fn>" for every @traced_job function
_APP_ORIGINS = frozenset() # exact app origins (scheme+host), from CLERK_AUTHORIZED_PARTIES
# Upstream (host, path) pairs whose path is a fixed template; other outgoing URLs
# keep scheme+host only (R2 object keys, FieldPulse ids, ...).
_UPSTREAM_PATHS = frozenset((("api.deepgram.com", "/v1/listen"),
                             ("api.anthropic.com", "/v1/messages")))


def _remember_flask_app(sender, **_kwargs):
    global _flask_app
    _flask_app = sender


def _route_template(url, method):
    """The Flask URL rule that serves this request URL, else _UNMATCHED_ROUTE.
    Only the template ever leaves; the real path, query and Host never do."""
    app = _flask_app
    if app is None or not isinstance(url, str):
        return _UNMATCHED_ROUTE
    try:
        from urllib.parse import unquote
        path = unquote(urlsplit(url.strip()).path) or "/"
        rule, _args = app.url_map.bind("localhost").match(
            path, method=_method(method) or "GET", return_rule=True)
        return rule.rule if isinstance(rule.rule, str) else _UNMATCHED_ROUTE
    except Exception:  # noqa: BLE001 — 404/405/redirect/anything: no template
        return _UNMATCHED_ROUTE


def _known_route(v):
    if not isinstance(v, str):
        return None
    if v in (_UNMATCHED_ROUTE, "/unmatched"):
        return v
    app = _flask_app
    try:
        return v if app is not None and v in {r.rule for r in app.url_map.iter_rules()} else None
    except Exception:  # noqa: BLE001
        return None


def _origin_of(value):
    """scheme://host[:port] of an absolute http(s) URL (no userinfo), else None."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        host = parts.hostname.lower()
        if not _HOST.fullmatch(host):
            return None
        return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")
    except ValueError:
        return None


def _outgoing_url(value):
    """Outgoing request URL: scheme+host only, plus the path when (host, path) is
    a known fixed upstream template. No userinfo, query, fragment, ids."""
    origin = _origin_of(value)
    if origin is None:
        return None
    path = urlsplit(value.strip()).path
    host = urlsplit(origin).hostname
    return origin + path if (host, path) in _UPSTREAM_PATHS else origin


def _app_origin(v):
    origin = _origin_of(v)
    return origin if origin is not None and origin == v.strip() and origin in _APP_ORIGINS \
        else None


def _rule(rule, value):
    """Apply a value rule: a set (enum of strings), a regex (fullmatch), or a function."""
    if isinstance(rule, (set, frozenset)):
        return value if isinstance(value, str) and value in rule else None
    if isinstance(rule, re.Pattern):
        return value if isinstance(value, str) and rule.fullmatch(value) else None
    return rule(value)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _status_code(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 599 else None


def _count(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10_000_000 else None


def _duration(v):
    return v if _num(v) is not None and 0 <= v <= 1_000_000_000 else None


def _bool(v):
    return v if isinstance(v, bool) else None


def _any_time(v):
    import datetime
    return v if isinstance(v, (datetime.datetime, str, int, float)) and not isinstance(v, bool) \
        else None


def _method(v):
    return v.upper() if isinstance(v, str) and v.upper() in _HTTP_METHODS else None


def _redis_command(v):
    return v.upper() if isinstance(v, str) and v.upper() in _REDIS_COMMANDS else None


def _op(v):
    return v if isinstance(v, str) and _OP.fullmatch(v) else "default"


def _pick(src, rules):
    out = {}
    if isinstance(src, dict):
        for key, rule in rules.items():
            val = _rule(rule, src.get(key))
            if val is not None:
                out[key] = val
    return out


_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>]+")


def _scrub_text(value):
    """Embedded URLs in a log TEMPLATE (code text) keep scheme+host only."""
    if not isinstance(value, str):
        return value
    return _URL_IN_TEXT.sub(lambda m: _outgoing_url(m.group(0)) or _REDACTED, value)[:_MAX_TEXT]


# Span/breadcrumb/trace data: EXACT key -> rule (F-39: no prefix matches). Keys
# without a rule are dropped (query strings, bodies, headers, redis keys/args, AI
# prompts, HTTP reason phrases, thread ids/names, ...). Numbers only for counts,
# durations and status codes, each bounded.
_DATA_RULES = {
    **{k: _outgoing_url for k in ("url", "http.url", "url.full")},
    **{k: _method for k in ("method", "http.method", "http.request.method")},
    **{k: _status_code for k in ("status_code", "http.status_code", "http.response.status_code")},
    **{k: _count for k in (
        "messaging.message.retry.count", "gen_ai.request.max_tokens",
        "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens",
        "gen_ai.usage.input_tokens.cached", "gen_ai.usage.input_tokens.cache_write",
        "gen_ai.usage.output_tokens.reasoning", "gen_ai.usage.cache_creation.input_tokens",
        "gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.reasoning.output_tokens")},
    "messaging.message.receive.latency": _duration,
    **{k: _bool for k in ("gen_ai.response.streaming", "redis.is_cluster")},
    **{k: _redis_command for k in ("redis.command", "db.operation")},
    "server.address": _HOST,
    "db.system": {"redis", "postgresql"},
    "messaging.system": {"rq"},
    "messaging.destination.name": {"consultations"},          # queue_setup.QUEUE_NAME
    "messaging.message.id": _UUID,
    "gen_ai.system": {"anthropic"},
    "gen_ai.operation.name": {"chat"},
    "gen_ai.request.model": re.compile(r"claude-[a-z0-9.-]{1,60}"),
    "gen_ai.response.model": re.compile(r"claude-[a-z0-9.-]{1,60}"),
    "sentry.origin": _ORIGIN,
    "sentry.op": lambda v: v if isinstance(v, str) and _OP.fullmatch(v) else None,
    "sentry.source": _SOURCES,
}


def _safe_data(data):
    if not isinstance(data, dict):
        return None
    out = {}
    for k, v in data.items():
        if isinstance(k, str) and k in _DATA_RULES:
            v = _rule(_DATA_RULES[k], v)
            if v is not None:
                out[k] = v
    return out or None


# --- dynamic sampling context (F-34) ---------------------------------------------------

def _safe_dsc(dsc, own):
    """Only trace_id / sample_rate / sample_rand / sampled from the (possibly
    incoming, untrusted) DSC; public_key / environment / release are our own."""
    if not isinstance(dsc, dict):
        return None
    out = _pick(dsc, {"trace_id": _HEX32,
                      "sample_rate": re.compile(r"(?:0|1|0?\.\d{1,16}|1\.0+|\d(?:\.\d+)?e-\d{1,3})"),
                      "sample_rand": re.compile(r"0|0?\.\d{1,16}"),
                      "sampled": {"true", "false"}})
    if "trace_id" not in out:
        return None
    for k in ("public_key", "environment", "release"):
        if isinstance(own.get(k), str) and own[k]:
            out[k] = own[k]
    return out


def _own_dsc_values(options, public_key):
    return {"public_key": public_key, "environment": options.get("environment"),
            "release": options.get("release")}


def init_sentry(component):
    """Initialise Sentry for this process. Returns True when enabled."""
    global _enabled
    load_dotenv(_HERE / ".env")
    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        return False
    if _enabled:
        return True
    try:
        import sentry_sdk
        from sentry_sdk.integrations.anthropic import AnthropicIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:  # requirements.txt pins it; never break boot over monitoring
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; monitoring off")
        return False
    try:
        if not sentry_sdk.get_client().is_active():
            sentry_sdk.init(
                dsn=dsn,
                environment=_environment(),
                release=(os.environ.get("SENTRY_RELEASE") or os.environ.get("RENDER_GIT_COMMIT")
                         or None),
                traces_sample_rate=1.0,
                send_default_pii=False,
                max_request_body_size="never",
                include_local_variables=False,
                enable_logs=False,
                integrations=[
                    LoggingIntegration(level=logging.INFO, event_level=None),
                    AnthropicIntegration(include_prompts=False),
                ],
                before_send=_before_send,
                before_send_transaction=_before_send_transaction,
                before_breadcrumb=_before_breadcrumb,
                # Final boundary: only scrubbed item types, rebuilt trace header (F-33/F-34).
                transport=_safe_transport_class(),
                # No sentry-trace/baggage on outgoing HTTP (Deepgram, Anthropic, R2,
                # FieldPulse). RQ job propagation uses job.meta and is unaffected.
                trace_propagation_targets=[],
            )
        sentry_sdk.get_global_scope().set_tag("component", component)
        _remember_config()
        _forward_pool_warnings()
    except Exception:  # noqa: BLE001 — a bad DSN must not stop the app booting
        logger.exception("Sentry init failed; monitoring off")
        return False
    _enabled = True
    return True


def _remember_config():
    """App origins (for the Origin header rule) and the Flask app (for route templates)."""
    global _APP_ORIGINS
    raw = os.environ.get("CLERK_AUTHORIZED_PARTIES") or ""
    _APP_ORIGINS = frozenset(o for o in map(_origin_of, raw.split(",")) if o)
    try:
        from flask import request_started
        request_started.connect(_remember_flask_app, weak=False)
    except ImportError:  # worker-only installs: no incoming requests to name
        pass


# psycopg.pool WARNING+ records -> one Sentry message (F-82). Their text can hold
# the DB host/user ("error connecting in 'pool-1': connection to server at ...
# failed"), so only a FIXED text and the exception's class name are sent, at most
# once per _POOL_LOG_EVERY_S (the pool's reconnect loop can log every second).
_POOL_LOG_MESSAGE = "Database pool warning (psycopg.pool)"
_POOL_LOG_EVERY_S = 60.0


class _PoolLogForwarder(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self._last = None
        self._lock_ = threading.Lock()

    def emit(self, record):
        try:
            now = time.monotonic()
            with self._lock_:
                if self._last is not None and now - self._last < _POOL_LOG_EVERY_S:
                    return
                self._last = now
            exc = record.exc_info[1] if record.exc_info and record.exc_info[1] else next(
                (a for a in (record.args if isinstance(record.args, tuple) else ())
                 if isinstance(a, BaseException)), None)
            report(message=_POOL_LOG_MESSAGE,
                   level="error" if record.levelno >= logging.ERROR else "warning",
                   exc_class=type(exc).__name__ if exc is not None else "none")
        except Exception:  # noqa: BLE001 — logging must never break the pool
            pass


def _forward_pool_warnings():
    pool_logger = logging.getLogger("psycopg.pool")
    if not any(isinstance(h, _PoolLogForwarder) for h in pool_logger.handlers):
        pool_logger.addHandler(_PoolLogForwarder())
    if pool_logger.level == logging.NOTSET or pool_logger.level > logging.WARNING:
        pool_logger.setLevel(logging.WARNING)


def traced_job(fn):
    """Wrap an RQ job function (first argument: our job id). See module doc."""
    _JOB_NAMES.add(f"worker.{fn.__name__}")
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _enabled:
            return fn(*args, **kwargs)
        import sentry_sdk
        job_id = args[0] if args else kwargs.get("job_id")
        try:
            with sentry_sdk.new_scope() as scope:
                if job_id is not None:
                    scope.set_tag("job_id", str(job_id))
                with _job_transaction(fn.__name__):
                    try:
                        return fn(*args, **kwargs)
                    except Exception as exc:
                        _capture_unhandled(exc)
                        raise
        finally:
            # RQ ends the horse with os._exit(), which skips Sentry's atexit flush.
            sentry_sdk.flush(timeout=_FLUSH_TIMEOUT_S)
    return wrapper


def report(exc=None, message=None, level="error", **tags):
    """Report a handled problem. `message` MUST be fixed text, never customer or
    upstream data (it is the one free text sent as-is, and it groups the issue);
    put variable, non-personal values (job_id, step, status) in tags. The
    exception's own text is redacted before sending (see module doc)."""
    if not _enabled:
        return
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            for k, v in tags.items():
                if v is not None:
                    scope.set_tag(k, str(v)[:200])
            if exc is not None:
                # capture_event with our hint: before_send attaches `message` as the
                # voice_intake note only from here (a scope context is not trusted).
                from sentry_sdk.utils import event_from_exception
                event, hint = event_from_exception(
                    exc, client_options=sentry_sdk.get_client().options,
                    mechanism={"type": "generic", "handled": True})
                if message:
                    hint[_REPORT_HINT] = str(message)
                sentry_sdk.capture_event(event, hint=hint)
            else:
                # capture_event (not capture_message) so before_send can tell
                # this fixed text from any other message event.
                sentry_sdk.capture_event({"message": message or "unnamed problem",
                                          "level": level}, hint={_REPORT_HINT: True})
    except Exception:  # noqa: BLE001 — reporting must never break the caller
        logger.exception("Sentry report failed")


# --- internals ---------------------------------------------------------------------------

def _environment():
    env = (os.environ.get("SENTRY_ENVIRONMENT") or "").strip()
    if env:
        return env
    return "production" if os.environ.get("RENDER") else "development"


def _job_transaction(name):
    import contextlib
    import sentry_sdk
    if sentry_sdk.get_current_span() is not None:
        return contextlib.nullcontext()
    headers = {}
    try:
        from rq import get_current_job
        rq_job = get_current_job()
        if rq_job is not None:
            headers = rq_job.meta.get("_sentry_trace_headers") or {}
    except Exception:  # noqa: BLE001 — no trace to continue is fine
        headers = {}
    transaction = sentry_sdk.continue_trace(
        headers, op="queue.task.rq", name=f"worker.{name}", source="task")
    return sentry_sdk.start_transaction(transaction)


def _capture_unhandled(exc):
    import sentry_sdk
    from sentry_sdk.utils import event_from_exception
    client = sentry_sdk.get_client()
    event, hint = event_from_exception(
        exc, client_options=client.options, mechanism={"type": "rq", "handled": False})
    sentry_sdk.capture_event(event, hint=hint)


def _client_own_values():
    import sentry_sdk
    client = sentry_sdk.get_client()
    dsn = getattr(client, "dsn", None) or client.options.get("dsn") or ""
    try:
        key = urlsplit(dsn).username
    except ValueError:
        key = None
    return _own_dsc_values(client.options, key)


# --- breadcrumbs --------------------------------------------------------------------------

_CRUMB_TYPES = frozenset(("default", "http", "log", "navigation", "query", "error", "info",
                          "debug", "redis", "subprocess"))
_CATEGORY = re.compile(r"[a-z_][a-z0-9_.-]{0,63}")      # logger names / integration names


def _clean_crumb(crumb, record=None):
    """before_breadcrumb (every breadcrumb passes here when it is added). Log
    records keep their TEMPLATE (code text, never the formatted values); every
    other message is dropped (custom/redis/event text, F-37)."""
    if record is not None:
        crumb["type"] = "log"
        # No args: the message may be a pre-formatted (f-string) line.
        crumb["message"] = _scrub_text(str(record.msg)) if getattr(record, "args", None) \
            else _LOG_OMITTED
        crumb.pop("data", None)
    elif crumb.get("type") == "log" and crumb.get("message"):
        crumb["message"] = _LOG_OMITTED       # a log-type crumb added by hand
    else:
        crumb.pop("message", None)
    return _rebuild_crumb(crumb)


def _rebuild_crumb(crumb):
    """Send-time pass (idempotent): allowlisted fields only."""
    if not isinstance(crumb, dict):
        return None
    out = _pick(crumb, {"timestamp": _any_time, "level": _LEVELS, "type": _CRUMB_TYPES,
                        "category": _CATEGORY})
    if out.get("type") == "log" and isinstance(crumb.get("message"), str):
        out["message"] = crumb["message"][:_MAX_TEXT]   # template / omitted marker
    data = _safe_data(crumb.get("data"))
    if data:
        out["data"] = data
    return out


# --- exceptions ---------------------------------------------------------------------------

def _exception_chain(hint):
    """Exception objects in the event's chain (outermost first)."""
    exc_info = (hint or {}).get("exc_info")
    exc = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) > 1 else None
    chain, seen = [], set()
    while isinstance(exc, BaseException) and id(exc) not in seen:
        chain.append(exc)
        seen.add(id(exc))
        exc = exc.__cause__ if exc.__suppress_context__ else exc.__context__
    return chain


def _safe_value(value, exc_type, exc_module, obj):
    """The telemetry copy of an exception message (the real exception and the
    job's Postgres error text are untouched)."""
    text = value if isinstance(value, str) else ""
    first = text.strip().split("\n", 1)[0]
    if (str(exc_module or "").split(".")[-1], exc_type) in _SAFE_TYPES:
        out = _scrub_text(first)
    elif any(p.fullmatch(first) for p in _SAFE_VALUES) and first == text.strip():
        out = first
    else:
        out = _REDACTED
        for p in _SAFE_PREFIXES:
            m = p.match(first)
            if m:
                out = (m.group(0) + (": " + _REDACTED if len(text.strip()) > m.end() else ""))
                break
    sqlstate = getattr(obj, "sqlstate", None)
    if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
        out = f"{out} (sqlstate {sqlstate})"
    return out


_PY_NAME = re.compile(r"[A-Za-z_][\w]{0,127}")
_PY_MODULE = re.compile(r"[A-Za-z_][\w]{0,63}(?:\.[A-Za-z_][\w]{0,63}){0,10}")
_PY_FUNC = re.compile(r"[\w.<>]{1,200}")
_PATH = re.compile(r"[\w./ <>@+-]{1,400}")


def _source_lines(v):
    # Source code context of our/library frames (code text, not data).
    if isinstance(v, str):
        return v[:_MAX_TEXT]
    if isinstance(v, list):
        return [x[:_MAX_TEXT] for x in v if isinstance(x, str)][:10]
    return None


def _frames(stacktrace):
    frames = []
    for f in (stacktrace or {}).get("frames") or [] if isinstance(stacktrace, dict) else []:
        clean = _pick(f, {"function": _PY_FUNC, "module": _PY_MODULE, "filename": _PATH,
                          "abs_path": _PATH, "lineno": _num, "colno": _num, "in_app": _bool,
                          "context_line": _source_lines, "pre_context": _source_lines,
                          "post_context": _source_lines})
        if clean:
            frames.append(clean)
    return {"frames": frames} if frames else None


def _exceptions(event, hint):
    chain = _exception_chain(hint)
    values = []
    for exc in (event.get("exception") or {}).get("values") or []:
        if not isinstance(exc, dict):
            continue
        # Pair the payload entry with its exception object (for sqlstate).
        obj = next((o for o in chain if type(o).__name__ == exc.get("type")), None)
        if obj is not None:
            chain.remove(obj)
        out = _pick(exc, {"type": _PY_NAME, "module": _PY_MODULE})
        out["value"] = _safe_value(exc.get("value"), exc.get("type"), exc.get("module"), obj)
        mech = _pick(exc.get("mechanism"), {
            "type": re.compile(r"[a-z][a-z0-9_.]{0,63}"), "handled": _bool, "synthetic": _bool,
            "exception_id": _num, "parent_id": _num, "is_exception_group": _bool,
            "source": re.compile(r"__cause__|__context__|exceptions\[\d{1,3}\]")})
        if mech:
            out["mechanism"] = mech
        st = _frames(exc.get("stacktrace"))
        if st:
            out["stacktrace"] = st
        values.append(out)
    return {"values": values} if values else None


def _threads(event):
    values = []
    for t in (event.get("threads") or {}).get("values") or []:
        if isinstance(t, dict):
            out = _pick(t, {"id": _num, "current": _bool, "crashed": _bool, "main": _bool})
            st = _frames(t.get("stacktrace"))
            if st:
                out["stacktrace"] = st
            values.append(out)
    return {"values": values} if values else None


# --- request / contexts / tags --------------------------------------------------------------

# Request headers: exact names, fixed values (F-39). No Host (caller-controlled).
_CONTENT_TYPES = frozenset(("application/json", "text/plain", "multipart/form-data",
                            "application/x-www-form-urlencoded", "application/octet-stream"))


def _content_type(v):
    if not isinstance(v, str):
        return None
    base = v.split(";", 1)[0].strip().lower()
    if base in _CONTENT_TYPES:
        return base
    return "audio/*" if re.fullmatch(r"audio/[a-z0-9.+-]{1,32}", base) else None


_HEADER_RULES = {
    "content-type": _content_type,
    "content-length": re.compile(r"\d{1,12}"),
    "origin": _app_origin,
}
_SAFE_HEADERS = set(_HEADER_RULES)


def _request(req, route):
    """Incoming request: method, the route TEMPLATE as url (F-38), safe headers."""
    out = _pick(req, {"method": _method})
    out["url"] = route
    headers = req.get("headers")
    if isinstance(headers, dict):
        clean = {}
        for k, v in headers.items():
            if isinstance(k, str) and k.lower() in _HEADER_RULES:
                val = _rule(_HEADER_RULES[k.lower()], v)
                if val is not None:
                    clean[k] = val
        if clean:
            out["headers"] = clean
    return out


def _trace_context(t, own):
    if not isinstance(t, dict):
        return None
    out = _pick(t, {"trace_id": _HEX32, "span_id": _HEX16, "parent_span_id": _HEX16,
                    "status": _SPAN_STATUS, "origin": _ORIGIN})
    if t.get("op") is not None:
        out["op"] = _op(t.get("op"))
    data = _safe_data(t.get("data"))
    if data:
        out["data"] = data
    # The SDK moves this into the envelope header after before_send (F-34).
    dsc = _safe_dsc(t.get("dynamic_sampling_context"), own)
    if dsc:
        out["dynamic_sampling_context"] = dsc
    return out


def _contexts(contexts, hint, own):
    out = {}
    if isinstance(contexts, dict):
        trace = _trace_context(contexts.get("trace"), own)
        if trace:
            out["trace"] = trace
        runtime = _pick(contexts.get("runtime"), {"name": {"CPython", "PyPy"},
                                                  "version": re.compile(r"\d+(?:\.\d+){0,3}")})
        if runtime:
            out["runtime"] = runtime
    note = (hint or {}).get(_REPORT_HINT)
    if isinstance(note, str) and note:
        out["voice_intake"] = {"note": note[:_MAX_TEXT]}     # report()'s fixed text
    return out or None


def _tags(tags):
    out = {}
    if isinstance(tags, dict):
        for k, v in tags.items():
            if k in _TAG_RULES and _rule(_TAG_RULES[k], v) is not None:
                out[k] = v
    return out or None


# --- spans --------------------------------------------------------------------------------

def _description(op, desc):
    """Rebuilt from the op; free text otherwise dropped."""
    if not isinstance(desc, str):
        return None
    if op.startswith("http"):
        m = re.fullmatch(r"([A-Za-z]{3,7}) (\S+)", desc.strip())
        url = _outgoing_url(m.group(2)) if m and _method(m.group(1)) else None
        return f"{m.group(1).upper()} {url}" if url else None
    if op.startswith("db.redis"):
        return _redis_command(desc.split(" ", 1)[0])
    if op.startswith("queue."):
        return desc if _CODE_NAME.fullmatch(desc) else None
    return None


def _span(s):
    if not isinstance(s, dict):
        return None
    out = _pick(s, {"span_id": _HEX16, "parent_span_id": _HEX16, "trace_id": _HEX32,
                    "start_timestamp": _any_time, "timestamp": _any_time,
                    "same_process_as_parent": _bool, "status": _SPAN_STATUS,
                    "origin": _ORIGIN})
    out["op"] = _op(s.get("op"))
    desc = _description(out["op"], s.get("description"))
    if desc:
        out["description"] = desc
    data = _safe_data(s.get("data"))
    if data:
        out["data"] = data
    return out


def _job_name(v):
    """Non-request transaction names: only our @traced_job names."""
    if not isinstance(v, str):
        return None
    return v if v in _JOB_NAMES else _REDACTED


# --- sdk metadata (F-41) ------------------------------------------------------------------

# Integration names that may be listed (and only if the client really has them).
_PY_INTEGRATIONS = frozenset((
    "anthropic argv atexit boto3 dedupe excepthook flask httpx logging modules redis rq "
    "stdlib threading").split())


def _trusted_sdk(with_integrations=True):
    """`sdk` from the installed SDK's own constants, never from the payload."""
    import sentry_sdk
    from sentry_sdk.client import SDK_INFO
    name = SDK_INFO.get("name") if isinstance(SDK_INFO, dict) else None
    if not (isinstance(name, str) and re.fullmatch(r"sentry\.python(?:\.[a-z_]{1,32})?", name)):
        name = "sentry.python"
    version = str(sentry_sdk.VERSION)
    if not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}[a-z0-9.]{0,16}", version):
        version = "unknown"
    out = {"name": name, "version": version}
    if with_integrations:
        installed = getattr(sentry_sdk.get_client(), "integrations", None) or {}
        out["integrations"] = sorted(_PY_INTEGRATIONS & set(installed))
    return out


# --- events ---------------------------------------------------------------------------------

def _rebuild_event(event, hint):
    """A NEW event holding only allowlisted, value-checked fields (F-31..F-37)."""
    own = _client_own_values()
    out = _pick(event, {
        "event_id": _HEX32, "timestamp": _any_time, "start_timestamp": _any_time,
        "platform": {"python"}, "level": _LEVELS, "type": {"transaction"},
        "environment": re.compile(r"[\w.-]{1,64}"), "release": re.compile(r"[\w.@+-]{1,128}"),
        "server_name": _HOST, "transaction": _job_name,
    })
    out["sdk"] = _trusted_sdk()                    # F-41: never the payload's sdk
    if isinstance(event.get("_dropped_spans"), int):
        out["_dropped_spans"] = event["_dropped_spans"]   # SDK bookkeeping, not sent
    source = _rule(_SOURCES, (event.get("transaction_info") or {}).get("source"))
    if source:
        out["transaction_info"] = {"source": source}
    req = event.get("request")
    if isinstance(req, dict):
        # F-38: an incoming request is named by its route template, never its path.
        route = _route_template(req.get("url"), req.get("method"))
        out["request"] = _request(req, route)
        if "transaction" in event or event.get("type") == "transaction":
            out["transaction"] = route
            out["transaction_info"] = {"source": "route"}
    for key, value in (("exception", _exceptions(event, hint)),
                       ("threads", _threads(event)),
                       ("contexts", _contexts(event.get("contexts"), hint, own)),
                       ("tags", _tags(event.get("tags")))):
        if value:
            out[key] = value
    crumbs = event.get("breadcrumbs")
    crumbs = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
    if isinstance(crumbs, list):
        out["breadcrumbs"] = {"values": [c for c in map(_rebuild_crumb, crumbs) if c]}
    # Free-text message: report()'s fixed text (hint), or nothing.
    has_message = event.get("message") is not None or event.get("logentry") is not None
    if has_message:
        trusted = (hint or {}).get(_REPORT_HINT) is True
        out["message"] = (str(event.get("message"))[:_MAX_TEXT]
                          if trusted and event.get("message") is not None else _REDACTED_MESSAGE)
    if isinstance(event.get("spans"), list):
        out["spans"] = [s for s in map(_span, event["spans"]) if s]
    # measurements: none from the Python SDK; free-form keys, so never sent (F-39).
    return out


def _before_send(event, hint):
    try:
        values = (event.get("exception") or {}).get("values") or []
        if values and (values[-1].get("mechanism") or {}).get("type") == "anthropic":
            # The Anthropic integration reports API errors itself; the worker's
            # job-failure event already carries the same error (chained under
            # ExtractionError), so this would be a second issue per failure.
            # The integration stays for its AI spans (timing/tokens, no prompts).
            return None
        return _rebuild_event(event, hint)
    except Exception:  # noqa: BLE001 — drop the event rather than leak on a scrub bug
        return None


def _before_send_transaction(event, hint):
    try:
        return _rebuild_event(event, hint)
    except Exception:  # noqa: BLE001
        return None


def _before_breadcrumb(crumb, hint):
    try:
        return _clean_crumb(crumb, (hint or {}).get("log_record"))
    except Exception:  # noqa: BLE001
        return None


# --- transport: the final boundary (F-33/F-34) -------------------------------------------

# Item types whose payload went through before_send / before_send_transaction
# (or carries no free text). Everything else (streamed spans, logs, metrics,
# profiles, attachments, check-ins, ...) is dropped here.
_ITEM_TYPES = frozenset(("event", "transaction", "session", "sessions", "client_report"))


_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?")
_SESSION_STATUS = frozenset(("ok", "exited", "crashed", "abnormal"))
_ABNORMAL = frozenset(("anr_foreground", "anr_background"))


def _session_attrs(own):
    release, env = own.get("release"), own.get("environment")
    if not (isinstance(release, str) and release):
        return None                       # Sentry ignores sessions without a release
    return {"release": release, **({"environment": env} if isinstance(env, str) and env else {})}


def _safe_session(payload, own):
    """F-40: an individual session rebuilt from a strict schema; attrs are ours."""
    attrs = _session_attrs(own)
    out = _pick(payload, {
        "sid": _UUID, "init": _bool, "started": _ISO_TIME, "timestamp": _ISO_TIME,
        "status": _SESSION_STATUS, "errors": _count, "duration": _duration, "seq": _count,
        "abnormal_mechanism": _ABNORMAL})
    if attrs is None or "sid" not in out or "started" not in out:
        return None
    out["attrs"] = attrs
    return out


def _safe_session_aggregates(payload, own):
    """F-40: aggregate `sessions` rebuilt (started + counts only, no did)."""
    attrs = _session_attrs(own)
    rows = payload.get("aggregates") if isinstance(payload, dict) else None
    aggs = []
    for a in rows if isinstance(rows, list) else []:
        clean = _pick(a, {"started": _ISO_TIME, "exited": _count, "errored": _count,
                          "crashed": _count, "abnormal": _count})
        if "started" in clean:
            aggs.append(clean)
    if attrs is None or not aggs:
        return None
    return {"attrs": attrs, "aggregates": aggs}


def _sanitize_envelope(envelope, own):
    """Clean an outgoing Envelope in place; None when nothing is left to send."""
    kept = []
    for item in envelope.items:
        if item.type not in _ITEM_TYPES:
            continue
        payload = item.payload.json
        if item.type in ("session", "sessions"):
            fix = _safe_session if item.type == "session" else _safe_session_aggregates
            clean = fix(payload, own)
            if clean is None:
                continue
            item.payload = type(item.payload)(json=clean)
        elif item.type in ("event", "transaction"):
            if not isinstance(payload, dict):
                continue
            payload["sdk"] = _trusted_sdk()          # F-41, whatever the payload said
            item.payload = type(item.payload)(json=payload)
        kept.append(item)
    envelope.items[:] = kept
    if not envelope.items:
        return None
    headers = envelope.headers
    trace = _safe_dsc(headers.get("trace"), own)
    keep = _pick(headers, {"event_id": _HEX32, "sent_at": _ISO_TIME})
    if "sdk" in headers:
        keep["sdk"] = _trusted_sdk(with_integrations=False)
    if trace:
        keep["trace"] = trace
    headers.clear()
    headers.update(keep)
    return envelope


def _safe_transport_class():
    from sentry_sdk.transport import HttpTransport

    class SafeTransport(HttpTransport):
        """HttpTransport behind _sanitize_envelope (every envelope, every source)."""

        def capture_envelope(self, envelope):
            try:
                own = _own_dsc_values(self.options, self.parsed_dsn.public_key)
                clean = _sanitize_envelope(envelope, own)
            except Exception:  # noqa: BLE001 — drop rather than send unchecked
                return
            if clean is not None:
                self._deliver(clean)

        def _deliver(self, envelope):
            HttpTransport.capture_envelope(self, envelope)

    return SafeTransport
