"""Redis-backed RQ queue for the consultation pipeline.

The orchestrator calls `enqueue_consultation(job_id)` to push work onto the
queue. A separate worker process pops the message and runs
`worker.process_consultation_job(job_id)` — see worker.py for the function
and the README at the bottom of test_jobs_queue.py for how to start it.

Conventions: load_dotenv at import, module-level logger to stdout, single
Redis connection + single Queue created at module load.
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from redis import Redis
from rq import Queue

_HERE = Path(__file__).resolve().parent

logger = logging.getLogger("voice_intake.queue")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


load_dotenv(_HERE / ".env")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
QUEUE_NAME = "consultations"

# `Redis.from_url` returns a lazy client — it doesn't connect until the first
# command, so importing this module never fails on Redis-down (the error
# surfaces on the first enqueue/count call instead, which is clearer).
redis_conn = Redis.from_url(REDIS_URL)
consultation_queue = Queue(QUEUE_NAME, connection=redis_conn)


def enqueue_consultation(job_id: str):
    """Push a consultation-processing job onto the queue. Returns the RQ
    Job object (so callers can read .id for logging / debugging)."""
    # 30 min: transcribing + extracting a 1–2 hour recording can take well past
    # RQ's 180 s default, which would kill the job mid-way (review-log F-63).
    rq_job = consultation_queue.enqueue("worker.process_consultation_job", job_id,
                                        job_timeout=1800)
    logger.info("Enqueued job %s (rq id=%s)", job_id, rq_job.id)
    return rq_job


def enqueue_manual_send(job_id: str, request_id: str):
    """Queue an admin "Send to FieldPulse" for a finished job. The worker runs
    worker.send_consultation_to_fieldpulse; the request handler never talks to
    FieldPulse itself. Same queue as consultations (same worker service)."""
    rq_job = consultation_queue.enqueue(
        "worker.send_consultation_to_fieldpulse", job_id, request_id,
        job_timeout=300,
    )
    logger.info("Enqueued manual FieldPulse send for %s (request %s, rq id=%s)",
                job_id, request_id, rq_job.id)
    return rq_job
