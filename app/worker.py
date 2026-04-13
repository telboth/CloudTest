from __future__ import annotations

import argparse
import time

from app.core.logging import get_logger, setup_logging
from app.core.database import SessionLocal
from app.services.background_jobs import (
    claim_next_pending_job,
    mark_background_job_completed,
    mark_background_job_failed,
)
from app.services.job_handlers import UnsupportedBackgroundJobError, handle_background_job

setup_logging()
logger = get_logger("app.worker")


def process_next_job(*, job_types: list[str] | None = None) -> bool:
    with SessionLocal() as db:
        job = claim_next_pending_job(db, job_types=job_types)
        if job is None:
            return False

        try:
            result = handle_background_job(db, job)
        except UnsupportedBackgroundJobError as exc:
            db.rollback()
            mark_background_job_failed(db, job, error_message=str(exc))
            logger.warning("Unsupported job type id=%s type=%s", job.id, job.job_type)
            return True
        except Exception as exc:  # pragma: no cover - defensive worker guard
            db.rollback()
            mark_background_job_failed(db, job, error_message=f"{exc.__class__.__name__}: {exc}")
            logger.exception("Background job crashed id=%s type=%s", job.id, job.job_type)
            return True

        mark_background_job_completed(db, job, result_json=result)
        logger.info("Background job processed id=%s type=%s", job.id, job.job_type)
        return True


def run_worker_loop(*, poll_seconds: float = 2.0, job_types: list[str] | None = None) -> None:
    logger.info("Worker started poll_seconds=%s job_types=%s", poll_seconds, job_types or "all")
    while True:
        processed = process_next_job(job_types=job_types)
        if not processed:
            time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Background job worker")
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="Seconds to sleep when no pending jobs are available.",
    )
    parser.add_argument(
        "--job-type",
        action="append",
        dest="job_types",
        help="Optional job type filter. Can be passed multiple times.",
    )
    args = parser.parse_args()

    if args.once:
        processed = process_next_job(job_types=args.job_types)
        logger.info("Worker single-run completed processed=%s", processed)
        return

    run_worker_loop(poll_seconds=args.poll_seconds, job_types=args.job_types)


if __name__ == "__main__":
    main()
