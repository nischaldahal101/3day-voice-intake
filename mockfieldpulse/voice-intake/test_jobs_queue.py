"""Standalone test for jobs.py + queue_setup.py.

Exercises the Postgres job model (create / get / update) and confirms the
RQ queue accepts work. Cleans up after itself so re-runs are clean.

To actually watch a queued job get processed by the stub worker, start an
RQ worker in a second terminal (see the printed instructions at the end of
a successful run).

PREREQUISITES:
  - Postgres running (DATABASE_URL from .env)
  - Redis running   (REDIS_URL   from .env; default localhost:6379)
"""

from jobs import JobError, create_job, get_job, pool as _jobs_pool, update_job_status
from queue_setup import consultation_queue, enqueue_consultation


PASS = "✅ PASS"
FAIL = "❌ FAIL"


def _check(name, ok, detail=""):
    tag = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {tag}  {name}{suffix}")
    return bool(ok)


def main():
    all_ok = True
    rq_job = None

    try:
        # 1. create_job → status='queued'
        job = create_job(
            audio_key="consultations/test-fake-key.bin",
            customer_id="cust_test",
            source="test-script",
        )
        job_id = job["id"]
        all_ok &= _check(
            "create_job returned a record with status='queued'",
            ok=(
                isinstance(job, dict)
                and job.get("status") == "queued"
                and isinstance(job.get("id"), str)
                and job["id"].startswith("job_")
            ),
            detail=f"id={job_id!r}, status={job.get('status')!r}",
        )

        # 2. get_job returns the same row
        fetched = get_job(job_id)
        all_ok &= _check(
            "get_job(job_id) returns the inserted row",
            ok=(
                fetched is not None
                and fetched["id"] == job_id
                and fetched["audio_key"] == "consultations/test-fake-key.bin"
                and fetched["customer_id"] == "cust_test"
                and fetched["status"] == "queued"
            ),
            detail=f"got status={fetched and fetched.get('status')!r}",
        )

        # 2b. get_job on a missing id returns None
        missing = get_job("job_does_not_exist_xxxxxxxx")
        all_ok &= _check(
            "get_job on a missing id returns None",
            ok=missing is None,
            detail=f"got {missing!r}",
        )

        # 3a. update_job_status → 'processing'
        upd = update_job_status(job_id, "processing")
        all_ok &= _check(
            "update_job_status -> 'processing' persisted",
            ok=upd["status"] == "processing" and get_job(job_id)["status"] == "processing",
        )

        # 3b. update_job_status → 'done' with a result payload
        result_payload = {
            "extraction": {"project_type": "kitchen"},
            "fieldpulse": {"job_id": "job_xyz"},
        }
        upd = update_job_status(job_id, "done", result=result_payload)
        fresh = get_job(job_id)
        all_ok &= _check(
            "update_job_status -> 'done' + result persisted as JSONB",
            ok=(
                upd["status"] == "done"
                and fresh["status"] == "done"
                and fresh["result"] == result_payload
            ),
            detail=f"result={fresh and fresh.get('result')!r}"
                   if not (fresh and fresh.get("result") == result_payload) else "",
        )

        # 3c. update_job_status on a missing id raises JobError
        try:
            update_job_status("job_does_not_exist_xxxxxxxx", "done")
            all_ok &= _check(
                "update_job_status on a missing id raises JobError",
                ok=False, detail="no exception raised",
            )
        except JobError:
            all_ok &= _check(
                "update_job_status on a missing id raises JobError", ok=True
            )

        # 4. Enqueue a fresh job — verify it lands on the consultations queue
        queued = create_job(
            audio_key="consultations/queue-test.bin",
            source="test-script",
        )
        before = consultation_queue.count
        rq_job = enqueue_consultation(queued["id"])
        after = consultation_queue.count
        all_ok &= _check(
            "enqueue_consultation pushed the job onto 'consultations'",
            ok=(after == before + 1 and rq_job is not None),
            detail=f"queue depth {before} -> {after}",
        )
        print(
            f"  ℹ  RQ job id: {rq_job.id} — points at "
            f"worker.process_consultation_job({queued['id']!r})"
        )

    except Exception as exc:  # noqa: BLE001 — catch-all so cleanup always runs
        all_ok &= _check("unexpected error during test", ok=False, detail=repr(exc))

    finally:
        # Drop the test rows so re-runs start clean (the queue test job is
        # also removed below so a stale stub run doesn't pick it up later).
        try:
            with _jobs_pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM consultation_jobs WHERE source = 'test-script'"
                    )
                conn.commit()
        except Exception as exc:
            print(f"  ⚠  cleanup of test DB rows failed: {exc}")
        if rq_job is not None:
            try:
                rq_job.delete()
            except Exception as exc:
                print(f"  ⚠  cleanup of RQ test job failed: {exc}")

    print()
    if all_ok:
        print("✅ ALL CHECKS PASSED — jobs.py + queue_setup.py wired up correctly.")
        print()
        print("To watch a queued job actually run through the stub worker, start an")
        print("RQ worker in a SECOND terminal:")
        print()
        print('    cd "/Users/nischaldahal/Desktop/my projects/VoiceControlExcel/mockfieldpulse/voice-intake"')
        print("    # macOS only — RQ's default worker forks for each job and macOS")
        print("    # aborts the child if certain Apple frameworks are loaded.")
        print("    # OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES turns that abort off.")
        print("    # On Linux (Render) this prefix is unnecessary; the bare command works.")
        print("    OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES venv/bin/rq worker consultations")
        print()
        print("…then in this terminal, push a job and watch the worker output:")
        print()
        print("    venv/bin/python -c \"from jobs import create_job; from queue_setup import enqueue_consultation;\\")
        print("                       j = create_job('consultations/x.bin', source='manual');\\")
        print("                       enqueue_consultation(j['id']); print(j['id'])\"")
    else:
        print("❌ ONE OR MORE CHECKS FAILED — see output above.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
