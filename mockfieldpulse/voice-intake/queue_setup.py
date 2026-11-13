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


def enqueue_vendor_document(doc_id: str):
    """Queue reading one uploaded vendor PDF (worker.process_vendor_document).
    Same queue/worker as consultations. 300 s: download + up to two Claude
    reads of <= 120 s each + a short pause; the read itself never retries
    inside the SDK (see vendor_documents.py)."""
    rq_job = consultation_queue.enqueue("worker.process_vendor_document", doc_id,
                                        job_timeout=300)
    logger.info("Enqueued vendor document %s (rq id=%s)", doc_id, rq_job.id)
    return rq_job


def enqueue_vendor_materials(customer_id: str, manual: bool = False, customer: dict = None):
    """Queue rebuilding + sending one customer's combined vendor material list
    (worker.sync_vendor_materials_job -> fieldpulse_sync.sync_vendor_materials).
    Queued after a document is read, after one is deleted, and by the manual
    "Send to FieldPulse". 120 s: at most a PUT and a POST (20 s each) per round."""
    rq_job = consultation_queue.enqueue("worker.sync_vendor_materials_job", customer_id,
                                        manual, customer, job_timeout=120)
    logger.info("Enqueued vendor material sync for customer %s (manual=%s, rq id=%s)",
                customer_id, manual, rq_job.id)
    return rq_job


def enqueue_cart_match(user_id: str, cart_id: str):
    """Queue matching one Ferguson cart to a FieldPulse customer by its project name
    (worker.match_cart_customer -> carts_panel.match_cart; a READ through
    customer_source, never a FieldPulse write). Queued after an inbound cart is stored
    and by "Try matching again". 90 s: the customer list read has a 20 s budget."""
    rq_job = consultation_queue.enqueue("worker.match_cart_customer", user_id, cart_id,
                                        job_timeout=90)
    logger.info("Enqueued cart match for %s (rq id=%s)", cart_id, rq_job.id)
    return rq_job


def enqueue_cart_send(user_id: str, cart_id: str, manual: bool = False):
    """Queue sending one LINKED Ferguson cart to FieldPulse as a draft estimate + a
    material list (worker.send_cart_to_fieldpulse -> fieldpulse_sync.sync_cart; the
    FIELDPULSE_WRITE_MODE gate decides). Queued after a cart becomes linked and by the
    manual Send/Retry. 300 s: two targets x (PUT + GET + POST, 20 s each) + DB."""
    rq_job = consultation_queue.enqueue("worker.send_cart_to_fieldpulse", user_id, cart_id,
                                        manual, job_timeout=300)
    logger.info("Enqueued cart send for %s (manual=%s, rq id=%s)", cart_id, manual, rq_job.id)
    return rq_job
