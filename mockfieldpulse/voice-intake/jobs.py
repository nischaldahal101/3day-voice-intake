"""Postgres-backed job model for the async consultation pipeline.

Each consultation request becomes a row in `consultation_jobs`. The
orchestrator inserts rows here (status='queued') and pushes a queue
message; the RQ worker pops the message, updates status as it works,
and writes the result back here.

Conventions follow fieldpulse_writer.py + mock_fieldpulse.py:
  - load_dotenv at module import
  - module-level logger to stdout
  - single connection pool, opened at import (`_bootstrap`)
  - schema created idempotently — safe to import in any process

The `from __future__ import annotations` makes the `dict | None` return
type a lazy string, so we can use the Python 3.10+ union-type syntax on
the 3.9 venv without `from typing import Optional` clutter.
"""

from __future__ import annotations

import logging
import os
import re
import select
import sys
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from psycopg import OperationalError
from psycopg import errors as psycopg_errors
from psycopg.pq import ExecStatus, TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

_HERE = Path(__file__).resolve().parent

# --- Logging ----------------------------------------------------------------

logger = logging.getLogger("voice_intake.jobs")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


class JobError(Exception):
    """Raised when a job operation fails (e.g. update on a missing row)."""


# --- Database connection ---------------------------------------------------

load_dotenv(_HERE / ".env")

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://localhost:5432/fieldpulse_dev"
)

# Separate pool from the mock's (different process, different connections).
# Same database for dev; in production this can point at the orchestrator's
# own Postgres add-on.
#
# Hardening (review-log F-26): a request must never wait forever on the DB.
#   - check: each connection is pinged when handed out (with a 5 s deadline,
#     see _check_connection), so a dead one (DB restart, dropped TCP) is
#     replaced instead of failing/hanging a request.
#   - max_idle / max_lifetime: recycle connections well before Render/the
#     network drops them silently.
#   - timeout: getconn waits 10s, not 30s, so a stall surfaces quickly.
#   - server-side timeouts on every connection: no statement may run > 20s,
#     wait for a lock > 5s, or sit idle inside a transaction > 60s. Every
#     jobs.py transaction is a few short statements with no I/O between them
#     (the worker never holds a transaction across Claude / Deepgram /
#     FieldPulse HTTP calls), so these limits only ever cut off a stall.
_SERVER_OPTIONS = (
    "-c statement_timeout=20000 -c lock_timeout=5000 "
    "-c idle_in_transaction_session_timeout=60000"
)

# Client-side timeouts (review-log F-42). Production: after ~30 min idle the
# orchestrator's pooled connections went silently dead (no RST), and every DB
# request then failed with a 10 s PoolTimeout until a restart. The pool's
# per-checkout check (and any query) on such a socket waits with no client
# deadline (Linux only gives up after ~15 min of TCP retransmits), and once
# max_size checkouts are stuck nothing is left to hand out. So:
#   - connect_timeout: a new connection gives up after 10 s (psycopg's default
#     is 130 s), so the pool's reconnect loop keeps moving.
#   - TCP keepalives: an idle dead connection is detected within ~60 s
#     (30 s idle + 3 probes x 10 s) and then fails its check instantly; the
#     probes also keep NAT/proxy idle timers from dropping a live one.
#   - tcp_user_timeout: data unacknowledged for 30 s errors the socket (a query
#     sent into a dead link). Linux only; libpq accepts it and ignores it on
#     macOS.
#   - _check_connection: the checkout ping has its own 5 s deadline, so a stale
#     connection is discarded (and replaced) even when the TCP layer still looks
#     healthy (e.g. a stalled proxy in between).
_CLIENT_TIMEOUTS = {
    "connect_timeout": 10,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "tcp_user_timeout": 30000,
}
_CHECK_TIMEOUT_S = 5.0


def _check_connection(conn) -> None:
    """ConnectionPool.check_connection (an empty query) with a deadline.
    Raises on a broken, busy or unresponsive connection; the pool then closes
    it, opens a replacement and retries the checkout within its own timeout."""
    pgconn = conn.pgconn
    if pgconn.transaction_status != TransactionStatus.IDLE:
        raise OperationalError("pooled connection is not idle")
    deadline = time.monotonic() + _CHECK_TIMEOUT_S
    pgconn.send_query(b"")
    # One deadline covers the whole exchange (review-log F-44): get_result()
    # blocks while libpq is busy, so it is only called once is_busy() is false,
    # for every result INCLUDING the final end-of-results (ReadyForQuery). A
    # reply that arrives in pieces and then stalls fails here instead of hanging.
    results = 0
    while True:
        _wait_not_busy(pgconn, deadline)
        res = pgconn.get_result()
        if res is None:
            break
        results += 1
        if results > 1 or res.status not in (ExecStatus.EMPTY_QUERY, ExecStatus.COMMAND_OK):
            raise OperationalError("connection check failed")
    if results != 1 or pgconn.transaction_status != TransactionStatus.IDLE:
        raise OperationalError("connection check failed")


def _wait_not_busy(pgconn, deadline: float) -> None:
    """Flush and read until libpq can hand out the next result without
    blocking; raise OperationalError once `deadline` (monotonic) passes."""
    while True:
        flushed = pgconn.flush() == 0
        if flushed:
            pgconn.consume_input()
            if not pgconn.is_busy():
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OperationalError(
                f"connection check timed out after {_CHECK_TIMEOUT_S:.0f}s")
        sock = pgconn.socket
        select.select([sock], [] if flushed else [sock], [], remaining)

# Surface psycopg_pool's own warnings (e.g. "error connecting in 'pool-1':
# <real reason>") in the Render logs. Without a handler they are silently
# dropped and a failure only shows up as a bare PoolTimeout.
# (observability.py may add its own Sentry forwarder to this logger first, so
# look for a stream handler rather than "any handler".)
_pool_logger = logging.getLogger("psycopg.pool")
if not any(isinstance(h, logging.StreamHandler) for h in _pool_logger.handlers):
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _pool_logger.addHandler(_h)
    _pool_logger.setLevel(logging.WARNING)

def _new_pool() -> ConnectionPool:
    """The one place the pool settings live (import-time pool and every
    replacement made by note_pool_timeout). Not opened here."""
    return ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        open=False,
        timeout=10.0,
        max_idle=300.0,
        max_lifetime=1800.0,
        check=_check_connection,
        kwargs={"row_factory": dict_row, "options": _SERVER_OPTIONS, **_CLIENT_TIMEOUTS},
    )


# Every function below reads the module global `pool` at call time, so a
# replacement (note_pool_timeout) is picked up by the next call. Other modules
# and tests must use `jobs.pool`, never `from jobs import pool` (a stale copy).
pool = _new_pool()


# --- Pool self-repair + health (review-log F-82) ------------------------------
#
# Production (2026-11-05, 2026-11-08): every DB route failed with
# "PoolTimeout: couldn't get a connection after 10.00 sec" until a manual
# restart, even with the F-26/F-42/F-44 hardening above. The root cause is
# still unknown (dead pool worker threads, a hung connect, ...). Whatever it
# is, it lives inside one ConnectionPool object, so after repeated timeouts we
# throw that object away and start a fresh one with the same settings.
_REPAIR_THRESHOLD = 2          # PoolTimeouts ...
_REPAIR_WINDOW_S = 60.0        # ... within this many seconds trigger a repair
_REPAIR_COOLDOWN_S = 60.0      # at most one repair per this many seconds
_OLD_POOL_CLOSE_S = 5.0        # background close of the replaced pool
_HEALTH_CONN_TIMEOUT_S = 3.0   # /health: wait for a pooled connection
_HEALTH_DEADLINE_S = 4.5       # /health never waits longer than this, whatever hangs
# Numeric pool counters that may be logged / sent to Sentry (psycopg_pool names).
POOL_STAT_KEYS = ("pool_min", "pool_max", "pool_size", "pool_available", "requests_waiting",
                  "requests_num", "requests_queued", "requests_errors", "returns_bad",
                  "connections_num", "connections_errors", "connections_lost")

_repair_lock = threading.Lock()
_timeouts: list = []           # monotonic times of recent PoolTimeouts
_last_repair = None            # monotonic time of the last replacement
_ping_lock = threading.Lock()
_ping = None                   # (thread, result dict, pool) of the newest /health ping


def pool_stats() -> dict:
    """The current pool's numeric counters (POOL_STAT_KEYS only). Lock-free in
    psycopg_pool, so it cannot hang on a stuck pool. {} if unavailable."""
    try:
        raw = pool.get_stats()
    except Exception:  # noqa: BLE001 — stats must never break the caller
        return {}
    return {k: int(raw[k]) for k in POOL_STAT_KEYS
            if isinstance(raw.get(k), int) and not isinstance(raw.get(k), bool)}


def note_pool_timeout(seen_pool=None) -> dict:
    """Record one PoolTimeout (a request or /health could not get a connection).

    Logs the pool counters to stdout. After _REPAIR_THRESHOLD timeouts within
    _REPAIR_WINDOW_S (and at most once per _REPAIR_COOLDOWN_S) the module-level
    pool is replaced by a fresh one (opened without waiting: its connections
    are made in the background) and the old one is closed in a daemon thread,
    so a stuck old pool can never hang the caller. Thread-safe. Never raises.

    seen_pool: the pool that timed out, when the caller knows it (db_ping). If
    it is no longer the current pool (already replaced), the timeout is stale
    and ignored: it must never count toward replacing the fresh pool (F-83).

    Returns {"pool_replaced": bool, "stats": {counter: int}} (the counters of
    the pool that timed out, taken before any replacement)."""
    global pool, _last_repair
    stats = pool_stats()
    replaced, old = False, None
    now = time.monotonic()
    with _repair_lock:
        if seen_pool is not None and seen_pool is not pool:
            logger.info("DB pool timeout on an already replaced pool; ignored")
            return {"pool_replaced": False, "stats": {}}
        _timeouts[:] = [t for t in _timeouts if now - t <= _REPAIR_WINDOW_S] + [now]
        cooling = _last_repair is not None and now - _last_repair < _REPAIR_COOLDOWN_S
        if len(_timeouts) >= _REPAIR_THRESHOLD and not cooling:
            try:
                fresh = _new_pool()
                fresh.open(wait=False)
            except Exception:  # noqa: BLE001 — keep the old pool rather than none
                logger.exception("DB pool replacement failed; keeping the current pool")
            else:
                old, pool = pool, fresh
                _last_repair = now
                _timeouts.clear()
                replaced = True
    logger.error("DB pool timeout: stats=%s pool_replaced=%s",
                 " ".join(f"{k}={v}" for k, v in stats.items()) or "unavailable", replaced)
    if old is not None:
        threading.Thread(target=_close_quietly, args=(old,), daemon=True,
                         name="jobs-old-pool-close").start()
    return {"pool_replaced": replaced, "stats": stats}


def _close_quietly(old) -> None:
    try:
        old.close(timeout=_OLD_POOL_CLOSE_S)
        logger.info("Replaced DB pool closed")
    except Exception as exc:  # noqa: BLE001 — a background thread; just log the class
        logger.warning("Replaced DB pool did not close cleanly (%s)", type(exc).__name__)


def db_ping() -> bool:
    """True when the database answers SELECT 1 through the pool. For /health:
    returns within _HEALTH_DEADLINE_S even if the pool or the socket hangs (the
    ping runs in a daemon thread; while one is still running ON THE CURRENT
    POOL, later calls wait on it instead of starting another). A pool timeout or
    a hang counts as a PoolTimeout of the pool it ran on (note_pool_timeout), so
    failing health checks can repair the pool too. A ping still hung on a pool
    that has since been replaced is abandoned (F-83): it is neither waited on
    nor counted, so at most one abandoned ping per replacement (<= 1/min).
    Never raises."""
    global _ping
    with _ping_lock:
        if _ping is None or not _ping[0].is_alive() or _ping[2] is not pool:
            result, probed = {}, pool
            t = threading.Thread(target=_ping_once, args=(probed, result), daemon=True,
                                 name="jobs-db-ping")
            t.start()
            _ping = (t, result, probed)
        t, result, probed = _ping
    t.join(_HEALTH_DEADLINE_S)
    if result.get("ok") is True:
        return True
    if t.is_alive() or result.get("error") == "PoolTimeout":
        note_pool_timeout(seen_pool=probed)
    else:
        logger.error("DB health check failed (%s)", result.get("error") or "unknown")
    return False


def _ping_once(probed, result: dict) -> None:
    try:
        with probed.connection(timeout=_HEALTH_CONN_TIMEOUT_S) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        result["ok"] = True
    except PoolTimeout:
        result["error"] = "PoolTimeout"
    except Exception as exc:  # noqa: BLE001 — class name only (messages can hold the host)
        result["error"] = type(exc).__name__


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS consultation_jobs (
    id           TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'queued',
    customer_id  TEXT,
    audio_key    TEXT,
    source       TEXT,
    result       JSONB,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_consultation_jobs_status
    ON consultation_jobs(status);
CREATE INDEX IF NOT EXISTS idx_consultation_jobs_created_at
    ON consultation_jobs(created_at DESC);
-- Additive (prod already has the table): the customer the rep picked, as
-- {id, display_name, first_name, last_name, phone, email}. NULL for older rows.
ALTER TABLE consultation_jobs ADD COLUMN IF NOT EXISTS customer JSONB;
-- External customer id (e.g. a real FieldPulse id from the picker) -> the
-- mock FieldPulse customer the writer uses for it, so a repeat consultation
-- reuses the same mock customer and nothing is matched by name.
CREATE TABLE IF NOT EXISTS customer_links (
    source            TEXT NOT NULL,
    external_id       TEXT NOT NULL,
    mock_customer_id  TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, external_id)
);
-- Additive: a pasted transcript (paste path: no audio_key, the worker skips
-- transcription and reads this). NULL for recorded/uploaded jobs.
ALTER TABLE consultation_jobs ADD COLUMN IF NOT EXISTS transcript TEXT;
-- One row per (job, FieldPulse write step), owned by fieldpulse_sync.py.
-- UNIQUE(job_id, step) is the idempotency guard: a 'succeeded' or 'sent'
-- step is never sent again. `request` is {method, path, body} only (never
-- headers or the API key); `response` is truncated.
CREATE TABLE IF NOT EXISTS fieldpulse_writes (
    job_id                TEXT NOT NULL REFERENCES consultation_jobs(id) ON DELETE CASCADE,
    step                  TEXT NOT NULL,
    status                TEXT NOT NULL
        CHECK (status IN ('planned', 'sent', 'succeeded', 'failed', 'skipped')),
    mode                  TEXT NOT NULL,
    request               JSONB,
    response              JSONB,
    fieldpulse_record_id  TEXT,
    error                 TEXT,
    attempts              INTEGER NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, step)
);
-- Vendor documents (stage P1b): one row per uploaded vendor PDF (R2 key under
-- vendor-docs/), read by the worker (worker.process_vendor_document) into
-- `result` = vendor_documents.read_vendor_document()'s dict. Not linked to a
-- consultation; `customer` is the picker's identity snapshot, like consultations.
CREATE TABLE IF NOT EXISTS vendor_documents (
    id           TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'done', 'failed')),
    customer_id  TEXT,
    customer     JSONB,
    filename     TEXT,
    file_key     TEXT NOT NULL UNIQUE,   -- one row per upload (delete removes the PDF)
    file_size    INTEGER,
    result       JSONB,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_vendor_documents_created_at
    ON vendor_documents(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vendor_documents_customer
    ON vendor_documents(customer_id, created_at DESC);
-- Vendor materials (stage P2): one row per customer for OUR combined vendor material
-- list in FieldPulse (fieldpulse_sync.sync_vendor_materials is the only writer).
-- lease_until serializes syncs per customer (claimed under FOR UPDATE, never held
-- across the HTTP call); resync = another sync asked while the lease was taken.
-- create_pending: a POST was dispatched and has not answered with an id (never
-- auto-retried); held: recorded in dry_run (only a manual Send releases it).
CREATE TABLE IF NOT EXISTS vendor_material_lists (
    customer_id         TEXT PRIMARY KEY,
    fieldpulse_list_id  BIGINT,
    status              TEXT NOT NULL DEFAULT 'new',
    mode                TEXT,
    last_request        JSONB,
    last_response       JSONB,
    last_error          TEXT,
    history             JSONB NOT NULL DEFAULT '[]'::jsonb,
    lease_until         TIMESTAMPTZ,
    resync              BOOLEAN NOT NULL DEFAULT false,
    resync_manual       BOOLEAN NOT NULL DEFAULT false,
    create_pending      BOOLEAN NOT NULL DEFAULT false,
    held                BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Ferguson carts (stage E2): each signed-in user's inbound address <alias>@<domain>
-- (carts_panel picks the alias) and every accepted cart email (raw .eml in R2 under
-- ferguson-carts/, parsed by ferguson_cart.parse_cart_email; Decimals as strings).
-- UNIQUE(clerk_user_id, raw_sha256): the same email forwarded twice is one cart.
CREATE TABLE IF NOT EXISTS cart_addresses (
    clerk_user_id  TEXT PRIMARY KEY,
    alias          TEXT NOT NULL UNIQUE CHECK (alias ~ '^[a-z0-9.-]{1,40}$'),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ferguson_carts (
    id                      TEXT PRIMARY KEY,
    clerk_user_id           TEXT NOT NULL,
    alias                   TEXT NOT NULL,
    received_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    sender                  TEXT,
    message_id              TEXT,
    raw_key                 TEXT NOT NULL,
    raw_sha256              TEXT NOT NULL,
    parsed                  JSONB,
    ok                      BOOLEAN NOT NULL DEFAULT false,
    warnings                JSONB NOT NULL DEFAULT '[]'::jsonb,
    project_name            TEXT,
    status                  TEXT NOT NULL DEFAULT 'needs_customer',
    fieldpulse_customer_id  TEXT,
    UNIQUE (clerk_user_id, raw_sha256)
);
CREATE INDEX IF NOT EXISTS idx_ferguson_carts_user
    ON ferguson_carts(clerk_user_id, received_at DESC);
-- Stage E3 (additive): the FieldPulse customer a cart is linked to. `customer` = snapshot
-- {id, display_name, first_name, last_name, phone, email, company_name, address} read from
-- customer_source at link time (NULL while status = 'needs_customer'); `customer_match` =
-- {match: auto|manual|null, state: pending|done|error, reason, at}. status: needs_customer |
-- linked (E4 adds sending | sent | failed; relinking is allowed only from needs_customer/linked).
ALTER TABLE ferguson_carts ADD COLUMN IF NOT EXISTS customer JSONB;
ALTER TABLE ferguson_carts ADD COLUMN IF NOT EXISTS customer_match JSONB;
-- Stage E4 (additive): sending a linked cart to FieldPulse (fieldpulse_sync.sync_cart is
-- the only writer). Cart status gains 'sent' (set right BEFORE the first request goes out;
-- a sent cart can't be relinked). fieldpulse = the cart's last send record {status,
-- message, error, mode, at, estimate_id, list_id, steps{estimate, material_list}, replaces};
-- superseded_by = the newer cart (same user + customer + project) that took its records.
ALTER TABLE ferguson_carts ADD COLUMN IF NOT EXISTS fieldpulse JSONB;
ALTER TABLE ferguson_carts ADD COLUMN IF NOT EXISTS fieldpulse_history JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE ferguson_carts ADD COLUMN IF NOT EXISTS superseded_by TEXT;
-- One row per (user, customer, normalized project name): the ONE draft estimate + ONE
-- material list every cart of that project writes to (a re-forward PUTs the same ids).
-- Same safety model as vendor_material_lists: lease_until serializes syncs; per target
-- *_create_pending = a POST went out without an id back (automatic syncs then send
-- nothing); held = recorded in dry_run (only a manual Send releases it). *_cart_id = the
-- cart whose lines that target holds (set only when it was written successfully).
CREATE TABLE IF NOT EXISTS cart_fieldpulse_slots (
    clerk_user_id            TEXT NOT NULL,
    customer_id              TEXT NOT NULL,
    project_key              TEXT NOT NULL,
    cart_id                  TEXT,
    estimate_id              BIGINT,
    estimate_status          TEXT,
    estimate_cart_id         TEXT,
    estimate_create_pending  BOOLEAN NOT NULL DEFAULT false,
    list_id                  BIGINT,
    list_status              TEXT,
    list_cart_id             TEXT,
    list_create_pending      BOOLEAN NOT NULL DEFAULT false,
    held                     BOOLEAN NOT NULL DEFAULT false,
    mode                     TEXT,
    lease_until              TIMESTAMPTZ,
    resync                   BOOLEAN NOT NULL DEFAULT false,
    resync_manual            BOOLEAN NOT NULL DEFAULT false,
    resync_cart_id           TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (clerk_user_id, customer_id, project_key)
);
-- R4 (F-114/F-115, 2026-11-13): a supersede becomes PERMANENT once the newer cart made a
-- real (non-dry-run) request for the slot. superseded_final_customer = the customer both
-- carts were on when that happened (written by mark_cart_sending in the same transaction
-- as the request attempt); see _LIVE_SUPERSEDED. writer_* = the NEWEST cart that ever made
-- a real request for the slot; an older cart never sends into it again (mark refuses).
ALTER TABLE ferguson_carts ADD COLUMN IF NOT EXISTS superseded_final_customer TEXT;
ALTER TABLE cart_fieldpulse_slots ADD COLUMN IF NOT EXISTS writer_cart_id TEXT;
ALTER TABLE cart_fieldpulse_slots ADD COLUMN IF NOT EXISTS writer_received_at TIMESTAMPTZ;
"""


# Fixed key for pg_advisory_xact_lock: serializes schema bootstrap across
# processes. Without it, the orchestrator and the worker booting at the same
# moment deadlock (both take ShareLock via CREATE INDEX IF NOT EXISTS, then
# both want AccessExclusiveLock for the ALTER), even when nothing needs
# migrating (review-log F-22).
_SCHEMA_LOCK_KEY = 0x3D_4B_10B5  # arbitrary constant, only used here

# What _SCHEMA_SQL creates, derived from the SQL itself so the two can't drift:
# relations (tables + indexes) and (table, column) pairs. A statement of any
# other shape makes _schema_objects return None, which means "always run the
# DDL" (safe, just slower); test_jobs_bootstrap fails if that ever happens.
_DDL_PATTERNS = (
    ("relation", re.compile(r"^CREATE TABLE IF NOT EXISTS (\w+)\s*\(", re.I | re.S)),
    ("relation", re.compile(r"^CREATE INDEX IF NOT EXISTS (\w+)\s+ON\b", re.I | re.S)),
    ("column", re.compile(r"^ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)\b", re.I | re.S)),
)


def _schema_objects(sql: str = None):
    """(relations, columns) that `sql` (default _SCHEMA_SQL) creates, or None
    if any statement isn't one of the recognised idempotent forms."""
    sql = _SCHEMA_SQL if sql is None else sql
    body = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    relations, columns = [], []
    for stmt in (s.strip() for s in body.split(";")):
        if not stmt:
            continue
        for kind, pattern in _DDL_PATTERNS:
            m = pattern.match(stmt)
            if m:
                if kind == "relation":
                    relations.append(m.group(1).lower())
                else:
                    columns.append((m.group(1).lower(), m.group(2).lower()))
                break
        else:
            return None
    return relations, columns


# Catalog-only check: to_regclass resolves a name through search_path (same as
# the unqualified DDL) WITHOUT locking the relation, and pg_attribute is a
# plain catalog read, so this never queues behind (or in front of) a lock on
# consultation_jobs.
_MISSING_SQL = """
SELECT
  (SELECT count(*) FROM unnest(%(rels)s::text[]) AS r(name)
    WHERE to_regclass(r.name) IS NULL)
  +
  (SELECT count(*) FROM unnest(%(tbls)s::text[], %(cols)s::text[]) AS c(tbl, col)
    WHERE NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_attribute a
       WHERE a.attrelid = to_regclass(c.tbl) AND a.attname = c.col
         AND a.attnum > 0 AND NOT a.attisdropped))
  AS missing
"""

_DDL_ATTEMPTS = 3
_DDL_BACKOFF_S = (1.0, 3.0)   # sleep after attempt 1, after attempt 2


def _schema_missing(conn) -> bool:
    """True if any table/index/column _SCHEMA_SQL creates is absent (or the
    SQL can't be checked). Catalog reads only; commits its own read."""
    objects = _schema_objects()
    if objects is None:
        return True
    relations, columns = objects
    with conn.cursor() as cur:
        cur.execute(_MISSING_SQL, {
            "rels": relations,
            "tbls": [t for t, _ in columns],
            "cols": [c for _, c in columns],
        })
        row = cur.fetchone()
    conn.commit()
    missing = row["missing"] if isinstance(row, dict) else row[0]
    return bool(missing)


def _apply_schema(conn) -> None:
    """Run _SCHEMA_SQL in one transaction under the F-22 advisory lock. The
    advisory lock is the transaction's FIRST statement, in its own execute,
    so a process that waits for it runs the DDL only after the other process
    has committed (released automatically on commit/rollback). lock_timeout
    bounds every lock wait (the advisory lock and ALTER TABLE's
    AccessExclusiveLock), so the DDL can't sit queued in front of every
    other query on the table: it fails with LockNotAvailable instead."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", [_SCHEMA_LOCK_KEY])
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '20s'")
        cur.execute(_SCHEMA_SQL)
    conn.commit()


def _bootstrap():
    """Open the pool and ensure the schema exists. Runs once on import (the
    orchestrator, every RQ work-horse, tests). The usual case, schema already
    there, is one catalog query and NO DDL, so no table lock is ever taken
    (review-log F-26: an ALTER TABLE queued for AccessExclusiveLock on every
    worker boot stalled every orchestrator query behind it). When DDL is
    needed it runs with bounded lock waits, is retried a few times, and then
    raises: boot fails loudly rather than freezing other processes."""
    pool.open()
    pool.wait()
    try:
        for attempt in range(1, _DDL_ATTEMPTS + 1):
            try:
                with pool.connection() as conn:
                    if not _schema_missing(conn):
                        if attempt > 1:
                            logger.info("Schema present (created by another process)")
                        return
                    logger.info("Schema incomplete: applying DDL (attempt %d)", attempt)
                    _apply_schema(conn)
                    logger.info("Schema DDL applied")
                    return
            except (psycopg_errors.LockNotAvailable, psycopg_errors.QueryCanceled,
                    psycopg_errors.DeadlockDetected) as exc:
                if attempt == _DDL_ATTEMPTS:
                    logger.error("Schema bootstrap gave up after %d attempts: %s",
                                 attempt, exc)
                    raise
                delay = _DDL_BACKOFF_S[min(attempt, len(_DDL_BACKOFF_S)) - 1]
                logger.warning("Schema bootstrap attempt %d blocked (%s: %s); retrying in %.0fs",
                               attempt, type(exc).__name__, str(exc).strip(), delay)
                time.sleep(delay)
    except Exception:
        pool.close()
        raise


_bootstrap()


# --- Serialization ---------------------------------------------------------

def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _serialize_job(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "status": row["status"],
        "customer_id": row["customer_id"],
        "customer": row.get("customer"),
        "audio_key": row["audio_key"],
        "transcript": row.get("transcript"),
        "source": row["source"],
        "result": row["result"],
        "error": row["error"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


# --- Public API ------------------------------------------------------------

def create_job(
    audio_key: str = None, customer_id: str = None, source: str = None,
    customer: dict = None, transcript: str = None,
) -> dict:
    """Insert a new job with status='queued'. Exactly one input: `audio_key`
    (recorded/uploaded audio in R2) or `transcript` (pasted text). `customer`
    is the selected customer's identity snapshot (or None). Returns the
    serialized record."""
    if bool(audio_key) == bool(transcript):
        raise JobError("create_job needs exactly one of audio_key or transcript")
    new_id = f"job_{uuid.uuid4().hex[:12]}"
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO consultation_jobs
                    (id, status, customer_id, customer, audio_key, transcript, source)
                VALUES (%s, 'queued', %s, %s, %s, %s, %s)
                RETURNING *
                """,
                [new_id, customer_id, Jsonb(customer) if customer is not None else None,
                 audio_key, transcript, source],
            )
            row = cur.fetchone()
        conn.commit()
    job = _serialize_job(row)
    logger.info(
        "Created job %s (audio_key=%s transcript=%s customer_id=%s source=%s)",
        job["id"], audio_key or "—", f"{len(transcript)} chars" if transcript else "—",
        customer_id or "—", source or "—",
    )
    return job


def get_job(job_id: str) -> dict | None:
    """Fetch a job. Returns None if it doesn't exist."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM consultation_jobs WHERE id = %s", [job_id])
        row = cur.fetchone()
    return _serialize_job(row) if row else None


def list_recent_jobs(limit: int = 20) -> list:
    """Return recent jobs newest-first for a list view.

    The response is intentionally compact — a `summary` field extracts a
    handful of headline values from the result JSONB rather than dumping the
    full extraction + transcript payload. New jobs carry `fieldpulse_sync`
    (status + plain-language message + step statuses); old mock-writer jobs
    still carry match_path / FieldPulse ids. Failed jobs include their `error`.
    `customer` is the picker snapshot (display only).
    """
    limit = max(1, min(int(limit), 100))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, customer_id, customer, source, result, error,
                   created_at, updated_at
              FROM consultation_jobs
             ORDER BY created_at DESC
             LIMIT %s
            """,
            [limit],
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        item = {
            "id": r["id"],
            "status": r["status"],
            "customer_id": r["customer_id"],
            "customer": r["customer"] if isinstance(r["customer"], dict) else None,
            "source": r["source"],
            "created_at": _iso(r["created_at"]),
            "updated_at": _iso(r["updated_at"]),
        }
        result = r["result"]
        if isinstance(result, dict) and result:
            extraction = result.get("extraction") if isinstance(result.get("extraction"), dict) else {}
            fieldpulse = result.get("fieldpulse") if isinstance(result.get("fieldpulse"), dict) else {}
            sync = result.get("fieldpulse_sync") if isinstance(result.get("fieldpulse_sync"), dict) else None
            item["summary"] = {
                "project_type": extraction.get("project_type"),
                "input": result.get("input"),
                "match_path": fieldpulse.get("match_path"),
                "fieldpulse_customer_id": fieldpulse.get("customer_id"),
                "fieldpulse_job_id": fieldpulse.get("job_id"),
                "fieldpulse_url": fieldpulse.get("fieldpulse_url"),
                "fieldpulse_sync": {
                    "status": sync.get("status"),
                    "mode": sync.get("mode"),
                    "message": sync.get("message"),
                    "steps": [
                        {"step": s.get("step"), "status": s.get("status")}
                        for s in (sync.get("steps") or []) if isinstance(s, dict)
                    ],
                } if sync else None,
            }
        if r["error"]:
            item["error"] = r["error"]
        out.append(item)
    return out


def update_job_status(
    job_id: str, status: str, result: dict = None, error: str = None
) -> dict:
    """Update a job's status and optionally its result / error payloads.
    Returns the updated record. Raises JobError if the job doesn't exist."""
    sets = ["status = %s", "updated_at = now()"]
    params = [status]
    if result is not None:
        sets.append("result = %s")
        params.append(Jsonb(result))
    if error is not None:
        sets.append("error = %s")
        params.append(error)
    params.append(job_id)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE consultation_jobs SET {', '.join(sets)} "
                f"WHERE id = %s RETURNING *",
                params,
            )
            row = cur.fetchone()
        conn.commit()
    if row is None:
        raise JobError(f"Job not found: {job_id}")
    job = _serialize_job(row)
    logger.info("Updated job %s: status=%s", job_id, status)
    return job


# --- External customer -> mock customer links ------------------------------

def get_customer_link(source: str, external_id: str) -> str | None:
    """The mock customer id linked to (source, external_id), or None."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT mock_customer_id FROM customer_links "
            "WHERE source = %s AND external_id = %s",
            [source, str(external_id)],
        )
        row = cur.fetchone()
    return row["mock_customer_id"] if row else None


def save_customer_link(
    source: str, external_id: str, mock_customer_id: str, replace: str = None
) -> str:
    """Link (source, external_id) -> mock_customer_id and return the mock id
    that is linked afterwards. An existing link is kept (so a concurrent job
    that linked first wins), unless it equals `replace` (a stale link whose
    mock customer no longer exists)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_links (source, external_id, mock_customer_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (source, external_id) DO UPDATE
                   SET mock_customer_id = EXCLUDED.mock_customer_id, updated_at = now()
                 WHERE customer_links.mock_customer_id = %s
                RETURNING mock_customer_id
                """,
                [source, str(external_id), mock_customer_id, replace],
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT mock_customer_id FROM customer_links "
                    "WHERE source = %s AND external_id = %s",
                    [source, str(external_id)],
                )
                row = cur.fetchone()
        conn.commit()
    linked = row["mock_customer_id"]
    logger.info("Customer link %s:%s -> %s", source, external_id, linked)
    return linked


# --- FieldPulse write records (used only by fieldpulse_sync.py) -------------
#
# Statuses: planned (built, not sent) | sent (request went out, outcome not
# known yet, or never answered) | succeeded | failed | skipped. 'succeeded'
# and 'sent' rows are terminal for automation: nothing here overwrites them.
# Rows recorded with mode 'dry_run' are never claimed (sent) and never
# rewritten by a non-dry_run record: only the explicit admin manual send
# (release_held_fieldpulse_writes, called by fieldpulse_sync manual=True after
# its send gate allowed it) may release them.

def _serialize_write(row):
    return {
        "job_id": row["job_id"],
        "step": row["step"],
        "status": row["status"],
        "mode": row["mode"],
        "request": row["request"],
        "response": row["response"],
        "fieldpulse_record_id": row["fieldpulse_record_id"],
        "error": row["error"],
        "attempts": row["attempts"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def list_fieldpulse_writes(job_id: str) -> list:
    """All write steps recorded for a job, oldest first."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM fieldpulse_writes WHERE job_id = %s ORDER BY created_at, step",
            [job_id],
        )
        return [_serialize_write(r) for r in cur.fetchall()]


def record_fieldpulse_write(
    job_id: str, step: str, status: str, mode: str, request: dict = None, error: str = None
) -> None:
    """Insert or refresh a step as planned/skipped (with its exact request).
    Never touches a step that is already 'succeeded' or 'sent'."""
    if status not in ("planned", "skipped"):
        raise JobError(f"record_fieldpulse_write: bad status {status!r}")
    try:
        _record_write(job_id, step, status, mode, request, error)
    except psycopg_errors.ForeignKeyViolation:
        # The job was deleted (admin panel) mid-sync: nothing to record; the
        # following claim fails too, so nothing is sent (F-28).
        logger.info("Write %s/%s not recorded: job no longer exists", job_id, step)


def _record_write(job_id, step, status, mode, request, error):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fieldpulse_writes (job_id, step, status, mode, request, error)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id, step) DO UPDATE
                   SET status = EXCLUDED.status, mode = EXCLUDED.mode,
                       request = EXCLUDED.request, error = EXCLUDED.error,
                       updated_at = now()
                 WHERE fieldpulse_writes.status NOT IN ('succeeded', 'sent')
                   -- a dry-run row is never rewritten by a later send mode
                   AND NOT (fieldpulse_writes.mode = 'dry_run'
                            AND EXCLUDED.mode <> 'dry_run')
                """,
                [job_id, step, status, mode,
                 Jsonb(request) if request is not None else None, error],
            )
        conn.commit()


def claim_fieldpulse_write(job_id: str, step: str, max_attempts: int) -> bool:
    """Atomically move a planned/failed step to 'sent' (attempts + 1) right
    before the HTTP call. False if it isn't claimable (already sent/succeeded,
    skipped, out of attempts, claimed by a concurrent worker, or the job was
    deleted).

    Serializes with delete_job (review-log F-28): the claim first takes FOR KEY
    SHARE on the parent consultation_jobs row, which conflicts with delete_job's
    FOR UPDATE. Delete first -> the row is gone -> no claim -> no send. Claim
    first -> delete_job sees the in-flight claim (status 'sent', error NULL)
    and refuses."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM consultation_jobs WHERE id = %s FOR KEY SHARE", [job_id])
            if cur.fetchone() is None:
                conn.rollback()
                logger.info("Claim %s/%s refused: job no longer exists", job_id, step)
                return False
            cur.execute(
                """
                UPDATE fieldpulse_writes
                   SET status = 'sent', attempts = attempts + 1, error = NULL,
                       response = NULL, updated_at = now()
                 WHERE job_id = %s AND step = %s
                   AND status IN ('planned', 'failed') AND attempts < %s
                   AND mode <> 'dry_run'   -- dry-run rows: manual send only
                RETURNING step
                """,
                [job_id, step, max_attempts],
            )
            row = cur.fetchone()
        conn.commit()
    return row is not None


def finish_fieldpulse_write(
    job_id: str, step: str, status: str, response: dict = None,
    record_id: str = None, error: str = None,
) -> None:
    """Record the outcome of a claimed ('sent') step: succeeded | failed |
    sent (no answer: stays 'sent' with an error, never auto-resent)."""
    if status not in ("succeeded", "failed", "sent"):
        raise JobError(f"finish_fieldpulse_write: bad status {status!r}")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE fieldpulse_writes
                   SET status = %s, response = %s, fieldpulse_record_id = %s,
                       error = %s, updated_at = now()
                 WHERE job_id = %s AND step = %s AND status = 'sent'
                """,
                [status, Jsonb(response) if response is not None else None,
                 record_id, error, job_id, step],
            )
        conn.commit()


def release_held_fieldpulse_writes(job_id: str, mode: str) -> int:
    """EXPLICIT MANUAL SEND ONLY (admin "Send to FieldPulse", via
    fieldpulse_sync.sync_consultation(manual=True), after its send gate has
    allowed `mode`). Re-labels this job's dry-run rows that were never sent
    (planned / failed / skipped) with the current send mode, so the normal
    record -> claim -> send path may process them. 'succeeded' and 'sent' rows
    are never touched. Returns the number of rows released."""
    if mode not in ("test_customer_only", "live"):
        raise JobError(f"release_held_fieldpulse_writes: {mode!r} is not a send mode")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE fieldpulse_writes SET mode = %s, updated_at = now()
                 WHERE job_id = %s AND mode = 'dry_run'
                   AND status IN ('planned', 'failed', 'skipped')
                """,
                [mode, job_id],
            )
            n = cur.rowcount
        conn.commit()
    if n:
        logger.info("Released %d held dry-run write(s) for job %s (mode %s)", n, job_id, mode)
    return n


# --- Admin panel reads / manual-send log (admin_panel.py) -------------------

def list_jobs_for_admin(scan_limit: int = 2000) -> list:
    """Light rows for the admin list, newest first: no transcript, no comment
    text, no full extraction. Each row: id, status, customer_id, customer,
    source, has_audio, has_paste, error (first 300 chars), created_at,
    updated_at, input, project_type, client_name, close_score, sync
    ({status, mode, steps:[{step,status}]} or None), legacy_mock (bool),
    writes ([{step, status, mode}]), last_manual_send (dict or None).
    Every JSON path is guarded so a malformed `result` can't break the list."""
    scan_limit = max(1, min(int(scan_limit), 5000))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.id, j.status, j.customer_id,
                   CASE WHEN jsonb_typeof(j.customer) = 'object' THEN j.customer END AS customer,
                   j.source, (j.audio_key IS NOT NULL) AS has_audio,
                   (j.transcript IS NOT NULL) AS has_paste, left(j.error, 300) AS error,
                   j.created_at, j.updated_at,
                   CASE WHEN jsonb_typeof(j.result) = 'object' THEN j.result->>'input' END AS input,
                   CASE WHEN jsonb_typeof(j.result->'extraction') = 'object'
                        THEN j.result->'extraction'->>'project_type' END AS project_type,
                   CASE WHEN jsonb_typeof(j.result->'extraction'->'client') = 'object'
                        THEN j.result->'extraction'->'client'->>'name' END AS client_name,
                   CASE WHEN jsonb_typeof(j.result->'extraction'->'sales') = 'object'
                        THEN j.result->'extraction'->'sales'->'close_score_1_10' END AS close_score,
                   CASE WHEN jsonb_typeof(j.result->'fieldpulse_sync') = 'object'
                        THEN jsonb_build_object(
                               'status', j.result->'fieldpulse_sync'->'status',
                               'mode', j.result->'fieldpulse_sync'->'mode',
                               'steps', CASE WHEN jsonb_typeof(j.result->'fieldpulse_sync'->'steps') = 'array'
                                             THEN (SELECT coalesce(jsonb_agg(jsonb_build_object(
                                                          'step', s->'step', 'status', s->'status')), '[]')
                                                     FROM jsonb_array_elements(j.result->'fieldpulse_sync'->'steps') s
                                                    WHERE jsonb_typeof(s) = 'object')
                                             ELSE '[]'::jsonb END)
                   END AS sync,
                   coalesce(jsonb_typeof(j.result->'fieldpulse') = 'object', false) AS legacy_mock,
                   CASE WHEN jsonb_typeof(j.result->'fieldpulse_manual_sends') = 'array'
                        THEN j.result->'fieldpulse_manual_sends'-> -1 END AS last_manual_send,
                   coalesce(w.writes, '[]'::jsonb) AS writes
              FROM consultation_jobs j
              LEFT JOIN (
                    SELECT job_id, jsonb_agg(jsonb_build_object(
                               'step', step, 'status', status, 'mode', mode) ORDER BY step) AS writes
                      FROM fieldpulse_writes GROUP BY job_id
                   ) w ON w.job_id = j.id
             ORDER BY j.created_at DESC, j.id
             LIMIT %s
            """,
            [scan_limit],
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["created_at"] = _iso(r["created_at"])
        item["updated_at"] = _iso(r["updated_at"])
        out.append(item)
    return out


def delete_job(job_id: str, guard=None) -> dict | None:
    """Hard-delete one consultation from OUR database: its fieldpulse_writes
    rows and its consultation_jobs row, in one transaction. Never touches
    FieldPulse, R2 or customer_links (links are per customer, shared by every
    consultation for that customer).

    The row is locked (FOR UPDATE) first, so a concurrent worker update or
    manual-send save waits and then finds the row gone. `guard(job)` (the
    serialized row) runs under that lock and returns a reason string to refuse,
    or None to go ahead, so the check and the delete can't race.

    A FieldPulse step that is claimed and in flight (status 'sent' with no
    error: claim_fieldpulse_write cleared it, finish_fieldpulse_write hasn't
    run) always refuses, before `guard`: the worker may be mid-send, and
    deleting would lose the only record of it (review-log F-28). Claims take
    FOR KEY SHARE on this row, so a claim and a delete never interleave. A
    finished "sent, no answer" step always has an error and does not block.
    A worker killed between claim and finish leaves a claim that blocks delete
    until someone checks FieldPulse (deliberate: it may have been sent).

    Returns None if the job doesn't exist, else
      {"deleted": False, "reason": str, "job": job}   (refused; rolled back)
      {"deleted": True, "job": job, "writes_deleted": int}"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM consultation_jobs WHERE id = %s FOR UPDATE", [job_id])
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return None
            job = _serialize_job(row)
            cur.execute(
                "SELECT step FROM fieldpulse_writes "
                "WHERE job_id = %s AND status = 'sent' AND error IS NULL ORDER BY step",
                [job_id])
            in_flight = [r["step"] for r in cur.fetchall()]
            if in_flight:
                reason = (f"A FieldPulse send is in progress for this consultation "
                          f"({', '.join(in_flight)}). Wait for it to finish, then delete it.")
            else:
                reason = guard(job) if guard else None
            if reason:
                conn.rollback()
                return {"deleted": False, "reason": reason, "job": job}
            cur.execute("DELETE FROM fieldpulse_writes WHERE job_id = %s", [job_id])
            writes_deleted = cur.rowcount
            cur.execute("DELETE FROM consultation_jobs WHERE id = %s", [job_id])
        conn.commit()
    logger.info("Deleted job %s (status=%s, %d FieldPulse write rows)",
                job_id, job["status"], writes_deleted)
    return {"deleted": True, "job": job, "writes_deleted": writes_deleted}


_MANUAL_SENDS_KEPT = 20


def save_manual_send(job_id: str, entry: dict, sync: dict = None) -> bool:
    """Add or update (matched by entry["id"]) one entry in the job's
    result.fieldpulse_manual_sends log (last 20 kept), and, when `sync` is
    given, replace result.fieldpulse_sync with it (so Recent/Confirmation show
    the latest outcome). Row lock, so concurrent saves never lose an entry.
    Does not touch status or updated_at. False if the job doesn't exist."""
    if not isinstance(entry, dict) or not entry.get("id"):
        raise JobError("save_manual_send needs an entry with an 'id'")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT result FROM consultation_jobs WHERE id = %s FOR UPDATE", [job_id])
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return False
            result = row["result"] if isinstance(row["result"], dict) else {}
            sends = result.get("fieldpulse_manual_sends")
            sends = [s for s in sends if isinstance(s, dict)] if isinstance(sends, list) else []
            for s in sends:
                if s.get("id") == entry["id"]:
                    s.update(entry)
                    break
            else:
                sends.append(dict(entry))
            result["fieldpulse_manual_sends"] = sends[-_MANUAL_SENDS_KEPT:]
            if sync is not None:
                result["fieldpulse_sync"] = sync
            cur.execute("UPDATE consultation_jobs SET result = %s WHERE id = %s",
                        [Jsonb(result), job_id])
        conn.commit()
    return True


# --- Vendor documents (stage P1b; documents_panel.py + worker.py) ------------
#
# Statuses: queued -> processing -> done | failed. A queued/processing row can't
# be deleted (the worker may be reading it) unless it is STUCK: untouched for
# longer than VENDOR_DOC_STUCK_MINUTES (the RQ job times out after 5 min, so a
# row that old means the worker died, e.g. a deploy killed it).

VENDOR_DOC_STUCK_MINUTES = 15
_DOC_STATUSES = ("queued", "processing", "done", "failed")
_DOC_COLUMNS = f"""*, (status IN ('queued', 'processing')
                     AND updated_at < now() - interval '{VENDOR_DOC_STUCK_MINUTES} minutes') AS stuck"""


def _serialize_document(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "status": row["status"],
        "customer_id": row["customer_id"],
        "customer": row["customer"] if isinstance(row.get("customer"), dict) else None,
        "filename": row["filename"],
        "file_key": row["file_key"],
        "file_size": row["file_size"],
        "result": row["result"],
        "error": row["error"],
        "stuck": bool(row.get("stuck")),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def create_vendor_document(file_key: str, filename: str = None, file_size: int = None,
                           customer_id: str = None, customer: dict = None) -> dict:
    """Insert a vendor document row (status 'queued'). Returns the serialized row."""
    if not file_key:
        raise JobError("create_vendor_document needs a file_key")
    new_id = f"doc_{uuid.uuid4().hex[:12]}"
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO vendor_documents
                    (id, status, customer_id, customer, filename, file_key, file_size)
                VALUES (%s, 'queued', %s, %s, %s, %s, %s)
                RETURNING {_DOC_COLUMNS}
                """,
                [new_id, customer_id, Jsonb(customer) if customer is not None else None,
                 filename, file_key, file_size],
            )
            row = cur.fetchone()
        conn.commit()
    logger.info("Created vendor document %s (%s bytes, customer_id=%s)",
                new_id, file_size if file_size is not None else "?", customer_id or "—")
    return _serialize_document(row)


def get_vendor_document(doc_id: str) -> dict | None:
    """One vendor document row, or None."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_DOC_COLUMNS} FROM vendor_documents WHERE id = %s", [doc_id])
        row = cur.fetchone()
    return _serialize_document(row)


def list_vendor_documents(customer_id: str = None, offset: int = 0, limit: int = 50) -> dict:
    """Light rows newest first (optionally one customer's): no lines, no checks.
    {"items": [{id, status, customer_id, customer, filename, error (300 chars),
    created_at, updated_at, stuck, vendor, document_type, line_count,
    warn_count}], "total": int}. Every JSON path is guarded so a malformed
    `result` can't break the list."""
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 100))
    params = {"cid": customer_id, "off": offset, "lim": limit}
    where = "WHERE (%(cid)s::text IS NULL OR customer_id = %(cid)s::text)"
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM vendor_documents {where}", params)
        total = cur.fetchone()["n"]
        cur.execute(
            f"""
            SELECT id, status, customer_id,
                   CASE WHEN jsonb_typeof(customer) = 'object' THEN customer END AS customer,
                   filename, left(error, 300) AS error, created_at, updated_at,
                   (status IN ('queued', 'processing')
                    AND updated_at < now() - interval '{VENDOR_DOC_STUCK_MINUTES} minutes') AS stuck,
                   CASE WHEN jsonb_typeof(result->'vendor') = 'string'
                        THEN left(result->>'vendor', 80) END AS vendor,
                   CASE WHEN jsonb_typeof(result->'document_type') = 'string'
                        THEN left(result->>'document_type', 20) END AS document_type,
                   CASE WHEN jsonb_typeof(result->'lines') = 'array'
                        THEN jsonb_array_length(result->'lines') END AS line_count,
                   CASE WHEN jsonb_typeof(result->'checks') = 'array'
                        THEN (SELECT count(*) FROM jsonb_array_elements(result->'checks') c
                               WHERE jsonb_typeof(c) = 'object' AND c->>'level' = 'warn')
                   END AS warn_count
              FROM vendor_documents
              {where}
             ORDER BY created_at DESC, id
            OFFSET %(off)s LIMIT %(lim)s
            """,
            params,
        )
        rows = cur.fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["created_at"] = _iso(r["created_at"])
        item["updated_at"] = _iso(r["updated_at"])
        item["stuck"] = bool(r["stuck"])
        items.append(item)
    return {"items": items, "total": total}


def update_vendor_document(doc_id: str, status: str, result: dict = None,
                           error: str = None) -> dict | None:
    """Set a vendor document's status (and result / error). Returns the row,
    or None if it no longer exists (deleted meanwhile: the worker just stops)."""
    if status not in _DOC_STATUSES:
        raise JobError(f"update_vendor_document: bad status {status!r}")
    sets, params = ["status = %s", "updated_at = now()"], [status]
    if result is not None:
        sets.append("result = %s")
        params.append(Jsonb(result))
    if error is not None:
        sets.append("error = %s")
        params.append(error)
    params.append(doc_id)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE vendor_documents SET {', '.join(sets)} WHERE id = %s "
                        f"RETURNING {_DOC_COLUMNS}", params)
            row = cur.fetchone()
        conn.commit()
    if row is None:
        logger.info("Vendor document %s not updated (%s): it no longer exists", doc_id, status)
        return None
    logger.info("Updated vendor document %s: status=%s", doc_id, status)
    return _serialize_document(row)


def delete_vendor_document(doc_id: str) -> dict | None:
    """Hard-delete one vendor document row (under a row lock, so a concurrent
    worker update waits, then finds it gone). Refused while queued/processing
    unless stuck. Never touches R2 or FieldPulse (the caller deletes the PDF).
    None if it doesn't exist, else {"deleted": False, "reason", "document"} or
    {"deleted": True, "document"}."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_DOC_COLUMNS} FROM vendor_documents WHERE id = %s FOR UPDATE",
                        [doc_id])
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return None
            doc = _serialize_document(row)
            if doc["status"] in ("queued", "processing") and not doc["stuck"]:
                conn.rollback()
                return {"deleted": False, "document": doc,
                        "reason": "This document is still being read. Wait until it is done "
                                  "or failed, then delete it."}
            cur.execute("DELETE FROM vendor_documents WHERE id = %s", [doc_id])
        conn.commit()
    logger.info("Deleted vendor document %s (status=%s)", doc_id, doc["status"])
    return {"deleted": True, "document": doc}


# --- Vendor material lists (stage P2; fieldpulse_sync.sync_vendor_materials) ----------
#
# One row per customer. A sync CLAIMS the row (short transaction, SELECT ... FOR
# UPDATE after inserting it if missing) by setting lease_until; it then builds and
# sends with no transaction open, and FINISHES (another short transaction). A sync
# that finds the lease taken only sets resync (+ resync_manual) and stops; the holder
# sees that on finish, releases the lease and the worker queues one more run (the flag
# stays set until a run CLAIMS it, so a request is never lost). So two documents
# finishing together never create two lists, and the last build includes both.

VENDOR_LIST_LEASE_S = 120     # > one sync's worst case (PUT 404 + POST, 20 s each)
_VENDOR_LIST_HISTORY = 10


def _serialize_vendor_list(row):
    if row is None:
        return None
    return {
        "customer_id": row["customer_id"],
        "fieldpulse_list_id": row["fieldpulse_list_id"],
        "status": row["status"],
        "mode": row["mode"],
        "last_request": row["last_request"],
        "last_response": row["last_response"],
        "last_error": row["last_error"],
        "history": row["history"] if isinstance(row.get("history"), list) else [],
        "create_pending": bool(row.get("create_pending")),
        "held": bool(row.get("held")),
        "resync_manual": bool(row.get("resync_manual")),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def get_vendor_material_list(customer_id: str) -> dict | None:
    """The customer's vendor material list record, or None (never synced)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM vendor_material_lists WHERE customer_id = %s",
                    [customer_id])
        return _serialize_vendor_list(cur.fetchone())


def claim_vendor_material_list(customer_id: str, manual: bool = False) -> dict | None:
    """Take this customer's sync lease. Returns the row (serialized, with the
    resync_manual it had: a pending manual request is now this run's) when claimed and
    clears the resync flags; None when another sync holds it (then resync is flagged)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO vendor_material_lists (customer_id) VALUES (%s) "
                        "ON CONFLICT (customer_id) DO NOTHING", [customer_id])
            cur.execute("SELECT *, (lease_until IS NOT NULL AND lease_until > now()) AS busy "
                        "FROM vendor_material_lists WHERE customer_id = %s FOR UPDATE",
                        [customer_id])
            row = cur.fetchone()
            if row["busy"]:
                cur.execute("UPDATE vendor_material_lists SET resync = true, "
                            "resync_manual = resync_manual OR %s WHERE customer_id = %s",
                            [bool(manual), customer_id])
                conn.commit()
                logger.info("Vendor material list %s busy: resync flagged", customer_id)
                return None
            cur.execute("UPDATE vendor_material_lists SET lease_until = now() + "
                        "make_interval(secs => %s), resync = false, resync_manual = false "
                        "WHERE customer_id = %s", [VENDOR_LIST_LEASE_S, customer_id])
        conn.commit()
    return _serialize_vendor_list(row)


def mark_vendor_material_list_sending(customer_id: str, request: dict, mode: str) -> None:
    """Right before EVERY POST, in ONE write: create_pending = true, the old list id
    cleared, status 'sending' + the request. If the worker dies after dispatch, the next
    automatic sync sees create_pending and sends nothing (FieldPulse may have created
    the list); only finish(..., create_pending=False) after an id clears it."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE vendor_material_lists SET status = 'sending', mode = %s, "
                        "create_pending = true, fieldpulse_list_id = NULL, "
                        "last_request = %s, last_error = NULL, updated_at = now() "
                        "WHERE customer_id = %s", [mode, Jsonb(request), customer_id])
        conn.commit()


_UNSET = object()


def finish_vendor_material_list(customer_id: str, status: str, mode: str = None,
                                request: dict = None, response: dict = None,
                                error: str = None, list_id=_UNSET, create_pending=_UNSET,
                                held=_UNSET, history_entry: dict = None) -> tuple:
    """Record a sync's outcome and ALWAYS release the lease. list_id / create_pending /
    held: leave out to keep the stored value. Returns (again, again_manual): True when
    another sync was asked for meanwhile. The resync flags are NOT cleared here (the
    next claim consumes them), so the caller queues a follow-up run and nothing is lost
    even if that queueing fails (the next document event picks the flag up)."""
    sets = ["status = %s", "mode = %s", "last_request = %s", "last_response = %s",
            "last_error = %s", "updated_at = now()",
            "history = (SELECT coalesce(jsonb_agg(e), '[]'::jsonb) FROM (SELECT e FROM "
            "jsonb_array_elements(%s::jsonb || history) WITH ORDINALITY AS t(e, n) "
            "ORDER BY n LIMIT %s) h)"]
    params = [status, mode, Jsonb(request) if request is not None else None,
              Jsonb(response) if response is not None else None, error,
              Jsonb([history_entry] if history_entry else []), _VENDOR_LIST_HISTORY]
    for column, value in (("fieldpulse_list_id", list_id), ("create_pending", create_pending),
                          ("held", held)):
        if value is not _UNSET:
            sets.append(f"{column} = %s")
            params.append(value)
    sets.append("lease_until = NULL")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT resync, resync_manual FROM vendor_material_lists "
                        "WHERE customer_id = %s FOR UPDATE", [customer_id])
            row = cur.fetchone()
            again = bool(row and row["resync"])
            again_manual = bool(row and row["resync_manual"])
            cur.execute(f"UPDATE vendor_material_lists SET {', '.join(sets)} "
                        f"WHERE customer_id = %s", params + [customer_id])
        conn.commit()
    return again, again_manual


def list_customer_vendor_documents(customer_id: str, limit: int = 500) -> list:
    """One customer's vendor documents, full rows (incl. result), oldest first."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_DOC_COLUMNS} FROM vendor_documents WHERE customer_id = %s "
                    f"ORDER BY created_at, id LIMIT %s", [customer_id, max(1, int(limit))])
        return [_serialize_document(r) for r in cur.fetchall()]


def list_vendor_document_customers(limit: int = 500) -> list:
    """Customers that have vendor documents, newest upload first: {customer_id,
    customer (latest snapshot), document_count, last_upload_at, processing_count
    (queued/processing, not stuck), warn_count, list_status, fieldpulse_list_id,
    list_error, list_updated_at}."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT d.customer_id,
                   (array_agg(CASE WHEN jsonb_typeof(d.customer) = 'object' THEN d.customer END
                              ORDER BY d.created_at DESC)
                      FILTER (WHERE jsonb_typeof(d.customer) = 'object'))[1] AS customer,
                   count(*) AS document_count,
                   max(d.created_at) AS last_upload_at,
                   count(*) FILTER (WHERE d.status IN ('queued', 'processing')
                       AND d.updated_at >= now() - interval '{VENDOR_DOC_STUCK_MINUTES} minutes')
                       AS processing_count,
                   coalesce(sum(CASE WHEN jsonb_typeof(d.result->'checks') = 'array'
                        THEN (SELECT count(*) FROM jsonb_array_elements(d.result->'checks') c
                               WHERE jsonb_typeof(c) = 'object' AND c->>'level' = 'warn')
                        ELSE 0 END), 0) AS warn_count,
                   max(d.updated_at) FILTER (WHERE d.status = 'done') AS last_done_at,
                   m.status AS list_status, m.mode AS list_mode,
                   m.fieldpulse_list_id, left(m.last_error, 300) AS list_error,
                   m.updated_at AS list_updated_at,
                   coalesce(m.create_pending, false) AS create_pending
              FROM vendor_documents d
              LEFT JOIN vendor_material_lists m ON m.customer_id = d.customer_id
             WHERE d.customer_id IS NOT NULL
             GROUP BY d.customer_id, m.status, m.mode, m.fieldpulse_list_id, m.last_error,
                      m.updated_at, m.create_pending
             ORDER BY max(d.created_at) DESC, d.customer_id
             LIMIT %s
            """,
            [max(1, int(limit))],
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["document_count"] = int(r["document_count"])
        item["processing_count"] = int(r["processing_count"])
        item["warn_count"] = int(r["warn_count"])
        item["last_upload_at"] = _iso(r["last_upload_at"])
        item["list_updated_at"] = _iso(r["list_updated_at"])
        item["last_done_at"] = _iso(r["last_done_at"])
        out.append(item)
    return out


# --- Ferguson carts (stage E2; carts_panel.py is the only caller) ----------------------
#
# cart_addresses: one inbound alias per Clerk user. ferguson_carts: one row per accepted
# cart email, always read and written for ONE user (every query filters clerk_user_id).

def get_cart_alias(user_id: str) -> str | None:
    """The user's alias, or None."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT alias FROM cart_addresses WHERE clerk_user_id = %s", [user_id])
        row = cur.fetchone()
    return row["alias"] if row else None


def ensure_cart_alias(user_id: str, candidates) -> str:
    """The user's alias; when there is none, the first candidate nobody holds is
    claimed (INSERT ... ON CONFLICT DO NOTHING, so two requests at once can't give
    one user two aliases or two users one alias). JobError when every candidate is
    taken."""
    if not user_id:
        raise JobError("ensure_cart_alias needs a user id")
    existing = get_cart_alias(user_id)
    if existing:
        return existing
    for alias in candidates:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cart_addresses (clerk_user_id, alias) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING RETURNING alias", [user_id, alias])
                row = cur.fetchone()
            conn.commit()
        if row:
            logger.info("Created a cart address")
            return row["alias"]
        existing = get_cart_alias(user_id)      # a parallel request created it
        if existing:
            return existing
    raise JobError("No free cart address alias")


def get_cart_alias_user(alias: str) -> str | None:
    """The Clerk user id that owns `alias`, or None."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT clerk_user_id FROM cart_addresses WHERE alias = %s", [alias])
        row = cur.fetchone()
    return row["clerk_user_id"] if row else None


# F-109 invariant (derived, never trusted as stored): cart C is "replaced" only while the
# cart named in C.superseded_by still exists for the same user, both carts are linked/sent,
# and both are linked to the SAME customer. (Project names never change after a cart is
# stored, and supersede is only ever written between carts of the same project key, so
# same customer = same slot.) The stored column is a hint written by finish_cart_slot;
# every read below returns this live value instead, so a replacement that is relinked to
# another customer, unlinked or deleted stops blocking the older cart (no stale state to
# repair, and a relink racing finish_cart_slot can't leave a cross-customer block).
#
# R4 product rule (Nick, 2026-11-13; F-114/F-115): once the NEWER cart actually wrote, or
# tried to write, to FieldPulse for the slot (any non-dry-run request, incl. a POST whose
# answer was lost), the older cart is replaced FOR GOOD, even if the newer cart is later
# deleted or relinked: the estimate/list hold the newer lines (or an unresolved create), so
# the older cart must never send into them again. To update the estimate the user forwards
# the cart again. mark_cart_sending writes that in the same transaction as the attempt:
# superseded_by = the sender, superseded_final_customer = the slot's customer. A final
# supersede counts while the older cart is on that customer (a cart that never sent may be
# relinked elsewhere: a different slot, not replaced there), whatever the newer cart does.
# Until then (newer cart only linked / dry run) it stays the live F-109 value above.
_FINAL_SUPERSEDED = """(c.superseded_by IS NOT NULL AND c.superseded_final_customer IS NOT NULL
        AND c.superseded_final_customer = c.fieldpulse_customer_id
        AND c.status IN ('linked', 'sent'))"""
_LIVE_SUPERSEDED = """(CASE WHEN """ + _FINAL_SUPERSEDED + """ THEN c.superseded_by ELSE
      (SELECT n.id FROM ferguson_carts n
      WHERE n.id = c.superseded_by AND n.clerk_user_id = c.clerk_user_id AND n.id <> c.id
        AND n.status IN ('linked', 'sent') AND c.status IN ('linked', 'sent')
        AND n.fieldpulse_customer_id = c.fieldpulse_customer_id) END)"""
_CART_COLS = (f"c.*, {_LIVE_SUPERSEDED} AS superseded_live, "
              f"{_FINAL_SUPERSEDED} AS superseded_final_live")


def _serialize_cart(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "clerk_user_id": row["clerk_user_id"],
        "alias": row["alias"],
        "received_at": _iso(row["received_at"]),
        "sender": row["sender"],
        "message_id": row["message_id"],
        "raw_key": row["raw_key"],
        "raw_sha256": row["raw_sha256"],
        "parsed": row["parsed"] if isinstance(row.get("parsed"), dict) else None,
        "ok": bool(row["ok"]),
        "warnings": row["warnings"] if isinstance(row.get("warnings"), list) else [],
        "project_name": row["project_name"],
        "status": row["status"],
        "fieldpulse_customer_id": row["fieldpulse_customer_id"],
        "customer": row["customer"] if isinstance(row.get("customer"), dict) else None,
        "customer_match": (row["customer_match"]
                           if isinstance(row.get("customer_match"), dict) else None),
        "fieldpulse": row["fieldpulse"] if isinstance(row.get("fieldpulse"), dict) else None,
        "fieldpulse_history": (row["fieldpulse_history"]
                               if isinstance(row.get("fieldpulse_history"), list) else []),
        # Live value only (see _LIVE_SUPERSEDED); rows read without it (insert, delete)
        # are never "replaced".
        "superseded_by": row.get("superseded_live"),
        # True = replaced for good (the newer cart really wrote; R4 rule above).
        "superseded_final": bool(row.get("superseded_final_live")),
    }


def find_cart_by_sha(user_id: str, raw_sha256: str) -> dict | None:
    """This user's cart for exactly these raw bytes, or None (duplicate forward)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_CART_COLS} FROM ferguson_carts c WHERE c.clerk_user_id = %s "
                    f"AND c.raw_sha256 = %s", [user_id, raw_sha256])
        row = cur.fetchone()
    return _serialize_cart(row)


def create_ferguson_cart(user_id: str, alias: str, sender: str, message_id: str,
                         raw_key: str, raw_sha256: str, parsed: dict, ok: bool,
                         warnings: list, project_name: str) -> tuple:
    """Insert a cart (status 'needs_customer') -> (cart, True). The same raw bytes
    already stored for this user -> (existing cart, False), nothing written."""
    new_id = f"cart_{uuid.uuid4().hex[:12]}"
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ferguson_carts
                    (id, clerk_user_id, alias, sender, message_id, raw_key, raw_sha256,
                     parsed, ok, warnings, project_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (clerk_user_id, raw_sha256) DO NOTHING
                RETURNING *
                """,
                [new_id, user_id, alias, sender, message_id, raw_key, raw_sha256,
                 Jsonb(parsed) if parsed is not None else None, bool(ok),
                 Jsonb(list(warnings or [])), project_name],
            )
            row = cur.fetchone()
        conn.commit()
    if row is not None:
        logger.info("Created Ferguson cart %s (ok=%s)", new_id, bool(ok))
        return _serialize_cart(row), True
    return find_cart_by_sha(user_id, raw_sha256), False


def list_ferguson_carts(user_id: str, offset: int = 0, limit: int = 50) -> dict:
    """One user's carts, newest first, compact: {"items": [{id, received_at,
    project_name, item_count, subtotal (string|None), ok, warning_count, status,
    customer_name, match_state, fieldpulse_status, fieldpulse_at, superseded_by}],
    "total"}. JSON paths are guarded so an odd `parsed` can't break the list."""
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 100))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM ferguson_carts WHERE clerk_user_id = %s",
                    [user_id])
        total = cur.fetchone()["n"]
        cur.execute(
            """
            SELECT id, received_at, left(project_name, 255) AS project_name, ok, status,
                   CASE WHEN jsonb_typeof(customer) = 'object'
                        THEN left(customer->>'display_name', 200) END AS customer_name,
                   CASE WHEN jsonb_typeof(customer_match) = 'object'
                        THEN left(customer_match->>'state', 20) END AS match_state,
                   CASE WHEN jsonb_typeof(parsed->'items') = 'array'
                        THEN jsonb_array_length(parsed->'items') END AS item_count,
                   CASE WHEN jsonb_typeof(parsed->'subtotal') = 'string'
                        THEN left(parsed->>'subtotal', 20) END AS subtotal,
                   CASE WHEN jsonb_typeof(warnings) = 'array'
                        THEN jsonb_array_length(warnings) ELSE 0 END AS warning_count,
                   CASE WHEN jsonb_typeof(fieldpulse) = 'object'
                        THEN left(fieldpulse->>'status', 20) END AS fieldpulse_status,
                   CASE WHEN jsonb_typeof(fieldpulse) = 'object'
                        THEN left(fieldpulse->>'at', 40) END AS fieldpulse_at,
                   left(""" + _LIVE_SUPERSEDED + """, 40) AS superseded_by
              FROM ferguson_carts c
             WHERE clerk_user_id = %s
             ORDER BY received_at DESC, id
            OFFSET %s LIMIT %s
            """,
            [user_id, offset, limit],
        )
        rows = cur.fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["received_at"] = _iso(r["received_at"])
        items.append(item)
    return {"items": items, "total": total}


def get_ferguson_cart(user_id: str, cart_id: str) -> dict | None:
    """One cart of THIS user, or None (another user's cart is None too)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_CART_COLS} FROM ferguson_carts c WHERE c.id = %s "
                    f"AND c.clerk_user_id = %s", [cart_id, user_id])
        row = cur.fetchone()
    return _serialize_cart(row)


def delete_ferguson_cart(user_id: str, cart_id: str) -> dict | None:
    """Hard-delete one cart of THIS user. Returns the deleted row (the caller removes
    the raw email from R2), or None when there is no such cart for this user."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ferguson_carts WHERE id = %s AND clerk_user_id = %s "
                        "RETURNING *", [cart_id, user_id])
            row = cur.fetchone()
        conn.commit()
    if row is not None:
        logger.info("Deleted Ferguson cart %s", cart_id)
    return _serialize_cart(row)


def set_cart_customer(user_id: str, cart_id: str, customer: dict | None, match: dict,
                      only_from) -> dict | None:
    """Set THIS user's cart's customer link in one conditional write: `customer` (a
    snapshot dict with an "id") -> status 'linked' + fieldpulse_customer_id; None ->
    status 'needs_customer', link cleared. `match` is stored as customer_match. Only
    when the cart's current status is in `only_from` (so a stale auto-match never
    overwrites a manual link, and nothing here touches a sent cart). Returns the
    updated cart, or None (no such cart for this user, or its status didn't allow it)."""
    statuses = [str(x) for x in only_from]
    if customer is not None and customer.get("id") in (None, ""):
        raise JobError("set_cart_customer needs a customer id")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ferguson_carts c
                   SET status = %s, fieldpulse_customer_id = %s, customer = %s,
                       customer_match = %s
                 WHERE id = %s AND clerk_user_id = %s AND status = ANY(%s)
                RETURNING """ + _CART_COLS,
                ["linked" if customer else "needs_customer",
                 str(customer["id"]) if customer else None,
                 Jsonb(customer) if customer else None, Jsonb(match),
                 cart_id, user_id, statuses],
            )
            row = cur.fetchone()
        conn.commit()
    return _serialize_cart(row)


def set_cart_item_types(user_id: str, cart_id: str, parsed: dict) -> bool:
    """Store the cart's product types (carts_panel.ensure_item_types): `parsed` = the
    cart's parsed JSON with items[i].item_type + "item_types" marker. ONE conditional write,
    only while no marker is stored yet (the first result wins, so every send reuses the
    same titles). True if written."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ferguson_carts SET parsed = %s
                 WHERE id = %s AND clerk_user_id = %s AND parsed IS NOT NULL
                   AND NOT (parsed ? 'item_types')
                """,
                [Jsonb(parsed), cart_id, user_id],
            )
            n = cur.rowcount
        conn.commit()
    return n == 1


# --- Ferguson cart -> FieldPulse (stage E4; fieldpulse_sync.sync_cart is the only caller) --
#
# cart_fieldpulse_slots: one row per (user, customer, project key) = ONE draft estimate +
# ONE material list. Same lease pattern as vendor_material_lists: CLAIM (short tx, FOR
# UPDATE) -> build + send with no transaction open -> FINISH (short tx, always releases
# the lease). A busy claim flags resync (+ the newest asking cart) for one follow-up run.

CART_SLOT_LEASE_S = 300       # > one sync's worst case (2 targets x PUT + GET + POST, 20 s each)
_CART_HISTORY = 10
_SLOT_TARGETS = ("estimate", "list")


def _serialize_slot(row):
    if row is None:
        return None
    out = {k: row.get(k) for k in (
        "clerk_user_id", "customer_id", "project_key", "cart_id", "estimate_id",
        "estimate_status", "estimate_cart_id", "list_id", "list_status", "list_cart_id",
        "mode", "resync_cart_id", "writer_cart_id")}
    for k in ("estimate_create_pending", "list_create_pending", "held", "resync_manual"):
        out[k] = bool(row.get(k))
    out["updated_at"] = _iso(row.get("updated_at"))
    out["writer_received_at"] = _iso(row.get("writer_received_at"))
    return out


def get_cart_slot(user_id: str, customer_id: str, project_key: str) -> dict | None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM cart_fieldpulse_slots WHERE clerk_user_id = %s "
                    "AND customer_id = %s AND project_key = %s",
                    [user_id, customer_id, project_key])
        return _serialize_slot(cur.fetchone())


def claim_cart_slot(user_id: str, customer_id: str, project_key: str, cart_id: str,
                    manual: bool = False) -> dict | None:
    """Take the slot's lease. Returns the slot (with the resync_manual it had) and clears
    the resync flags; None when another sync holds it (then resync is flagged with the
    NEWER of the already-flagged cart and this one, by received_at)."""
    key = [user_id, customer_id, project_key]
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO cart_fieldpulse_slots (clerk_user_id, customer_id, "
                        "project_key) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", key)
            cur.execute("SELECT *, (lease_until IS NOT NULL AND lease_until > now()) AS busy "
                        "FROM cart_fieldpulse_slots WHERE clerk_user_id = %s AND "
                        "customer_id = %s AND project_key = %s FOR UPDATE", key)
            row = cur.fetchone()
            if row["busy"]:
                cur.execute(
                    """
                    UPDATE cart_fieldpulse_slots s SET resync = true,
                           resync_manual = s.resync_manual OR %s,
                           resync_cart_id = CASE
                             WHEN s.resync_cart_id IS NULL THEN %s
                             WHEN (SELECT received_at FROM ferguson_carts
                                    WHERE id = s.resync_cart_id) IS NULL THEN %s
                             WHEN (SELECT received_at FROM ferguson_carts WHERE id = %s)
                                  >= (SELECT received_at FROM ferguson_carts
                                       WHERE id = s.resync_cart_id) THEN %s
                             ELSE s.resync_cart_id END
                     WHERE clerk_user_id = %s AND customer_id = %s AND project_key = %s
                    """,
                    [bool(manual), cart_id, cart_id, cart_id, cart_id] + key)
                conn.commit()
                logger.info("Cart slot busy: resync flagged (%s)", cart_id)
                return None
            cur.execute("UPDATE cart_fieldpulse_slots SET lease_until = now() + "
                        "make_interval(secs => %s), resync = false, resync_manual = false, "
                        "resync_cart_id = NULL WHERE clerk_user_id = %s AND customer_id = %s "
                        "AND project_key = %s", [CART_SLOT_LEASE_S] + key)
        conn.commit()
    return _serialize_slot(row)


def mark_cart_sending(user_id: str, cart_id: str, customer_id: str, project_key: str,
                      target: str, post: bool, request: dict, mode: str) -> bool:
    """Right before EVERY real request to FieldPulse for a cart, in ONE transaction:
      - the cart becomes 'sent' (no relink any more) ONLY if it is still linked to
        `customer_id` (else nothing is written and False is returned: don't send);
      - refused (False, nothing written) when a NEWER cart already made a real request for
        this slot (writer_*): an older cart never writes over newer lines or an unresolved
        create, even after that newer cart was deleted (F-114, defense in depth: sync_cart
        checks the same thing first);
      - the slot records this cart as its newest writer;
      - R4 rule (see _LIVE_SUPERSEDED): every OLDER cart of this slot (same user, customer,
        project key, linked/sent) becomes replaced FOR GOOD by this cart;
      - for a POST also, like the vendor list: <target>_create_pending = true + old id cleared.
    Only called for non-dry-run requests (a dry run never reaches here)."""
    if target not in _SLOT_TARGETS:
        raise JobError("mark_cart_sending: unknown target")
    import ferguson_cart                      # pure module (project_key normalization)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ferguson_carts
                   SET status = 'sent',
                       fieldpulse = coalesce(fieldpulse, '{}'::jsonb)
                                    || jsonb_build_object('status', 'sending', 'at', %s::text,
                                                          'mode', %s::text)
                 WHERE id = %s AND clerk_user_id = %s AND fieldpulse_customer_id = %s
                   AND status IN ('linked', 'sent')
                RETURNING id, received_at
                """,
                [_iso_now(), mode, cart_id, user_id, customer_id])
            me = cur.fetchone()
            if me is None:
                conn.rollback()
                return False
            key = [user_id, customer_id, project_key]
            cur.execute("SELECT writer_cart_id, writer_received_at FROM cart_fieldpulse_slots "
                        "WHERE clerk_user_id = %s AND customer_id = %s AND project_key = %s "
                        "FOR UPDATE", key)
            slot = cur.fetchone()
            if (slot and slot["writer_cart_id"] and slot["writer_cart_id"] != cart_id
                    and (slot["writer_received_at"], slot["writer_cart_id"])
                    > (me["received_at"], cart_id)):
                conn.rollback()
                logger.warning("[%s] not sent: a newer cart already wrote to this slot", cart_id)
                return False
            sets = ["writer_cart_id = %s", "writer_received_at = %s", "mode = %s",
                    "updated_at = now()"]
            params = [cart_id, me["received_at"], mode]
            if post:
                sets[3:3] = [f"{target}_create_pending = true", f"{target}_id = NULL",
                             f"{target}_status = 'sending'"]
            cur.execute(f"UPDATE cart_fieldpulse_slots SET {', '.join(sets)} WHERE "
                        f"clerk_user_id = %s AND customer_id = %s AND project_key = %s",
                        params + key)
            # Older carts of this slot -> replaced for good. Project names are normalized in
            # Python, so read the candidates (no row locks: other slots' marks never touch
            # these rows) and let the UPDATE re-check customer/status/age.
            cur.execute("SELECT id, project_name FROM ferguson_carts WHERE clerk_user_id = %s "
                        "AND fieldpulse_customer_id = %s AND status IN ('linked', 'sent') "
                        "AND id <> %s AND (received_at, id) < (%s, %s)",
                        [user_id, customer_id, cart_id, me["received_at"], cart_id])
            older = [r["id"] for r in cur.fetchall()
                     if ferguson_cart.project_key(r["project_name"]) == project_key]
            if older:
                cur.execute("UPDATE ferguson_carts SET superseded_by = %s, "
                            "superseded_final_customer = %s WHERE id = ANY(%s) "
                            "AND clerk_user_id = %s AND fieldpulse_customer_id = %s "
                            "AND status IN ('linked', 'sent') AND id <> %s",
                            [cart_id, customer_id, older, user_id, customer_id, cart_id])
        conn.commit()
    return True


def _iso_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


_SLOT_COLUMNS = frozenset((
    "cart_id", "estimate_id", "estimate_status", "estimate_cart_id", "estimate_create_pending",
    "list_id", "list_status", "list_cart_id", "list_create_pending", "held", "mode"))


def _cart_record_sql(record, history_entry):
    return ("fieldpulse = %s, fieldpulse_history = (SELECT coalesce(jsonb_agg(e), '[]'::jsonb) "
            "FROM (SELECT e FROM jsonb_array_elements(%s::jsonb || fieldpulse_history) "
            "WITH ORDINALITY AS t(e, n) ORDER BY n LIMIT %s) h)",
            [Jsonb(record), Jsonb([history_entry] if history_entry else []), _CART_HISTORY])


def set_cart_fieldpulse(user_id: str, cart_id: str, record: dict,
                        history_entry: dict = None) -> bool:
    """Store a cart's send record (refusals and skips outside the slot lease)."""
    sql, params = _cart_record_sql(record, history_entry)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE ferguson_carts SET {sql} WHERE id = %s AND clerk_user_id = %s "
                        f"RETURNING id", params + [cart_id, user_id])
            ok = cur.fetchone() is not None
        conn.commit()
    return ok


def finish_cart_slot(user_id: str, customer_id: str, project_key: str, updates: dict,
                     cart_id: str, record: dict, history_entry: dict = None,
                     supersede: tuple = None) -> tuple:
    """One transaction: apply `updates` to the slot (only known columns), ALWAYS release
    the lease, store the cart's record, and (supersede = (old_cart, new_cart)) mark the old
    cart superseded_by the new one. Returns (again, again_manual, again_cart_id); the
    resync flags are NOT cleared here (the next claim consumes them). The supersede
    write only happens while the old cart is still linked/sent to `customer_id`.
    supersede = (old, new, "final") = the R4 permanent form (new = the slot's newest real
    writer, which may no longer exist)."""
    bad = set(updates or {}) - _SLOT_COLUMNS
    if bad:
        raise JobError("finish_cart_slot: unknown slot column")
    key = [user_id, customer_id, project_key]
    sets, params = ["lease_until = NULL", "updated_at = now()"], []
    for column, value in (updates or {}).items():
        sets.append(f"{column} = %s")
        params.append(value)
    rec_sql, rec_params = _cart_record_sql(record, history_entry)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT resync, resync_manual, resync_cart_id FROM cart_fieldpulse_slots "
                        "WHERE clerk_user_id = %s AND customer_id = %s AND project_key = %s "
                        "FOR UPDATE", key)
            row = cur.fetchone()
            cur.execute(f"UPDATE cart_fieldpulse_slots SET {', '.join(sets)} WHERE "
                        f"clerk_user_id = %s AND customer_id = %s AND project_key = %s",
                        params + key)
            cur.execute(f"UPDATE ferguson_carts SET {rec_sql} WHERE id = %s AND "
                        f"clerk_user_id = %s", rec_params + [cart_id, user_id])
            if supersede and len(supersede) > 2 and supersede[2] == "final":
                # R4: the slot's newest real writer is newer than this cart (it may have been
                # deleted since): replaced for good, only while it is on this customer.
                old, new = supersede[0], supersede[1]
                cur.execute("UPDATE ferguson_carts SET superseded_by = %s, "
                            "superseded_final_customer = %s WHERE id = %s "
                            "AND clerk_user_id = %s AND id <> %s "
                            "AND fieldpulse_customer_id = %s AND status IN ('linked', 'sent')",
                            [new, customer_id, old, user_id, new, customer_id])
            elif supersede:
                # Only while the old cart still belongs to THIS slot's customer (F-109: a
                # relink racing this run must not mark another customer's cart replaced).
                old, new = supersede[0], supersede[1]
                # The new cart must still be in it too; reads re-check both anyway
                # (_LIVE_SUPERSEDED), so a relink committing after this write is harmless.
                cur.execute("UPDATE ferguson_carts SET superseded_by = %s WHERE id = %s "
                            "AND clerk_user_id = %s AND id <> %s "
                            "AND fieldpulse_customer_id = %s AND status IN ('linked', 'sent') "
                            "AND EXISTS (SELECT 1 FROM ferguson_carts n WHERE n.id = %s "
                            "AND n.clerk_user_id = %s AND n.fieldpulse_customer_id = %s "
                            "AND n.status IN ('linked', 'sent'))",
                            [new, old, user_id, new, customer_id, new, user_id, customer_id])
        conn.commit()
    if not row:
        return False, False, None
    return bool(row["resync"]), bool(row["resync_manual"]), row["resync_cart_id"]
