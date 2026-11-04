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
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from psycopg import errors as psycopg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

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
#   - check: each connection is pinged when handed out, so a dead one (DB
#     restart, dropped TCP) is replaced instead of failing/hanging a request.
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
pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    open=False,
    timeout=10.0,
    max_idle=300.0,
    max_lifetime=1800.0,
    check=ConnectionPool.check_connection,
    kwargs={"row_factory": dict_row, "options": _SERVER_OPTIONS},
)

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
