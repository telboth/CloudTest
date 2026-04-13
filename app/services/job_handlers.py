from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.models.background_job import BackgroundJob
from app.models.bug import Bug, BugHistory
from app.models.user import User
from app.services.ai_draft import analyze_bug_sentiment, suggest_bug_solution, summarize_bug
from app.services.ai_draft import build_bug_draft
from app.schemas.bug import BugAIDraftRequest
from app.services.search import retrieve_similar_visible_bugs

logger = get_logger("app.job_handlers")


class UnsupportedBackgroundJobError(Exception):
    pass


def handle_background_job(db: Session, job: BackgroundJob) -> dict | None:
    if job.job_type == "noop":
        logger.info("Handled noop background job id=%s", job.id)
        return {"message": "noop completed"}

    if job.job_type == "echo":
        payload = job.payload_json or {}
        logger.info("Handled echo background job id=%s", job.id)
        return {"echo": payload}

    if job.job_type == "build_ai_draft":
        payload = job.payload_json or {}
        draft_request = BugAIDraftRequest.model_validate(payload)
        draft = build_bug_draft(draft_request)
        logger.info("Handled build_ai_draft background job id=%s", job.id)
        return draft.model_dump(mode="json")

    if job.job_type == "summarize_bug":
        payload = job.payload_json or {}
        if not job.bug_id:
            raise UnsupportedBackgroundJobError("Summarize job is missing bug_id")

        bug = db.scalar(
            select(Bug)
            .options(selectinload(Bug.comments))
            .where(Bug.id == job.bug_id)
        )
        if bug is None:
            raise UnsupportedBackgroundJobError(f"Bug not found for summarize job: {job.bug_id}")

        comments = [comment.body for comment in bug.comments if comment.body]
        summary = summarize_bug(
            title=bug.title,
            description=bug.description,
            repro_steps=bug.repro_steps,
            environment=bug.environment,
            tags=bug.tags,
            status=bug.status,
            comments=comments,
            ai_provider=payload.get("ai_provider"),
            ai_model=payload.get("ai_model"),
        )
        bug.bug_summary = summary
        bug.bug_summary_updated_at = datetime.now(timezone.utc)
        if job.requested_by:
            db.add(
                BugHistory(
                    bug_id=bug.id,
                    actor_email=job.requested_by,
                    action="bug_summarized",
                    details="Generated AI summary for the bug conversation.",
                )
            )
        db.commit()
        logger.info("Handled summarize_bug background job id=%s bug_id=%s", job.id, bug.id)
        return {
            "bug_id": bug.id,
            "bug_summary": summary,
            "bug_summary_updated_at": bug.bug_summary_updated_at.isoformat() if bug.bug_summary_updated_at else None,
        }

    if job.job_type == "analyze_sentiment":
        payload = job.payload_json or {}
        if not job.bug_id:
            raise UnsupportedBackgroundJobError("Sentiment job is missing bug_id")

        bug = db.scalar(
            select(Bug)
            .options(selectinload(Bug.comments))
            .where(Bug.id == job.bug_id)
        )
        if bug is None:
            raise UnsupportedBackgroundJobError(f"Bug not found for sentiment job: {job.bug_id}")

        comments = [comment.body for comment in bug.comments if comment.body]
        sentiment_label, sentiment_summary = analyze_bug_sentiment(
            title=bug.title,
            description=bug.description,
            repro_steps=bug.repro_steps,
            comments=comments,
            reporter_satisfaction=bug.reporter_satisfaction,
            ai_provider=payload.get("ai_provider"),
            ai_model=payload.get("ai_model"),
        )
        bug.sentiment_label = sentiment_label
        bug.sentiment_summary = sentiment_summary
        bug.sentiment_analyzed_at = datetime.now(timezone.utc)
        if job.requested_by:
            db.add(
                BugHistory(
                    bug_id=bug.id,
                    actor_email=job.requested_by,
                    action="sentiment_analyzed",
                    details=f'Sentiment analyzed as "{sentiment_label}". {sentiment_summary}',
                )
            )
        db.commit()
        logger.info("Handled analyze_sentiment background job id=%s bug_id=%s", job.id, bug.id)
        return {
            "bug_id": bug.id,
            "sentiment_label": sentiment_label,
            "sentiment_summary": sentiment_summary,
            "sentiment_analyzed_at": bug.sentiment_analyzed_at.isoformat() if bug.sentiment_analyzed_at else None,
        }

    if job.job_type == "suggest_solution":
        payload = job.payload_json or {}
        if not job.bug_id:
            raise UnsupportedBackgroundJobError("Solution suggestion job is missing bug_id")

        bug = db.scalar(
            select(Bug)
            .options(selectinload(Bug.comments))
            .where(Bug.id == job.bug_id)
        )
        if bug is None:
            raise UnsupportedBackgroundJobError(f"Bug not found for solution suggestion job: {job.bug_id}")
        if not job.requested_by:
            raise UnsupportedBackgroundJobError("Solution suggestion job is missing requested_by")

        current_user = db.get(User, job.requested_by)
        if current_user is None:
            raise UnsupportedBackgroundJobError(f"User not found for solution suggestion job: {job.requested_by}")

        query_text = " ".join(
            part for part in [
                bug.title,
                bug.description,
                bug.repro_steps or "",
                bug.environment or "",
                bug.tags or "",
            ]
            if part
        )
        similar_bugs_raw = retrieve_similar_visible_bugs(
            db,
            current_user=current_user,
            query=query_text,
            limit=5,
            embedding_provider=payload.get("embedding_provider"),
            embedding_model=payload.get("embedding_model"),
        )
        similar_bugs = []
        for similar_bug in similar_bugs_raw:
            if similar_bug.id == bug.id:
                continue
            similar_bugs.append(
                {
                    "id": similar_bug.id,
                    "title": similar_bug.title,
                    "description": similar_bug.description,
                    "repro_steps": similar_bug.repro_steps,
                    "resolution_summary": similar_bug.resolution_summary or similar_bug.workaround or similar_bug.bug_summary,
                    "status": similar_bug.status,
                }
            )
        comments = [
            {
                "author_role": comment.author_role,
                "body": comment.body,
            }
            for comment in bug.comments
            if comment.body
        ]
        suggestion = suggest_bug_solution(
            title=bug.title,
            description=bug.description,
            repro_steps=bug.repro_steps,
            environment=bug.environment,
            tags=bug.tags,
            comments=comments,
            similar_bugs=similar_bugs,
            ai_provider=payload.get("ai_provider"),
            ai_model=payload.get("ai_model"),
        )
        logger.info("Handled suggest_solution background job id=%s bug_id=%s", job.id, bug.id)
        return {
            "bug_id": bug.id,
            "suggestion": suggestion,
            "source": payload.get("ai_provider") or "default",
            "similar_bug_ids": [item["id"] for item in similar_bugs[:3]],
        }

    raise UnsupportedBackgroundJobError(f"Unsupported background job type: {job.job_type}")
