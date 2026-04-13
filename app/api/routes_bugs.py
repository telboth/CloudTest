from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

import requests
from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.logging import get_logger
from app.models.bug import Attachment, Bug, BugComment, BugHistory, BugViewState
from app.models.user import User
from app.api.routes_auth import get_entra_access_token_for_email
from app.schemas.background_job import BackgroundJobRead
from app.schemas.bug import (
    AITaskRequest,
    BugAIDraftRequest,
    BugChangesSinceLastView,
    BugDuplicateCandidate,
    BugCreate,
    BugDescriptionTypeaheadRequest,
    BugDescriptionTypeaheadResponse,
    BugDuplicateCheckRequest,
    BugDuplicateCheckResponse,
    BugRead,
    BugUpdate,
    CommentCreate,
    CommentRead,
)
from app.services.azure_devops import (
    AzureDevOpsError,
    apply_ado_sync_to_bug,
    azure_devops_enabled,
    clear_ado_sync_from_bug,
    create_task_from_bug,
    delete_task_from_bug,
    verify_task_removed,
    list_assignable_devops_users,
    update_task_from_bug,
)
from app.services.ai_draft import (
    infer_bug_environment,
    infer_bug_severity,
    normalize_notify_targets,
    normalize_tag_list,
    review_bug_title,
    suggest_bug_description_continuation,
)
from app.services.background_jobs import (
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    create_background_job,
    get_background_job,
    get_latest_background_job,
    list_background_jobs,
)
from app.services.ai_provider import get_ai_provider_status, get_embedding_provider_status
from app.services.permissions import can_assign_bug, can_close_bug, can_reopen_bug, can_update_bug, can_view_bug
from app.services.search import (
    get_search_telemetry_snapshot,
    rebuild_bug_search_index,
    retrieve_similar_visible_bugs,
    search_visible_bugs,
    upsert_bug_search_index_by_id,
)

router = APIRouter()
logger = get_logger("app.bugs")


def _get_user_by_email(db: Session, email: str) -> User | None:
    normalized = email.strip()
    if not normalized:
        return None
    return db.scalar(select(User).where(func.lower(User.email) == normalized.casefold()))


def _is_valid_assignable_email(email: str) -> bool:
    try:
        validate_email(email, check_deliverability=False)
        return True
    except EmailNotValidError:
        return False


def _normalize_comment_body(body: str) -> str:
    return " ".join(body.split()).casefold()


def _normalize_bug_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _bug_duplicate_signature(
    *,
    title: str,
    description: str,
    category: str | None,
    severity: str | None,
    repro_steps: str | None,
) -> tuple[str, str, str, str, str]:
    return (
        _normalize_bug_text(title),
        _normalize_bug_text(description),
        _normalize_bug_text(category or ""),
        _normalize_bug_text(severity or ""),
        _normalize_bug_text(repro_steps or ""),
    )


def _duplicate_core_signature(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
) -> tuple[str, str, str]:
    return (
        _normalize_bug_text(title),
        _normalize_bug_text(description),
        _normalize_bug_text(repro_steps or ""),
    )


def _text_similarity(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _find_duplicate_matches(
    db: Session,
    *,
    title: str,
    description: str,
    category: str | None,
    severity: str | None,
    repro_steps: str | None,
) -> tuple[int | None, list[int]]:
    new_bug_signature = _bug_duplicate_signature(
        title=title,
        description=description,
        category=category,
        severity=severity,
        repro_steps=repro_steps,
    )
    new_bug_core_signature = _duplicate_core_signature(
        title=title,
        description=description,
        repro_steps=repro_steps,
    )
    new_bug_text = " ".join(new_bug_signature)
    exact_duplicate_id: int | None = None
    possible_duplicate_ids: list[int] = []

    existing_bugs = db.scalars(select(Bug)).all()
    for existing_bug in existing_bugs:
        existing_signature = _bug_duplicate_signature(
            title=existing_bug.title,
            description=existing_bug.description,
            category=existing_bug.category,
            severity=existing_bug.severity,
            repro_steps=existing_bug.repro_steps,
        )
        existing_core_signature = _duplicate_core_signature(
            title=existing_bug.title,
            description=existing_bug.description,
            repro_steps=existing_bug.repro_steps,
        )
        if (
            existing_signature == new_bug_signature
            or existing_core_signature == new_bug_core_signature
            or (
                existing_core_signature[1] == new_bug_core_signature[1]
                and existing_core_signature[2] == new_bug_core_signature[2]
            )
        ):
            exact_duplicate_id = existing_bug.id
            break

        existing_bug_text = " ".join(existing_signature)
        title_similarity = _text_similarity(new_bug_core_signature[0], existing_core_signature[0])
        description_similarity = _text_similarity(new_bug_core_signature[1], existing_core_signature[1])
        repro_similarity = _text_similarity(new_bug_core_signature[2], existing_core_signature[2])
        combined_similarity = _text_similarity(new_bug_text, existing_bug_text)
        if (
            combined_similarity >= 0.82
            or title_similarity >= 0.9
            or description_similarity >= 0.86
            or (description_similarity >= 0.78 and repro_similarity >= 0.78)
        ):
            possible_duplicate_ids.append(existing_bug.id)

    return exact_duplicate_id, possible_duplicate_ids


def _visible_bugs_for_user(db: Session, current_user: User) -> list[Bug]:
    query = (
        select(Bug)
        .options(selectinload(Bug.attachments), selectinload(Bug.comments), selectinload(Bug.history))
        .order_by(Bug.created_at.desc())
    )
    if current_user.role == "admin":
        return list(db.scalars(query).all())
    if current_user.role == "assignee":
        return list(db.scalars(query.where(Bug.assignee_id == current_user.email)).all())
    return list(db.scalars(query.where(Bug.reporter_id == current_user.email)).all())


def _bug_keep_score(bug: Bug) -> int:
    score = 0
    if bug.status != "closed":
        score += 5
    if bug.ado_work_item_id:
        score += 4
    score += len(bug.comments or []) * 2
    score += len(bug.attachments or [])
    if bug.created_at:
        score += 1
    return score


def _summarize_keep_reason(keep_bug: Bug, delete_bug: Bug) -> str:
    reasons: list[str] = []
    if keep_bug.status != "closed" and delete_bug.status == "closed":
        reasons.append("den er fortsatt åpen")
    if keep_bug.ado_work_item_id and not delete_bug.ado_work_item_id:
        reasons.append("den har DevOps-kobling")
    if len(keep_bug.comments or []) > len(delete_bug.comments or []):
        reasons.append("den har mer samtalehistorikk")
    if len(keep_bug.attachments or []) > len(delete_bug.attachments or []):
        reasons.append("den har flere vedlegg")
    if keep_bug.created_at and delete_bug.created_at and keep_bug.created_at <= delete_bug.created_at:
        reasons.append("den er den eldste registrerte saken")
    if not reasons:
        reasons.append("den ser ut til å være den mest komplette saken")
    return "Behold denne fordi " + ", ".join(reasons) + "."


def _build_duplicate_candidate(left_bug: Bug, right_bug: Bug) -> BugDuplicateCandidate | None:
    left_core = _duplicate_core_signature(
        title=left_bug.title,
        description=left_bug.description,
        repro_steps=left_bug.repro_steps,
    )
    right_core = _duplicate_core_signature(
        title=right_bug.title,
        description=right_bug.description,
        repro_steps=right_bug.repro_steps,
    )
    left_text = " ".join(_bug_duplicate_signature(
        title=left_bug.title,
        description=left_bug.description,
        category=left_bug.category,
        severity=left_bug.severity,
        repro_steps=left_bug.repro_steps,
    ))
    right_text = " ".join(_bug_duplicate_signature(
        title=right_bug.title,
        description=right_bug.description,
        category=right_bug.category,
        severity=right_bug.severity,
        repro_steps=right_bug.repro_steps,
    ))
    title_similarity = _text_similarity(left_core[0], right_core[0])
    description_similarity = _text_similarity(left_core[1], right_core[1])
    repro_similarity = _text_similarity(left_core[2], right_core[2])
    combined_similarity = _text_similarity(left_text, right_text)
    exact_core_match = (
        left_core == right_core
        or (
            left_core[1] == right_core[1]
            and left_core[2] == right_core[2]
            and left_core[1] != ""
        )
    )
    is_candidate = (
        exact_core_match
        or combined_similarity >= 0.82
        or title_similarity >= 0.9
        or description_similarity >= 0.86
        or (description_similarity >= 0.78 and repro_similarity >= 0.78)
    )
    if not is_candidate:
        return None

    left_score = _bug_keep_score(left_bug)
    right_score = _bug_keep_score(right_bug)
    if right_score > left_score or (right_score == left_score and right_bug.id < left_bug.id):
        keep_bug = right_bug
        delete_bug = left_bug
    else:
        keep_bug = left_bug
        delete_bug = right_bug

    return BugDuplicateCandidate(
        keep_bug_id=keep_bug.id,
        keep_title=keep_bug.title,
        keep_status=keep_bug.status,
        delete_bug_id=delete_bug.id,
        delete_title=delete_bug.title,
        delete_status=delete_bug.status,
        similarity_score=round(max(combined_similarity, description_similarity, title_similarity), 3),
        recommendation_reason=_summarize_keep_reason(keep_bug, delete_bug),
    )


def _find_duplicate_candidates_for_visible_bugs(bugs: list[Bug]) -> list[BugDuplicateCandidate]:
    best_candidates_by_delete_bug: dict[int, BugDuplicateCandidate] = {}
    for left_index, left_bug in enumerate(bugs):
        for right_bug in bugs[left_index + 1 :]:
            candidate = _build_duplicate_candidate(left_bug, right_bug)
            if not candidate:
                continue
            existing_candidate = best_candidates_by_delete_bug.get(candidate.delete_bug_id)
            if (
                existing_candidate is None
                or candidate.similarity_score > existing_candidate.similarity_score
            ):
                best_candidates_by_delete_bug[candidate.delete_bug_id] = candidate
    candidates = list(best_candidates_by_delete_bug.values())
    candidates.sort(key=lambda item: item.similarity_score, reverse=True)
    return candidates[:10]


def _get_bug_or_404(db: Session, bug_id: int) -> Bug:
    bug = db.scalar(
        select(Bug)
        .options(selectinload(Bug.attachments), selectinload(Bug.comments), selectinload(Bug.history))
        .where(Bug.id == bug_id)
    )
    if not bug:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")
    return bug


def _get_bug_view_state(db: Session, *, bug_id: int, user_email: str) -> BugViewState | None:
    return db.scalar(
        select(BugViewState).where(
            BugViewState.bug_id == bug_id,
            BugViewState.user_email == user_email,
        )
    )


def _build_changes_since_last_view(bug: Bug, *, user_email: str, db: Session) -> BugChangesSinceLastView | None:
    view_state = _get_bug_view_state(db, bug_id=bug.id, user_email=user_email)
    if not view_state or not view_state.last_viewed_at:
        return None

    last_viewed_at = view_state.last_viewed_at
    new_comments = [
        comment for comment in (bug.comments or [])
        if comment.created_at and comment.created_at > last_viewed_at
    ]
    new_history = [
        entry for entry in (bug.history or [])
        if entry.created_at and entry.created_at > last_viewed_at
    ]
    change_count = len(new_comments) + len(new_history)
    if change_count == 0:
        return None

    latest_comment = max(new_comments, key=lambda item: item.created_at, default=None)
    latest_history = max(new_history, key=lambda item: item.created_at, default=None)
    latest_change_at = max(
        [item.created_at for item in [latest_comment, latest_history] if item and item.created_at],
        default=None,
    )

    summary_parts: list[str] = []
    if new_comments:
        latest_reporter_comment = next(
            (
                comment for comment in sorted(new_comments, key=lambda item: item.created_at, reverse=True)
                if str(comment.author_role).strip().casefold() == "reporter"
            ),
            None,
        )
        if latest_reporter_comment:
            summary_parts.append("Nytt svar fra reporter")
        else:
            summary_parts.append(f"{len(new_comments)} nye samtaleoppdateringer")
    if new_history:
        summary_parts.append(f"{len(new_history)} felt- eller systemendringer")

    summary = " og ".join(summary_parts) if summary_parts else "Nye endringer siden sist"
    return BugChangesSinceLastView(
        count=change_count,
        summary=summary,
        last_change_at=latest_change_at,
    )


def _attach_view_metadata(bug: Bug, *, current_user: User, db: Session) -> Bug:
    setattr(bug, "changes_since_last_view", _build_changes_since_last_view(bug, user_email=current_user.email, db=db))
    return bug


def _attach_view_metadata_for_bugs(bugs: list[Bug], *, current_user: User, db: Session) -> list[Bug]:
    return [_attach_view_metadata(bug, current_user=current_user, db=db) for bug in bugs]


def _mark_bug_viewed(db: Session, *, bug: Bug, user_email: str) -> BugViewState:
    view_state = _get_bug_view_state(db, bug_id=bug.id, user_email=user_email)
    now = datetime.now(timezone.utc)
    if view_state is None:
        view_state = BugViewState(bug_id=bug.id, user_email=user_email, last_viewed_at=now)
        db.add(view_state)
    else:
        view_state.last_viewed_at = now
    db.commit()
    db.refresh(view_state)
    return view_state


def _ensure_user_exists(db: Session, email: str) -> None:
    if not _get_user_by_email(db, email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown user: {email}")


def _ensure_assignable_user_exists(db: Session, email: str) -> None:
    existing_user = _get_user_by_email(db, email)
    if existing_user:
        return
    try:
        devops_users = list_assignable_devops_users()
    except AzureDevOpsError:
        devops_users = []
    matching_user = next((user for user in devops_users if user["email"].casefold() == email.casefold()), None)
    if matching_user:
        db.add(
            User(
                email=matching_user["email"],
                full_name=matching_user.get("name") or matching_user["email"],
                password_hash="external-user",
                role="assignee",
                auth_provider="azure_devops",
            )
        )
        db.flush()
        return
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown user: {email}")


def _write_history(db: Session, bug_id: int, actor_email: str, action: str, details: str) -> None:
    db.add(BugHistory(bug_id=bug_id, actor_email=actor_email, action=action, details=details))


def _is_reopen_request(changes: dict) -> bool:
    return set(changes.keys()) == {"status"} and changes.get("status") in {"open", "in_progress", "resolved"}


def _sync_existing_bug_to_devops(
    db: Session,
    bug: Bug,
    *,
    current_user: User,
    changed_fields: list[str] | None = None,
    comment_text: str | None = None,
) -> None:
    if not bug.ado_work_item_id or not azure_devops_enabled():
        return

    access_token = get_entra_access_token_for_email(current_user.email)
    try:
        work_item = update_task_from_bug(
            bug,
            access_token,
            changed_fields=changed_fields,
            comment_text=comment_text,
        )
        apply_ado_sync_to_bug(bug, work_item, sync_status="updated")
        logger.info("Auto-synced bug to Azure DevOps bug_id=%s work_item_id=%s", bug.id, bug.ado_work_item_id)
    except (AzureDevOpsError, requests.RequestException) as exc:
        bug.ado_sync_status = "update_failed"
        logger.warning("Auto-sync to Azure DevOps failed bug_id=%s error=%s", bug.id, exc)
    bug.ado_synced_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(bug)


def _refresh_bug_search_index(
    db: Session,
    *,
    bug_id: int,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    build_embedding: bool = False,
) -> None:
    try:
        row = upsert_bug_search_index_by_id(
            db,
            bug_id=bug_id,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            build_embedding=build_embedding,
        )
        if row is None:
            return
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning(
            "Bug search index update failed bug_id=%s provider=%s model=%s error=%s",
            bug_id,
            embedding_provider or settings.embedding_provider,
            embedding_model or settings.embedding_model,
            exc,
        )


def _apply_ai_title_review(
    *,
    title: str,
    description: str,
    repro_steps: str | None = None,
    environment: str | None = None,
    tags: str | None = None,
    category: str | None = None,
    severity: str | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
) -> tuple[str, bool, str]:
    review = review_bug_title(
        title=title,
        description=description,
        repro_steps=repro_steps,
        environment=environment,
        tags=tags,
        category=category,
        severity=severity,
        ai_provider=ai_provider,
        ai_model=ai_model,
    )
    return review["title"], review["was_changed"], review["reason"]


@router.get("/", response_model=list[BugRead])
def list_bugs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Bug]:
    return _attach_view_metadata_for_bugs(_visible_bugs_for_user(db, current_user), current_user=current_user, db=db)


@router.get("/search", response_model=list[BugRead])
def search_bugs(
    query: str,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Bug]:
    bugs = search_visible_bugs(
        db,
        current_user=current_user,
        query=query,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
    )
    return _attach_view_metadata_for_bugs(bugs, current_user=current_user, db=db)


@router.get("/assignable-users")
def list_assignable_users(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, str]]:
    del current_user
    merged_users: list[dict[str, str]] = []
    seen_emails: set[str] = set()

    local_users = db.scalars(select(User).order_by(User.full_name, User.email)).all()
    for user in local_users:
        if not _is_valid_assignable_email(user.email):
            continue
        normalized_email = user.email.casefold()
        if normalized_email in seen_emails:
            continue
        seen_emails.add(normalized_email)
        merged_users.append({"email": user.email, "full_name": user.full_name})

    try:
        devops_users = list_assignable_devops_users()
    except AzureDevOpsError:
        devops_users = []
    for user in devops_users:
        if not _is_valid_assignable_email(user["email"]):
            continue
        normalized_email = user["email"].casefold()
        if normalized_email in seen_emails:
            continue
        seen_emails.add(normalized_email)
        merged_users.append(user)

    merged_users.sort(key=lambda user: (user["full_name"].casefold(), user["email"].casefold()))
    return merged_users


@router.get("/provider-status")
def provider_status(
    ai_provider: str | None = None,
    ai_model: str | None = None,
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    del current_user
    return get_ai_provider_status(ai_provider=ai_provider, ai_model=ai_model)


@router.get("/embedding-status")
def embedding_status(
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    del current_user
    return get_embedding_provider_status(
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
    )


@router.get("/search-telemetry")
def search_telemetry(
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return {
        "status": "ok",
        "telemetry": get_search_telemetry_snapshot(),
    }


@router.post("/search-index/rebuild")
def rebuild_semantic_search_index(
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    build_embeddings: bool = True,
    limit: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    if limit is not None and limit < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Limit must be greater than 0")

    try:
        indexed_count = rebuild_bug_search_index(
            db,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            build_embedding=build_embeddings,
            limit=limit,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not rebuild semantic search index: {exc.__class__.__name__}: {exc}",
        ) from exc

    return {
        "status": "ok",
        "indexed": indexed_count,
        "embedding_provider": embedding_provider or settings.embedding_provider,
        "embedding_model": embedding_model
        or (settings.local_embedding_model if (embedding_provider or settings.embedding_provider).strip().casefold() == "local" else settings.embedding_model),
        "build_embeddings": build_embeddings,
    }


@router.post("/ai-draft", response_model=BackgroundJobRead, status_code=status.HTTP_202_ACCEPTED)
def create_ai_draft(
    payload: BugAIDraftRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BackgroundJobRead:
    job = create_background_job(
        db,
        job_type="build_ai_draft",
        payload_json=payload.model_dump(mode="json"),
        requested_by=current_user.email,
    )
    return job


@router.post("/description-typeahead", response_model=BugDescriptionTypeaheadResponse)
def create_description_typeahead(
    payload: BugDescriptionTypeaheadRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BugDescriptionTypeaheadResponse:
    query_text = " ".join(
        part for part in [
            payload.title or "",
            payload.description,
            payload.repro_steps or "",
            payload.category or "",
            payload.environment or "",
            payload.tags or "",
        ] if part
    )
    similar_bugs = retrieve_similar_visible_bugs(
        db,
        current_user=current_user,
        query=query_text,
        limit=3,
        embedding_provider=payload.embedding_provider,
        embedding_model=payload.embedding_model,
    )
    similar_bug_context = [
        {
            "id": bug.id,
            "title": bug.title,
            "description": bug.description,
            "repro_steps": bug.repro_steps,
            "resolution_summary": bug.resolution_summary or bug.workaround or bug.bug_summary or (bug.comments[-1].body if bug.comments else None),
            "status": bug.status,
        }
        for bug in similar_bugs
    ]
    return suggest_bug_description_continuation(
        title=payload.title,
        description=payload.description,
        repro_steps=payload.repro_steps,
        category=payload.category,
        severity=payload.severity,
        environment=payload.environment,
        tags=payload.tags,
        similar_bugs=similar_bug_context,
        ai_provider=payload.ai_provider,
        ai_model=payload.ai_model,
    )


@router.post("/{bug_id}/sentiment-analysis", response_model=BackgroundJobRead, status_code=status.HTTP_202_ACCEPTED)
def run_bug_sentiment_analysis(
    bug_id: int,
    payload: AITaskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BackgroundJobRead:
    bug = _get_bug_or_404(db, bug_id)
    if not can_view_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    existing_job = get_latest_background_job(
        db,
        job_type="analyze_sentiment",
        bug_id=bug.id,
        requested_by=current_user.email,
        statuses=[JOB_STATUS_PENDING, JOB_STATUS_RUNNING],
    )
    if existing_job:
        return existing_job

    job = create_background_job(
        db,
        job_type="analyze_sentiment",
        payload_json={
            "ai_provider": payload.ai_provider,
            "ai_model": payload.ai_model,
        },
        requested_by=current_user.email,
        bug_id=bug.id,
    )
    return job


@router.post("/{bug_id}/solution-suggestion", response_model=BackgroundJobRead, status_code=status.HTTP_202_ACCEPTED)
def create_solution_suggestion(
    bug_id: int,
    payload: AITaskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BackgroundJobRead:
    bug = _get_bug_or_404(db, bug_id)
    if not can_view_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    existing_job = get_latest_background_job(
        db,
        job_type="suggest_solution",
        bug_id=bug.id,
        requested_by=current_user.email,
        statuses=[JOB_STATUS_PENDING, JOB_STATUS_RUNNING],
    )
    if existing_job:
        return existing_job

    job = create_background_job(
        db,
        job_type="suggest_solution",
        payload_json={
            "ai_provider": payload.ai_provider,
            "ai_model": payload.ai_model,
            "embedding_provider": payload.embedding_provider,
            "embedding_model": payload.embedding_model,
        },
        requested_by=current_user.email,
        bug_id=bug.id,
    )
    return job


@router.get("/background-jobs/{job_id}", response_model=BackgroundJobRead)
def get_background_job_status(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BackgroundJobRead:
    job = get_background_job(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Background job not found")

    if job.requested_by and job.requested_by != current_user.email and current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    if job.bug_id is not None:
        bug = _get_bug_or_404(db, job.bug_id)
        if not can_view_bug(current_user, bug):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return job


@router.get("/background-jobs", response_model=list[BackgroundJobRead])
def list_background_job_statuses(
    limit: int = 20,
    status_filter: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BackgroundJobRead]:
    jobs = list_background_jobs(
        db,
        status=status_filter,
        limit=max(1, min(limit, 100)),
    )
    visible_jobs: list = []
    for job in jobs:
        if current_user.role != "admin" and job.requested_by and job.requested_by != current_user.email:
            continue
        if job.bug_id is not None:
            bug = _get_bug_or_404(db, job.bug_id)
            if not can_view_bug(current_user, bug):
                continue
        visible_jobs.append(job)
    return visible_jobs


@router.get("/jobs/recent", response_model=list[BackgroundJobRead])
def list_recent_background_jobs(
    limit: int = 20,
    status_filter: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BackgroundJobRead]:
    return list_background_job_statuses(
        limit=limit,
        status_filter=status_filter,
        current_user=current_user,
        db=db,
    )


@router.post("/{bug_id}/summarize", response_model=BackgroundJobRead, status_code=status.HTTP_202_ACCEPTED)
def summarize_bug_route(
    bug_id: int,
    payload: AITaskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BackgroundJobRead:
    bug = _get_bug_or_404(db, bug_id)
    if not can_view_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    existing_job = get_latest_background_job(
        db,
        job_type="summarize_bug",
        bug_id=bug.id,
        requested_by=current_user.email,
        statuses=[JOB_STATUS_PENDING, JOB_STATUS_RUNNING],
    )
    if existing_job:
        return existing_job

    job = create_background_job(
        db,
        job_type="summarize_bug",
        payload_json={
            "ai_provider": payload.ai_provider,
            "ai_model": payload.ai_model,
        },
        requested_by=current_user.email,
        bug_id=bug.id,
    )
    return job


@router.post("/duplicate-check", response_model=BugDuplicateCheckResponse)
def duplicate_check(
    payload: BugDuplicateCheckRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BugDuplicateCheckResponse:
    del current_user
    exact_duplicate_id, possible_duplicate_ids = _find_duplicate_matches(
        db,
        title=payload.title,
        description=payload.description,
        category=payload.category,
        severity=payload.severity,
        repro_steps=payload.repro_steps,
    )
    return BugDuplicateCheckResponse(
        exact_duplicate_id=exact_duplicate_id,
        possible_duplicate_ids=possible_duplicate_ids,
    )


@router.get("/duplicate-candidates", response_model=list[BugDuplicateCandidate])
def duplicate_candidates(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BugDuplicateCandidate]:
    visible_bugs = _visible_bugs_for_user(db, current_user)
    return _find_duplicate_candidates_for_visible_bugs(visible_bugs)


@router.post("/", response_model=BugRead, status_code=status.HTTP_201_CREATED)
def create_bug(
    payload: BugCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Bug:
    if current_user.role != "admin" and str(payload.reporter_id) != current_user.email:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Reporter email must match your account")

    reporter_email = str(payload.reporter_id)
    assignee_email = str(payload.assignee_id) if payload.assignee_id else None

    _ensure_user_exists(db, reporter_email)
    if assignee_email:
        _ensure_assignable_user_exists(db, assignee_email)

    exact_duplicate_id, _possible_duplicate_ids = _find_duplicate_matches(
        db,
        title=payload.title,
        description=payload.description,
        category=payload.category,
        severity=payload.severity,
        repro_steps=payload.repro_steps,
    )
    if exact_duplicate_id is not None:
        logger.warning("Duplicate bug blocked reporter=%s duplicate_id=%s", reporter_email, exact_duplicate_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This bug has already been submitted as bug #{exact_duplicate_id}.",
        )

    reviewed_title, title_changed, title_reason = _apply_ai_title_review(
        title=payload.title,
        description=payload.description,
        repro_steps=payload.repro_steps,
        environment=payload.environment,
        tags=payload.tags,
        category=payload.category,
        severity=payload.severity,
        ai_provider=payload.ai_provider,
        ai_model=payload.ai_model,
    )
    inferred_severity = payload.severity
    severity_changed = False
    if not (payload.severity or "").strip():
        inferred_severity = infer_bug_severity(
            title=reviewed_title,
            description=payload.description,
            repro_steps=payload.repro_steps,
            environment=payload.environment,
            tags=payload.tags,
            category=payload.category,
            ai_provider=payload.ai_provider,
            ai_model=payload.ai_model,
        )
        severity_changed = True
    inferred_environment = payload.environment
    environment_changed = False
    if not (payload.environment or "").strip():
        inferred_environment = infer_bug_environment(
            title=reviewed_title,
            description=payload.description,
            repro_steps=payload.repro_steps,
            tags=payload.tags,
            category=payload.category,
            severity=inferred_severity,
            ai_provider=payload.ai_provider,
            ai_model=payload.ai_model,
        )
        environment_changed = True

    bug = Bug(
        title=reviewed_title,
        description=payload.description,
        category=payload.category,
        severity=inferred_severity or "medium",
        reporting_date=payload.reporting_date or datetime.now(timezone.utc),
        notify_emails=normalize_notify_targets(payload.notify_emails),
        environment=inferred_environment,
        repro_steps=payload.repro_steps,
        tags=normalize_tag_list(payload.tags),
        workaround=None,
        resolution_summary=None,
        reporter_id=reporter_email,
        assignee_id=assignee_email,
    )
    db.add(bug)
    db.flush()
    if payload.description.strip():
        db.add(
            BugComment(
                bug_id=bug.id,
                author_email=current_user.email,
                author_role=current_user.role,
                body=payload.description.strip(),
            )
        )
    _write_history(db, bug.id, current_user.email, "created", f"Bug created with status {bug.status}")
    if title_changed:
        _write_history(
            db,
            bug.id,
            current_user.email,
            "title_ai_updated",
            f'AI updated title from "{payload.title}" to "{reviewed_title}". Reason: {title_reason}',
        )
    if severity_changed:
        _write_history(
            db,
            bug.id,
            current_user.email,
            "severity_ai_inferred",
            f'AI inferred severity as "{inferred_severity}" because the field was empty.',
        )
    if environment_changed:
        _write_history(
            db,
            bug.id,
            current_user.email,
            "environment_ai_inferred",
            f'AI inferred environment as "{inferred_environment}" because the field was empty.',
        )
    db.commit()
    db.refresh(bug)
    _refresh_bug_search_index(db, bug_id=bug.id, build_embedding=False)
    logger.info("Bug created bug_id=%s reporter=%s assignee=%s", bug.id, reporter_email, assignee_email or "")
    return _attach_view_metadata(_get_bug_or_404(db, bug.id), current_user=current_user, db=db)


@router.delete("/{bug_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bug(
    bug_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    bug = _get_bug_or_404(db, bug_id)
    if current_user.role != "admin" and bug.reporter_id != current_user.email:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    for attachment in bug.attachments:
        storage_path = Path(attachment.storage_path)
        if storage_path.exists():
            storage_path.unlink(missing_ok=True)

    db.delete(bug)
    db.commit()
    logger.info("Bug deleted bug_id=%s by=%s", bug_id, current_user.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{bug_id}", response_model=BugRead)
def get_bug(
    bug_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Bug:
    bug = _get_bug_or_404(db, bug_id)
    if not can_view_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return _attach_view_metadata(bug, current_user=current_user, db=db)


@router.post("/{bug_id}/mark-viewed")
def mark_bug_viewed(
    bug_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    bug = _get_bug_or_404(db, bug_id)
    if not can_view_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    view_state = _mark_bug_viewed(db, bug=bug, user_email=current_user.email)
    return {"status": "ok", "last_viewed_at": view_state.last_viewed_at.isoformat()}


@router.patch("/{bug_id}", response_model=BugRead)
def update_bug(
    bug_id: int,
    payload: BugUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Bug:
    bug = _get_bug_or_404(db, bug_id)
    if not can_update_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    changes = payload.model_dump(exclude_unset=True)
    ai_provider = changes.pop("ai_provider", payload.ai_provider)
    ai_model = changes.pop("ai_model", payload.ai_model)
    if "notify_emails" in changes:
        changes["notify_emails"] = normalize_notify_targets(changes["notify_emails"])
    if "tags" in changes:
        changes["tags"] = normalize_tag_list(changes["tags"])
    for text_field in ("workaround", "resolution_summary"):
        if text_field in changes and changes[text_field] is not None:
            trimmed_value = str(changes[text_field]).strip()
            changes[text_field] = trimmed_value or None
    if bug.status == "closed":
        if not _is_reopen_request(changes):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Closed bugs are read-only. Reopen the bug before making changes.",
            )
        if not can_reopen_bug(current_user, bug):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
        bug.closed_at = None

    if "assignee_id" in changes:
        assignee_value = changes["assignee_id"]
        if assignee_value is not None:
            changes["assignee_id"] = str(assignee_value)
            _ensure_assignable_user_exists(db, changes["assignee_id"])

        if not can_assign_bug(current_user, bug):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not allowed to change the assignee for this bug",
            )

    if "status" in changes and changes["status"] == "closed":
        if not can_close_bug(current_user, bug):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
        changes["closed_at"] = datetime.now(timezone.utc)

    title_review_fields = {"title", "description", "repro_steps", "environment", "tags", "category", "severity"}
    if any(field in changes for field in title_review_fields):
        original_title = bug.title
        reviewed_title, title_changed, title_reason = _apply_ai_title_review(
            title=str(changes.get("title", bug.title)),
            description=str(changes.get("description", bug.description)),
            repro_steps=changes.get("repro_steps", bug.repro_steps),
            environment=changes.get("environment", bug.environment),
            tags=changes.get("tags", bug.tags),
            category=changes.get("category", bug.category),
            severity=changes.get("severity", bug.severity),
            ai_provider=ai_provider,
            ai_model=ai_model,
        )
        changes["title"] = reviewed_title
    else:
        title_changed = False
        title_reason = ""
        original_title = bug.title

    for field, value in changes.items():
        setattr(bug, field, value)

    change_list = ", ".join(sorted(changes.keys())) if changes else "no fields"
    action = "reopened" if bug.status in {"open", "in_progress", "resolved"} and "status" in changes and bug.closed_at is None and _is_reopen_request(changes) else "updated"
    _write_history(db, bug.id, current_user.email, action, f"Updated fields: {change_list}")
    if title_changed and bug.title != original_title:
        _write_history(
            db,
            bug.id,
            current_user.email,
            "title_ai_updated",
            f'AI updated title from "{original_title}" to "{bug.title}". Reason: {title_reason}',
        )
    db.commit()
    db.refresh(bug)
    _refresh_bug_search_index(db, bug_id=bug.id, build_embedding=False)
    logger.info("Bug updated bug_id=%s fields=%s by=%s", bug.id, ",".join(sorted(changes.keys())), current_user.email)
    _sync_existing_bug_to_devops(
        db,
        bug,
        current_user=current_user,
        changed_fields=list(changes.keys()),
    )
    return _attach_view_metadata(_get_bug_or_404(db, bug.id), current_user=current_user, db=db)


@router.post("/{bug_id}/close", response_model=BugRead)
def close_bug(
    bug_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Bug:
    bug = _get_bug_or_404(db, bug_id)
    if not can_close_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    if bug.status == "closed":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bug is already closed")

    bug.status = "closed"
    bug.closed_at = datetime.now(timezone.utc)
    _write_history(db, bug.id, current_user.email, "closed", "Bug closed")
    db.commit()
    db.refresh(bug)
    _refresh_bug_search_index(db, bug_id=bug.id, build_embedding=False)
    logger.info("Bug closed bug_id=%s by=%s", bug.id, current_user.email)
    _sync_existing_bug_to_devops(
        db,
        bug,
        current_user=current_user,
        changed_fields=["status", "closed_at"],
    )
    return _get_bug_or_404(db, bug.id)


@router.post("/{bug_id}/submit-to-devops", response_model=BugRead)
def submit_bug_to_devops(
    bug_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Bug:
    bug = _get_bug_or_404(db, bug_id)
    if not can_view_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    if bug.ado_work_item_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This bug has already been submitted to Azure DevOps as work item #{bug.ado_work_item_id}.",
        )
    if not azure_devops_enabled():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Azure DevOps is not configured.")

    access_token = get_entra_access_token_for_email(current_user.email)

    try:
        work_item = create_task_from_bug(bug, access_token)
    except AzureDevOpsError as exc:
        logger.warning("Submit to Azure DevOps failed bug_id=%s error=%s", bug.id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except requests.RequestException as exc:
        logger.warning("Azure DevOps request failed during submit bug_id=%s error=%s", bug.id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps request failed: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected Azure DevOps submit error bug_id=%s", bug.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected Azure DevOps integration error: {exc.__class__.__name__}: {exc}",
        ) from exc

    apply_ado_sync_to_bug(bug, work_item, sync_status="submitted")
    _write_history(
        db,
        bug.id,
        current_user.email,
        "submitted_to_devops",
        f"Submitted to Azure DevOps work item #{bug.ado_work_item_id}",
    )
    db.commit()
    db.refresh(bug)
    logger.info("Bug submitted to Azure DevOps bug_id=%s work_item_id=%s by=%s", bug.id, bug.ado_work_item_id, current_user.email)
    return _get_bug_or_404(db, bug.id)


@router.post("/{bug_id}/sync-to-devops", response_model=BugRead)
def sync_bug_to_devops(
    bug_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Bug:
    bug = _get_bug_or_404(db, bug_id)
    if not can_view_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    if not bug.ado_work_item_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This bug has not been submitted to Azure DevOps yet.",
        )
    if not azure_devops_enabled():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Azure DevOps is not configured.")

    access_token = get_entra_access_token_for_email(current_user.email)
    try:
        work_item = update_task_from_bug(
            bug,
            access_token,
            changed_fields=["title", "description", "severity", "status", "environment", "repro_steps", "tags", "notify_emails", "assignee_id", "reporting_date"],
        )
    except AzureDevOpsError as exc:
        logger.warning("Manual sync to Azure DevOps failed bug_id=%s error=%s", bug.id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except requests.RequestException as exc:
        logger.warning("Azure DevOps request failed during manual sync bug_id=%s error=%s", bug.id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps request failed: {exc}",
        ) from exc

    apply_ado_sync_to_bug(bug, work_item, sync_status="updated")
    _write_history(
        db,
        bug.id,
        current_user.email,
        "synced_to_devops",
        f"Synchronized Azure DevOps work item #{bug.ado_work_item_id}",
    )
    db.commit()
    db.refresh(bug)
    logger.info("Bug manually synced to Azure DevOps bug_id=%s work_item_id=%s by=%s", bug.id, bug.ado_work_item_id, current_user.email)
    return _get_bug_or_404(db, bug.id)


@router.post("/{bug_id}/remove-from-devops", response_model=BugRead)
def remove_bug_from_devops(
    bug_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Bug:
    bug = _get_bug_or_404(db, bug_id)
    if not can_view_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    if not bug.ado_work_item_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This bug is not currently linked to an Azure DevOps work item.",
        )
    if not azure_devops_enabled():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Azure DevOps is not configured.")

    work_item_id = bug.ado_work_item_id
    access_token = get_entra_access_token_for_email(current_user.email)
    try:
        delete_task_from_bug(bug, access_token)
        removed = verify_task_removed(bug, access_token)
    except AzureDevOpsError as exc:
        logger.warning("Remove from Azure DevOps failed bug_id=%s work_item_id=%s error=%s", bug.id, work_item_id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except requests.RequestException as exc:
        logger.warning("Azure DevOps request failed during removal bug_id=%s work_item_id=%s error=%s", bug.id, work_item_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps request failed: {exc}",
        ) from exc
    if not removed:
        logger.warning(
            "Azure DevOps removal could not be verified bug_id=%s work_item_id=%s",
            bug.id,
            work_item_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Arbeidselementet ser fortsatt ut til å eksistere i Azure DevOps etter sletting. "
                "Den lokale koblingen er derfor beholdt."
            ),
        )

    clear_ado_sync_from_bug(bug, sync_status="removed")
    _write_history(
        db,
        bug.id,
        current_user.email,
        "removed_from_devops",
        f"Removed Azure DevOps work item #{work_item_id}",
    )
    db.commit()
    db.refresh(bug)
    logger.info("Bug removed from Azure DevOps bug_id=%s former_work_item_id=%s by=%s", bug.id, work_item_id, current_user.email)
    return _get_bug_or_404(db, bug.id)


@router.post("/{bug_id}/attachments", response_model=BugRead)
async def upload_attachment(
    bug_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Bug:
    bug = _get_bug_or_404(db, bug_id)
    if not can_update_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    if bug.status == "closed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Closed bugs are read-only. Reopen the bug before adding attachments.",
        )

    safe_name = f"{uuid4()}_{Path(file.filename or 'upload').name}"
    target_path = settings.attachment_dir / safe_name
    with target_path.open("wb") as output:
        output.write(await file.read())

    db.add(
        Attachment(
            bug_id=bug_id,
            filename=file.filename or safe_name,
            content_type=file.content_type,
            storage_path=str(target_path),
            uploaded_by=current_user.email,
        )
    )
    _write_history(db, bug.id, current_user.email, "attachment_uploaded", f"Uploaded {file.filename}")
    db.commit()
    db.refresh(bug)
    _refresh_bug_search_index(db, bug_id=bug.id, build_embedding=False)
    logger.info("Attachment uploaded bug_id=%s filename=%s by=%s", bug.id, file.filename or safe_name, current_user.email)
    _sync_existing_bug_to_devops(
        db,
        bug,
        current_user=current_user,
        comment_text=f"Attachment uploaded in Bug Ticket System: {file.filename}",
    )
    return _get_bug_or_404(db, bug.id)


@router.post("/{bug_id}/comments", response_model=CommentRead, status_code=status.HTTP_201_CREATED)
def add_comment(
    bug_id: int,
    payload: CommentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BugComment:
    bug = _get_bug_or_404(db, bug_id)
    if not can_update_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    if bug.status == "closed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Closed bugs are read-only. Reopen the bug before adding conversation updates.",
        )

    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Comment cannot be empty")

    normalized_body = _normalize_comment_body(body)
    for existing_comment in bug.comments:
        if _normalize_comment_body(existing_comment.body) == normalized_body:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An identical conversation entry already exists for this bug",
            )

    comment = BugComment(
        bug_id=bug_id,
        author_email=current_user.email,
        author_role=current_user.role,
        body=body,
    )
    db.add(comment)
    _write_history(db, bug.id, current_user.email, "comment_added", "Added a conversation entry")
    db.commit()
    db.refresh(comment)
    db.refresh(bug)
    _refresh_bug_search_index(db, bug_id=bug.id, build_embedding=False)
    logger.info("Comment added bug_id=%s author=%s", bug.id, current_user.email)
    _sync_existing_bug_to_devops(
        db,
        bug,
        current_user=current_user,
        comment_text=body,
    )
    return comment


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    attachment = db.get(Attachment, attachment_id)
    if not attachment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")

    bug = _get_bug_or_404(db, attachment.bug_id)
    if not can_view_bug(current_user, bug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return FileResponse(path=attachment.storage_path, filename=attachment.filename)


@router.get("/stats/summary")
def bug_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")

    total = db.scalar(select(func.count(Bug.id))) or 0
    open_count = db.scalar(select(func.count(Bug.id)).where(Bug.status == "open")) or 0
    in_progress = db.scalar(select(func.count(Bug.id)).where(Bug.status == "in_progress")) or 0
    closed = db.scalar(select(func.count(Bug.id)).where(Bug.status == "closed")) or 0
    return {"total": total, "open": open_count, "in_progress": in_progress, "closed": closed}
