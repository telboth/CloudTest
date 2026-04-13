from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

_BG_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_BG_FUTURES: dict[int, Any] = {}
_BG_FUTURES_LOCK = threading.Lock()


def serialize_background_job(job: Any) -> dict[str, Any]:
    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    created_at = job.created_at
    started_at = job.started_at
    finished_at = job.finished_at
    updated_at = finished_at or started_at or created_at
    return {
        "id": int(job.id),
        "prefix": str(payload.get("prefix") or ""),
        "bug_id": job.bug_id,
        "job_key": str(payload.get("job_key") or job.job_type or ""),
        "job_label": str(payload.get("job_label") or ""),
        "label": str(payload.get("job_label") or payload.get("job_key") or job.job_type or "job"),
        "status": str(job.status or "unknown"),
        "result": job.result_json if isinstance(job.result_json, dict) else {},
        "error": str(job.error_message or ""),
        "created_at": created_at.isoformat() if created_at else "",
        "updated_at": updated_at.isoformat() if updated_at else "",
        "started_at": started_at.isoformat() if started_at else "",
        "finished_at": finished_at.isoformat() if finished_at else "",
    }


def json_safe_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        try:
            json.dumps(value, ensure_ascii=False)
            return value
        except TypeError:
            pass
    return {"value": str(value)}


def start_background_job(
    *,
    prefix: str,
    bug_id: int,
    job_key: str,
    job_label: str,
    target: Callable[[], Any],
    normalize_email: Callable[[str | None], str],
    db_session: Callable[[], Any],
    background_job_model: Any,
    session_state: Mapping[str, Any] | dict[str, Any],
    logger: Any = None,
) -> int:
    requester_email = normalize_email(str(getattr(session_state, "get", lambda _k, _d=None: "")("email") or ""))
    with db_session() as db:
        row = background_job_model(
            job_type=job_key,
            status="pending",
            payload_json={
                "prefix": prefix,
                "job_key": job_key,
                "job_label": job_label,
            },
            requested_by=requester_email or None,
            bug_id=bug_id if bug_id > 0 else None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        job_id = int(row.id)
        created_at_iso = row.created_at.isoformat() if row.created_at else datetime.now(timezone.utc).isoformat()

    tracked_jobs_key = f"{prefix}_background_jobs"
    tracked_jobs = session_state.setdefault(tracked_jobs_key, {})
    tracked_jobs[f"{bug_id}:{job_key}"] = {
        "job_id": job_id,
        "job_label": job_label,
        "created_at": created_at_iso,
    }

    def _runner() -> None:
        with db_session() as db:
            row = db.get(background_job_model, job_id)
            if row:
                row.status = "running"
                row.started_at = datetime.now(timezone.utc)
                db.commit()
        try:
            result = target()
            with db_session() as db:
                row = db.get(background_job_model, job_id)
                if row:
                    row.status = "completed"
                    row.result_json = json_safe_payload(result)
                    row.error_message = None
                    row.finished_at = datetime.now(timezone.utc)
                    db.commit()
        except Exception as exc:
            if logger is not None:
                logger.exception("Background job failed: id=%s key=%s bug_id=%s", job_id, job_key, bug_id)
            with db_session() as db:
                row = db.get(background_job_model, job_id)
                if row:
                    row.status = "failed"
                    row.error_message = f"{exc.__class__.__name__}: {exc}"
                    row.finished_at = datetime.now(timezone.utc)
                    db.commit()

    future = _BG_EXECUTOR.submit(_runner)
    with _BG_FUTURES_LOCK:
        _BG_FUTURES[job_id] = future
    return job_id


def get_background_job(
    *,
    job_id: int,
    db_session: Callable[[], Any],
    background_job_model: Any,
) -> dict[str, Any] | None:
    with db_session() as db:
        row = db.get(background_job_model, int(job_id))
        if row is None:
            return None
        return serialize_background_job(row)


def finalize_background_job(job_id: int) -> None:
    with _BG_FUTURES_LOCK:
        _BG_FUTURES.pop(int(job_id), None)


def get_tracked_job(
    *,
    prefix: str,
    bug_id: int,
    job_key: str,
    session_state: Mapping[str, Any] | dict[str, Any],
) -> dict[str, Any] | None:
    tracked_jobs = session_state.get(f"{prefix}_background_jobs", {})
    tracked = tracked_jobs.get(f"{bug_id}:{job_key}")
    if not isinstance(tracked, dict):
        return None
    return tracked


def clear_tracked_job(
    *,
    prefix: str,
    bug_id: int,
    job_key: str,
    session_state: Mapping[str, Any] | dict[str, Any],
) -> None:
    tracked_jobs = session_state.get(f"{prefix}_background_jobs", {})
    tracked_jobs.pop(f"{bug_id}:{job_key}", None)


def wait_for_background_job_completion(
    *,
    job_id: int,
    get_background_job_fn: Callable[[int], dict[str, Any] | None],
    timeout_seconds: float = 20.0,
    poll_seconds: float = 0.5,
) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        job = get_background_job_fn(job_id)
        if not job:
            return "missing"
        status = str(job.get("status") or "unknown")
        if status in {"completed", "failed"}:
            return status
        time.sleep(max(0.1, poll_seconds))
    return "timeout"


def background_jobs_snapshot(
    *,
    db_session: Callable[[], Any],
    background_job_model: Any,
    limit: int = 80,
) -> list[dict[str, Any]]:
    with db_session() as db:
        rows = (
            db.query(background_job_model)
            .order_by(background_job_model.created_at.desc(), background_job_model.id.desc())
            .limit(int(limit))
            .all()
        )
        return [serialize_background_job(row) for row in rows]
