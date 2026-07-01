"""RQ worker entry-point for consultation jobs.

This is a STUB. The real transcribe -> extract -> write pipeline lands in
the next chunk. For now `process_consultation_job` just logs and marks the
job done so we can prove the queue wiring end-to-end.

To start the worker:
    cd voice-intake/
    venv/bin/rq worker consultations
"""

import logging
import sys

from jobs import JobError, update_job_status

logger = logging.getLogger("voice_intake.worker")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def process_consultation_job(job_id):
    """STUB: log + mark the job done. Real implementation comes next."""
    logger.info("would process job %s", job_id)
    try:
        update_job_status(job_id, "done", result={"stub": True})
        logger.info("job %s marked done", job_id)
    except JobError as exc:
        logger.error("job %s: %s", job_id, exc)
        raise
