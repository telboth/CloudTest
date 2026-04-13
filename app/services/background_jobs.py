from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.background_job import BackgroundJob

logger = get_logger("app.background_jobs")

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"

FINAL_JOB_STATUSES = {JOB_STATUS_COMPLETED, JOB_STATUS_FAILED}


def create_background_job(
    db: Session,
    *,
    job_type: str,
    payload_json: dict | None = None,
    requested_by: str | None = None,
    bug_id: int | None = None,
) -> BackgroundJob:
    job = BackgroundJob(
        job_type=job_type,
        status=JOB_STATUS_PENDING,
        payload_json=payload_json,
        requested_by=requested_by,
        bug_id=bug_id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info(
        "Background job created id=%s type=%s requested_by=%s bug_id=%s",
        job.id,
        job.job_type,
        requested_by,
        bug_id,
    )
    return job


def get_background_job(db: Session, job_id: int) -> BackgroundJob | None:
    return db.get(BackgroundJob, job_id)


def list_background_jobs(
    db: Session,
    *,
    status: str | None = None,
    job_type: str | None = None,
    bug_id: int | None = None,
    limit: int = 100,
) -> list[BackgroundJob]:
    stmt: Select[tuple[BackgroundJob]] = select(BackgroundJob)
    if status:
        stmt = stmt.where(BackgroundJob.status == status)
    if job_type:
        stmt = stmt.where(BackgroundJob.job_type == job_type)
    if bug_id is not None:
        stmt = stmt.where(BackgroundJob.bug_id == bug_id)
    stmt = stmt.order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def get_latest_background_job(
    db: Session,
    *,
    job_type: str | None = None,
    bug_id: int | None = None,
    requested_by: str | None = None,
    statuses: list[str] | None = None,
) -> BackgroundJob | None:
    stmt: Select[tuple[BackgroundJob]] = select(BackgroundJob)
    if job_type:
        stmt = stmt.where(BackgroundJob.job_type == job_type)
    if bug_id is not None:
        stmt = stmt.where(BackgroundJob.bug_id == bug_id)
    if requested_by:
        stmt = stmt.where(BackgroundJob.requested_by == requested_by)
    if statuses:
        stmt = stmt.where(BackgroundJob.status.in_(statuses))
    stmt = stmt.order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc()).limit(1)
    return db.scalar(stmt)


def claim_next_pending_job(
    db: Session,
    *,
    job_types: list[str] | None = None,
) -> BackgroundJob | None:
    stmt: Select[tuple[BackgroundJob]] = (
        select(BackgroundJob)
        .where(BackgroundJob.status == JOB_STATUS_PENDING)
        .order_by(BackgroundJob.created_at.asc(), BackgroundJob.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job_types:
        stmt = stmt.where(BackgroundJob.job_type.in_(job_types))

    job = db.execute(stmt).scalar_one_or_none()
    if job is None:
        return None

    job.status = JOB_STATUS_RUNNING
    job.started_at = datetime.now(timezone.utc)
    job.finished_at = None
    job.error_message = None
    db.commit()
    db.refresh(job)
    logger.info("Background job claimed id=%s type=%s", job.id, job.job_type)
    return job


def mark_background_job_completed(
    db: Session,
    job: BackgroundJob,
    *,
    result_json: dict | None = None,
) -> BackgroundJob:
    job.status = JOB_STATUS_COMPLETED
    job.result_json = result_json
    job.error_message = None
    if job.started_at is None:
        job.started_at = datetime.now(timezone.utc)
    job.finished_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    logger.info("Background job completed id=%s type=%s", job.id, job.job_type)
    return job


def mark_background_job_failed(
    db: Session,
    job: BackgroundJob,
    *,
    error_message: str,
    result_json: dict | None = None,
) -> BackgroundJob:
    job.status = JOB_STATUS_FAILED
    job.result_json = result_json
    job.error_message = error_message
    if job.started_at is None:
        job.started_at = datetime.now(timezone.utc)
    job.finished_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    logger.warning("Background job failed id=%s type=%s error=%s", job.id, job.job_type, error_message)
    return job


def requeue_background_job(
    db: Session,
    job: BackgroundJob,
    *,
    payload_json: dict | None = None,
) -> BackgroundJob:
    job.status = JOB_STATUS_PENDING
    if payload_json is not None:
        job.payload_json = payload_json
    job.result_json = None
    job.error_message = None
    job.started_at = None
    job.finished_at = None
    db.commit()
    db.refresh(job)
    logger.info("Background job requeued id=%s type=%s", job.id, job.job_type)
    return job
