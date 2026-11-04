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
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
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
pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    open=False,
    kwargs={"row_factory": dict_row},
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
"""


# Fixed key for pg_advisory_xact_lock: serializes schema bootstrap across
# processes. Without it, the orchestrator and the worker booting at the same
# moment deadlock (both take ShareLock via CREATE INDEX IF NOT EXISTS, then
# both want AccessExclusiveLock for the ALTER), even when nothing needs
# migrating (review-log F-22).
_SCHEMA_LOCK_KEY = 0x3D_4B_10B5  # arbitrary constant, only used here


def _bootstrap():
    """Open the pool and ensure the schema exists. Runs once on import so
    the orchestrator, the RQ worker, and the test script all see the
    table immediately. The advisory lock is the transaction's FIRST
    statement, in its own execute, so a process that waits for it runs the
    DDL only after the other process has committed. It is released
    automatically on commit/rollback."""
    pool.open()
    pool.wait()
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", [_SCHEMA_LOCK_KEY])
                cur.execute(_SCHEMA_SQL)
            conn.commit()
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
        "source": row["source"],
        "result": row["result"],
        "error": row["error"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


# --- Public API ------------------------------------------------------------

def create_job(
    audio_key: str, customer_id: str = None, source: str = None, customer: dict = None
) -> dict:
    """Insert a new job with status='queued'. `customer` is the selected
    customer's identity snapshot (or None). Returns the serialized record."""
    new_id = f"job_{uuid.uuid4().hex[:12]}"
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO consultation_jobs
                    (id, status, customer_id, customer, audio_key, source)
                VALUES (%s, 'queued', %s, %s, %s, %s)
                RETURNING *
                """,
                [new_id, customer_id, Jsonb(customer) if customer is not None else None,
                 audio_key, source],
            )
            row = cur.fetchone()
        conn.commit()
    job = _serialize_job(row)
    logger.info(
        "Created job %s (audio_key=%s customer_id=%s source=%s)",
        job["id"], audio_key, customer_id or "—", source or "—",
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
    handful of headline values (project_type, match_path, FieldPulse ids,
    admin URL) from the result JSONB rather than dumping the full extraction
    + fieldpulse + transcript payload. Failed jobs include their `error`.
    """
    limit = max(1, min(int(limit), 100))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, customer_id, source, result, error,
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
            "source": r["source"],
            "created_at": _iso(r["created_at"]),
            "updated_at": _iso(r["updated_at"]),
        }
        result = r["result"]
        if result:
            extraction = result.get("extraction") or {}
            fieldpulse = result.get("fieldpulse") or {}
            item["summary"] = {
                "project_type": extraction.get("project_type"),
                "match_path": fieldpulse.get("match_path"),
                "fieldpulse_customer_id": fieldpulse.get("customer_id"),
                "fieldpulse_job_id": fieldpulse.get("job_id"),
                "fieldpulse_url": fieldpulse.get("fieldpulse_url"),
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
