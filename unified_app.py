from __future__ import annotations

import os
import sys
import json
import html
import time
import base64
import re
import csv
import smtplib
import shutil
import sqlite3
import tempfile
import zipfile
from io import BytesIO, StringIO
from email.message import EmailMessage
from difflib import SequenceMatcher
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Mapping
from statistics import mean, median
from typing import Any, Callable
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from uuid import uuid4

CLOUD_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = CLOUD_ROOT.parent


def _is_truthy(value: str | None, *, default: bool = False) -> bool:
    normalized = str(value or "").strip().casefold()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on"}


def _prioritize_cloudtest_on_sys_path() -> None:
    ordered = [str(PROJECT_ROOT), str(CLOUD_ROOT)]
    for item in ordered:
        while item in sys.path:
            sys.path.remove(item)
    for item in ordered:
        sys.path.insert(0, item)


_prioritize_cloudtest_on_sys_path()

import streamlit as st
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm.exc import DetachedInstanceError
from streamlit.web.server.authlib_tornado_integration import TornadoIntegration
from ai_client import (
    extract_json_object as _extract_json_object_impl,
    request_assignee_solution as _request_assignee_solution,
    request_bug_sentiment as _request_bug_sentiment,
    request_bug_summary as _request_bug_summary,
    request_reporter_draft as _request_reporter_draft,
)
from auth_ui import render_auth_gate as _render_auth_gate
from error_utils import format_user_error
import job_runtime as _job_runtime
from storage_backend import AttachmentStorageError, build_attachment_storage


def _bootstrap_cloud_test_env_from_secrets() -> None:
    try:
        app_cfg = st.secrets.get("app", {})
    except Exception:
        app_cfg = {}

    mapping = {
        "DATABASE_URL": "database_url",
        "STORAGE_DIR": "storage_dir",
        "ATTACHMENT_STORAGE_BACKEND": "attachment_storage_backend",
        "CLOUD_TEST_ALLOW_SQLITE_FALLBACK": "cloud_test_allow_sqlite_fallback",
        "AI_PROVIDER": "ai_provider",
        "AI_MODEL": "ai_model",
        "EMBEDDING_PROVIDER": "embedding_provider",
        "EMBEDDING_MODEL": "embedding_model",
        "OPENAI_API_KEY": "openai_api_key",
        "OPENAI_MODEL": "openai_model",
        "OPENAI_EMBEDDING_MODEL": "openai_embedding_model",
        "CLOUD_TEST_ALLOW_LOCAL_LOGIN": "cloud_test_allow_local_login",
        "CLOUD_TEST_ENABLE_TEST_LOGIN": "cloud_test_enable_test_login",
        "CLOUD_TEST_LOCAL_TEST_EMAIL": "cloud_test_local_test_email",
        "CLOUD_TEST_LOCAL_TEST_PASSWORD": "cloud_test_local_test_password",
    }
    for env_key, app_key in mapping.items():
        if os.getenv(env_key):
            continue
        value = None
        try:
            value = st.secrets.get(env_key)
            if value is None:
                value = st.secrets.get(env_key.casefold())
        except Exception:
            value = None
        if value is None and isinstance(app_cfg, Mapping):
            value = app_cfg.get(app_key)
            if value is None:
                value = app_cfg.get(env_key)
            if value is None:
                value = app_cfg.get(env_key.casefold())
        if value is not None and str(value).strip():
            os.environ[env_key] = str(value).strip()

    def _host_from_database_url(database_url: str) -> str:
        try:
            return str(urlparse(database_url).hostname or "").strip().casefold()
        except Exception:
            return ""

    running_on_streamlit_cloud = _is_truthy(os.getenv("STREAMLIT_CLOUD"))
    allow_sqlite_fallback = _is_truthy(os.getenv("CLOUD_TEST_ALLOW_SQLITE_FALLBACK"))
    database_url = str(os.getenv("DATABASE_URL") or "").strip()
    if running_on_streamlit_cloud:
        sqlite_url = f"sqlite:///{(CLOUD_ROOT / 'bug_tracker_cloud.db').as_posix()}"
        if not database_url:
            os.environ["DATABASE_URL"] = sqlite_url
            os.environ.setdefault("CLOUD_TEST_ALLOW_SQLITE_FALLBACK", "true")
        elif database_url.casefold().startswith(("postgresql", "postgres")):
            host = _host_from_database_url(database_url)
            if host in {"localhost", "127.0.0.1", "::1"}:
                os.environ["DATABASE_URL"] = sqlite_url
                os.environ.setdefault("CLOUD_TEST_ALLOW_SQLITE_FALLBACK", "true")
        elif allow_sqlite_fallback and database_url.casefold().startswith("sqlite"):
            os.environ.setdefault("CLOUD_TEST_ALLOW_SQLITE_FALLBACK", "true")


_bootstrap_cloud_test_env_from_secrets()

from foundation import (
    apply_shared_app_style,
    apply_sidebar_bug_filters,
    build_bug_expander_title,
    cached_value,
    clear_cached_value,
    format_datetime_display,
    normalize_bug_status,
    render_sidebar_logo,
    render_sidebar_bug_filters,
    render_bug_list_controls,
    render_bug_status_summary,
    render_sidebar_search,
    status_label,
)
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.core.logging import get_logger
from app.core.security import get_password_hash, verify_password
from app.models.background_job import BackgroundJob
from app.models.bug import AppRuntimeMeta, Attachment, Bug, BugComment, BugHistory, BugSearchIndex
from app.models.notification import InAppNotification, NotificationOutboxEvent
from app.models.user import User
from app.services.devops import (
    DevOpsConfig,
    create_bug_work_item,
    fetch_bug_work_item,
    list_assignable_devops_users,
    remove_bug_work_item,
    test_connection as test_devops_connection,
    update_bug_work_item,
)
from app.services.health import get_ready_health
from app.services.search import (
    get_search_telemetry_snapshot,
    mark_bug_search_index_dirty_by_id,
    rebuild_bug_search_index,
    retrieve_similar_visible_bugs,
    search_visible_bugs,
)
from app.services.migrations import run_cloudtest_migrations
from app.services.schema_bootstrap import run_local_schema_upgrades
from runtime_ui import (
    CATEGORY_OPTIONS,
    MAX_AI_EXTRACTED_TEXT_CHARS,
    MAX_ATTACHMENTS_PER_UPLOAD,
    MAX_ATTACHMENT_BYTES,
    REPORTER_SATISFACTION_OPTIONS,
    SEVERITY_OPTIONS,
    STATUS_OPTIONS,
    allow_local_login as _allow_local_login,
    config_value as _config_value,
    current_search_settings as _current_search_settings,
    render_ai_and_embedding_sidebar_settings as _render_ai_and_embedding_sidebar_settings,
    render_system_and_ops_sidebar as _render_system_and_ops_sidebar,
    render_todo_sidebar as _render_todo_sidebar,
    selected_ai_model as _selected_ai_model,
)
from page_admin import render_admin_page as _render_admin_page_module
from page_assignee import render_assignee_page as _render_assignee_page_module
from page_reporter import render_reporter_page as _render_reporter_page_module

logger = get_logger("cloud_test.unified")
_ATTACHMENT_STORAGE = build_attachment_storage()
_SMTP_OAUTH2_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _patch_streamlit_oidc_none_session() -> None:
    """Work around Streamlit/Authlib bug where Tornado flow uses session=None."""
    if getattr(TornadoIntegration, "_cloud_test_none_session_patch", False):
        return
    fallback_state_store: dict[str, dict] = {}

    def _set_state_data(self, session, state, data):
        key = f"_state_{self.name}_{state}"
        now = time.time()
        if self.cache:
            self.cache.set(key, json.dumps({"data": data}), self.expires_in)
            if session is not None:
                session[key] = {"exp": now + self.expires_in}
            return
        if session is None:
            fallback_state_store[key] = {"data": data, "exp": now + self.expires_in}
            return
        session[key] = {"data": data, "exp": now + self.expires_in}

    def _get_state_data(self, session, state):
        key = f"_state_{self.name}_{state}"
        if session is None:
            if self.cache:
                cached_value = self._get_cache_data(key)
                if cached_value:
                    return cached_value.get("data")
            fallback_value = fallback_state_store.get(key)
            if fallback_value and fallback_value.get("exp", 0) > time.time():
                return fallback_value.get("data")
            return None

        session_data = session.get(key)
        if not session_data:
            return None
        if self.cache:
            cached_value = self._get_cache_data(key)
        else:
            cached_value = session_data
        if cached_value:
            return cached_value.get("data")
        return None

    def _clear_state_data(self, session, state):
        key = f"_state_{self.name}_{state}"
        if self.cache:
            self.cache.delete(key)
        fallback_state_store.pop(key, None)
        if session is None:
            return
        session.pop(key, None)
        self._clear_session_state(session)

    TornadoIntegration.set_state_data = _set_state_data
    TornadoIntegration.get_state_data = _get_state_data
    TornadoIntegration.clear_state_data = _clear_state_data
    TornadoIntegration._cloud_test_none_session_patch = True


_patch_streamlit_oidc_none_session()


def _reporter_update_text_key(bug_id: int) -> str:
    return f"reporter_comment_{bug_id}"


def _reporter_clear_update_text_key(bug_id: int) -> str:
    return f"reporter_clear_comment_{bug_id}"


def _queue_clear_reporter_update_text(bug_id: int) -> None:
    st.session_state[_reporter_clear_update_text_key(bug_id)] = True


def _apply_pending_reporter_update_text_clear(bug_id: int) -> None:
    clear_key = _reporter_clear_update_text_key(bug_id)
    if st.session_state.get(clear_key):
        st.session_state[_reporter_update_text_key(bug_id)] = ""
        st.session_state[clear_key] = False


def _reporter_desc_key(bug_id: int) -> str:
    return f"reporter_desc_{bug_id}"


def _reporter_desc_clear_request_key(bug_id: int) -> str:
    return f"reporter_desc_clear_{bug_id}"


def _queue_clear_reporter_desc(bug_id: int) -> None:
    st.session_state[_reporter_desc_clear_request_key(bug_id)] = True


def _apply_pending_reporter_desc_clear(bug_id: int) -> None:
    clear_key = _reporter_desc_clear_request_key(bug_id)
    if st.session_state.get(clear_key):
        st.session_state[_reporter_desc_key(bug_id)] = ""
        st.session_state[clear_key] = False


def _admin_desc_key(bug_id: int) -> str:
    return f"admin_desc_{bug_id}"


def _admin_desc_clear_request_key(bug_id: int) -> str:
    return f"admin_desc_clear_{bug_id}"


def _queue_clear_admin_desc(bug_id: int) -> None:
    st.session_state[_admin_desc_clear_request_key(bug_id)] = True


def _apply_pending_admin_desc_clear(bug_id: int) -> None:
    clear_key = _admin_desc_clear_request_key(bug_id)
    if st.session_state.get(clear_key):
        st.session_state[_admin_desc_key(bug_id)] = ""
        st.session_state[clear_key] = False


def _assignee_note_key(bug_id: int) -> str:
    return f"assignee_note_{bug_id}"


def _assignee_note_clear_request_key(bug_id: int) -> str:
    return f"assignee_note_clear_{bug_id}"


def _assignee_solution_state_key(bug_id: int, suffix: str) -> str:
    return f"assignee_solution_{suffix}_{bug_id}"


def _queue_clear_assignee_note(bug_id: int) -> None:
    st.session_state[_assignee_note_clear_request_key(bug_id)] = True


def _apply_pending_assignee_note_clear(bug_id: int) -> None:
    clear_key = _assignee_note_clear_request_key(bug_id)
    if st.session_state.get(clear_key):
        st.session_state[_assignee_note_key(bug_id)] = ""
        st.session_state[clear_key] = False


def _queue_apply_assignee_solution_to_note(bug_id: int) -> None:
    st.session_state[_assignee_solution_state_key(bug_id, "apply_pending")] = True


def _clear_assignee_solution_state(bug_id: int) -> None:
    st.session_state[_assignee_solution_state_key(bug_id, "text")] = ""
    st.session_state[_assignee_solution_state_key(bug_id, "source")] = ""
    st.session_state[_assignee_solution_state_key(bug_id, "error")] = ""
    st.session_state[_assignee_solution_state_key(bug_id, "apply_pending")] = False


def _apply_pending_assignee_solution_to_note(bug_id: int) -> None:
    pending_key = _assignee_solution_state_key(bug_id, "apply_pending")
    if not st.session_state.get(pending_key):
        return
    suggestion = str(st.session_state.get(_assignee_solution_state_key(bug_id, "text"), "") or "").strip()
    if not suggestion:
        st.session_state[pending_key] = False
        return
    note_key = _assignee_note_key(bug_id)
    existing = str(st.session_state.get(note_key, "") or "").rstrip()
    separator = "\n\n" if existing else ""
    st.session_state[note_key] = f"{existing}{separator}{suggestion}".strip()
    st.session_state[_assignee_solution_state_key(bug_id, "text")] = ""
    st.session_state[_assignee_solution_state_key(bug_id, "source")] = ""
    st.session_state[_assignee_solution_state_key(bug_id, "error")] = ""
    st.session_state[pending_key] = False


def _serialize_background_job(job: BackgroundJob) -> dict[str, Any]:
    return _job_runtime.serialize_background_job(job)


def _json_safe_payload(value: Any) -> dict[str, Any]:
    return _job_runtime.json_safe_payload(value)


def _start_background_job(
    *,
    prefix: str,
    bug_id: int,
    job_key: str,
    job_label: str,
    target: Callable[[], Any],
) -> int:
    return _job_runtime.start_background_job(
        prefix=prefix,
        bug_id=bug_id,
        job_key=job_key,
        job_label=job_label,
        target=target,
        normalize_email=_normalize_email,
        db_session=db_session,
        background_job_model=BackgroundJob,
        session_state=st.session_state,
        logger=logger,
    )


def _get_background_job(job_id: int) -> dict[str, Any] | None:
    return _job_runtime.get_background_job(
        job_id=int(job_id),
        db_session=db_session,
        background_job_model=BackgroundJob,
    )


def _finalize_background_job(job_id: int) -> None:
    _job_runtime.finalize_background_job(int(job_id))


def _get_tracked_job(prefix: str, bug_id: int, job_key: str) -> dict[str, Any] | None:
    return _job_runtime.get_tracked_job(
        prefix=prefix,
        bug_id=bug_id,
        job_key=job_key,
        session_state=st.session_state,
    )


def _clear_tracked_job(prefix: str, bug_id: int, job_key: str) -> None:
    _job_runtime.clear_tracked_job(
        prefix=prefix,
        bug_id=bug_id,
        job_key=job_key,
        session_state=st.session_state,
    )


def _wait_for_background_job_completion(job_id: int, *, timeout_seconds: float = 20.0, poll_seconds: float = 0.5) -> str:
    started = time.perf_counter()
    status = _job_runtime.wait_for_background_job_completion(
        job_id=int(job_id),
        get_background_job_fn=_get_background_job,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    _record_runtime_metric("ai_wait_ms", elapsed_ms)
    return status


def _background_jobs_snapshot() -> list[dict[str, Any]]:
    return _job_runtime.background_jobs_snapshot(
        db_session=db_session,
        background_job_model=BackgroundJob,
        limit=80,
    )


def _mask_database_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return "-"
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", value)


def _pgvector_forced_text_fallback() -> bool:
    return _is_truthy(os.getenv("BUGSEARCH_DISABLE_PGVECTOR", ""))


def _cloud_test_mode_enabled() -> bool:
    return _is_truthy(os.getenv("STREAMLIT_CLOUD_TEST_MODE", ""))


def _legacy_schema_bootstrap_enabled() -> bool:
    return _is_truthy(os.getenv("CLOUDTEST_ENABLE_LEGACY_SCHEMA_BOOTSTRAP", ""))


def _allow_migration_failure_fallback() -> bool:
    return _is_truthy(os.getenv("CLOUDTEST_ALLOW_MIGRATION_FALLBACK", "true"))


def _is_external_postgres_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").strip().casefold()
    return hostname not in {"", "localhost", "127.0.0.1"}


def _validate_cloud_database_profile() -> None:
    if not _cloud_test_mode_enabled():
        return
    if settings.database_is_sqlite:
        logger.info("CloudTest kjører med SQLite-backend.")
        return
    if not settings.database_is_postgresql:
        raise RuntimeError("Ugyldig DATABASE_URL for CloudTest. Bruk sqlite:///... eller postgresql+psycopg://...")

    database_url = str(settings.database_url or "").strip()
    if _is_external_postgres_url(database_url) and "sslmode=" not in database_url.casefold():
        logger.warning(
            "External PostgreSQL URL without sslmode detected in cloud mode. "
            "Consider appending '?sslmode=require'."
        )


def _ensure_postgresql_vector_extension() -> None:
    if not settings.database_is_postgresql:
        return
    try:
        with engine.connect() as connection:
            installed = connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector' LIMIT 1")
            ).scalar()
            if installed:
                os.environ.pop("BUGSEARCH_DISABLE_PGVECTOR", None)
                return
            try:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                connection.commit()
                os.environ.pop("BUGSEARCH_DISABLE_PGVECTOR", None)
            except SQLAlchemyError as exc:
                os.environ["BUGSEARCH_DISABLE_PGVECTOR"] = "true"
                logger.warning(
                    "pgvector extension missing/unavailable. Falling back to text embeddings. error=%s",
                    exc.__class__.__name__,
                )
    except SQLAlchemyError as exc:
        os.environ["BUGSEARCH_DISABLE_PGVECTOR"] = "true"
        logger.warning(
            "Unable to check pgvector extension. Falling back to text embeddings. error=%s",
            exc.__class__.__name__,
        )


def _vector_extension_status() -> str:
    if not settings.database_is_postgresql:
        return "not_applicable"
    if _pgvector_forced_text_fallback():
        return "fallback_text"
    try:
        with engine.connect() as connection:
            installed = connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector' LIMIT 1")
            ).scalar()
            return "enabled" if installed else "missing"
    except SQLAlchemyError:
        return "unknown"


def _admin_index_snapshot() -> dict[str, Any]:
    with db_session() as db:
        total_bugs = int(db.scalar(select(func.count(Bug.id)).where(Bug.deleted_at.is_(None))) or 0)
        deleted_bugs = int(db.scalar(select(func.count(Bug.id)).where(Bug.deleted_at.is_not(None))) or 0)
        indexed_rows = int(db.scalar(select(func.count(BugSearchIndex.bug_id))) or 0)
        embedded_rows = int(
            db.scalar(
                select(func.count(BugSearchIndex.bug_id)).where(BugSearchIndex.embedding.is_not(None))
            )
            or 0
        )
        missing_or_stale_rows = int(
            db.scalar(
                select(func.count(Bug.id))
                .outerjoin(BugSearchIndex, BugSearchIndex.bug_id == Bug.id)
                .where(
                    Bug.deleted_at.is_(None),
                    (BugSearchIndex.bug_id.is_(None))
                    | (BugSearchIndex.needs_reindex == 1)
                    | (BugSearchIndex.updated_at.is_(None))
                    | (BugSearchIndex.updated_at < Bug.updated_at)
                )
            )
            or 0
        )
        last_reindexed_at = db.scalar(select(func.max(BugSearchIndex.indexed_at)))
        try:
            alembic_revision = str(db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar() or "")
        except Exception:
            alembic_revision = ""
        try:
            reindex_meta_rows = db.execute(
                text(
                    "SELECT key, value FROM app_runtime_meta "
                    "WHERE key IN ('search.last_reindex_at', 'search.last_reindex_count')"
                )
            ).all()
            reindex_meta = {str(key): str(value) for key, value in reindex_meta_rows}
        except Exception:
            reindex_meta = {}

    jobs = _background_jobs_snapshot()
    recent_jobs = jobs[:10]
    recent_errors = [
        job
        for job in jobs
        if str(job.get("status") or "").strip().casefold() == "failed" or str(job.get("error") or "").strip()
    ][:5]
    running_count = sum(1 for job in jobs if str(job.get("status") or "") == "running")
    pending_count = sum(1 for job in jobs if str(job.get("status") or "") == "pending")
    failed_count = sum(1 for job in jobs if str(job.get("status") or "") == "failed")

    return {
        "database_backend": settings.database_backend,
        "database_url_masked": _mask_database_url(settings.database_url),
        "storage_backend": str(getattr(_ATTACHMENT_STORAGE, "backend_name", "unknown")),
        "vector_extension": _vector_extension_status(),
        "total_bugs": total_bugs,
        "deleted_bugs": deleted_bugs,
        "indexed_rows": indexed_rows,
        "embedded_rows": embedded_rows,
        "missing_or_stale_rows": missing_or_stale_rows,
        "last_reindexed_at": last_reindexed_at,
        "alembic_revision": alembic_revision,
        "last_reindex_meta_at": reindex_meta.get("search.last_reindex_at"),
        "last_reindex_meta_count": reindex_meta.get("search.last_reindex_count"),
        "recent_jobs": recent_jobs,
        "recent_errors": recent_errors,
        "running_count": running_count,
        "pending_count": pending_count,
        "failed_count": failed_count,
    }


def _run_rebuild_index_job(*, embedding_provider: str, embedding_model: str) -> dict[str, Any]:
    try:
        with db_session() as db:
            processed = rebuild_bug_search_index(
                db,
                embedding_provider=embedding_provider,
                embedding_model=embedding_model,
                build_embedding=True,
                dirty_only=True,
            )
            _commit_with_retry(db, operation="Rebuild index")
        return {
            "processed": int(processed),
            "mode": "dirty_only",
            "embedding_provider": embedding_provider,
            "embedding_model": embedding_model,
        }
    except Exception as exc:
        logger.exception(
            "Rebuild index job failed: provider=%s model=%s",
            embedding_provider,
            embedding_model,
        )
        return {
            "error": format_user_error(
                "Rebuild index feilet",
                exc,
                fallback="Sjekk driftslogger for detaljer.",
            )
        }


def _save_devops_bulk_sync_summary(summary: dict[str, Any]) -> None:
    payload = json.dumps(summary, ensure_ascii=False)
    with db_session() as db:
        _runtime_meta_set(db, _DEVOPS_BULK_SYNC_LAST_SUMMARY_KEY, payload)
        _commit_with_retry(db, operation="Lagring av DevOps bulk-sync sammendrag")


def _load_devops_bulk_sync_summary() -> dict[str, Any] | None:
    try:
        with db_session() as db:
            raw = _runtime_meta_get(db, _DEVOPS_BULK_SYNC_LAST_SUMMARY_KEY, "")
    except Exception:
        return None
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _save_devops_bulk_push_summary(summary: dict[str, Any]) -> None:
    payload = json.dumps(summary, ensure_ascii=False)
    with db_session() as db:
        _runtime_meta_set(db, _DEVOPS_BULK_PUSH_LAST_SUMMARY_KEY, payload)
        _commit_with_retry(db, operation="Lagring av DevOps bulk-push sammendrag")


def _load_devops_bulk_push_summary() -> dict[str, Any] | None:
    try:
        with db_session() as db:
            raw = _runtime_meta_get(db, _DEVOPS_BULK_PUSH_LAST_SUMMARY_KEY, "")
    except Exception:
        return None
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _run_devops_bulk_pull_job(*, actor_email: str, config: DevOpsConfig) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "finished_at": "",
        "linked": 0,
        "updated": 0,
        "unchanged": 0,
        "failed": 0,
        "skipped": 0,
        "failed_samples": [],
    }
    max_failed_samples = 8

    try:
        with db_session() as db:
            linked_bug_ids = [
                int(item)
                for item in db.scalars(
                    select(Bug.id)
                    .where(Bug.deleted_at.is_(None))
                    .where(Bug.ado_work_item_id.is_not(None))
                    .order_by(Bug.id.asc())
                ).all()
            ]
        summary["linked"] = len(linked_bug_ids)

        for bug_id in linked_bug_ids:
            try:
                with db_session() as db:
                    bug = db.get(Bug, int(bug_id))
                    if bug is None or _is_deleted_bug(bug) or not bug.ado_work_item_id:
                        summary["skipped"] = int(summary.get("skipped", 0)) + 1
                        continue

                    payload = fetch_bug_work_item(config, work_item_id=int(bug.ado_work_item_id))
                    remote_values = _extract_devops_remote_values(payload.get("fields"))
                    if not str(remote_values.get("title") or "").strip():
                        remote_values["title"] = str(bug.title or "").strip()
                    if not str(remote_values.get("description") or "").strip():
                        remote_values["description"] = str(bug.description or "").strip()

                    status_value = normalize_bug_status(remote_values.get("status"))
                    severity_value = str(remote_values.get("severity") or "medium").strip().casefold()
                    assignee_value = _normalize_email(remote_values.get("assignee_id"))
                    tags_value = _devops_normalize_tags(remote_values.get("tags"))
                    if status_value not in set(STATUS_OPTIONS):
                        status_value = "open"
                    if severity_value not in set(SEVERITY_OPTIONS):
                        severity_value = "medium"

                    changed_fields: list[str] = []
                    title_value = str(remote_values.get("title") or "").strip()
                    description_value = str(remote_values.get("description") or "").strip()
                    if title_value and str(bug.title or "").strip() != title_value:
                        bug.title = title_value
                        changed_fields.append("title")
                    if description_value and str(bug.description or "").strip() != description_value:
                        bug.description = description_value
                        changed_fields.append("description")

                    if normalize_bug_status(bug.status) != status_value:
                        bug.status = status_value
                        changed_fields.append("status")
                    if str(bug.severity or "").strip().casefold() != severity_value:
                        bug.severity = severity_value
                        changed_fields.append("severity")

                    current_assignee = _normalize_email(bug.assignee_id)
                    if current_assignee != assignee_value:
                        if assignee_value:
                            _ensure_user_exists(db, email=assignee_value, role=_role_for_email(assignee_value))
                        bug.assignee_id = assignee_value or None
                        changed_fields.append("assignee_id")

                    current_tags = _devops_normalize_tags(bug.tags)
                    if current_tags != tags_value:
                        bug.tags = tags_value or None
                        changed_fields.append("tags")

                    if status_value == "resolved":
                        if bug.closed_at is None:
                            bug.closed_at = datetime.now(timezone.utc)
                    else:
                        bug.closed_at = None

                    remote_url = str(payload.get("url") or "").strip()
                    if remote_url and str(bug.ado_work_item_url or "").strip() != remote_url:
                        bug.ado_work_item_url = remote_url
                        changed_fields.append("ado_work_item_url")
                    bug.ado_sync_status = "synced"
                    bug.ado_synced_at = datetime.now(timezone.utc)

                    if changed_fields:
                        _write_history(
                            db,
                            bug_id=bug.id,
                            actor_email=actor_email,
                            action="devops_pulled",
                            details=(
                                f"Bulk-sync fra DevOps work item #{int(bug.ado_work_item_id)}. "
                                f"Felter: {', '.join(sorted(set(changed_fields)))}."
                            ),
                        )
                        try:
                            db.flush()
                            mark_bug_search_index_dirty_by_id(
                                db,
                                bug_id=bug.id,
                                embedding_provider=None,
                                embedding_model=None,
                            )
                        except Exception as mark_exc:
                            logger.warning(
                                "Bulk-sync could not mark search index dirty for bug_id=%s: %s",
                                bug.id,
                                mark_exc.__class__.__name__,
                            )
                        summary["updated"] = int(summary.get("updated", 0)) + 1
                    else:
                        summary["unchanged"] = int(summary.get("unchanged", 0)) + 1

                    _commit_with_retry(db, operation="Bulk sync fra DevOps")
            except Exception as exc:
                summary["failed"] = int(summary.get("failed", 0)) + 1
                if len(summary["failed_samples"]) < max_failed_samples:
                    summary["failed_samples"].append(
                        {
                            "bug_id": int(bug_id),
                            "error": format_user_error(
                                "Bulk-sync av bug feilet",
                                exc,
                                fallback="Ukjent feil mot DevOps.",
                            ),
                        }
                    )

        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["duration_ms"] = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        try:
            _save_devops_bulk_sync_summary(summary)
        except Exception as save_exc:
            logger.warning("Could not persist DevOps bulk-sync summary: %s", save_exc.__class__.__name__)
        return summary
    except Exception as exc:
        logger.exception("DevOps bulk-sync job failed before completion")
        error_payload = {
            "error": format_user_error(
                "Bulk-sync fra DevOps feilet",
                exc,
                fallback="Sjekk driftslogger for detaljer.",
            ),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _save_devops_bulk_sync_summary(error_payload)
        except Exception:
            pass
        return error_payload


def _run_devops_bulk_push_job(*, actor_email: str, config: DevOpsConfig) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "finished_at": "",
        "linked": 0,
        "updated": 0,
        "failed": 0,
        "skipped": 0,
        "failed_samples": [],
    }
    max_failed_samples = 8

    try:
        with db_session() as db:
            linked_bug_ids = [
                int(item)
                for item in db.scalars(
                    select(Bug.id)
                    .where(Bug.deleted_at.is_(None))
                    .where(Bug.ado_work_item_id.is_not(None))
                    .order_by(Bug.id.asc())
                ).all()
            ]
        summary["linked"] = len(linked_bug_ids)

        for bug_id in linked_bug_ids:
            try:
                with db_session() as db:
                    bug = db.get(Bug, int(bug_id))
                    if bug is None or _is_deleted_bug(bug) or not bug.ado_work_item_id:
                        summary["skipped"] = int(summary.get("skipped", 0)) + 1
                        continue

                    status_value = normalize_bug_status(bug.status)
                    severity_value = str(bug.severity or "medium").strip().casefold()
                    if status_value not in set(STATUS_OPTIONS):
                        status_value = "open"
                    if severity_value not in set(SEVERITY_OPTIONS):
                        severity_value = "medium"
                    assignee_value = _normalize_email(bug.assignee_id)

                    work_item_id, work_item_url = update_bug_work_item(
                        config,
                        work_item_id=int(bug.ado_work_item_id),
                        title=str(bug.title or "").strip() or f"Bug #{bug.id}",
                        description=str(bug.description or "").strip(),
                        severity=severity_value,
                        status=status_value,
                        tags=str(bug.tags or "").strip() or None,
                        assignee_email=assignee_value,
                        changed_fields=[
                            "title",
                            "description",
                            "status",
                            "severity",
                            "tags",
                            "assignee_id",
                        ],
                        comment_text=None,
                    )

                    bug.ado_work_item_url = str(work_item_url).strip() or bug.ado_work_item_url
                    bug.ado_sync_status = "synced"
                    bug.ado_synced_at = datetime.now(timezone.utc)
                    _write_history(
                        db,
                        bug_id=bug.id,
                        actor_email=actor_email,
                        action="devops_updated",
                        details=f"Bulk-push oppdaterte Azure DevOps work item #{int(work_item_id)}.",
                    )
                    _commit_with_retry(db, operation="Bulk push til DevOps")
                    summary["updated"] = int(summary.get("updated", 0)) + 1
            except Exception as exc:
                summary["failed"] = int(summary.get("failed", 0)) + 1
                if len(summary["failed_samples"]) < max_failed_samples:
                    summary["failed_samples"].append(
                        {
                            "bug_id": int(bug_id),
                            "error": format_user_error(
                                "Bulk-push av bug feilet",
                                exc,
                                fallback="Ukjent feil mot DevOps.",
                            ),
                        }
                    )

        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["duration_ms"] = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        try:
            _save_devops_bulk_push_summary(summary)
        except Exception as save_exc:
            logger.warning("Could not persist DevOps bulk-push summary: %s", save_exc.__class__.__name__)
        return summary
    except Exception as exc:
        logger.exception("DevOps bulk-push job failed before completion")
        error_payload = {
            "error": format_user_error(
                "Bulk-push til DevOps feilet",
                exc,
                fallback="Sjekk driftslogger for detaljer.",
            ),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _save_devops_bulk_push_summary(error_payload)
        except Exception:
            pass
        return error_payload


def _get_admin_health_snapshot(*, force_refresh: bool = False) -> dict[str, Any]:
    state_key = "admin_health_snapshot"
    if force_refresh or state_key not in st.session_state:
        try:
            payload = get_ready_health()
        except Exception as exc:
            logger.warning("Health refresh failed: %s", exc)
            payload = {
                "status": "error",
                "checks": {},
                "detail": format_user_error(
                    "Health refresh feilet",
                    exc,
                    fallback="Prøv igjen om litt.",
                ),
            }
        st.session_state[state_key] = {
            "captured_at": datetime.now(timezone.utc),
            "payload": payload,
        }
    snapshot = st.session_state.get(state_key)
    if not isinstance(snapshot, dict):
        return {"captured_at": None, "payload": {"status": "unknown", "checks": {}}}
    return snapshot


def _render_admin_operations_panel(user: dict[str, str]) -> None:
    st.caption("Drift")
    with st.expander("Indeks og drift", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        rebuild_clicked = c1.button(
            "Rebuild index",
            key="admin_rebuild_index",
            use_container_width=True,
            help="Rebygger søkeindeks for manglende/utdaterte bugs i bakgrunnen.",
        )
        refresh_health_clicked = c2.button(
            "Health refresh",
            key="admin_health_refresh",
            use_container_width=True,
            help="Henter ny health-status fra runtime-sjekkene.",
        )
        devops_bulk_sync_clicked = c3.button(
            "Synk alle DevOps-bugs",
            key="admin_devops_bulk_sync",
            use_container_width=True,
            help="Henter siste data fra DevOps for alle koblede bugs og oppdaterer lokal database.",
        )
        devops_bulk_push_clicked = c4.button(
            "Publiser alle til DevOps",
            key="admin_devops_bulk_push",
            use_container_width=True,
            help="Pusher alle koblede bugs fra lokal database til DevOps work items.",
        )

        if rebuild_clicked:
            search_settings = _current_search_settings()
            _start_background_job(
                prefix="admin",
                bug_id=0,
                job_key="rebuild_index",
                job_label="Rebuild index",
                target=lambda: _run_rebuild_index_job(
                    embedding_provider=search_settings["embedding_provider"],
                    embedding_model=search_settings["embedding_model"],
                ),
            )
            st.rerun()

        if devops_bulk_sync_clicked:
            config, config_error = _build_effective_devops_config(user, require_access=True)
            if config is None:
                st.error(str(config_error or "DevOps er ikke tilgjengelig."))
            else:
                _start_background_job(
                    prefix="admin",
                    bug_id=0,
                    job_key="devops_bulk_pull",
                    job_label="DevOps bulk sync",
                    target=lambda cfg=config, actor=user["email"]: _run_devops_bulk_pull_job(
                        actor_email=str(actor),
                        config=cfg,
                    ),
                )
                st.rerun()

        if devops_bulk_push_clicked:
            config, config_error = _build_effective_devops_config(user, require_access=True)
            if config is None:
                st.error(str(config_error or "DevOps er ikke tilgjengelig."))
            else:
                _start_background_job(
                    prefix="admin",
                    bug_id=0,
                    job_key="devops_bulk_push",
                    job_label="DevOps bulk push",
                    target=lambda cfg=config, actor=user["email"]: _run_devops_bulk_push_job(
                        actor_email=str(actor),
                        config=cfg,
                    ),
                )
                st.rerun()

        tracked = _get_tracked_job("admin", 0, "rebuild_index")
        if tracked:
            tracked_job_id = int(tracked.get("job_id", 0) or 0)
            job_payload = _get_background_job(tracked_job_id)
            if job_payload is None:
                _clear_tracked_job("admin", 0, "rebuild_index")
            else:
                status = str(job_payload.get("status") or "unknown")
                if status in {"pending", "running"}:
                    st.info("Rebuild index kjører i bakgrunnen.")
                    if st.button("Oppdater jobbstatus", key="admin_rebuild_refresh_status", use_container_width=True):
                        st.rerun()
                else:
                    result = job_payload.get("result")
                    error_text = ""
                    if isinstance(result, dict):
                        error_text = str(result.get("error") or "").strip()
                    if not error_text:
                        error_text = str(job_payload.get("error") or "").strip()
                    if error_text:
                        st.error(error_text)
                    else:
                        processed = result.get("processed") if isinstance(result, dict) else None
                        if processed is None:
                            st.success("Rebuild index fullført.")
                        else:
                            st.success(f"Rebuild index fullført. Oppdaterte {processed} bugs.")
                    _clear_tracked_job("admin", 0, "rebuild_index")
                    _finalize_background_job(tracked_job_id)
                    _clear_bug_cache()

        tracked_bulk = _get_tracked_job("admin", 0, "devops_bulk_pull")
        if tracked_bulk:
            tracked_job_id = int(tracked_bulk.get("job_id", 0) or 0)
            job_payload = _get_background_job(tracked_job_id)
            if job_payload is None:
                _clear_tracked_job("admin", 0, "devops_bulk_pull")
            else:
                status = str(job_payload.get("status") or "unknown")
                if status in {"pending", "running"}:
                    st.info("DevOps bulk-sync kjører i bakgrunnen.")
                    if st.button(
                        "Oppdater DevOps jobbstatus",
                        key="admin_devops_bulk_refresh_status",
                        use_container_width=True,
                    ):
                        st.rerun()
                else:
                    result = job_payload.get("result")
                    error_text = ""
                    if isinstance(result, dict):
                        error_text = str(result.get("error") or "").strip()
                    if not error_text:
                        error_text = str(job_payload.get("error") or "").strip()
                    if error_text:
                        st.error(error_text)
                    else:
                        updated = int((result or {}).get("updated") or 0) if isinstance(result, dict) else 0
                        unchanged = int((result or {}).get("unchanged") or 0) if isinstance(result, dict) else 0
                        failed = int((result or {}).get("failed") or 0) if isinstance(result, dict) else 0
                        linked = int((result or {}).get("linked") or 0) if isinstance(result, dict) else 0
                        skipped = int((result or {}).get("skipped") or 0) if isinstance(result, dict) else 0
                        st.success(
                            "DevOps bulk-sync fullført. "
                            f"Koblede: {linked}, oppdatert: {updated}, uendret: {unchanged}, "
                            f"feilet: {failed}, hoppet over: {skipped}."
                        )
                        failed_samples = (result or {}).get("failed_samples") if isinstance(result, dict) else []
                        if isinstance(failed_samples, list) and failed_samples:
                            st.warning("Noen bugs feilet under bulk-sync:")
                            for item in failed_samples[:8]:
                                if not isinstance(item, dict):
                                    continue
                                bug_id = str(item.get("bug_id") or "-").strip()
                                error_detail = str(item.get("error") or "Ukjent feil").strip()
                                st.caption(f"#{bug_id}: {error_detail}")
                    _clear_tracked_job("admin", 0, "devops_bulk_pull")
                    _finalize_background_job(tracked_job_id)
                    _clear_bug_cache()

        tracked_bulk_push = _get_tracked_job("admin", 0, "devops_bulk_push")
        if tracked_bulk_push:
            tracked_job_id = int(tracked_bulk_push.get("job_id", 0) or 0)
            job_payload = _get_background_job(tracked_job_id)
            if job_payload is None:
                _clear_tracked_job("admin", 0, "devops_bulk_push")
            else:
                status = str(job_payload.get("status") or "unknown")
                if status in {"pending", "running"}:
                    st.info("DevOps bulk-push kjører i bakgrunnen.")
                    if st.button(
                        "Oppdater DevOps push-status",
                        key="admin_devops_bulk_push_refresh_status",
                        use_container_width=True,
                    ):
                        st.rerun()
                else:
                    result = job_payload.get("result")
                    error_text = ""
                    if isinstance(result, dict):
                        error_text = str(result.get("error") or "").strip()
                    if not error_text:
                        error_text = str(job_payload.get("error") or "").strip()
                    if error_text:
                        st.error(error_text)
                    else:
                        updated = int((result or {}).get("updated") or 0) if isinstance(result, dict) else 0
                        failed = int((result or {}).get("failed") or 0) if isinstance(result, dict) else 0
                        linked = int((result or {}).get("linked") or 0) if isinstance(result, dict) else 0
                        skipped = int((result or {}).get("skipped") or 0) if isinstance(result, dict) else 0
                        st.success(
                            "DevOps bulk-push fullført. "
                            f"Koblede: {linked}, oppdatert: {updated}, feilet: {failed}, hoppet over: {skipped}."
                        )
                        failed_samples = (result or {}).get("failed_samples") if isinstance(result, dict) else []
                        if isinstance(failed_samples, list) and failed_samples:
                            st.warning("Noen bugs feilet under bulk-push:")
                            for item in failed_samples[:8]:
                                if not isinstance(item, dict):
                                    continue
                                bug_id = str(item.get("bug_id") or "-").strip()
                                error_detail = str(item.get("error") or "Ukjent feil").strip()
                                st.caption(f"#{bug_id}: {error_detail}")
                    _clear_tracked_job("admin", 0, "devops_bulk_push")
                    _finalize_background_job(tracked_job_id)
                    _clear_bug_cache()

        last_bulk_summary = _load_devops_bulk_sync_summary()
        if isinstance(last_bulk_summary, dict) and last_bulk_summary:
            if str(last_bulk_summary.get("error") or "").strip():
                st.caption(
                    "Siste DevOps bulk-sync feilet: "
                    f"{str(last_bulk_summary.get('error') or '').strip()}"
                )
            else:
                st.caption(
                    "Siste DevOps bulk-sync: "
                    f"koblede={int(last_bulk_summary.get('linked') or 0)}, "
                    f"oppdatert={int(last_bulk_summary.get('updated') or 0)}, "
                    f"uendret={int(last_bulk_summary.get('unchanged') or 0)}, "
                    f"feilet={int(last_bulk_summary.get('failed') or 0)}"
                )
            finished = str(last_bulk_summary.get("finished_at") or "").strip()
            if finished:
                st.caption(f"Sist kjørt: {finished}")

        last_bulk_push_summary = _load_devops_bulk_push_summary()
        if isinstance(last_bulk_push_summary, dict) and last_bulk_push_summary:
            if str(last_bulk_push_summary.get("error") or "").strip():
                st.caption(
                    "Siste DevOps bulk-push feilet: "
                    f"{str(last_bulk_push_summary.get('error') or '').strip()}"
                )
            else:
                st.caption(
                    "Siste DevOps bulk-push: "
                    f"koblede={int(last_bulk_push_summary.get('linked') or 0)}, "
                    f"oppdatert={int(last_bulk_push_summary.get('updated') or 0)}, "
                    f"feilet={int(last_bulk_push_summary.get('failed') or 0)}"
                )
            finished = str(last_bulk_push_summary.get("finished_at") or "").strip()
            if finished:
                st.caption(f"Sist publisert: {finished}")

        health_snapshot = _get_admin_health_snapshot(force_refresh=refresh_health_clicked)
        health_payload = health_snapshot.get("payload") if isinstance(health_snapshot, dict) else {}
        health_status = str((health_payload or {}).get("status") or "unknown")
        captured_at = health_snapshot.get("captured_at") if isinstance(health_snapshot, dict) else None
        st.caption(
            f"Health-status: {health_status.upper()} | Sist oppdatert: {format_datetime_display(captured_at)}"
        )
        checks = (health_payload or {}).get("checks") if isinstance(health_payload, dict) else {}
        if isinstance(checks, dict):
            db_check = checks.get("database", {}) if isinstance(checks.get("database", {}), dict) else {}
            search_check = checks.get("search", {}) if isinstance(checks.get("search", {}), dict) else {}
            st.caption(
                f"DB: {str(db_check.get('status') or 'unknown')} | "
                f"Søk: {str(search_check.get('status') or 'unknown')}"
            )

        snapshot = _admin_index_snapshot()
        perf = _runtime_performance_snapshot()
        st.caption(
            f"Database: {snapshot['database_backend']} ({snapshot['database_url_masked']}) | "
            f"Lagring: {snapshot.get('storage_backend', 'unknown')}"
        )
        st.caption(f"Alembic revisjon: {snapshot.get('alembic_revision') or '-'}")
        vector_status = str(snapshot.get("vector_extension") or "unknown")
        vector_label = {
            "enabled": "pgvector: aktiv",
            "fallback_text": "pgvector: fallback (tekstlagring)",
            "missing": "pgvector: mangler",
            "unknown": "pgvector: ukjent",
            "not_applicable": "pgvector: ikke relevant",
        }.get(vector_status, f"pgvector: {vector_status}")
        st.caption(vector_label)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Bugs", int(snapshot.get("total_bugs") or 0))
        m2.metric("Indekserte", int(snapshot.get("indexed_rows") or 0))
        m3.metric("Med embedding", int(snapshot.get("embedded_rows") or 0))
        m4.metric("Mangl./utdatert", int(snapshot.get("missing_or_stale_rows") or 0))
        st.caption(f"Papirkurv: {int(snapshot.get('deleted_bugs') or 0)}")
        st.caption(
            f"Sist reindeksert: {format_datetime_display(snapshot.get('last_reindexed_at'))}"
        )
        last_meta_at = str(snapshot.get("last_reindex_meta_at") or "").strip()
        last_meta_count = str(snapshot.get("last_reindex_meta_count") or "").strip()
        if last_meta_at or last_meta_count:
            st.caption(
                f"Reindex-meta: at={last_meta_at or '-'} | count={last_meta_count or '-'}"
            )

        q1, q2, q3 = st.columns(3)
        q1.metric("Jobber kjører", int(snapshot.get("running_count") or 0))
        q2.metric("Jobber venter", int(snapshot.get("pending_count") or 0))
        q3.metric("Jobber feilet", int(snapshot.get("failed_count") or 0))

        st.caption("Ytelse")
        p1, p2, p3 = st.columns(3)
        p1.metric("Søk snitt (ms)", round(float(perf.get("search_avg_ms") or 0.0), 1))
        p2.metric("AI venting snitt (ms)", round(float(perf.get("ai_wait_avg_ms") or 0.0), 1))
        p3.metric("Admin visning (ms)", round(float(perf.get("page_admin_ms") or 0.0), 1))
        st.caption(
            "Siste side-render (ms): "
            f"Reporter={round(float(perf.get('page_reporter_ms') or 0.0), 1)} | "
            f"Assignee={round(float(perf.get('page_assignee_ms') or 0.0), 1)} | "
            f"Admin={round(float(perf.get('page_admin_ms') or 0.0), 1)}"
        )
        st.caption(
            "Siste søk (ms): "
            f"Reporter={round(float(perf.get('search_reporter_ms') or 0.0), 1)} | "
            f"Assignee={round(float(perf.get('search_assignee_ms') or 0.0), 1)} | "
            f"Admin={round(float(perf.get('search_admin_ms') or 0.0), 1)}"
        )

        st.divider()
        st.caption("SLA-regler (timer fra opprettet til forventet løsning)")
        sla_rules = _load_sla_hours()
        s1, s2, s3, s4 = st.columns(4)
        with s1:
            sla_critical = st.number_input(
                "Critical",
                min_value=1,
                max_value=24 * 365,
                value=int(sla_rules.get("critical", _SLA_DEFAULT_HOURS["critical"])),
                step=1,
                key="admin_sla_critical",
            )
        with s2:
            sla_high = st.number_input(
                "High",
                min_value=1,
                max_value=24 * 365,
                value=int(sla_rules.get("high", _SLA_DEFAULT_HOURS["high"])),
                step=1,
                key="admin_sla_high",
            )
        with s3:
            sla_medium = st.number_input(
                "Medium",
                min_value=1,
                max_value=24 * 365,
                value=int(sla_rules.get("medium", _SLA_DEFAULT_HOURS["medium"])),
                step=1,
                key="admin_sla_medium",
            )
        with s4:
            sla_low = st.number_input(
                "Low",
                min_value=1,
                max_value=24 * 365,
                value=int(sla_rules.get("low", _SLA_DEFAULT_HOURS["low"])),
                step=1,
                key="admin_sla_low",
            )
        if st.button("Lagre SLA-regler", key="admin_sla_save", use_container_width=True):
            sla_error = _save_sla_hours(
                {
                    "critical": int(sla_critical),
                    "high": int(sla_high),
                    "medium": int(sla_medium),
                    "low": int(sla_low),
                }
            )
            if sla_error:
                st.error(sla_error)
            else:
                st.success("SLA-regler lagret.")
                st.rerun()

        st.divider()
        st.caption("Backup / restore")
        b1, b2 = st.columns(2)
        if b1.button("Lag backup (.zip)", key="admin_backup_build", use_container_width=True):
            backup_bytes, backup_error = _build_backup_zip_bytes()
            if backup_error:
                st.error(backup_error)
            elif backup_bytes:
                now_text = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                st.session_state["admin_backup_bytes"] = backup_bytes
                st.session_state["admin_backup_name"] = f"cloudtest_backup_{now_text}.zip"
                st.success("Backup klar for nedlasting.")
        backup_payload = st.session_state.get("admin_backup_bytes")
        backup_name = str(st.session_state.get("admin_backup_name") or "cloudtest_backup.zip")
        if isinstance(backup_payload, (bytes, bytearray)) and backup_payload:
            b2.download_button(
                "Last ned backup",
                data=bytes(backup_payload),
                file_name=backup_name,
                mime="application/zip",
                key="admin_backup_download",
                use_container_width=True,
            )

        restore_file = st.file_uploader(
            "Restore fra backup (.zip)",
            type=["zip"],
            key="admin_restore_upload",
            help="Gjenoppretter database og vedlegg fra valgt backupfil.",
        )
        confirm_restore = st.checkbox(
            "Jeg forstår at restore overskriver dagens data.",
            key="admin_restore_confirm",
            value=False,
        )
        if st.button("Kjør restore", key="admin_restore_run", use_container_width=True):
            if not confirm_restore:
                st.warning("Bekreft restore før du fortsetter.")
            else:
                restore_error = _restore_from_backup_zip(restore_file)
                if restore_error:
                    st.error(restore_error)
                else:
                    st.success("Restore fullført. Oppdaterer visning ...")
                    _clear_bug_cache()
                    st.rerun()

        st.divider()
        st.caption("Papirkurv")
        can_permanent_delete = _policy_allows(
            policy_key="policy.hard_delete_roles",
            user_role=user.get("role", ""),
            default_roles={"admin"},
        )
        deleted_bugs = _load_deleted_bugs_for_admin(limit=120)
        if not deleted_bugs:
            st.caption("Papirkurven er tom.")
        else:
            st.caption(f"{len(deleted_bugs)} bug(s) i papirkurv")
            for bug in deleted_bugs[:40]:
                deleted_at_text = format_datetime_display(getattr(bug, "deleted_at", None))
                deleted_by = str(getattr(bug, "deleted_by", "") or "-")
                st.write(f"#{bug.id} - {bug.title} | slettet: {deleted_at_text} av {deleted_by}")
                r_col, d_col = st.columns(2)
                with r_col:
                    if st.button(
                        "Gjenopprett",
                        key=f"admin_restore_deleted_{bug.id}",
                        use_container_width=True,
                    ):
                        restore_error = _restore_deleted_bug(user, int(bug.id))
                        if restore_error:
                            st.error(restore_error)
                        else:
                            st.success(f"Bug #{bug.id} gjenopprettet.")
                            st.rerun()
                with d_col:
                    if st.button(
                        "Slett permanent",
                        key=f"admin_hard_delete_{bug.id}",
                        use_container_width=True,
                        disabled=not can_permanent_delete,
                    ):
                        _request_delete_confirmation(prefix="admin", item_key=f"hard_delete_{bug.id}")
                        st.rerun()
                    if _render_delete_confirmation(
                        prefix="admin",
                        item_key=f"hard_delete_{bug.id}",
                        message=f"Permanent sletting av bug #{bug.id}. Dette kan ikke angres.",
                    ):
                        hard_error = _hard_delete_bug(user, int(bug.id))
                        if hard_error:
                            st.error(hard_error)
                        else:
                            st.success(f"Bug #{bug.id} slettet permanent.")
                            st.rerun()

        st.caption("Jobbkø (siste)")
        recent_jobs = snapshot.get("recent_jobs") if isinstance(snapshot, dict) else []
        if isinstance(recent_jobs, list) and recent_jobs:
            for job in recent_jobs[:8]:
                label = str(job.get("label") or job.get("job_key") or "job")
                status = str(job.get("status") or "unknown")
                bug_id = job.get("bug_id")
                line = f"{label} [{status}]"
                if bug_id:
                    line += f" • bug #{bug_id}"
                st.write(line)
        else:
            st.caption("Ingen jobber registrert.")

        st.caption("Siste feil")
        recent_errors = snapshot.get("recent_errors") if isinstance(snapshot, dict) else []
        if isinstance(recent_errors, list) and recent_errors:
            for idx, item in enumerate(recent_errors[:5]):
                label = str(item.get("label") or item.get("job_key") or f"jobb {idx + 1}")
                error = str(item.get("error") or "").strip() or "Ukjent feil."
                st.warning(f"{label}: {error}")
        else:
            st.caption("Ingen feil i siste jobber.")

def _request_delete_confirmation(prefix: str, item_key: str) -> None:
    st.session_state[f"{prefix}_confirm_delete_{item_key}"] = True


def _render_delete_confirmation(prefix: str, item_key: str, message: str) -> bool:
    flag_key = f"{prefix}_confirm_delete_{item_key}"
    if not st.session_state.get(flag_key):
        return False
    st.warning(message)
    confirm_col, cancel_col = st.columns(2)
    with confirm_col:
        confirm = st.button("Bekreft sletting", key=f"{flag_key}_confirm", use_container_width=True)
    with cancel_col:
        cancel = st.button("Avbryt", key=f"{flag_key}_cancel", use_container_width=True)
    if cancel:
        st.session_state.pop(flag_key, None)
        st.rerun()
    if confirm:
        st.session_state.pop(flag_key, None)
        return True
    return False


def _render_sidebar_work_queue_filters(prefix: str, *, mode: str) -> None:
    with st.sidebar.expander("Arbeidskø-filtre", expanded=False):
        st.selectbox(
            "Køstatus",
            options=["all", "open", "resolved"],
            key=f"{prefix}_queue_status",
            format_func=lambda value: {
                "all": "Begge",
                "open": "Åpne",
                "resolved": "Løste",
            }.get(str(value), str(value)),
            help="Filtrer arbeidskøen på åpne eller løste bugs.",
        )
        st.checkbox("Kun kritiske", key=f"{prefix}_queue_critical_only", value=False)
        st.checkbox("Kun negativt sentiment", key=f"{prefix}_queue_negative_only", value=False)
        st.checkbox("Kun inaktive (7+ dager)", key=f"{prefix}_queue_stale_only", value=False)
        if mode == "admin":
            st.checkbox("Kun uten ansvarlig", key=f"{prefix}_queue_unassigned_only", value=False)


def _apply_sidebar_work_queue_filters(bugs: list[Bug], *, prefix: str, mode: str) -> list[Bug]:
    queue_status = str(st.session_state.get(f"{prefix}_queue_status", "all") or "all").strip()
    critical_only = bool(st.session_state.get(f"{prefix}_queue_critical_only", False))
    negative_only = bool(st.session_state.get(f"{prefix}_queue_negative_only", False))
    stale_only = bool(st.session_state.get(f"{prefix}_queue_stale_only", False))
    unassigned_only = bool(st.session_state.get(f"{prefix}_queue_unassigned_only", False)) if mode == "admin" else False

    filtered: list[Bug] = []
    for bug in bugs:
        bug_status = normalize_bug_status(bug.status)
        if queue_status != "all" and bug_status != queue_status:
            continue
        if critical_only and str(bug.severity or "") != "critical":
            continue
        if negative_only and str(bug.sentiment_label or "").strip().casefold() != "negative":
            continue
        if stale_only and not _is_stale_bug(bug):
            continue
        if unassigned_only and str(bug.assignee_id or "").strip():
            continue
        filtered.append(bug)
    return filtered


def _ensure_reporter_state() -> None:
    defaults = {
        "reporter_ai_input": "",
        "reporter_ai_status": "",
        "reporter_ai_error": "",
        "reporter_ai_validation_warnings": [],
        "reporter_ai_debug_details": "",
        "reporter_ai_file_extract_summary": "",
        "reporter_similar_results": [],
        "reporter_similar_query": "",
        "reporter_typeahead_suggestion": "",
        "reporter_typeahead_error": "",
        "reporter_typeahead_source": "",
        "reporter_duplicate_exact_id": None,
        "reporter_duplicate_candidates": [],
        "reporter_duplicate_checked": False,
        "reporter_form_reset_pending": False,
        "reporter_append_description_pending": "",
        "reporter_create_title": "",
        "reporter_create_description": "",
        "reporter_create_category": "software",
        "reporter_create_severity": "medium",
        "reporter_create_assignee": "",
        "reporter_create_notify_emails": "",
        "reporter_create_environment": "",
        "reporter_create_tags": "",
        "reporter_uploader_nonce": 0,
        "reporter_ai_uploader_nonce": 0,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    if st.session_state.get("reporter_form_reset_pending"):
        _apply_reporter_form_reset()
        st.session_state["reporter_form_reset_pending"] = False
    pending_append = str(st.session_state.get("reporter_append_description_pending", "") or "").strip()
    if pending_append:
        existing = str(st.session_state.get("reporter_create_description", "")).rstrip()
        separator = "\n\n" if existing else ""
        st.session_state["reporter_create_description"] = f"{existing}{separator}{pending_append}".strip()
        st.session_state["reporter_append_description_pending"] = ""


def _reset_reporter_form_state() -> None:
    st.session_state["reporter_form_reset_pending"] = True


def _apply_reporter_form_reset() -> None:
    st.session_state["reporter_create_title"] = ""
    st.session_state["reporter_create_description"] = ""
    st.session_state["reporter_create_category"] = "software"
    st.session_state["reporter_create_severity"] = "medium"
    st.session_state["reporter_create_assignee"] = ""
    st.session_state["reporter_create_notify_emails"] = ""
    st.session_state["reporter_create_environment"] = ""
    st.session_state["reporter_create_tags"] = ""
    st.session_state["reporter_ai_debug_details"] = ""
    st.session_state["reporter_ai_file_extract_summary"] = ""
    st.session_state["reporter_ai_validation_warnings"] = []
    st.session_state["reporter_typeahead_suggestion"] = ""
    st.session_state["reporter_typeahead_error"] = ""
    st.session_state["reporter_typeahead_source"] = ""
    st.session_state["reporter_duplicate_exact_id"] = None
    st.session_state["reporter_duplicate_candidates"] = []
    st.session_state["reporter_duplicate_checked"] = False
    st.session_state["reporter_append_description_pending"] = ""
    st.session_state["reporter_uploader_nonce"] = int(st.session_state.get("reporter_uploader_nonce", 0)) + 1
    st.session_state["reporter_ai_uploader_nonce"] = int(st.session_state.get("reporter_ai_uploader_nonce", 0)) + 1


def _normalize_email(value: str | None) -> str:
    return str(value or "").strip().casefold()


def _is_valid_email(value: str) -> bool:
    candidate = _normalize_email(value)
    if not candidate:
        return False
    pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    return re.match(pattern, candidate) is not None


def _parse_email_list(value: str | None) -> list[str]:
    if not value:
        return []
    entries = [
        _normalize_email(item)
        for item in re.split(r"[;, \n]+", str(value))
        if _normalize_email(item)
    ]
    unique: list[str] = []
    for entry in entries:
        if entry not in unique:
            unique.append(entry)
    return unique


def _normalize_ai_choice(value: Any, *, allowed: list[str], default: str) -> tuple[str, bool]:
    candidate = str(value or "").strip().casefold()
    if candidate in set(allowed):
        return candidate, False
    return default, bool(candidate)


def _sanitize_ai_tags(
    value: Any,
    *,
    max_tags: int = 12,
    max_tag_length: int = 32,
    max_total_length: int = 240,
) -> tuple[str, int]:
    if value is None:
        return "", 0
    if isinstance(value, list):
        raw_parts = [str(item or "") for item in value]
    else:
        raw_parts = re.split(r"[,;\n]+", str(value or ""))

    cleaned_tags: list[str] = []
    seen: set[str] = set()
    dropped_count = 0
    for raw in raw_parts:
        token = str(raw or "").strip()
        if not token:
            continue
        token = token.lstrip("#")
        token = re.sub(r"[^\w\-./ ]+", "", token, flags=re.UNICODE)
        token = re.sub(r"\s+", "-", token.strip())
        token = token.strip("-._")
        if len(token) < 2:
            dropped_count += 1
            continue
        if len(token) > max_tag_length:
            token = token[:max_tag_length].rstrip("-._")
        if len(token) < 2:
            dropped_count += 1
            continue
        normalized = token.casefold()
        if normalized in seen:
            continue
        tentative = cleaned_tags + [token]
        if len(", ".join(tentative)) > max_total_length:
            dropped_count += 1
            continue
        seen.add(normalized)
        cleaned_tags.append(token)
        if len(cleaned_tags) >= max_tags:
            dropped_count += max(0, len(raw_parts) - len(cleaned_tags))
            break
    return ", ".join(cleaned_tags), dropped_count


def _allow_ai_action(
    action_key: str,
    *,
    cooldown_seconds: float = 2.5,
    max_calls_in_window: int = 3,
    window_seconds: float = 20.0,
) -> tuple[bool, str | None]:
    now = time.time()
    state = st.session_state.setdefault("_ai_action_limiter", {})
    raw_timestamps = state.get(action_key, []) if isinstance(state, dict) else []
    timestamps = []
    if isinstance(raw_timestamps, list):
        for value in raw_timestamps:
            try:
                ts = float(value)
            except (TypeError, ValueError):
                continue
            if (now - ts) <= max(1.0, float(window_seconds)):
                timestamps.append(ts)

    if timestamps and (now - timestamps[-1]) < max(0.1, float(cooldown_seconds)):
        wait_seconds = max(0.1, float(cooldown_seconds) - (now - timestamps[-1]))
        state[action_key] = timestamps
        st.session_state["_ai_action_limiter"] = state
        return (
            False,
            f"AI-knappen ble nylig brukt. Vent {wait_seconds:.1f} sek før du prøver igjen.",
        )

    if len(timestamps) >= max(1, int(max_calls_in_window)):
        state[action_key] = timestamps
        st.session_state["_ai_action_limiter"] = state
        return (
            False,
            "For mange AI-kall på kort tid. Vent litt og prøv igjen.",
        )

    timestamps.append(now)
    state[action_key] = timestamps
    st.session_state["_ai_action_limiter"] = state
    return True, None


def _build_assignable_emails() -> list[str]:
    with db_session() as db:
        query = select(User.email, User.role)
        rows = db.execute(query).all()
    allowed_roles = {"assignee", "admin"}
    merged_emails = {
        _normalize_email(email)
        for email, role in rows
        if _normalize_email(email) and str(role or "").strip().casefold() in allowed_roles
    }

    current_user = _current_user()
    if current_user and _is_entra_session(current_user) and _devops_ui_enabled():
        payload = _load_devops_settings()
        config_org = str(payload.get("resolved_org") or "").strip()
        config_project = str(payload.get("resolved_project") or "").strip()
        config_pat = str(payload.get("resolved_pat") or "").strip()
        config_work_item_type = str(payload.get("resolved_work_item_type") or "Task").strip() or "Task"
        if config_org and config_project and config_pat:
            try:
                devops_users = list_assignable_devops_users(
                    DevOpsConfig(
                        org=config_org,
                        project=config_project,
                        pat=config_pat,
                        work_item_type=config_work_item_type,
                    ),
                    timeout_seconds=12.0,
                )
                merged_emails.update(
                    _normalize_email(str(item.get("email") or ""))
                    for item in devops_users
                    if isinstance(item, dict) and _normalize_email(str(item.get("email") or ""))
                )
            except RuntimeError as exc:
                logger.warning("DevOps assignable user lookup skipped: %s", exc)
            except Exception as exc:
                logger.warning("DevOps assignable user lookup failed: %s", exc.__class__.__name__)

    return sorted(merged_emails)


def _assignee_select_options(current: str | None, assignable_emails: list[str]) -> list[str]:
    options = [""] + sorted({email for email in assignable_emails if email})
    current_email = _normalize_email(current)
    if current_email and current_email not in options:
        options.append(current_email)
    return options


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", filename).strip("._")
    return cleaned or "attachment.bin"


def _extract_json_object(text: str) -> dict | None:
    return _extract_json_object_impl(text)


@st.cache_resource
def _get_docling_converter() -> object | None:
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
    except Exception:
        return None
    try:
        return DocumentConverter()
    except Exception:
        return None


def _extract_text_from_uploaded_files(files: list) -> tuple[str, list[str]]:
    if not files:
        return "", []

    chunks: list[str] = []
    messages: list[str] = []
    converter = _get_docling_converter()
    document_stream_cls = None
    if converter is not None:
        try:
            from docling.datamodel.base_models import DocumentStream  # type: ignore

            document_stream_cls = DocumentStream
        except Exception:
            converter = None
    for uploaded_file in files:
        try:
            filename = str(getattr(uploaded_file, "name", "") or "unknown")
            suffix = Path(filename).suffix.casefold()
            raw_content = uploaded_file.getvalue()
            if not isinstance(raw_content, (bytes, bytearray)):
                messages.append(f"{filename}: kunne ikke lese filinnhold.")
                continue

            if converter is not None and document_stream_cls is not None:
                try:
                    stream = document_stream_cls(name=filename, stream=BytesIO(bytes(raw_content)))
                    result = converter.convert(stream)
                    extracted_text = str(result.document.export_to_markdown() or "").strip()
                    if extracted_text:
                        chunks.append(f"[File: {filename}]\n{extracted_text}")
                        messages.append(f"{filename}: Docling hentet ut {len(extracted_text)} tegn (inkl. OCR for bilde/PDF).")
                        continue
                    messages.append(f"{filename}: Docling fant ingen tekst, forsøker fallback.")
                except Exception as exc:
                    messages.append(f"{filename}: Docling-feil ({exc.__class__.__name__}), forsøker fallback.")

            text: str = ""
            if suffix in {
                ".txt",
                ".md",
                ".csv",
                ".json",
                ".log",
                ".yaml",
                ".yml",
                ".xml",
                ".ini",
                ".cfg",
                ".py",
                ".js",
                ".ts",
                ".html",
                ".css",
                ".sql",
            }:
                text = bytes(raw_content).decode("utf-8", errors="ignore").strip()
            elif suffix == ".pdf":
                try:
                    from pypdf import PdfReader  # type: ignore

                    reader = PdfReader(BytesIO(bytes(raw_content)))
                    pages = [str(page.extract_text() or "") for page in reader.pages[:15]]
                    text = "\n".join(pages).strip()
                except Exception as exc:
                    messages.append(f"{filename}: PDF-uttrekk feilet ({exc.__class__.__name__}).")
                    continue
            else:
                messages.append(
                    f"{filename}: filtype støttes ikke av fallback-uttrekk. "
                    "Installer Docling for OCR/uttrekk av flere formater."
                )
                continue

            if not text:
                messages.append(f"{filename}: ingen tekst funnet.")
                continue

            chunks.append(f"[File: {filename}]\n{text}")
            messages.append(f"{filename}: hentet ut {len(text)} tegn.")
        except Exception as exc:
            file_name = str(getattr(uploaded_file, "name", "") or "unknown")
            messages.append(f"{file_name}: uttrekk feilet ({exc.__class__.__name__}).")

    if converter is None:
        messages.append("Docling er ikke tilgjengelig i miljøet; bruker enkel tekst/PDF-fallback.")

    combined_text = "\n\n".join(chunks).strip()
    if len(combined_text) > MAX_AI_EXTRACTED_TEXT_CHARS:
        combined_text = combined_text[:MAX_AI_EXTRACTED_TEXT_CHARS]
        messages.append(
            f"Filtekst ble avkortet til {MAX_AI_EXTRACTED_TEXT_CHARS} tegn før AI-kall."
        )
    return combined_text, messages


def _openai_reporter_draft(raw_text: str) -> tuple[dict | None, str | None, dict]:
    return _request_reporter_draft(
        raw_text=raw_text,
        api_key=_config_value("OPENAI_API_KEY", settings.openai_api_key or ""),
        model=_selected_ai_model(),
    )


def _build_bug_ai_context(bug: Bug, *, max_comments: int = 12, max_chars: int = 6000) -> str:
    comments = sorted(_resolve_bug_comments(bug), key=lambda item: item.created_at or datetime.min)
    recent_comments = comments[-max_comments:]
    conversation_lines = []
    for item in recent_comments:
        role = str(item.author_role or "user")
        author = str(item.author_email or "-")
        body = re.sub(r"\s+", " ", str(item.body or "").strip())
        if body:
            conversation_lines.append(f"{role} ({author}): {body[:450]}")
    context = "\n".join(
        part
        for part in [
            f"Bug #{bug.id}",
            f"Tittel: {bug.title or ''}",
            f"Beskrivelse: {bug.description or ''}",
            f"Status: {bug.status or ''}",
            f"Alvorlighetsgrad: {bug.severity or ''}",
            f"Kategori: {bug.category or ''}",
            f"Miljø: {bug.environment or ''}",
            f"Tagger: {bug.tags or ''}",
            "Samtale:",
            "\n".join(conversation_lines) if conversation_lines else "(ingen samtale)",
        ]
        if part is not None
    )
    return context[:max_chars]


def _openai_assignee_solution_suggestion(bug: Bug) -> tuple[str, str, str | None]:
    return _request_assignee_solution(
        context=_build_bug_ai_context(bug),
        api_key=_config_value("OPENAI_API_KEY", settings.openai_api_key or ""),
        model=_selected_ai_model(),
    )


def _normalize_sentiment_label(value: str | None) -> str:
    normalized = str(value or "").strip().casefold()
    if not normalized:
        return "unknown"
    if normalized in {"positive", "positiv"}:
        return "positive"
    if normalized in {"negative", "negativ"}:
        return "negative"
    if normalized in {"neutral", "nøytral", "noytral"}:
        return "neutral"
    return "unknown"


def _sentiment_symbol(label: str | None) -> str:
    normalized = _normalize_sentiment_label(label)
    if normalized == "positive":
        return ":-)"
    if normalized == "negative":
        return ":-("
    if normalized == "neutral":
        return ":-|"
    return ""


def _openai_bug_sentiment_analysis(bug: Bug) -> tuple[str, str, str | None]:
    return _request_bug_sentiment(
        context=_build_bug_ai_context(bug, max_comments=20, max_chars=7000),
        api_key=_config_value("OPENAI_API_KEY", settings.openai_api_key or ""),
        model=_selected_ai_model(),
    )


def _run_bug_sentiment_analysis(user: dict[str, str], bug_id: int) -> str | None:
    with db_session() as db:
        bug = db.get(Bug, bug_id)
        if not bug:
            return "Fant ikke bug."
        if normalize_bug_status(bug.status) == "resolved":
            return "Bugen er løst og kan ikke oppdateres. Sett status tilbake til Åpen først."
        label, summary, error = _openai_bug_sentiment_analysis(bug)
        if error:
            return error
        bug.sentiment_label = label
        bug.sentiment_summary = summary or None
        bug.sentiment_analyzed_at = datetime.now(timezone.utc)
        _write_history(
            db,
            bug_id=bug.id,
            actor_email=user["email"],
            action="sentiment_analyzed",
            details=f"label={label}, summary={summary or '-'}",
        )
        _commit_with_retry(db, operation="Lagring av sentimentanalyse")
    _clear_bug_cache()
    return None


def _openai_bug_summary(bug: Bug) -> tuple[str, str | None]:
    return _request_bug_summary(
        context=_build_bug_ai_context(bug, max_comments=20, max_chars=7000),
        api_key=_config_value("OPENAI_API_KEY", settings.openai_api_key or ""),
        model=_selected_ai_model(),
    )


def _run_bug_summary(user: dict[str, str], bug_id: int) -> str | None:
    with db_session() as db:
        bug = db.get(Bug, bug_id)
        if not bug:
            return "Fant ikke bug."
        if normalize_bug_status(bug.status) == "resolved":
            return "Bugen er løst og kan ikke oppdateres. Sett status tilbake til Åpen først."
        summary, error = _openai_bug_summary(bug)
        if error:
            return error
        bug.bug_summary = summary
        bug.bug_summary_updated_at = datetime.now(timezone.utc)
        _write_history(
            db,
            bug_id=bug.id,
            actor_email=user["email"],
            action="bug_summarized",
            details=summary[:400],
        )
        _commit_with_retry(db, operation="Lagring av oppsummering")
    _clear_bug_cache()
    return None


def _sanitize_reporter_ai_payload(
    payload: dict,
    *,
    allowed_assignees: set[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    warnings: list[str] = []
    title = str(payload.get("title", "") or "").strip()[:255]
    description = str(payload.get("description", "") or "").strip()
    severity, severity_corrected = _normalize_ai_choice(
        payload.get("severity"),
        allowed=SEVERITY_OPTIONS,
        default="medium",
    )
    category, category_corrected = _normalize_ai_choice(
        payload.get("category"),
        allowed=CATEGORY_OPTIONS,
        default="software",
    )
    if severity_corrected:
        warnings.append("AI-forslag for alvorlighetsgrad var ugyldig og ble satt til 'medium'.")
    if category_corrected:
        warnings.append("AI-forslag for kategori var ugyldig og ble satt til 'software'.")

    assignee_email = _normalize_email(str(payload.get("assignee_email", "") or ""))
    normalized_allowed_assignees = {
        _normalize_email(item)
        for item in (allowed_assignees or set())
        if _normalize_email(item)
    }
    if assignee_email:
        if not _is_valid_email(assignee_email):
            warnings.append("AI-forslag for tildelt bruker var ikke en gyldig e-post og ble fjernet.")
            assignee_email = ""
        elif not normalized_allowed_assignees:
            warnings.append("AI-forslag for tildelt bruker kunne ikke valideres mot systemet og ble fjernet.")
            assignee_email = ""
        elif assignee_email not in normalized_allowed_assignees:
            warnings.append("AI-forslag for tildelt bruker finnes ikke i systemet; feltet ble tømt.")
            assignee_email = ""

    notify_value = payload.get("notify_emails", "")
    if isinstance(notify_value, list):
        parsed_notify = _parse_email_list(", ".join(str(item) for item in notify_value))
    else:
        parsed_notify = _parse_email_list(str(notify_value))
    valid_notify = [entry for entry in parsed_notify if _is_valid_email(entry)]
    if len(valid_notify) != len(parsed_notify):
        warnings.append("Noen AI-forslåtte varslingsadresser var ugyldige og ble fjernet.")

    tags_value = payload.get("tags", "")
    sanitized_tags, dropped_count = _sanitize_ai_tags(tags_value)
    if dropped_count:
        warnings.append("Noen AI-forslåtte tagger ble normalisert eller fjernet.")

    return (
        {
            "title": title,
            "description": description,
            "severity": severity,
            "category": category,
            "assignee": assignee_email,
            "notify_emails": ", ".join(valid_notify),
            "environment": str(payload.get("environment", "") or "").strip(),
            "tags": sanitized_tags,
        },
        warnings,
    )


def _apply_reporter_ai_draft(payload: dict, *, allowed_assignees: set[str] | None = None) -> list[str]:
    sanitized, warnings = _sanitize_reporter_ai_payload(payload, allowed_assignees=allowed_assignees)
    st.session_state["reporter_create_title"] = sanitized["title"]
    st.session_state["reporter_create_description"] = sanitized["description"]
    st.session_state["reporter_create_severity"] = sanitized["severity"]
    st.session_state["reporter_create_category"] = sanitized["category"]
    st.session_state["reporter_create_assignee"] = sanitized["assignee"]
    st.session_state["reporter_create_notify_emails"] = sanitized["notify_emails"]
    st.session_state["reporter_create_environment"] = sanitized["environment"]
    st.session_state["reporter_create_tags"] = sanitized["tags"]
    return warnings


def _find_similar_bugs(query_text: str, bugs: list[Bug], *, limit: int = 5) -> list[tuple[float, Bug]]:
    query = str(query_text or "").strip().casefold()
    if len(query) < 4:
        return []

    current_user = _current_user()
    if current_user:
        search_settings = _current_search_settings()
        try:
            with db_session() as db:
                db_user = _get_or_create_user(db, email=current_user["email"], role=current_user["role"])
                semantic_matches = retrieve_similar_visible_bugs(
                    db,
                    current_user=db_user,
                    query=query,
                    limit=limit,
                    embedding_provider=search_settings["embedding_provider"],
                    embedding_model=search_settings["embedding_model"],
                )
            if semantic_matches:
                scored: list[tuple[float, Bug]] = []
                for idx, match in enumerate(semantic_matches):
                    score = max(0.01, 1.0 - (idx * 0.08))
                    scored.append((score, match))
                return scored
        except (SQLAlchemyError, DetachedInstanceError, RuntimeError, ValueError) as exc:
            logger.warning("Semantic similarity search failed; using local fallback. error=%s", exc.__class__.__name__)

    terms = {part for part in re.split(r"\W+", query) if len(part) > 2}
    candidates: list[tuple[float, Bug]] = []
    for bug in bugs:
        haystack = f"{bug.title or ''} {bug.description or ''} {bug.tags or ''}".casefold()
        if not haystack.strip():
            continue
        seq_score = SequenceMatcher(None, query, haystack[:2000]).ratio()
        hay_terms = {part for part in re.split(r"\W+", haystack) if len(part) > 2}
        overlap = len(terms & hay_terms) / max(1, len(terms)) if terms else 0.0
        score = (seq_score * 0.65) + (overlap * 0.35)
        if score > 0.12:
            candidates.append((score, bug))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[:limit]


def _build_reporter_draft_query() -> str:
    return " ".join(
        part
        for part in [
            str(st.session_state.get("reporter_create_title", "")).strip(),
            str(st.session_state.get("reporter_create_description", "")).strip(),
            str(st.session_state.get("reporter_create_category", "")).strip(),
            str(st.session_state.get("reporter_create_environment", "")).strip(),
            str(st.session_state.get("reporter_create_tags", "")).strip(),
        ]
        if part
    ).strip()


def _request_reporter_typeahead(all_bugs: list[Bug]) -> tuple[str, str, str]:
    description = str(st.session_state.get("reporter_create_description", "")).strip()
    if len(description) < 20:
        return "", "Skriv litt mer i beskrivelsen før du ber om forslag.", ""

    query = _build_reporter_draft_query()
    matches = _find_similar_bugs(query, all_bugs, limit=3)
    if not matches:
        fallback = "Legg til tydelige steg for reproduksjon, forventet resultat og faktisk resultat."
        return fallback, "", "heuristic"

    top_score, top_bug = matches[0]
    top_desc = str(top_bug.description or "").strip().replace("\n", " ")
    top_desc = re.sub(r"\s+", " ", top_desc)
    top_desc_short = top_desc[:220].strip()
    if len(top_desc) > 220:
        top_desc_short += "..."
    suggestion = (
        f"Sammenlign med lignende sak #{top_bug.id}: {top_desc_short}\n\n"
        "Presiser hvordan dette tilfellet avviker, og legg ved konkrete reproduksjonssteg."
    )
    source = f"similar_bug#{top_bug.id}:{round(top_score * 100)}%"
    return suggestion, "", source


def _check_reporter_duplicates(*, title: str, description: str, bugs: list[Bug], limit: int = 5) -> tuple[int | None, list[dict]]:
    title_norm = str(title or "").strip().casefold()
    desc_norm = re.sub(r"\s+", " ", str(description or "").strip().casefold())
    if not title_norm and not desc_norm:
        return None, []

    exact_id: int | None = None
    candidates: list[dict] = []
    query = f"{title_norm} {desc_norm}".strip()
    terms = {part for part in re.split(r"\W+", query) if len(part) > 2}

    for bug in bugs:
        hay_title = str(bug.title or "").strip().casefold()
        hay_desc = re.sub(r"\s+", " ", str(bug.description or "").strip().casefold())
        if not hay_title and not hay_desc:
            continue

        if title_norm and desc_norm and title_norm == hay_title and desc_norm == hay_desc:
            exact_id = bug.id
            break

        haystack = f"{hay_title} {hay_desc} {str(bug.tags or '').casefold()}".strip()
        seq_score = SequenceMatcher(None, query, haystack[:2500]).ratio()
        hay_terms = {part for part in re.split(r"\W+", haystack) if len(part) > 2}
        overlap = len(terms & hay_terms) / max(1, len(terms)) if terms else 0.0
        score = (seq_score * 0.7) + (overlap * 0.3)
        if score >= 0.45:
            candidates.append(
                {
                    "id": bug.id,
                    "title": bug.title,
                    "status": bug.status,
                    "severity": bug.severity,
                    "score": score,
                }
            )

    candidates.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    unique_candidates: list[dict] = []
    seen_ids: set[int] = set()
    for item in candidates:
        bug_id = int(item.get("id"))
        if bug_id in seen_ids:
            continue
        seen_ids.add(bug_id)
        unique_candidates.append(item)
        if len(unique_candidates) >= limit:
            break
    return exact_id, unique_candidates


def _bug_similarity_score(left: Bug, right: Bug) -> float:
    left_text = re.sub(
        r"\s+",
        " ",
        f"{left.title or ''} {left.description or ''} {left.tags or ''}".strip().casefold(),
    )
    right_text = re.sub(
        r"\s+",
        " ",
        f"{right.title or ''} {right.description or ''} {right.tags or ''}".strip().casefold(),
    )
    if not left_text or not right_text:
        return 0.0

    seq_score = SequenceMatcher(None, left_text[:2500], right_text[:2500]).ratio()
    left_terms = {part for part in re.split(r"\W+", left_text) if len(part) > 2}
    right_terms = {part for part in re.split(r"\W+", right_text) if len(part) > 2}
    overlap = len(left_terms & right_terms) / max(1, len(left_terms | right_terms))
    return (seq_score * 0.7) + (overlap * 0.3)


def _detect_duplicate_bug_pairs(bugs: list[Bug], *, threshold: float = 0.72, limit: int = 20) -> list[dict]:
    candidates: list[dict] = []
    for i, left in enumerate(bugs):
        for right in bugs[i + 1 :]:
            score = _bug_similarity_score(left, right)
            if score < threshold:
                continue

            left_age = left.created_at or datetime.min
            right_age = right.created_at or datetime.min
            keep_bug = left if left_age <= right_age else right
            delete_bug = right if keep_bug is left else left

            candidates.append(
                {
                    "keep_bug_id": keep_bug.id,
                    "keep_title": keep_bug.title or "Uten tittel",
                    "keep_status": keep_bug.status or "-",
                    "delete_bug_id": delete_bug.id,
                    "delete_title": delete_bug.title or "Uten tittel",
                    "delete_status": delete_bug.status or "-",
                    "similarity_score": float(score),
                    "recommendation_reason": "Høy tekstlikhet mellom tittel/beskrivelse/tagger.",
                }
            )

    candidates.sort(key=lambda item: float(item.get("similarity_score", 0.0)), reverse=True)
    unique: list[dict] = []
    seen_pairs: set[tuple[int, int]] = set()
    for item in candidates:
        pair = (int(item["keep_bug_id"]), int(item["delete_bug_id"]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def _render_assignee_sidebar_queue_summary(bugs: list[Bug]) -> None:
    with st.sidebar.expander("Arbeidskø", expanded=False):
        if not bugs:
            st.caption("Ingen bugs i visningen.")
            return
        open_count = sum(1 for bug in bugs if normalize_bug_status(bug.status) == "open")
        resolved_count = sum(1 for bug in bugs if normalize_bug_status(bug.status) == "resolved")
        critical_count = sum(1 for bug in bugs if str(bug.severity or "") == "critical")
        negative_sentiment_count = sum(1 for bug in bugs if _normalize_sentiment_label(bug.sentiment_label) == "negative")

        st.caption(f"Totalt: {len(bugs)}")
        st.caption(f"Åpne: {open_count}")
        st.caption(f"Løst: {resolved_count}")
        st.caption(f"Kritiske: {critical_count}")
        st.caption(f"Negativt sentiment: {negative_sentiment_count}")


def _render_assignee_sidebar_duplicates(user: dict[str, str], bugs: list[Bug]) -> None:
    if "assignee_duplicate_candidates" not in st.session_state:
        st.session_state["assignee_duplicate_candidates"] = None
    if "assignee_hidden_duplicate_candidates" not in st.session_state:
        st.session_state["assignee_hidden_duplicate_candidates"] = []

    with st.sidebar.expander("Mulige duplikater", expanded=False):
        if st.button(
            "Se etter duplikater",
            key="assignee_scan_duplicates_sidebar",
            use_container_width=True,
            help="Skanner buger i gjeldende visning for mulige duplikater.",
        ):
            st.session_state["assignee_duplicate_candidates"] = _detect_duplicate_bug_pairs(bugs)
            st.session_state["assignee_hidden_duplicate_candidates"] = []
            st.rerun()

        candidates = st.session_state.get("assignee_duplicate_candidates")
        if candidates is None:
            st.caption("Trykk 'Se etter duplikater' for å kjøre skann.")
            return
        if not candidates:
            st.success("Fant ingen tydelige duplikater i visningen.")
            return

        hidden_bug_ids = {int(item) for item in st.session_state.get("assignee_hidden_duplicate_candidates", [])}
        visible_candidates = [item for item in candidates if int(item["delete_bug_id"]) not in hidden_bug_ids]

        if not visible_candidates:
            st.info("Alle duplikatforslag er skjult i denne økten.")
            return

        st.caption(f"Forslag: {len(visible_candidates)}")
        for idx, candidate in enumerate(visible_candidates):
            st.warning(
                f"Behold #{candidate['keep_bug_id']} og vurder å slette #{candidate['delete_bug_id']} "
                f"(likhet {round(float(candidate['similarity_score']) * 100)}%)."
            )
            st.caption(
                f"#{candidate['keep_bug_id']}: {candidate['keep_title']} | "
                f"#{candidate['delete_bug_id']}: {candidate['delete_title']}"
            )
            col_delete, col_hide = st.columns(2)
            with col_delete:
                if st.button(
                    f"Slett #{candidate['delete_bug_id']}",
                    key=f"assignee_delete_duplicate_{candidate['keep_bug_id']}_{candidate['delete_bug_id']}_{idx}",
                    use_container_width=True,
                ):
                    _request_delete_confirmation(
                        prefix="assignee",
                        item_key=f"duplicate_{candidate['keep_bug_id']}_{candidate['delete_bug_id']}",
                    )
                    st.rerun()
                if _render_delete_confirmation(
                    prefix="assignee",
                    item_key=f"duplicate_{candidate['keep_bug_id']}_{candidate['delete_bug_id']}",
                    message=f"Bekreft sletting av foreslått duplikatbug #{candidate['delete_bug_id']}.",
                ):
                    error = _delete_bug(user, int(candidate["delete_bug_id"]))
                    if error:
                        st.error(error)
                    else:
                        st.success(f"Flyttet bug #{candidate['delete_bug_id']} til papirkurv.")
                        st.session_state["assignee_duplicate_candidates"] = _detect_duplicate_bug_pairs(
                            [bug for bug in bugs if bug.id != int(candidate["delete_bug_id"])]
                        )
                        _clear_bug_cache()
                        st.rerun()
            with col_hide:
                if st.button(
                    "Skjul",
                    key=f"assignee_hide_duplicate_{candidate['keep_bug_id']}_{candidate['delete_bug_id']}_{idx}",
                    use_container_width=True,
                ):
                    hidden = st.session_state.get("assignee_hidden_duplicate_candidates", [])
                    st.session_state["assignee_hidden_duplicate_candidates"] = [*hidden, int(candidate["delete_bug_id"])]
                    st.rerun()


def _render_admin_sidebar_queue_summary(bugs: list[Bug]) -> None:
    with st.sidebar.expander("Arbeidskø", expanded=False):
        if not bugs:
            st.caption("Ingen bugs i visningen.")
            return
        open_count = sum(1 for bug in bugs if normalize_bug_status(bug.status) == "open")
        resolved_count = sum(1 for bug in bugs if normalize_bug_status(bug.status) == "resolved")
        critical_count = sum(1 for bug in bugs if str(bug.severity or "") == "critical")
        unassigned_count = sum(1 for bug in bugs if not str(bug.assignee_id or "").strip())
        negative_sentiment_count = sum(1 for bug in bugs if _normalize_sentiment_label(bug.sentiment_label) == "negative")

        st.caption(f"Totalt: {len(bugs)}")
        st.caption(f"Åpne: {open_count}")
        st.caption(f"Løst: {resolved_count}")
        st.caption(f"Kritiske: {critical_count}")
        st.caption(f"Uten ansvarlig: {unassigned_count}")
        st.caption(f"Negativt sentiment: {negative_sentiment_count}")


def _render_admin_sidebar_duplicates(user: dict[str, str], bugs: list[Bug]) -> None:
    if "admin_duplicate_candidates" not in st.session_state:
        st.session_state["admin_duplicate_candidates"] = None
    if "admin_hidden_duplicate_candidates" not in st.session_state:
        st.session_state["admin_hidden_duplicate_candidates"] = []

    with st.sidebar.expander("Mulige duplikater", expanded=False):
        if st.button(
            "Se etter duplikater",
            key="admin_scan_duplicates_sidebar",
            use_container_width=True,
            help="Skanner buger i gjeldende admin-visning for mulige duplikater.",
        ):
            st.session_state["admin_duplicate_candidates"] = _detect_duplicate_bug_pairs(bugs)
            st.session_state["admin_hidden_duplicate_candidates"] = []
            st.rerun()

        candidates = st.session_state.get("admin_duplicate_candidates")
        if candidates is None:
            st.caption("Trykk 'Se etter duplikater' for å kjøre skann.")
            return
        if not candidates:
            st.success("Fant ingen tydelige duplikater i visningen.")
            return

        hidden_bug_ids = {int(item) for item in st.session_state.get("admin_hidden_duplicate_candidates", [])}
        visible_candidates = [item for item in candidates if int(item["delete_bug_id"]) not in hidden_bug_ids]

        if not visible_candidates:
            st.info("Alle duplikatforslag er skjult i denne økten.")
            return

        st.caption(f"Forslag: {len(visible_candidates)}")
        for idx, candidate in enumerate(visible_candidates):
            st.warning(
                f"Behold #{candidate['keep_bug_id']} og vurder å slette #{candidate['delete_bug_id']} "
                f"(likhet {round(float(candidate['similarity_score']) * 100)}%)."
            )
            st.caption(
                f"#{candidate['keep_bug_id']}: {candidate['keep_title']} | "
                f"#{candidate['delete_bug_id']}: {candidate['delete_title']}"
            )
            col_delete, col_hide = st.columns(2)
            with col_delete:
                if st.button(
                    f"Slett #{candidate['delete_bug_id']}",
                    key=f"admin_delete_duplicate_{candidate['keep_bug_id']}_{candidate['delete_bug_id']}_{idx}",
                    use_container_width=True,
                ):
                    _request_delete_confirmation(
                        prefix="admin",
                        item_key=f"duplicate_{candidate['keep_bug_id']}_{candidate['delete_bug_id']}",
                    )
                    st.rerun()
                if _render_delete_confirmation(
                    prefix="admin",
                    item_key=f"duplicate_{candidate['keep_bug_id']}_{candidate['delete_bug_id']}",
                    message=f"Bekreft sletting av foreslått duplikatbug #{candidate['delete_bug_id']}.",
                ):
                    error = _delete_bug(user, int(candidate["delete_bug_id"]))
                    if error:
                        st.error(error)
                    else:
                        st.success(f"Flyttet bug #{candidate['delete_bug_id']} til papirkurv.")
                        st.session_state["admin_duplicate_candidates"] = _detect_duplicate_bug_pairs(
                            [bug for bug in bugs if bug.id != int(candidate["delete_bug_id"])]
                        )
                        _clear_bug_cache()
                        st.rerun()
            with col_hide:
                if st.button(
                    "Skjul",
                    key=f"admin_hide_duplicate_{candidate['keep_bug_id']}_{candidate['delete_bug_id']}_{idx}",
                    use_container_width=True,
                ):
                    hidden = st.session_state.get("admin_hidden_duplicate_candidates", [])
                    st.session_state["admin_hidden_duplicate_candidates"] = [*hidden, int(candidate["delete_bug_id"])]
                    st.rerun()


def _parse_admin_created_from(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
        return parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _render_admin_sidebar_advanced_filters() -> None:
    with st.sidebar.expander("Admin-filtrering", expanded=False):
        st.text_input(
            "Opprettet fra (YYYY-MM-DD)",
            key="admin_created_from",
            help="Vis kun bugs opprettet på eller etter denne datoen.",
        )
        st.selectbox(
            "Sentiment",
            options=["all", "positive", "neutral", "negative"],
            key="admin_sentiment_filter",
            help="Filtrer på sentiment-label.",
        )
        st.checkbox(
            "Kun uten ansvarlig",
            key="admin_only_unassigned",
            value=False,
            help="Vis kun bugs uten tildelt ansvarlig.",
        )
        st.text_input(
            "Rapportør inneholder",
            key="admin_reporter_contains",
            help="Filtrer på e-post/tekst i rapportør-feltet.",
        )
        st.selectbox(
            "Tilfredshet",
            options=[
                "all",
                "ikke oppgitt",
                "Very satisfied",
                "Satisfied",
                "Neutral",
                "Dissatisfied",
                "Very dissatisfied",
            ],
            key="admin_satisfaction_filter",
        )


def _apply_admin_advanced_filters(bugs: list[Bug]) -> list[Bug]:
    created_from_input = str(st.session_state.get("admin_created_from", "") or "").strip()
    created_from = _parse_admin_created_from(created_from_input)
    sentiment_filter = str(st.session_state.get("admin_sentiment_filter", "all") or "all").strip().casefold()
    only_unassigned = bool(st.session_state.get("admin_only_unassigned", False))
    reporter_contains = str(st.session_state.get("admin_reporter_contains", "") or "").strip().casefold()
    satisfaction_filter = str(st.session_state.get("admin_satisfaction_filter", "all") or "all").strip()

    if created_from_input and created_from is None:
        st.sidebar.warning("Ugyldig datoformat i 'Opprettet fra'. Bruk YYYY-MM-DD.")

    filtered: list[Bug] = []
    for bug in bugs:
        if created_from is not None:
            created_at = bug.created_at
            if created_at is None:
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at < created_from:
                continue

        if sentiment_filter != "all":
            if _normalize_sentiment_label(bug.sentiment_label) != sentiment_filter:
                continue

        if only_unassigned and str(bug.assignee_id or "").strip():
            continue

        if reporter_contains and reporter_contains not in str(bug.reporter_id or "").casefold():
            continue

        satisfaction_value = str(bug.reporter_satisfaction or "").strip()
        if satisfaction_filter == "ikke oppgitt" and satisfaction_value:
            continue
        if satisfaction_filter not in {"all", "ikke oppgitt"} and satisfaction_value != satisfaction_filter:
            continue

        filtered.append(bug)
    return filtered


def _days_since_datetime(value: datetime | None) -> int | None:
    if value is None:
        return None
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - current).days)


def _is_stale_bug(bug: Bug) -> bool:
    if normalize_bug_status(bug.status) == "resolved":
        return False
    days = _days_since_datetime(bug.updated_at or bug.created_at)
    return (days or 0) >= 7


def _is_critical_aging_bug(bug: Bug) -> bool:
    if normalize_bug_status(bug.status) == "resolved":
        return False
    if str(bug.severity or "") != "critical":
        return False
    days = _days_since_datetime(bug.created_at)
    return (days or 0) >= 2


def _render_admin_dashboard_cards(bugs: list[Bug]) -> None:
    if not bugs:
        return
    open_count = sum(1 for bug in bugs if normalize_bug_status(bug.status) == "open")
    resolved_count = sum(1 for bug in bugs if normalize_bug_status(bug.status) == "resolved")
    unassigned_count = sum(1 for bug in bugs if not str(bug.assignee_id or "").strip())
    critical_open_count = sum(
        1 for bug in bugs if normalize_bug_status(bug.status) == "open" and str(bug.severity or "") == "critical"
    )
    negative_sentiment_count = sum(1 for bug in bugs if _normalize_sentiment_label(bug.sentiment_label) == "negative")
    stale_count = sum(1 for bug in bugs if _is_stale_bug(bug))
    critical_aging_count = sum(1 for bug in bugs if _is_critical_aging_bug(bug))
    feedback_count = sum(1 for bug in bugs if str(bug.reporter_satisfaction or "").strip())
    sla_breach_count = sum(
        1
        for bug in bugs
        if normalize_bug_status(bug.status) != "resolved" and bool(_bug_sla_snapshot(bug).get("overdue"))
    )

    st.caption("Admin-dashboard")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Åpne", open_count)
    c2.metric("Løste", resolved_count)
    c3.metric("Uten ansvarlig", unassigned_count)
    c4.metric("Kritiske åpne", critical_open_count)

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Negativt sentiment", negative_sentiment_count)
    c6.metric("Inaktive 7+ dager", stale_count)
    c7.metric("Kritisk aldring", critical_aging_count)
    c8.metric("Med tilbakemelding", feedback_count)
    st.caption(f"SLA-brudd (åpne): {sla_breach_count}")

    stale_bugs = sorted(
        [bug for bug in bugs if _is_stale_bug(bug)],
        key=lambda item: (item.updated_at or item.created_at or datetime.min.replace(tzinfo=timezone.utc)),
    )[:5]
    if stale_bugs:
        with st.expander("Prioriter nå", expanded=False):
            for item in stale_bugs:
                days = _days_since_datetime(item.updated_at or item.created_at)
                st.write(
                    f"#{item.id} - {item.title} [{status_label(item.status)}] | "
                    f"Alvorlighet: {item.severity} | Inaktiv: {days if days is not None else '-'} dager"
                )


def _validate_reporter_create_input(*, assignable_emails: list[str]) -> str | None:
    title = str(st.session_state.get("reporter_create_title", "")).strip()
    description = str(st.session_state.get("reporter_create_description", "")).strip()
    assignee = _normalize_email(str(st.session_state.get("reporter_create_assignee", "")))
    notify_emails = _parse_email_list(str(st.session_state.get("reporter_create_notify_emails", "")))
    severity = str(st.session_state.get("reporter_create_severity", "")).strip().casefold()
    category = str(st.session_state.get("reporter_create_category", "")).strip().casefold()
    raw_tags = str(st.session_state.get("reporter_create_tags", "") or "").strip()

    if len(title) < 3:
        return "Tittel må være minst 3 tegn."
    if len(description) < 10:
        return "Beskrivelse må være minst 10 tegn."
    if severity not in set(SEVERITY_OPTIONS):
        return "Alvorlighetsgrad må være en gyldig verdi."
    if category not in set(CATEGORY_OPTIONS):
        return "Kategori må være en gyldig verdi."
    if raw_tags:
        sanitized_tags, _ = _sanitize_ai_tags(raw_tags)
        if not sanitized_tags:
            return "Tagger inneholder ingen gyldige verdier."
        if len(sanitized_tags) > 255:
            return "Tagger er for langt. Reduser antall eller lengde på tagger."
    if assignee and assignee not in set(assignable_emails):
        return "Tildelt bruker må velges fra listen over tildelbare brukere."
    invalid_notify = [entry for entry in notify_emails if not _is_valid_email(entry)]
    if invalid_notify:
        return f"Ugyldige e-postadresser i varsling: {', '.join(invalid_notify)}"
    return None


def _bug_cache_key(user: dict[str, str]) -> str:
    return f"bugs::{user['role']}::{user['email'].casefold()}"


def _clear_bug_cache() -> None:
    try:
        clear_cached_value("bugs::")
    except (RuntimeError, ValueError) as exc:
        logger.debug("Unable to clear bug cache: %s", exc)
    for key in ("_bug_comments_cache", "_bug_history_cache", "_bug_attachments_cache"):
        st.session_state.pop(key, None)


def _record_runtime_metric(name: str, value: float) -> None:
    key = "_runtime_metrics"
    metrics = st.session_state.setdefault(key, {})
    if not isinstance(metrics, dict):
        metrics = {}
        st.session_state[key] = metrics
    values = metrics.setdefault(name, [])
    if not isinstance(values, list):
        values = []
        metrics[name] = values
    values.append(float(value))
    if len(values) > 80:
        del values[:-80]


def _runtime_metric_latest(name: str) -> float:
    metrics = st.session_state.get("_runtime_metrics", {})
    if not isinstance(metrics, dict):
        return 0.0
    values = metrics.get(name)
    if not isinstance(values, list) or not values:
        return 0.0
    return float(values[-1] or 0.0)


def _runtime_metric_average(name: str) -> float:
    metrics = st.session_state.get("_runtime_metrics", {})
    if not isinstance(metrics, dict):
        return 0.0
    values = metrics.get(name)
    if not isinstance(values, list) or not values:
        return 0.0
    return float(mean([float(item or 0.0) for item in values]))


def _runtime_performance_snapshot() -> dict[str, float]:
    return {
        "page_reporter_ms": _runtime_metric_latest("page_reporter_ms"),
        "page_assignee_ms": _runtime_metric_latest("page_assignee_ms"),
        "page_admin_ms": _runtime_metric_latest("page_admin_ms"),
        "search_reporter_ms": _runtime_metric_latest("search_reporter_ms"),
        "search_assignee_ms": _runtime_metric_latest("search_assignee_ms"),
        "search_admin_ms": _runtime_metric_latest("search_admin_ms"),
        "ai_wait_avg_ms": _runtime_metric_average("ai_wait_ms"),
        "search_avg_ms": float(get_search_telemetry_snapshot().get("avg_latency_ms", 0.0) or 0.0),
    }


def _is_sqlite_write_conflict(exc: Exception) -> bool:
    message = str(exc or "").strip().casefold()
    if "database is locked" in message:
        return True
    if "database table is locked" in message:
        return True
    if "database is busy" in message:
        return True
    if "resource busy" in message:
        return True
    return False


def _commit_with_retry(
    db,
    *,
    operation: str,
    max_attempts: int = 5,
    base_delay_seconds: float = 0.12,
) -> None:
    attempts = max(1, int(max_attempts))
    for attempt in range(1, attempts + 1):
        try:
            db.commit()
            return
        except (OperationalError, SQLAlchemyError) as exc:
            db.rollback()
            if settings.database_is_sqlite and _is_sqlite_write_conflict(exc) and attempt < attempts:
                delay = base_delay_seconds * (2 ** (attempt - 1))
                time.sleep(min(1.2, delay))
                continue
            if settings.database_is_sqlite and _is_sqlite_write_conflict(exc):
                raise RuntimeError(
                    "En annen bruker oppdaterer databasen akkurat nå. Prøv igjen om noen sekunder."
                ) from exc
            raise RuntimeError(
                format_user_error(
                    f"{operation} feilet",
                    exc,
                    fallback="Databasen svarte med en feil. Prøv igjen.",
                )
            ) from exc


def _runtime_meta_get(db, key: str, default: str) -> str:
    row = db.get(AppRuntimeMeta, key)
    if row is None or row.value is None:
        return str(default)
    return str(row.value)


def _runtime_meta_set(db, key: str, value: str) -> None:
    row = db.get(AppRuntimeMeta, key)
    if row is None:
        db.add(AppRuntimeMeta(key=key, value=str(value)))
    else:
        row.value = str(value)


def _runtime_meta_delete(db, key: str) -> None:
    row = db.get(AppRuntimeMeta, key)
    if row is not None:
        db.delete(row)


_DEVOPS_META_KEYS: dict[str, str] = {
    "enabled": "devops.enabled",
    "org": "devops.org",
    "project": "devops.project",
    "pat": "devops.pat",
    "work_item_type": "devops.work_item_type",
}
_DEVOPS_BULK_SYNC_LAST_SUMMARY_KEY = "devops.bulk_sync.last_summary"
_DEVOPS_BULK_PUSH_LAST_SUMMARY_KEY = "devops.bulk_push.last_summary"


def _mask_secret(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "*" * len(raw)
    return f"{raw[:4]}{'*' * max(4, len(raw) - 8)}{raw[-4:]}"


def _load_devops_settings() -> dict[str, Any]:
    config_enabled_raw = str(_config_value("ENABLE_DEVOPS_IN_UI", "false") or "").strip()
    secret_org = str(_config_value("AZURE_DEVOPS_ORG", "") or "").strip()
    secret_project = str(_config_value("AZURE_DEVOPS_PROJECT", "") or "").strip()
    secret_pat = str(_config_value("AZURE_DEVOPS_PAT", "") or "").strip()
    secret_work_item_type = str(_config_value("AZURE_DEVOPS_WORK_ITEM_TYPE", "Task") or "Task").strip()

    override_enabled_raw = ""
    override_org = ""
    override_project = ""
    override_pat = ""
    override_work_item_type = ""
    try:
        with db_session() as db:
            override_enabled_raw = str(_runtime_meta_get(db, _DEVOPS_META_KEYS["enabled"], "") or "").strip()
            override_org = str(_runtime_meta_get(db, _DEVOPS_META_KEYS["org"], "") or "").strip()
            override_project = str(_runtime_meta_get(db, _DEVOPS_META_KEYS["project"], "") or "").strip()
            override_pat = str(_runtime_meta_get(db, _DEVOPS_META_KEYS["pat"], "") or "").strip()
            override_work_item_type = str(_runtime_meta_get(db, _DEVOPS_META_KEYS["work_item_type"], "") or "").strip()
    except Exception:
        override_enabled_raw = ""
        override_org = ""
        override_project = ""
        override_pat = ""
        override_work_item_type = ""

    override_enabled = ""
    if override_enabled_raw:
        normalized = override_enabled_raw.casefold()
        if normalized in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            override_enabled = normalized

    resolved_enabled = _is_truthy(override_enabled) if override_enabled else _is_truthy(config_enabled_raw)
    resolved_org = override_org or secret_org
    resolved_project = override_project or secret_project
    resolved_pat = override_pat or secret_pat
    resolved_work_item_type = (override_work_item_type or secret_work_item_type or "Task").strip() or "Task"
    return {
        "config_enabled_raw": config_enabled_raw,
        "override_enabled": override_enabled,
        "resolved_enabled": bool(resolved_enabled),
        "enabled_source": "override" if override_enabled else "config",
        "secret_org": secret_org,
        "secret_project": secret_project,
        "secret_pat": secret_pat,
        "override_org": override_org,
        "override_project": override_project,
        "override_pat": override_pat,
        "override_work_item_type": override_work_item_type,
        "resolved_org": resolved_org,
        "resolved_project": resolved_project,
        "resolved_pat": resolved_pat,
        "secret_work_item_type": secret_work_item_type,
        "resolved_work_item_type": resolved_work_item_type,
        "org_source": "override" if override_org else ("secrets" if secret_org else "unset"),
        "project_source": "override" if override_project else ("secrets" if secret_project else "unset"),
        "pat_source": "override" if override_pat else ("secrets" if secret_pat else "unset"),
        "work_item_type_source": "override"
        if override_work_item_type
        else ("secrets" if secret_work_item_type else "default"),
    }


def _devops_ui_enabled() -> bool:
    payload = _load_devops_settings()
    return bool(payload.get("resolved_enabled", False))


def _build_effective_devops_config(
    user: dict[str, str],
    *,
    require_access: bool = True,
) -> tuple[DevOpsConfig | None, str | None]:
    role = str(user.get("role") or "").strip().casefold()
    if role not in {"admin", "assignee"}:
        return None, "DevOps er kun tilgjengelig for Assignee og Admin."
    if require_access:
        access_allowed, access_reason = _devops_access_state(user)
        if not access_allowed:
            return None, access_reason
    payload = _load_devops_settings()
    org = str(payload.get("resolved_org") or "").strip()
    project = str(payload.get("resolved_project") or "").strip()
    pat = str(payload.get("resolved_pat") or "").strip()
    work_item_type = str(payload.get("resolved_work_item_type") or "Task").strip() or "Task"
    if not org or not project or not pat:
        return None, "DevOps mangler konfigurasjon. Sett org/prosjekt/PAT i Admin -> DevOps-innstillinger."
    return DevOpsConfig(org=org, project=project, pat=pat, work_item_type=work_item_type), None


def _save_devops_settings(
    user: dict[str, str],
    *,
    enabled: bool | None = None,
    org: str,
    project: str,
    pat: str,
    work_item_type: str = "Task",
    keep_existing_pat: bool = True,
) -> str | None:
    access_allowed, access_reason = _devops_admin_manage_access_state(user)
    if not access_allowed:
        return access_reason

    org_value = str(org or "").strip()
    project_value = str(project or "").strip()
    pat_value = str(pat or "").strip()
    work_item_type_value = str(work_item_type or "Task").strip() or "Task"
    try:
        with db_session() as db:
            if enabled is not None:
                _runtime_meta_set(db, _DEVOPS_META_KEYS["enabled"], "true" if bool(enabled) else "false")
            if org_value:
                _runtime_meta_set(db, _DEVOPS_META_KEYS["org"], org_value)
            else:
                _runtime_meta_delete(db, _DEVOPS_META_KEYS["org"])

            if project_value:
                _runtime_meta_set(db, _DEVOPS_META_KEYS["project"], project_value)
            else:
                _runtime_meta_delete(db, _DEVOPS_META_KEYS["project"])

            if pat_value:
                _runtime_meta_set(db, _DEVOPS_META_KEYS["pat"], pat_value)
            elif not keep_existing_pat:
                _runtime_meta_delete(db, _DEVOPS_META_KEYS["pat"])

            _runtime_meta_set(db, _DEVOPS_META_KEYS["work_item_type"], work_item_type_value)

            _commit_with_retry(db, operation="Lagring av DevOps-innstillinger")
    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:
        return format_user_error("Kunne ikke lagre DevOps-innstillinger", exc, fallback="Prøv igjen.")
    return None


def _reset_devops_settings(user: dict[str, str]) -> str | None:
    access_allowed, access_reason = _devops_admin_manage_access_state(user)
    if not access_allowed:
        return access_reason
    try:
        with db_session() as db:
            _runtime_meta_delete(db, _DEVOPS_META_KEYS["enabled"])
            _runtime_meta_delete(db, _DEVOPS_META_KEYS["org"])
            _runtime_meta_delete(db, _DEVOPS_META_KEYS["project"])
            _runtime_meta_delete(db, _DEVOPS_META_KEYS["pat"])
            _runtime_meta_delete(db, _DEVOPS_META_KEYS["work_item_type"])
            _commit_with_retry(db, operation="Nullstilling av DevOps-innstillinger")
    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:
        return format_user_error("Kunne ikke nullstille DevOps-innstillinger", exc, fallback="Prøv igjen.")
    return None


def _test_devops_settings(user: dict[str, str]) -> tuple[bool, str]:
    access_allowed, access_reason = _devops_admin_manage_access_state(user)
    if not access_allowed:
        return False, access_reason
    config, config_error = _build_effective_devops_config(user, require_access=False)
    if config is None:
        return False, str(config_error or "DevOps er ikke konfigurert.")
    return test_devops_connection(config)


def _devops_html_to_text(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    text_value = re.sub(r"(?i)<br\\s*/?>", "\n", raw)
    text_value = re.sub(r"(?i)</p>", "\n", text_value)
    text_value = re.sub(r"<[^>]+>", "", text_value)
    text_value = html.unescape(text_value)
    text_value = re.sub(r"[ \t]+\n", "\n", text_value)
    text_value = re.sub(r"\n{3,}", "\n\n", text_value)
    return text_value.strip()


def _devops_normalize_tags(value: Any) -> str:
    entries = [str(item).strip() for item in re.split(r"[;,]+", str(value or "")) if str(item).strip()]
    deduped: list[str] = []
    seen: set[str] = set()
    for item in entries:
        lowered = item.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(item)
    return ", ".join(deduped)


def _map_devops_state_to_local_status(value: Any) -> str:
    state = str(value or "").strip().casefold()
    if state in {"resolved", "closed", "done", "removed", "completed"}:
        return "resolved"
    return "open"


def _map_devops_severity_to_local(value: Any) -> str:
    severity_text = str(value or "").strip().casefold()
    if not severity_text:
        return "medium"
    if severity_text.startswith("1") or "critical" in severity_text:
        return "critical"
    if severity_text.startswith("2") or "high" in severity_text:
        return "high"
    if severity_text.startswith("4") or "low" in severity_text:
        return "low"
    return "medium"


def _extract_devops_assignee_email(value: Any) -> str | None:
    if isinstance(value, dict):
        candidates = [
            value.get("uniqueName"),
            value.get("mailAddress"),
            value.get("email"),
            value.get("descriptor"),
            value.get("displayName"),
        ]
        for candidate in candidates:
            normalized = _extract_devops_assignee_email(candidate)
            if normalized:
                return normalized
        return None

    text_value = str(value or "").strip()
    if not text_value:
        return None
    match = re.search(r"<([^>]+)>", text_value)
    if match:
        text_value = match.group(1).strip()
    normalized = _normalize_email(text_value)
    if not _is_valid_email(normalized):
        return None
    return normalized


def _extract_devops_remote_values(fields: Mapping[str, Any] | Any) -> dict[str, Any]:
    payload = fields if isinstance(fields, Mapping) else {}
    return {
        "title": str(payload.get("System.Title") or "").strip(),
        "description": _devops_html_to_text(payload.get("System.Description")),
        "status": _map_devops_state_to_local_status(payload.get("System.State")),
        "severity": _map_devops_severity_to_local(payload.get("Microsoft.VSTS.Common.Severity")),
        "assignee_id": _extract_devops_assignee_email(payload.get("System.AssignedTo")),
        "tags": _devops_normalize_tags(payload.get("System.Tags")),
    }


def _fetch_bug_from_devops(user: dict[str, str], *, bug_id: int) -> tuple[str | None, dict[str, Any] | None]:
    role = str(user.get("role") or "").strip().casefold()
    if role not in {"admin", "assignee"}:
        return "DevOps er kun tilgjengelig for Assignee og Admin.", None
    config, config_error = _build_effective_devops_config(user, require_access=True)
    if config is None:
        return config_error or "DevOps er ikke tilgjengelig.", None

    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if bug is None:
                return "Fant ikke bug.", None
            if _is_deleted_bug(bug):
                return "Bugen ligger i papirkurv og kan ikke synkroniseres med DevOps.", None
            if not bug.ado_work_item_id:
                return "Bugen er ikke koblet til DevOps.", None

            work_item_id = int(bug.ado_work_item_id)
            payload = fetch_bug_work_item(config, work_item_id=work_item_id)
            fields = payload.get("fields")
            remote_values: dict[str, Any] = _extract_devops_remote_values(fields)
            local_values: dict[str, Any] = {
                "title": str(bug.title or "").strip(),
                "description": str(bug.description or "").strip(),
                "status": normalize_bug_status(bug.status),
                "severity": str(bug.severity or "medium").strip().casefold(),
                "assignee_id": _normalize_email(bug.assignee_id),
                "tags": _devops_normalize_tags(bug.tags),
            }

            if not remote_values["title"]:
                remote_values["title"] = local_values["title"]
            if not remote_values["description"]:
                remote_values["description"] = local_values["description"]

            def _norm_text(value: Any) -> str:
                return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

            compare_order = [
                ("title", "Tittel"),
                ("description", "Beskrivelse"),
                ("status", "Status"),
                ("severity", "Alvorlighetsgrad"),
                ("assignee_id", "Tildelt"),
                ("tags", "Tagger"),
            ]
            changes: list[dict[str, str]] = []
            for field_key, label in compare_order:
                local_value = local_values.get(field_key)
                remote_value = remote_values.get(field_key)
                is_same = False
                if field_key in {"status", "severity", "assignee_id", "tags"}:
                    is_same = _norm_text(local_value) == _norm_text(remote_value)
                else:
                    is_same = _norm_text(local_value) == _norm_text(remote_value)
                if is_same:
                    continue
                changes.append(
                    {
                        "field": label,
                        "local": str(local_value or "").strip() or "-",
                        "devops": str(remote_value or "").strip() or "-",
                    }
                )

            snapshot = {
                "work_item_id": work_item_id,
                "work_item_url": str(payload.get("url") or bug.ado_work_item_url or "").strip(),
                "pulled_at": datetime.now(timezone.utc).isoformat(),
                "values": remote_values,
                "changes": changes,
                "raw_state": str(((fields or {}).get("System.State") if isinstance(fields, Mapping) else "") or "").strip(),
                "raw_severity": str(
                    ((fields or {}).get("Microsoft.VSTS.Common.Severity") if isinstance(fields, Mapping) else "") or ""
                ).strip(),
            }
            return None, snapshot
    except RuntimeError as exc:
        return str(exc), None
    except Exception as exc:
        return (
            format_user_error(
                "Kunne ikke hente work item fra DevOps",
                exc,
                fallback="Sjekk DevOps-oppsettet og prøv igjen.",
            ),
            None,
        )


def _apply_devops_snapshot_to_local_bug(user: dict[str, str], *, bug_id: int, snapshot: dict[str, Any]) -> str | None:
    role = str(user.get("role") or "").strip().casefold()
    if role not in {"admin", "assignee"}:
        return "DevOps er kun tilgjengelig for Assignee og Admin."
    if not isinstance(snapshot, dict):
        return "Mangler gyldig DevOps-snapshot."

    work_item_id = int(snapshot.get("work_item_id") or 0)
    if work_item_id <= 0:
        return "Mangler gyldig work item-id i DevOps-snapshot."

    values = snapshot.get("values")
    if not isinstance(values, dict):
        return "Mangler verdier i DevOps-snapshot."

    title_value = str(values.get("title") or "").strip()
    description_value = str(values.get("description") or "").strip()
    status_value = normalize_bug_status(values.get("status"))
    severity_value = str(values.get("severity") or "").strip().casefold()
    assignee_value = _normalize_email(values.get("assignee_id"))
    tags_value = _devops_normalize_tags(values.get("tags"))

    if status_value not in set(STATUS_OPTIONS):
        status_value = "open"
    if severity_value not in set(SEVERITY_OPTIONS):
        severity_value = "medium"
    if assignee_value and not _is_valid_email(assignee_value):
        return "DevOps returnerte en ugyldig tildelt e-postadresse."

    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if bug is None:
                return "Fant ikke bug."
            if _is_deleted_bug(bug):
                return "Bugen ligger i papirkurv og kan ikke oppdateres."
            if not bug.ado_work_item_id:
                return "Bugen er ikke koblet til DevOps."
            if int(bug.ado_work_item_id) != work_item_id:
                return (
                    f"Work item-id mismatch: lokal kobling peker på #{int(bug.ado_work_item_id)}, "
                    f"mens snapshot er #{work_item_id}. Hent på nytt fra DevOps."
                )

            changed_fields: list[str] = []
            if title_value and str(bug.title or "").strip() != title_value:
                bug.title = title_value
                changed_fields.append("title")
            if description_value and str(bug.description or "").strip() != description_value:
                bug.description = description_value
                changed_fields.append("description")

            previous_status = normalize_bug_status(bug.status)
            if previous_status != status_value:
                bug.status = status_value
                changed_fields.append("status")
            if str(bug.severity or "").strip().casefold() != severity_value:
                bug.severity = severity_value
                changed_fields.append("severity")

            previous_assignee = _normalize_email(bug.assignee_id)
            if previous_assignee != assignee_value:
                if assignee_value:
                    _ensure_user_exists(db, email=assignee_value, role=_role_for_email(assignee_value))
                bug.assignee_id = assignee_value or None
                changed_fields.append("assignee_id")

            previous_tags = _devops_normalize_tags(bug.tags)
            if previous_tags != tags_value:
                bug.tags = tags_value or None
                changed_fields.append("tags")

            if status_value == "resolved":
                if bug.closed_at is None:
                    bug.closed_at = datetime.now(timezone.utc)
            else:
                bug.closed_at = None

            snapshot_url = str(snapshot.get("work_item_url") or "").strip()
            if snapshot_url and str(bug.ado_work_item_url or "").strip() != snapshot_url:
                bug.ado_work_item_url = snapshot_url
                if "ado_work_item_url" not in changed_fields:
                    changed_fields.append("ado_work_item_url")
            bug.ado_sync_status = "synced"
            bug.ado_synced_at = datetime.now(timezone.utc)

            details = f"Lokal bug oppdatert fra DevOps work item #{work_item_id}."
            if changed_fields:
                details = f"{details} Felter: {', '.join(sorted(set(changed_fields)))}."
            else:
                details = f"{details} Ingen feltendringer."
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="devops_pulled",
                details=details,
            )
            _mark_bug_search_index_dirty(db, bug_id=bug.id)
            _commit_with_retry(db, operation="Oppdatering av lokal bug fra DevOps")
    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:
        return format_user_error(
            "Kunne ikke oppdatere lokal bug fra DevOps",
            exc,
            fallback="Prøv igjen.",
        )

    _clear_bug_cache()
    return None


def _send_bug_to_devops(user: dict[str, str], *, bug_id: int) -> tuple[str | None, str | None]:
    role = str(user.get("role") or "").strip().casefold()
    if role not in {"admin", "assignee"}:
        return "DevOps er kun tilgjengelig for Assignee og Admin.", None
    config, config_error = _build_effective_devops_config(user, require_access=True)
    if config is None:
        return config_error or "DevOps er ikke tilgjengelig.", None

    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if bug is None:
                return "Fant ikke bug.", None
            if _is_deleted_bug(bug):
                return "Bugen ligger i papirkurv og kan ikke sendes til DevOps.", None
            if bug.ado_work_item_id:
                return "Bugen er allerede sendt til DevOps.", str(bug.ado_work_item_url or "")

            work_item_id, work_item_url, selected_work_item_type = create_bug_work_item(
                config,
                title=str(bug.title or "").strip() or f"Bug #{bug.id}",
                description=str(bug.description or "").strip(),
                severity=str(bug.severity or "medium"),
                tags=str(bug.tags or "").strip() or None,
                assignee_email=str(bug.assignee_id or "").strip() or None,
                reporter_email=str(bug.reporter_id or "").strip() or None,
                local_bug_id=int(bug.id),
                work_item_type=str(config.work_item_type or "auto"),
            )

            bug.ado_work_item_id = int(work_item_id)
            bug.ado_work_item_url = str(work_item_url).strip() or None
            bug.ado_sync_status = "synced"
            bug.ado_synced_at = datetime.now(timezone.utc)

            details = (
                f"Bug sendt til Azure DevOps work item #{work_item_id} "
                f"(type: {selected_work_item_type})."
            )
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="devops_synced",
                details=details,
            )
            _commit_with_retry(db, operation="Synkronisering mot DevOps")
    except RuntimeError as exc:
        return str(exc), None
    except Exception as exc:
        return format_user_error("Kunne ikke sende bug til DevOps", exc, fallback="Sjekk DevOps-oppsettet og prøv igjen."), None

    _clear_bug_cache()
    return None, work_item_url


def _update_bug_in_devops(
    user: dict[str, str],
    *,
    bug_id: int,
    status: str | None = None,
    severity: str | None = None,
    assignee_id: str | None = None,
    tags: str | None = None,
    comment_text: str | None = None,
    changed_fields: list[str] | None = None,
) -> tuple[str | None, str | None]:
    role = str(user.get("role") or "").strip().casefold()
    if role not in {"admin", "assignee"}:
        return "DevOps er kun tilgjengelig for Assignee og Admin.", None
    config, config_error = _build_effective_devops_config(user, require_access=True)
    if config is None:
        return config_error or "DevOps er ikke tilgjengelig.", None

    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if bug is None:
                return "Fant ikke bug.", None
            if _is_deleted_bug(bug):
                return "Bugen ligger i papirkurv og kan ikke synkroniseres med DevOps.", None
            if not bug.ado_work_item_id:
                return "Bugen er ikke sendt til DevOps ennå.", None

            status_value = normalize_bug_status(status or bug.status)
            severity_value = str(severity or bug.severity or "medium").strip().casefold()
            if severity_value not in set(SEVERITY_OPTIONS):
                severity_value = str(bug.severity or "medium").strip().casefold()
                if severity_value not in set(SEVERITY_OPTIONS):
                    severity_value = "medium"
            assignee_value = (assignee_id if assignee_id is not None else bug.assignee_id) or ""
            assignee_value = str(assignee_value).strip().casefold() or None
            tags_value = bug.tags if tags is None else tags
            comment_value = str(comment_text or "").strip() or None

            work_item_id, work_item_url = update_bug_work_item(
                config,
                work_item_id=int(bug.ado_work_item_id),
                title=str(bug.title or "").strip() or f"Bug #{bug.id}",
                description=str(bug.description or "").strip(),
                severity=severity_value,
                status=status_value,
                tags=str(tags_value or "").strip() or None,
                assignee_email=assignee_value,
                changed_fields=[str(item).strip() for item in (changed_fields or []) if str(item).strip()] or None,
                comment_text=comment_value,
            )

            bug.ado_work_item_url = str(work_item_url).strip() or bug.ado_work_item_url
            bug.ado_sync_status = "synced"
            bug.ado_synced_at = datetime.now(timezone.utc)

            details_parts = [f"Bug oppdatert i Azure DevOps work item #{work_item_id}."]
            if changed_fields:
                cleaned_fields = [str(item).strip() for item in changed_fields if str(item).strip()]
                if cleaned_fields:
                    details_parts.append(f"Felter: {', '.join(sorted(set(cleaned_fields)))}.")
            if comment_value:
                details_parts.append("Kommentar sendt til DevOps.")
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="devops_updated",
                details=" ".join(details_parts),
            )
            _commit_with_retry(db, operation="Oppdatering i DevOps")
    except RuntimeError as exc:
        return str(exc), None
    except Exception as exc:
        return (
            format_user_error(
                "Kunne ikke oppdatere bug i DevOps",
                exc,
                fallback="Sjekk DevOps-oppsettet og prøv igjen.",
            ),
            None,
        )

    _clear_bug_cache()
    return None, work_item_url


def _is_devops_delete_permission_or_policy_error(message: str) -> bool:
    lowered = str(message or "").strip().casefold()
    if not lowered:
        return False
    indicators = (
        "vs403145",
        "insufficient permissions to delete",
        "does not have permissions to delete",
        "permission to delete work item",
        "permission",
        "access denied",
        "forbidden",
        "mangler rettighet",
        "avviste sletting",
    )
    return any(token in lowered for token in indicators)


def _remove_bug_from_devops(user: dict[str, str], *, bug_id: int) -> tuple[str | None, str | None]:
    role = str(user.get("role") or "").strip().casefold()
    if role not in {"admin", "assignee"}:
        return "DevOps er kun tilgjengelig for Assignee og Admin.", None
    config, config_error = _build_effective_devops_config(user, require_access=True)
    if config is None:
        return config_error or "DevOps er ikke tilgjengelig.", None

    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if bug is None:
                return "Fant ikke bug.", None
            if _is_deleted_bug(bug):
                return "Bugen ligger i papirkurv og kan ikke oppdateres.", None
            if not bug.ado_work_item_id:
                return "Bugen er ikke koblet til DevOps.", None

            previous_work_item_id = int(bug.ado_work_item_id)
            try:
                remove_bug_work_item(config, work_item_id=previous_work_item_id)
            except RuntimeError as exc:
                remove_error = str(exc).strip()
                if not _is_devops_delete_permission_or_policy_error(remove_error):
                    raise

                bug.ado_work_item_id = None
                bug.ado_work_item_url = None
                bug.ado_sync_status = "unlinked_local"
                bug.ado_synced_at = datetime.now(timezone.utc)
                _write_history(
                    db,
                    bug_id=bug.id,
                    actor_email=user["email"],
                    action="devops_unlinked_local",
                    details=(
                        f"Lokal DevOps-kobling til work item #{previous_work_item_id} ble fjernet automatisk "
                        "etter mislykket sletting i DevOps. "
                        f"Årsak: {remove_error[:400]}"
                    ),
                )
                _commit_with_retry(db, operation="Lokal frakobling etter feilet DevOps-sletting")
                _clear_bug_cache()
                return (
                    None,
                    (
                        f"Sletting i DevOps ble avvist. Lokal kobling ble fjernet automatisk, "
                        f"men work item #{previous_work_item_id} finnes fortsatt i DevOps."
                    ),
                )

            bug.ado_work_item_id = None
            bug.ado_work_item_url = None
            bug.ado_sync_status = "removed"
            bug.ado_synced_at = datetime.now(timezone.utc)
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="devops_removed",
                details=f"Kobling mot Azure DevOps work item #{previous_work_item_id} ble fjernet.",
            )
            _commit_with_retry(db, operation="Fjerning fra DevOps")
    except RuntimeError as exc:
        return str(exc), None
    except Exception as exc:
        return (
            format_user_error(
                "Kunne ikke fjerne bug fra DevOps",
                exc,
                fallback="Sjekk DevOps-oppsettet og prøv igjen.",
            ),
            None,
        )

    _clear_bug_cache()
    return None, "Bug fjernet fra DevOps."


def _unlink_bug_from_devops_locally(user: dict[str, str], *, bug_id: int) -> str | None:
    role = str(user.get("role") or "").strip().casefold()
    if role not in {"admin", "assignee"}:
        return "DevOps er kun tilgjengelig for Assignee og Admin."
    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if bug is None:
                return "Fant ikke bug."
            if _is_deleted_bug(bug):
                return "Bugen ligger i papirkurv og kan ikke oppdateres."
            if not bug.ado_work_item_id:
                return "Bugen er ikke koblet til DevOps."

            previous_work_item_id = int(bug.ado_work_item_id)
            bug.ado_work_item_id = None
            bug.ado_work_item_url = None
            bug.ado_sync_status = "unlinked_local"
            bug.ado_synced_at = datetime.now(timezone.utc)
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="devops_unlinked_local",
                details=(
                    f"Lokal DevOps-kobling til work item #{previous_work_item_id} ble fjernet "
                    "uten sletting i Azure DevOps."
                ),
            )
            _commit_with_retry(db, operation="Lokal frakobling fra DevOps")
    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:
        return format_user_error(
            "Kunne ikke frakoble DevOps-lenke lokalt",
            exc,
            fallback="Prøv igjen.",
        )

    _clear_bug_cache()
    return None


_SLA_DEFAULT_HOURS: dict[str, int] = {
    "critical": 8,
    "high": 24,
    "medium": 72,
    "low": 168,
}


def _load_sla_hours(*, force_refresh: bool = False) -> dict[str, int]:
    cache_key = "_sla_hours_cache"
    cached = st.session_state.get(cache_key)
    if not force_refresh and isinstance(cached, dict):
        normalized_cached = {
            severity: int(cached.get(severity, default))
            for severity, default in _SLA_DEFAULT_HOURS.items()
        }
        return normalized_cached

    defaults = dict(_SLA_DEFAULT_HOURS)
    try:
        with db_session() as db:
            for severity, default_hours in _SLA_DEFAULT_HOURS.items():
                raw = _runtime_meta_get(db, f"sla.hours.{severity}", str(default_hours))
                try:
                    parsed = int(str(raw).strip())
                except (TypeError, ValueError):
                    parsed = default_hours
                defaults[severity] = max(1, min(24 * 365, parsed))
    except Exception:
        defaults = dict(_SLA_DEFAULT_HOURS)
    st.session_state[cache_key] = dict(defaults)
    return defaults


def _save_sla_hours(new_values: Mapping[str, Any]) -> str | None:
    normalized: dict[str, int] = {}
    for severity, default_hours in _SLA_DEFAULT_HOURS.items():
        raw = new_values.get(severity, default_hours)
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            parsed = default_hours
        normalized[severity] = max(1, min(24 * 365, parsed))
    try:
        with db_session() as db:
            for severity, hours in normalized.items():
                _runtime_meta_set(db, f"sla.hours.{severity}", str(hours))
            _commit_with_retry(db, operation="Lagring av SLA-regler")
    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:
        return format_user_error("Kunne ikke lagre SLA-regler", exc, fallback="Prøv igjen.")
    st.session_state["_sla_hours_cache"] = dict(normalized)
    return None


def _bug_sla_snapshot(bug: Bug) -> dict[str, Any]:
    severity = str(getattr(bug, "severity", "") or "").strip().casefold()
    if severity not in _SLA_DEFAULT_HOURS:
        severity = "medium"
    sla_hours = _load_sla_hours()
    hours = int(sla_hours.get(severity, _SLA_DEFAULT_HOURS["medium"]))

    created_at = bug.created_at
    if created_at is None:
        return {"severity": severity, "hours": hours, "due_at": None, "overdue": False, "remaining_hours": None}
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    due_at = created_at + timedelta(hours=hours)

    is_resolved = normalize_bug_status(str(bug.status or "")) == "resolved"
    if is_resolved:
        return {"severity": severity, "hours": hours, "due_at": due_at, "overdue": False, "remaining_hours": None}

    now = datetime.now(timezone.utc)
    remaining_hours = (due_at - now).total_seconds() / 3600.0
    overdue = remaining_hours < 0
    return {
        "severity": severity,
        "hours": hours,
        "due_at": due_at,
        "overdue": overdue,
        "remaining_hours": remaining_hours,
    }


def _sla_brief_label(bug: Bug) -> str:
    snapshot = _bug_sla_snapshot(bug)
    due_at = snapshot.get("due_at")
    if not isinstance(due_at, datetime):
        return "SLA: -"
    is_resolved = normalize_bug_status(str(bug.status or "")) == "resolved"
    if is_resolved:
        return f"SLA: {format_datetime_display(due_at)}"
    if bool(snapshot.get("overdue")):
        overdue_hours = abs(float(snapshot.get("remaining_hours") or 0.0))
        return f"SLA: BRUDD ({round(overdue_hours, 1)} t over)"
    remaining = float(snapshot.get("remaining_hours") or 0.0)
    return f"SLA: {round(max(0.0, remaining), 1)} t igjen"


def _filter_view_storage_key(*, user_email: str, prefix: str) -> str:
    return f"filters.views.{_normalize_email(user_email)}.{str(prefix).strip().casefold()}"


def _filter_state_keys(prefix: str) -> list[str]:
    keys = [
        f"{prefix}_search_query",
        f"{prefix}_filter_status_mode",
        f"{prefix}_filter_severity",
        f"{prefix}_filter_tags",
        f"{prefix}_sort_mode",
        f"{prefix}_queue_status",
        f"{prefix}_queue_critical_only",
        f"{prefix}_queue_negative_only",
        f"{prefix}_queue_stale_only",
        f"{prefix}_queue_unassigned_only",
        f"{prefix}_visible_count",
    ]
    if prefix == "admin":
        keys.extend(
            [
                "admin_created_from",
                "admin_sentiment_filter",
                "admin_only_unassigned",
                "admin_reporter_contains",
                "admin_satisfaction_filter",
            ]
        )
    return keys


def _capture_filter_state(prefix: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    for key in _filter_state_keys(prefix):
        value = st.session_state.get(key)
        if isinstance(value, (str, int, float, bool)):
            captured[key] = value
        elif isinstance(value, list):
            captured[key] = list(value)
    return captured


def _apply_filter_state(state: Mapping[str, Any]) -> None:
    for key, value in state.items():
        if isinstance(value, list):
            st.session_state[key] = list(value)
        else:
            st.session_state[key] = value


def _load_filter_views(*, user_email: str, prefix: str) -> dict[str, dict[str, Any]]:
    storage_key = _filter_view_storage_key(user_email=user_email, prefix=prefix)
    try:
        with db_session() as db:
            raw = _runtime_meta_get(db, storage_key, "{}")
    except Exception:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    sanitized: dict[str, dict[str, Any]] = {}
    for name, state in payload.items():
        if not isinstance(name, str) or not isinstance(state, dict):
            continue
        state_map = {str(k): v for k, v in state.items() if isinstance(k, str)}
        sanitized[name] = state_map
    return sanitized


def _save_filter_views(*, user_email: str, prefix: str, views: Mapping[str, Mapping[str, Any]]) -> str | None:
    storage_key = _filter_view_storage_key(user_email=user_email, prefix=prefix)
    serializable: dict[str, dict[str, Any]] = {}
    for name, state in views.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(state, Mapping):
            continue
        serializable[name.strip()] = {str(k): v for k, v in state.items() if isinstance(k, str)}
    try:
        with db_session() as db:
            _runtime_meta_set(db, storage_key, json.dumps(serializable, ensure_ascii=False))
            _commit_with_retry(db, operation="Lagring av filtervisning")
    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:
        return format_user_error("Kunne ikke lagre filtervisning", exc, fallback="Prøv igjen.")
    return None


def _render_saved_filter_views_sidebar(*, user: dict[str, str], prefix: str) -> None:
    with st.sidebar.expander("Lagrede filter", expanded=False):
        views = _load_filter_views(user_email=user["email"], prefix=prefix)
        names = sorted(views.keys())
        if names:
            selected = st.selectbox(
                "Velg visning",
                options=names,
                key=f"{prefix}_saved_filter_selected",
                help="Last inn en lagret filtervisning for denne siden.",
            )
            load_col, delete_col = st.columns(2)
            with load_col:
                if st.button("Last inn", key=f"{prefix}_saved_filter_load", use_container_width=True):
                    state = views.get(selected, {})
                    if isinstance(state, dict):
                        _apply_filter_state(state)
                        st.rerun()
            with delete_col:
                if st.button("Slett", key=f"{prefix}_saved_filter_delete", use_container_width=True):
                    remaining = {name: value for name, value in views.items() if name != selected}
                    error = _save_filter_views(user_email=user["email"], prefix=prefix, views=remaining)
                    if error:
                        st.error(error)
                    else:
                        st.success("Filtervisning slettet.")
                        st.rerun()
        else:
            st.caption("Ingen lagrede filtervisninger ennå.")

        new_name = st.text_input(
            "Nytt navn",
            key=f"{prefix}_saved_filter_name",
            placeholder="F.eks. Mine kritiske åpne",
        )
        if st.button("Lagre nåværende filter", key=f"{prefix}_saved_filter_save", use_container_width=True):
            trimmed = str(new_name or "").strip()
            if len(trimmed) < 2:
                st.warning("Oppgi et navn med minst 2 tegn.")
            else:
                updated = dict(views)
                updated[trimmed] = _capture_filter_state(prefix)
                error = _save_filter_views(user_email=user["email"], prefix=prefix, views=updated)
                if error:
                    st.error(error)
                else:
                    st.success(f"Filtervisning lagret: {trimmed}")
                    st.rerun()


def _bugs_to_export_rows(bugs: list[Bug]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bug in bugs:
        sla = _bug_sla_snapshot(bug)
        rows.append(
            {
                "id": int(bug.id),
                "title": str(bug.title or ""),
                "status": normalize_bug_status(str(bug.status or "")),
                "severity": str(bug.severity or ""),
                "category": str(bug.category or ""),
                "reporter": str(bug.reporter_id or ""),
                "assignee": str(bug.assignee_id or ""),
                "created_at": format_datetime_display(bug.created_at),
                "updated_at": format_datetime_display(bug.updated_at),
                "closed_at": format_datetime_display(bug.closed_at),
                "sla_due_at": format_datetime_display(sla.get("due_at")),
                "sla_overdue": "yes" if bool(sla.get("overdue")) else "no",
                "tags": str(bug.tags or ""),
                "environment": str(bug.environment or ""),
            }
        )
    return rows


def _build_bug_export_csv_bytes(bugs: list[Bug]) -> bytes:
    rows = _bugs_to_export_rows(bugs)
    if not rows:
        rows = [{"id": "", "title": "", "status": ""}]
    fieldnames = list(rows[0].keys())
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


def _build_bug_export_excel_bytes(bugs: list[Bug]) -> bytes | None:
    try:
        from openpyxl import Workbook
    except Exception:
        return None

    rows = _bugs_to_export_rows(bugs)
    if not rows:
        rows = [{"id": "", "title": "", "status": ""}]
    fieldnames = list(rows[0].keys())

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "bugs"
    sheet.append(fieldnames)
    for row in rows:
        sheet.append([row.get(name, "") for name in fieldnames])

    blob = BytesIO()
    workbook.save(blob)
    return blob.getvalue()


def _render_bug_export_sidebar(*, prefix: str, bugs: list[Bug]) -> None:
    with st.sidebar.expander("Eksport", expanded=False):
        st.caption(f"Rader i gjeldende visning: {len(bugs)}")
        csv_bytes = _build_bug_export_csv_bytes(bugs)
        st.download_button(
            "Last ned CSV",
            data=csv_bytes,
            file_name=f"bugs_{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key=f"{prefix}_export_csv",
            use_container_width=True,
        )
        excel_bytes = _build_bug_export_excel_bytes(bugs)
        if excel_bytes:
            st.download_button(
                "Last ned Excel",
                data=excel_bytes,
                file_name=f"bugs_{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"{prefix}_export_excel",
                use_container_width=True,
            )
        else:
            st.caption("Excel-eksport er ikke tilgjengelig i dette miljøet (mangler openpyxl).")


def _start_of_week(value: datetime) -> date:
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    day = current.date()
    return day - timedelta(days=day.weekday())


def _render_admin_trend_report(bugs: list[Bug]) -> None:
    with st.expander("Rapporter og trender", expanded=False):
        if not bugs:
            st.caption("Ingen data å vise.")
            return

        weekly_opened: dict[date, int] = {}
        weekly_resolved: dict[date, int] = {}
        resolution_hours: list[float] = []

        for bug in bugs:
            if isinstance(bug.created_at, datetime):
                open_week = _start_of_week(bug.created_at)
                weekly_opened[open_week] = int(weekly_opened.get(open_week, 0)) + 1
            if isinstance(bug.closed_at, datetime):
                resolved_week = _start_of_week(bug.closed_at)
                weekly_resolved[resolved_week] = int(weekly_resolved.get(resolved_week, 0)) + 1
            if isinstance(bug.created_at, datetime) and isinstance(bug.closed_at, datetime):
                created_at = bug.created_at if bug.created_at.tzinfo else bug.created_at.replace(tzinfo=timezone.utc)
                closed_at = bug.closed_at if bug.closed_at.tzinfo else bug.closed_at.replace(tzinfo=timezone.utc)
                duration_h = max(0.0, (closed_at - created_at).total_seconds() / 3600.0)
                resolution_hours.append(duration_h)

        all_weeks = sorted(set(weekly_opened.keys()) | set(weekly_resolved.keys()))
        if not all_weeks:
            st.caption("Ingen trenddata tilgjengelig ennå.")
            return
        all_weeks = all_weeks[-12:]

        trend_rows = []
        for week in all_weeks:
            label = f"{week.isoformat()}"
            trend_rows.append(
                {
                    "uke": label,
                    "åpnet": int(weekly_opened.get(week, 0)),
                    "løst": int(weekly_resolved.get(week, 0)),
                }
            )

        st.caption("Åpnet/løst per uke (siste 12 uker)")
        st.dataframe(trend_rows, use_container_width=True, hide_index=True)
        st.line_chart(
            {
                "Åpnet": [row["åpnet"] for row in trend_rows],
                "Løst": [row["løst"] for row in trend_rows],
            },
            use_container_width=True,
        )

        resolved_count = len(resolution_hours)
        median_hours = median(resolution_hours) if resolution_hours else 0.0
        st.caption(
            f"Løsningstid: median {round(float(median_hours), 1)} timer "
            f"({resolved_count} løste bugs i valgt datasett)."
        )


def _load_admin_audit_rows(*, limit: int = 150, actor_contains: str = "", action_filter: str = "all") -> list[dict[str, Any]]:
    normalized_actor = str(actor_contains or "").strip().casefold()
    normalized_action = str(action_filter or "all").strip().casefold()
    rows_out: list[dict[str, Any]] = []
    with db_session() as db:
        query = (
            select(BugHistory, Bug.title, Bug.status)
            .join(Bug, Bug.id == BugHistory.bug_id, isouter=True)
            .order_by(BugHistory.created_at.desc(), BugHistory.id.desc())
            .limit(max(10, int(limit)))
        )
        results = db.execute(query).all()
    for history, bug_title, bug_status in results:
        actor = str(history.actor_email or "")
        action = str(history.action or "")
        if normalized_actor and normalized_actor not in actor.casefold():
            continue
        if normalized_action != "all" and normalized_action != action.casefold():
            continue
        rows_out.append(
            {
                "tid": format_datetime_display(history.created_at),
                "bug_id": int(history.bug_id),
                "tittel": str(bug_title or ""),
                "handling": action,
                "aktor": actor,
                "status": status_label(str(bug_status or "")),
                "detaljer": str(history.details or "")[:180],
            }
        )
    return rows_out


def _render_admin_audit_log_panel() -> None:
    with st.expander("Audit-logg", expanded=False):
        left, right, third = st.columns([1.2, 1.2, 1])
        with left:
            actor_contains = st.text_input(
                "Aktor inneholder",
                key="admin_audit_actor_contains",
                placeholder="f.eks. thomas.elboth",
            )
        with right:
            action_filter = st.selectbox(
                "Handling",
                options=[
                    "all",
                    "created",
                    "updated",
                    "comment_added",
                    "status_changed",
                    "soft_deleted",
                    "restored",
                    "devops_synced",
                    "devops_updated",
                    "devops_pulled",
                    "devops_removed",
                    "devops_unlinked_local",
                ],
                key="admin_audit_action_filter",
                format_func=lambda value: "Alle" if value == "all" else str(value),
            )
        with third:
            limit = st.selectbox("Antall", options=[50, 100, 150, 250], index=1, key="admin_audit_limit")

        rows = _load_admin_audit_rows(
            limit=int(limit),
            actor_contains=str(actor_contains or ""),
            action_filter=str(action_filter or "all"),
        )
        if not rows:
            st.caption("Ingen audit-hendelser for valgte filtre.")
            return
        st.dataframe(rows, use_container_width=True, hide_index=True)
def _policy_roles(policy_key: str, default_roles: set[str]) -> set[str]:
    try:
        with db_session() as db:
            raw = _runtime_meta_get(db, policy_key, ",".join(sorted(default_roles)))
    except Exception:
        raw = ",".join(sorted(default_roles))
    roles = {_normalize_email(item) for item in str(raw).split(",") if str(item).strip()}
    return {role for role in roles if role in {"admin", "assignee", "reporter"}}


def _set_policy_roles(policy_key: str, roles: set[str], default_roles: set[str]) -> str | None:
    normalized_roles = {str(item or "").strip().casefold() for item in roles if str(item or "").strip()}
    valid_roles = {role for role in normalized_roles if role in {"admin", "assignee", "reporter"}}
    if not valid_roles:
        valid_roles = set(default_roles)
    value = ",".join(sorted(valid_roles))
    try:
        with db_session() as db:
            _runtime_meta_set(db, policy_key, value)
            _commit_with_retry(db, operation="Lagring av policy")
    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:
        return format_user_error("Kunne ikke lagre policy", exc, fallback="Prøv igjen.")
    return None


def _policy_allows(*, policy_key: str, user_role: str, default_roles: set[str]) -> bool:
    allowed_roles = _policy_roles(policy_key, default_roles)
    return str(user_role or "").strip().casefold() in allowed_roles


def _can_user_delete_bug(user: dict[str, str]) -> bool:
    return _policy_allows(
        policy_key="policy.delete_roles",
        user_role=user.get("role", ""),
        default_roles={"admin", "assignee"},
    )


def _can_user_reopen_bug(user: dict[str, str]) -> bool:
    return _policy_allows(
        policy_key="policy.reopen_roles",
        user_role=user.get("role", ""),
        default_roles={"admin", "assignee", "reporter"},
    )


def _sqlite_db_path() -> Path | None:
    if not settings.database_is_sqlite:
        return None
    value = str(settings.database_url or "").strip()
    if not value.startswith("sqlite:///"):
        return None
    raw_path = value.replace("sqlite:///", "", 1)
    return Path(raw_path).expanduser().resolve()


def _build_backup_zip_bytes() -> tuple[bytes | None, str | None]:
    db_path = _sqlite_db_path()
    if db_path is None:
        return None, "Backup via UI støttes nå kun for SQLite-profil."
    if not db_path.exists():
        return None, f"Fant ikke SQLite-fil: {db_path}"

    tmp_dir = Path(tempfile.mkdtemp(prefix="cloudtest_backup_"))
    try:
        sqlite_copy = tmp_dir / "bug_tracker_cloud.db"
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(sqlite_copy))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        archive_io = BytesIO()
        with zipfile.ZipFile(archive_io, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(sqlite_copy, arcname="database/bug_tracker_cloud.db")
            attachment_root = settings.attachment_dir
            if attachment_root.exists():
                for path in attachment_root.rglob("*"):
                    if path.is_file():
                        rel = path.relative_to(attachment_root).as_posix()
                        zf.write(path, arcname=f"attachments/{rel}")
            zf.writestr(
                "metadata.json",
                json.dumps(
                    {
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "database_backend": settings.database_backend,
                        "db_file": "database/bug_tracker_cloud.db",
                        "attachments_root": "attachments/",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        archive_io.seek(0)
        return archive_io.getvalue(), None
    except Exception as exc:
        return None, format_user_error("Kunne ikke lage backup", exc, fallback="Prøv igjen.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _restore_from_backup_zip(uploaded_file) -> str | None:
    db_path = _sqlite_db_path()
    if db_path is None:
        return "Restore via UI støttes nå kun for SQLite-profil."
    if uploaded_file is None:
        return "Velg en backup-fil (.zip) først."
    payload = uploaded_file.getvalue()
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        return "Kunne ikke lese backup-filen."

    tmp_dir = Path(tempfile.mkdtemp(prefix="cloudtest_restore_"))
    db_tmp_path = tmp_dir / "db_restored.sqlite"
    attachments_tmp = tmp_dir / "attachments"
    try:
        with zipfile.ZipFile(BytesIO(bytes(payload)), "r") as zf:
            names = set(zf.namelist())
            if "database/bug_tracker_cloud.db" not in names:
                return "Backup mangler database/bug_tracker_cloud.db."
            db_tmp_path.write_bytes(zf.read("database/bug_tracker_cloud.db"))
            for name in names:
                if not name.startswith("attachments/") or name.endswith("/"):
                    continue
                relative = name.removeprefix("attachments/").strip("/")
                if not relative:
                    continue
                target = (attachments_tmp / relative).resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(name))

        backup_current = db_path.with_suffix(f"{db_path.suffix}.pre_restore.bak")
        try:
            if db_path.exists():
                shutil.copy2(db_path, backup_current)
        except Exception:
            pass

        engine.dispose()
        for suffix in ("-wal", "-shm"):
            extra = Path(f"{db_path}{suffix}")
            if extra.exists():
                try:
                    extra.unlink()
                except Exception:
                    pass
        shutil.copy2(db_tmp_path, db_path)

        attachment_root = settings.attachment_dir
        if attachment_root.exists():
            shutil.rmtree(attachment_root, ignore_errors=True)
        attachment_root.mkdir(parents=True, exist_ok=True)
        if attachments_tmp.exists():
            for src_path in attachments_tmp.rglob("*"):
                if not src_path.is_file():
                    continue
                rel = src_path.relative_to(attachments_tmp)
                dst_path = (attachment_root / rel)
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dst_path)

        _clear_bug_cache()
        return None
    except zipfile.BadZipFile:
        return "Ugyldig backup-fil. Forventet .zip."
    except Exception as exc:
        return format_user_error("Restore feilet", exc, fallback="Kunne ikke gjenopprette backup.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _mark_bug_search_index_dirty(db, *, bug_id: int) -> None:
    embedding_provider: str | None = None
    embedding_model: str | None = None
    try:
        settings_payload = _current_search_settings()
        if isinstance(settings_payload, dict):
            embedding_provider = str(settings_payload.get("embedding_provider") or "").strip() or None
            embedding_model = str(settings_payload.get("embedding_model") or "").strip() or None
    except Exception:
        embedding_provider = None
        embedding_model = None

    try:
        db.flush()
        mark_bug_search_index_dirty_by_id(
            db,
            bug_id=bug_id,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
        )
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        logger.warning("Failed to mark bug_search_index as dirty for bug_id=%s: %s", bug_id, exc)


def _admin_emails() -> set[str]:
    configured = {
        item.strip().casefold()
        for item in _config_value("ENTRA_ADMIN_EMAILS").split(",")
        if item.strip()
    }
    default_admin = _config_value("DEFAULT_ADMIN_EMAIL", str(settings.default_admin_email)).strip().casefold()
    if default_admin:
        configured.add(default_admin)
    return {value for value in configured if value}


def _assignee_emails() -> set[str]:
    return {
        item.strip().casefold()
        for item in _config_value("ENTRA_ASSIGNEE_EMAILS").split(",")
        if item.strip()
    }


def _db_role_for_email(email: str) -> str | None:
    normalized = _normalize_email(email)
    if not normalized:
        return None
    try:
        with db_session() as db:
            user = db.get(User, normalized)
            if not user:
                return None
            role = str(user.role or "").strip().casefold()
            if role in {"reporter", "assignee", "admin"}:
                return role
    except SQLAlchemyError as exc:
        logger.warning("Failed to resolve role for email=%s: %s", normalized, exc.__class__.__name__)
        return None
    return None


def _role_for_email(email: str) -> str:
    normalized = str(email or "").strip().casefold()
    if normalized in _admin_emails():
        return "admin"
    db_role = _db_role_for_email(normalized)
    if db_role:
        return db_role
    if normalized in _assignee_emails():
        return "assignee"
    return "reporter"


def _db_admin_emails() -> set[str]:
    try:
        with db_session() as db:
            rows = db.execute(select(User.email).where(User.role == "admin")).all()
        return {
            _normalize_email(email)
            for (email,) in rows
            if _normalize_email(email)
        }
    except SQLAlchemyError as exc:
        logger.warning("Failed to load admin emails from DB: %s", exc.__class__.__name__)
        return set()


def _grant_admin_access(email: str) -> str | None:
    normalized = _normalize_email(email)
    if not _is_valid_email(normalized):
        return "Oppgi en gyldig e-postadresse."
    try:
        with db_session() as db:
            existing = db.get(User, normalized)
            if existing:
                existing.role = "admin"
                if not existing.auth_provider:
                    existing.auth_provider = "entra"
            else:
                db.add(
                    User(
                        email=normalized,
                        full_name=normalized.split("@", 1)[0],
                        password_hash=get_password_hash(str(uuid4())),
                        role="admin",
                        auth_provider="entra",
                    )
                )
            _commit_with_retry(db, operation="Lagring av admin-tilgang")
    except RuntimeError as exc:
        return str(exc)
    except SQLAlchemyError as exc:
        logger.error("Failed to grant admin access for email=%s: %s", normalized, exc)
        return f"Kunne ikke lagre admin-tilgang: {exc}"
    return None


def _set_user_role(email: str, role: str) -> str | None:
    normalized = _normalize_email(email)
    desired_role = str(role or "").strip().casefold()
    if not _is_valid_email(normalized):
        return "Oppgi en gyldig e-postadresse."
    if desired_role not in {"reporter", "assignee", "admin"}:
        return "Ugyldig rolle."
    try:
        with db_session() as db:
            existing = db.get(User, normalized)
            if existing:
                existing.role = desired_role
                if not existing.auth_provider:
                    existing.auth_provider = "entra"
            else:
                db.add(
                    User(
                        email=normalized,
                        full_name=normalized.split("@", 1)[0],
                        password_hash=get_password_hash(str(uuid4())),
                        role=desired_role,
                        auth_provider="entra",
                    )
                )
            _commit_with_retry(db, operation="Oppdatering av rolle")
    except RuntimeError as exc:
        return str(exc)
    except SQLAlchemyError as exc:
        logger.error("Failed to set role for email=%s role=%s: %s", normalized, desired_role, exc)
        return format_user_error("Kunne ikke lagre rolle", exc, fallback="Prøv igjen.")
    return None


def _list_users_with_roles() -> list[tuple[str, str, str]]:
    try:
        with db_session() as db:
            rows = db.execute(select(User.email, User.role, User.auth_provider).order_by(User.role.asc(), User.email.asc())).all()
        return [(str(email), str(role), str(auth_provider or "")) for email, role, auth_provider in rows]
    except SQLAlchemyError as exc:
        logger.warning("Failed to list users: %s", exc.__class__.__name__)
        return []


def _render_admin_access_management_sidebar(current_admin_email: str) -> None:
    with st.sidebar.expander("Admin-tilganger", expanded=False):
        st.caption("Legg til flere admin-brukere. Endringen gjelder fra neste innlogging.")
        new_admin_email = st.text_input(
            "Ny admin e-post",
            key="admin_access_new_email",
            placeholder="navn@domene.no",
            help="Brukeren får tilgang til Admin-siden når vedkommende logger inn på nytt.",
        )
        if st.button("Legg til admin", key="admin_access_add_btn", use_container_width=True):
            error = _grant_admin_access(new_admin_email)
            if error:
                st.error(error)
            else:
                st.success(f"{_normalize_email(new_admin_email)} er lagt til som admin.")
                _clear_bug_cache()
                st.rerun()

        active_admins = sorted(_admin_emails() | _db_admin_emails())
        st.caption("Aktive admin-brukere")
        if not active_admins:
            st.write("- Ingen")
            return
        current_norm = _normalize_email(current_admin_email)
        for email in active_admins:
            suffix = " (deg)" if email == current_norm else ""
            st.write(f"- {email}{suffix}")

        st.divider()
        st.caption("Rollehåndtering")
        users = _list_users_with_roles()
        if users:
            user_options = [email for email, _role, _auth in users]
            selected_user = st.selectbox(
                "Bruker",
                options=user_options,
                key="admin_role_selected_user",
                help="Velg bruker for å oppdatere rolle.",
            )
            role_map = {email: role for email, role, _auth in users}
            current_role = role_map.get(selected_user, "reporter")
            target_role = st.selectbox(
                "Ny rolle",
                options=["reporter", "assignee", "admin"],
                index=["reporter", "assignee", "admin"].index(current_role) if current_role in {"reporter", "assignee", "admin"} else 0,
                key="admin_role_target_role",
            )
            if st.button("Lagre rolle", key="admin_role_save_btn", use_container_width=True):
                error = _set_user_role(selected_user, target_role)
                if error:
                    st.error(error)
                else:
                    st.success(f"Rolle oppdatert: {selected_user} → {target_role}")
                    _clear_bug_cache()
                    st.rerun()
        else:
            st.caption("Ingen brukere å vise ennå.")

        st.divider()
        st.caption("Policy")
        delete_roles = _policy_roles("policy.delete_roles", {"admin", "assignee"})
        reopen_roles = _policy_roles("policy.reopen_roles", {"admin", "assignee", "reporter"})
        hard_delete_roles = _policy_roles("policy.hard_delete_roles", {"admin"})

        selected_delete_roles = set(
            st.multiselect(
                "Kan slette (til papirkurv)",
                options=["reporter", "assignee", "admin"],
                default=sorted(delete_roles),
                key="admin_policy_delete_roles",
            )
        )
        selected_reopen_roles = set(
            st.multiselect(
                "Kan gjenåpne løste bugs",
                options=["reporter", "assignee", "admin"],
                default=sorted(reopen_roles),
                key="admin_policy_reopen_roles",
            )
        )
        selected_hard_delete_roles = set(
            st.multiselect(
                "Kan slette permanent",
                options=["reporter", "assignee", "admin"],
                default=sorted(hard_delete_roles),
                key="admin_policy_hard_delete_roles",
            )
        )
        if st.button("Lagre policy", key="admin_policy_save_btn", use_container_width=True):
            errors = [
                _set_policy_roles("policy.delete_roles", selected_delete_roles, {"admin", "assignee"}),
                _set_policy_roles("policy.reopen_roles", selected_reopen_roles, {"admin", "assignee", "reporter"}),
                _set_policy_roles("policy.hard_delete_roles", selected_hard_delete_roles, {"admin"}),
            ]
            errors = [item for item in errors if item]
            if errors:
                for item in errors:
                    st.error(item)
            else:
                st.success("Policy lagret.")
                st.rerun()


def _render_admin_devops_settings_sidebar(user: dict[str, str]) -> None:
    with st.sidebar.expander("DevOps-innstillinger", expanded=False):
        settings_payload = _load_devops_settings()
        manage_allowed, manage_reason = _devops_admin_manage_access_state(user)
        integration_allowed, integration_reason = _devops_access_state(user)
        if not manage_allowed:
            st.warning(manage_reason)
        elif not integration_allowed:
            st.info(integration_reason)
            st.caption("Aktiver integrasjonen med bryteren under.")

        st.caption("Kilde for verdier: override fra Admin vinner over secrets.toml.")
        enabled_default = bool(settings_payload.get("resolved_enabled", False))
        enabled_source = str(settings_payload.get("enabled_source", "config") or "config")
        devops_enabled_toggle = st.toggle(
            "Aktiver DevOps-integrasjon i UI",
            value=enabled_default,
            key="admin_devops_enabled",
            disabled=not manage_allowed,
            help="Styrer om 'Send bugen til DevOps' er aktiv i Assignee/Admin.",
        )
        st.caption(f"Integrasjon: {'på' if enabled_default else 'av'} (kilde: {enabled_source})")
        st.caption(
            "Org: "
            f"{settings_payload.get('org_source', 'unset')} | "
            "Prosjekt: "
            f"{settings_payload.get('project_source', 'unset')} | "
            "PAT: "
            f"{settings_payload.get('pat_source', 'unset')} | "
            "Type: "
            f"{settings_payload.get('work_item_type_source', 'default')}"
        )

        org_default = str(settings_payload.get("override_org") or settings_payload.get("resolved_org") or "").strip()
        project_default = str(settings_payload.get("override_project") or settings_payload.get("resolved_project") or "").strip()
        org_input = st.text_input(
            "Azure DevOps org",
            value=org_default,
            key="admin_devops_org",
            disabled=not manage_allowed,
        )
        project_input = st.text_input(
            "Azure DevOps prosjekt",
            value=project_default,
            key="admin_devops_project",
            disabled=not manage_allowed,
        )
        work_item_type_default = str(
            settings_payload.get("override_work_item_type")
            or settings_payload.get("resolved_work_item_type")
            or "Task"
        ).strip() or "Task"
        work_item_type_input = st.text_input(
            "Work item type (f.eks. auto, Bug, Issue, Task)",
            value=work_item_type_default,
            key="admin_devops_work_item_type",
            disabled=not manage_allowed,
            help="Bruk 'Task' (samme som opprinnelig løsning) eller 'auto' for automatisk valg.",
        )
        st.caption(
            "Aktiv PAT: "
            + (
                _mask_secret(str(settings_payload.get("resolved_pat") or ""))
                if str(settings_payload.get("resolved_pat") or "").strip()
                else "-"
            )
        )
        pat_input = st.text_input(
            "Azure DevOps PAT (skriv kun ved endring)",
            value="",
            type="password",
            key="admin_devops_pat",
            disabled=not manage_allowed,
        )
        keep_pat = st.checkbox(
            "Behold eksisterende PAT når feltet over er tomt",
            value=True,
            key="admin_devops_keep_pat",
            disabled=not manage_allowed,
        )

        c1, c2 = st.columns(2)
        with c1:
            save_clicked = st.button(
                "Lagre DevOps-oppsett",
                key="admin_devops_save",
                use_container_width=True,
                disabled=not manage_allowed,
            )
        with c2:
            reset_clicked = st.button(
                "Nullstill til secrets",
                key="admin_devops_reset",
                use_container_width=True,
                disabled=not manage_allowed,
            )

        if save_clicked:
            save_error = _save_devops_settings(
                user,
                enabled=bool(devops_enabled_toggle),
                org=org_input,
                project=project_input,
                pat=pat_input,
                work_item_type=work_item_type_input,
                keep_existing_pat=bool(keep_pat),
            )
            if save_error:
                st.error(save_error)
            else:
                st.success("DevOps-innstillinger lagret.")
                st.rerun()

        if reset_clicked:
            reset_error = _reset_devops_settings(user)
            if reset_error:
                st.error(reset_error)
            else:
                st.success("DevOps-innstillinger er nullstilt til secrets.")
                st.rerun()

        if st.button(
            "Test DevOps-tilkobling",
            key="admin_devops_test",
            use_container_width=True,
            disabled=not manage_allowed,
        ):
            ok, detail = _test_devops_settings(user)
            if ok:
                st.success(detail)
            else:
                st.error(detail)


def _recover_orphan_background_jobs() -> None:
    with db_session() as db:
        orphaned = (
            db.query(BackgroundJob)
            .filter(BackgroundJob.status.in_(["pending", "running"]))
            .all()
        )
        if not orphaned:
            return
        finished_at = datetime.now(timezone.utc)
        for row in orphaned:
            row.status = "failed"
            if not row.error_message:
                row.error_message = "Job avbrutt fordi appen ble restartet før fullføring."
            row.finished_at = finished_at
        _commit_with_retry(db, operation="Oppdatering av bakgrunnsjobber")


def _resolve_test_login_settings() -> tuple[bool, str, str]:
    enabled = _is_truthy(_config_value("CLOUD_TEST_ENABLE_TEST_LOGIN", "true"))
    email = _normalize_email(_config_value("CLOUD_TEST_LOCAL_TEST_EMAIL", "admin@example.com"))
    password = _config_value("CLOUD_TEST_LOCAL_TEST_PASSWORD", "admin123")
    return enabled, email, password


def _upsert_test_login_user(
    *,
    email: str,
    password: str,
    force_password_refresh: bool = False,
    create_operation: str,
    update_operation: str,
) -> None:
    with db_session() as db:
        user = db.get(User, email)
        if user is None:
            db.add(
                User(
                    email=email,
                    full_name="Local Test Admin",
                    password_hash=get_password_hash(password),
                    role="admin",
                    auth_provider="local",
                )
            )
            _commit_with_retry(db, operation=create_operation)
            return

        changed = False
        if str(user.role or "").strip().casefold() != "admin":
            user.role = "admin"
            changed = True
        if str(user.auth_provider or "").strip().casefold() != "local":
            user.auth_provider = "local"
            changed = True
        if force_password_refresh or not verify_password(password, str(user.password_hash or "")):
            user.password_hash = get_password_hash(password)
            changed = True
        if changed:
            _commit_with_retry(db, operation=update_operation)


@st.cache_resource
def _init_local_data() -> bool:
    _validate_cloud_database_profile()
    _ensure_postgresql_vector_extension()
    migrations_ok = False
    try:
        run_cloudtest_migrations()
        migrations_ok = True
    except Exception as exc:
        if not _allow_migration_failure_fallback():
            raise
        logger.warning(
            "CloudTest migrations failed. Falling back to legacy schema bootstrap. error=%s",
            exc,
        )

    if not migrations_ok or _legacy_schema_bootstrap_enabled():
        Base.metadata.create_all(bind=engine)
        run_local_schema_upgrades()

    _recover_orphan_background_jobs()
    enable_test_login, test_login_email, test_login_password = _resolve_test_login_settings()
    if enable_test_login and _is_valid_email(test_login_email) and test_login_password:
        _upsert_test_login_user(
            email=test_login_email,
            password=test_login_password,
            force_password_refresh=True,
            create_operation="Oppretting av lokal test-innlogging",
            update_operation="Oppdatering av lokal test-innlogging",
        )

    if not _allow_local_login():
        return True

    admin_email = _config_value("DEFAULT_ADMIN_EMAIL", str(settings.default_admin_email)).casefold()
    admin_password = _config_value("DEFAULT_ADMIN_PASSWORD")
    weak_defaults = {"admin123", "changeme", "change-me", "password"}
    if not admin_email or not admin_password or admin_password.casefold() in weak_defaults:
        return True

    with db_session() as db:
        if not db.get(User, admin_email):
            db.add(
                User(
                    email=admin_email,
                    full_name="Local Administrator",
                    password_hash=get_password_hash(admin_password),
                    role="admin",
                    auth_provider="local",
                )
            )
            _commit_with_retry(db, operation="Oppretting av lokal admin")
    return True


@contextmanager
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_user_exists(db, *, email: str, role: str) -> None:
    existing = db.get(User, email)
    if existing:
        if existing.role != role and role == "admin":
            existing.role = role
            _commit_with_retry(db, operation="Oppdatering av brukerrolle")
        return
    db.add(
        User(
            email=email,
            full_name=email.split("@", 1)[0],
            password_hash=get_password_hash("entra-auth"),
            role=role,
            auth_provider="entra",
        )
    )
    _commit_with_retry(db, operation="Oppretting av bruker")


def _get_or_create_user(db, *, email: str, role: str) -> User:
    normalized_email = _normalize_email(email)
    user = db.get(User, normalized_email)
    if user is None:
        _ensure_user_exists(db, email=normalized_email, role=role)
        user = db.get(User, normalized_email)
    if user is None:
        raise RuntimeError(f"Fant ikke bruker i DB: {normalized_email}")
    return user


def _write_history(db, *, bug_id: int, actor_email: str, action: str, details: str) -> BugHistory:
    row = BugHistory(bug_id=bug_id, action=action, details=details, actor_email=actor_email)
    db.add(row)
    return row


def _notification_payload_text(payload: Mapping[str, Any] | None) -> str | None:
    if not payload:
        return None
    try:
        return json.dumps(dict(payload), ensure_ascii=False)[:4000]
    except Exception:
        return None


def _notification_payload_dict(payload_text: str | None) -> dict[str, Any] | None:
    if not str(payload_text or "").strip():
        return None
    try:
        parsed = json.loads(str(payload_text))
    except Exception:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _notification_dedupe_key(
    *,
    event_type: str,
    recipient_email: str,
    bug_id: int | None,
    history_id: int | None = None,
    comment_id: int | None = None,
    suffix: str | None = None,
) -> str:
    parts = [
        str(event_type or "").strip().casefold(),
        str(_normalize_email(recipient_email)),
        str(int(bug_id or 0)),
        str(int(history_id or 0)),
        str(int(comment_id or 0)),
        str(suffix or "").strip().casefold(),
    ]
    return ":".join(parts)[:255]


def _notifications_enabled_for_user(user: dict[str, str] | None) -> bool:
    return bool(user) and _is_entra_session(user)


def _notification_email_settings() -> dict[str, Any]:
    host = str(_config_value("SMTP_HOST", "") or "").strip()
    sender = str(_config_value("SMTP_FROM", "") or "").strip()
    username = str(_config_value("SMTP_USERNAME", "") or "").strip()
    password = str(_config_value("SMTP_PASSWORD", "") or "").strip()
    enabled_raw = str(_config_value("NOTIFY_EMAIL_ENABLED", "") or "").strip().casefold()
    use_ssl_raw = str(_config_value("SMTP_USE_SSL", "false") or "false").strip().casefold()
    use_tls_raw = str(_config_value("SMTP_USE_TLS", "true") or "true").strip().casefold()
    auth_method_raw = str(_config_value("SMTP_AUTH_METHOD", "auto") or "auto").strip().casefold()
    oauth2_enabled_raw = str(_config_value("SMTP_OAUTH2_ENABLED", "") or "").strip().casefold()
    oauth2_tenant_id = str(_config_value("SMTP_OAUTH2_TENANT_ID", "") or "").strip()
    oauth2_client_id = str(_config_value("SMTP_OAUTH2_CLIENT_ID", "") or "").strip()
    oauth2_client_secret = str(_config_value("SMTP_OAUTH2_CLIENT_SECRET", "") or "").strip()
    oauth2_scope = str(_config_value("SMTP_OAUTH2_SCOPE", "https://outlook.office365.com/.default") or "").strip()
    oauth2_token_url = str(_config_value("SMTP_OAUTH2_TOKEN_URL", "") or "").strip()
    oauth2_username = str(_config_value("SMTP_OAUTH2_USERNAME", username or sender) or "").strip()

    # Optional fallback to existing Streamlit OIDC settings for quick setup.
    if not oauth2_client_id or not oauth2_client_secret or not oauth2_tenant_id:
        try:
            auth_cfg = st.secrets.get("auth", {})
            if isinstance(auth_cfg, Mapping):
                microsoft_cfg = auth_cfg.get("microsoft", {})
                if isinstance(microsoft_cfg, Mapping):
                    if not oauth2_client_id:
                        oauth2_client_id = str(microsoft_cfg.get("client_id", "") or "").strip()
                    if not oauth2_client_secret:
                        oauth2_client_secret = str(microsoft_cfg.get("client_secret", "") or "").strip()
                    if not oauth2_tenant_id:
                        metadata_url = str(microsoft_cfg.get("server_metadata_url", "") or "").strip()
                        match = re.search(r"login\.microsoftonline\.com/([^/]+)/", metadata_url, flags=re.IGNORECASE)
                        if match:
                            oauth2_tenant_id = str(match.group(1) or "").strip()
        except Exception:
            pass

    def _as_bool(value: str, *, default: bool = False) -> bool:
        return _is_truthy(value, default=default)

    try:
        port = int(str(_config_value("SMTP_PORT", "587") or "587").strip())
    except (TypeError, ValueError):
        port = 587
    port = max(1, min(65535, int(port)))

    try:
        timeout_seconds = float(str(_config_value("SMTP_TIMEOUT_SECONDS", "12") or "12").strip())
    except (TypeError, ValueError):
        timeout_seconds = 12.0
    timeout_seconds = max(2.0, min(60.0, timeout_seconds))

    enabled = _as_bool(enabled_raw, default=False)
    if enabled_raw == "":
        enabled = bool(host and sender)

    oauth2_enabled = _as_bool(oauth2_enabled_raw, default=False)
    if auth_method_raw in {"oauth2", "xoauth2"}:
        oauth2_enabled = True

    auth_method = "basic"
    if auth_method_raw in {"oauth2", "xoauth2"}:
        auth_method = "oauth2"
    elif auth_method_raw in {"basic", "login", "password"}:
        auth_method = "basic"
    elif oauth2_enabled:
        auth_method = "oauth2"

    use_ssl = _as_bool(use_ssl_raw, default=False)
    use_tls = _as_bool(use_tls_raw, default=not use_ssl)
    if use_ssl:
        use_tls = False

    oauth2_ready = bool(
        oauth2_enabled
        and oauth2_client_id
        and oauth2_client_secret
        and oauth2_scope
        and oauth2_username
        and (oauth2_token_url or oauth2_tenant_id)
    )
    basic_ready = bool(not username or password)
    effective_enabled = enabled and bool(host and sender)
    if auth_method == "oauth2":
        effective_enabled = effective_enabled and oauth2_ready
    else:
        effective_enabled = effective_enabled and basic_ready

    return {
        "enabled": effective_enabled,
        "host": host,
        "port": port,
        "sender": sender,
        "username": username,
        "password": password,
        "use_ssl": use_ssl,
        "use_tls": use_tls,
        "timeout_seconds": timeout_seconds,
        "auth_method": auth_method,
        "oauth2_enabled": oauth2_enabled,
        "oauth2_ready": oauth2_ready,
        "oauth2_tenant_id": oauth2_tenant_id,
        "oauth2_client_id": oauth2_client_id,
        "oauth2_client_secret": oauth2_client_secret,
        "oauth2_scope": oauth2_scope,
        "oauth2_token_url": oauth2_token_url,
        "oauth2_username": oauth2_username,
    }


def _notification_email_enabled() -> bool:
    return bool(_notification_email_settings().get("enabled"))


def _notification_outbox_channels() -> list[str]:
    channels = ["in_app"]
    if _notification_email_enabled():
        channels.append("email")
    return channels


def _queue_notification_outbox_event_single(
    db,
    *,
    recipient_email: str,
    event_type: str,
    bug_id: int | None,
    title: str,
    message: str,
    actor_email: str | None,
    dedupe_key: str,
    payload: Mapping[str, Any] | None = None,
    channel: str,
) -> None:
    normalized_channel = str(channel or "").strip().casefold()
    if normalized_channel not in {"in_app", "email"}:
        return

    normalized_recipient = _normalize_email(recipient_email)
    if not normalized_recipient or not _is_valid_email(normalized_recipient):
        return
    dedupe = str(dedupe_key or "").strip()[:255]
    if not dedupe:
        return
    for pending in list(getattr(db, "new", set())):
        if isinstance(pending, NotificationOutboxEvent) and str(getattr(pending, "dedupe_key", "")) == dedupe:
            return
    existing = db.scalar(select(NotificationOutboxEvent.id).where(NotificationOutboxEvent.dedupe_key == dedupe).limit(1))
    if existing is not None:
        return

    payload_text = _notification_payload_text(payload)
    db.add(
        NotificationOutboxEvent(
            recipient_email=normalized_recipient,
            event_type=str(event_type or "event").strip()[:80],
            bug_id=int(bug_id) if bug_id else None,
            title=str(title or "Varsel").strip()[:255],
            message=str(message or "").strip()[:2000],
            payload_json=payload_text,
            dedupe_key=dedupe,
            actor_email=_normalize_email(actor_email) if actor_email else None,
            channel=normalized_channel,
            status="pending",
            attempts=0,
        )
    )


def _queue_notification_outbox_event(
    db,
    *,
    recipient_email: str,
    event_type: str,
    bug_id: int | None,
    title: str,
    message: str,
    actor_email: str | None,
    dedupe_key: str,
    payload: Mapping[str, Any] | None = None,
    channel: str = "auto",
) -> None:
    normalized_channel = str(channel or "auto").strip().casefold()
    if normalized_channel in {"in_app", "email"}:
        target_channels = [normalized_channel]
    else:
        target_channels = _notification_outbox_channels()
    dedupe_base = str(dedupe_key or "").strip()
    if not dedupe_base:
        return
    for target_channel in target_channels:
        _queue_notification_outbox_event_single(
            db,
            recipient_email=recipient_email,
            event_type=event_type,
            bug_id=bug_id,
            title=title,
            message=message,
            actor_email=actor_email,
            dedupe_key=f"{dedupe_base}:{target_channel}",
            payload=payload,
            channel=target_channel,
        )


def _queue_in_app_notification(
    db,
    *,
    recipient_email: str,
    event_type: str,
    bug_id: int | None,
    title: str,
    message: str,
    actor_email: str | None,
    dedupe_key: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    normalized_recipient = _normalize_email(recipient_email)
    if not normalized_recipient or not _is_valid_email(normalized_recipient):
        return
    dedupe = str(dedupe_key or "").strip()[:255]
    if not dedupe:
        return
    for pending in list(getattr(db, "new", set())):
        if isinstance(pending, InAppNotification) and str(getattr(pending, "dedupe_key", "")) == dedupe:
            return
    existing = db.scalar(select(InAppNotification.id).where(InAppNotification.dedupe_key == dedupe).limit(1))
    if existing is not None:
        return

    payload_text = _notification_payload_text(payload)
    row = InAppNotification(
        recipient_email=normalized_recipient,
        event_type=str(event_type or "event").strip()[:80],
        bug_id=int(bug_id) if bug_id else None,
        title=str(title or "Varsel").strip()[:255],
        message=str(message or "").strip()[:2000],
        payload_json=payload_text,
        dedupe_key=dedupe,
        actor_email=_normalize_email(actor_email) if actor_email else None,
    )
    db.add(row)


def _build_notification_email_body(row: NotificationOutboxEvent) -> str:
    lines = [
        str(row.message or "").strip(),
        "",
        f"Hendelse: {str(row.event_type or '-').strip()}",
        f"Bug: #{int(row.bug_id)}" if row.bug_id is not None else "Bug: -",
    ]
    actor = str(row.actor_email or "").strip()
    if actor:
        lines.append(f"Utført av: {actor}")
    created = format_datetime_display(row.created_at) if getattr(row, "created_at", None) else ""
    if created:
        lines.append(f"Tidspunkt: {created}")
    return "\n".join(lines).strip()


def _resolve_smtp_oauth2_token_url(settings_payload: Mapping[str, Any]) -> str:
    explicit = str(settings_payload.get("oauth2_token_url") or "").strip()
    if explicit:
        return explicit
    tenant = str(settings_payload.get("oauth2_tenant_id") or "").strip()
    if not tenant:
        raise RuntimeError("SMTP OAuth2 mangler tenant-id eller token-url.")
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


def _fetch_smtp_oauth2_access_token(settings_payload: Mapping[str, Any]) -> str:
    token_url = _resolve_smtp_oauth2_token_url(settings_payload)
    client_id = str(settings_payload.get("oauth2_client_id") or "").strip()
    client_secret = str(settings_payload.get("oauth2_client_secret") or "").strip()
    scope = str(settings_payload.get("oauth2_scope") or "").strip()
    if not client_id or not client_secret or not scope:
        raise RuntimeError("SMTP OAuth2 mangler client-id/client-secret/scope.")

    cache_key = f"{token_url}|{client_id}|{scope}"
    cached = _SMTP_OAUTH2_TOKEN_CACHE.get(cache_key)
    now = time.time()
    if cached and cached[1] - now > 90:
        return str(cached[0])

    body = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        }
    ).encode("utf-8")
    request = Request(
        token_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        details = ""
        try:
            details = exc.read().decode("utf-8", errors="replace")
        except Exception:
            details = str(exc)
        raise RuntimeError(f"SMTP OAuth2 token-feil ({exc.code}): {details[:240]}") from exc
    except URLError as exc:
        raise RuntimeError(f"SMTP OAuth2 token-endepunkt utilgjengelig: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SMTP OAuth2 token-respons var ikke gyldig JSON.") from exc

    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        error_code = str(payload.get("error") or "").strip()
        description = str(payload.get("error_description") or "").strip()
        raise RuntimeError(
            f"SMTP OAuth2 token mangler access_token ({error_code}: {description})".strip()
        )

    expires_in_raw = payload.get("expires_in", 3600)
    try:
        expires_in = max(120, int(expires_in_raw))
    except (TypeError, ValueError):
        expires_in = 3600
    _SMTP_OAUTH2_TOKEN_CACHE[cache_key] = (access_token, now + expires_in)
    return access_token


def _smtp_auth_xoauth2(smtp: smtplib.SMTP, *, username: str, access_token: str) -> None:
    user_value = str(username or "").strip()
    token_value = str(access_token or "").strip()
    if not user_value or not token_value:
        raise RuntimeError("SMTP OAuth2 mangler brukernavn eller access-token.")
    auth_string = f"user={user_value}\x01auth=Bearer {token_value}\x01\x01"
    auth_b64 = base64.b64encode(auth_string.encode("utf-8")).decode("ascii")
    code, response = smtp.docmd("AUTH", f"XOAUTH2 {auth_b64}")
    if int(code) not in {235, 250}:
        response_text = (
            response.decode("utf-8", errors="replace")
            if isinstance(response, (bytes, bytearray))
            else str(response or "")
        )
        raise RuntimeError(f"SMTP OAuth2 autentisering feilet ({code}): {response_text[:240]}")


def _decode_jwt_payload_unverified(token: str) -> dict[str, Any] | None:
    raw = str(token or "").strip()
    if not raw or "." not in raw:
        return None
    parts = raw.split(".")
    if len(parts) < 2:
        return None
    payload_part = parts[1]
    if not payload_part:
        return None
    padding = "=" * (-len(payload_part) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{payload_part}{padding}".encode("ascii"))
        parsed = json.loads(decoded.decode("utf-8", errors="replace"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _format_diag_timestamp(value: Any) -> str:
    try:
        timestamp = int(value)
        if timestamp <= 0:
            return "-"
        return format_datetime_display(datetime.fromtimestamp(timestamp, tz=timezone.utc))
    except Exception:
        return "-"


def _mask_text(value: str, *, keep_prefix: int = 5, keep_suffix: int = 3) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if len(text) <= keep_prefix + keep_suffix + 2:
        return "*" * len(text)
    return f"{text[:keep_prefix]}...{text[-keep_suffix:]}"


def _run_smtp_oauth2_diagnostic() -> dict[str, Any]:
    settings_payload = _notification_email_settings()
    results: list[dict[str, str]] = []

    def _add(status: str, title: str, detail: str) -> None:
        results.append(
            {
                "status": str(status or "info").strip().casefold(),
                "title": str(title or "").strip(),
                "detail": str(detail or "").strip(),
            }
        )

    _add(
        "info",
        "Varselkanal",
        "E-post aktivert." if bool(settings_payload.get("enabled")) else "E-post er ikke aktivert i konfigurasjon.",
    )
    _add(
        "info",
        "SMTP-endepunkt",
        f"{settings_payload.get('host') or '-'}:{settings_payload.get('port') or '-'} "
        f"(TLS={'på' if settings_payload.get('use_tls') else 'av'}, SSL={'på' if settings_payload.get('use_ssl') else 'av'})",
    )

    auth_method = str(settings_payload.get("auth_method") or "basic").strip().casefold()
    if auth_method != "oauth2":
        _add("warn", "Autentiseringsmetode", f"Står til '{auth_method}'. Sett SMTP_AUTH_METHOD='oauth2'.")
    else:
        _add("ok", "Autentiseringsmetode", "OAuth2/XOAUTH2 er valgt.")

    if bool(settings_payload.get("oauth2_ready")):
        _add("ok", "OAuth2-konfig", "Client/Tenant/Scope/bruker ser komplett ut.")
    else:
        missing = []
        if not str(settings_payload.get("oauth2_client_id") or "").strip():
            missing.append("client_id")
        if not str(settings_payload.get("oauth2_client_secret") or "").strip():
            missing.append("client_secret")
        if not str(settings_payload.get("oauth2_scope") or "").strip():
            missing.append("scope")
        if not str(settings_payload.get("oauth2_username") or "").strip():
            missing.append("username")
        if not (
            str(settings_payload.get("oauth2_token_url") or "").strip()
            or str(settings_payload.get("oauth2_tenant_id") or "").strip()
        ):
            missing.append("tenant_id/token_url")
        missing_text = ", ".join(missing) if missing else "ukjent"
        _add("fail", "OAuth2-konfig", f"Mangler felter: {missing_text}.")

    token_claims: dict[str, Any] | None = None
    access_token = ""
    token_fetch_error = ""
    if auth_method == "oauth2":
        try:
            access_token = _fetch_smtp_oauth2_access_token(settings_payload)
            _add("ok", "Token-henting", "Access token ble hentet fra Entra.")
        except Exception as exc:
            token_fetch_error = str(exc).strip()
            _add("fail", "Token-henting", token_fetch_error or "Ukjent feil ved token-henting.")

    if access_token:
        token_claims = _decode_jwt_payload_unverified(access_token)
        if token_claims:
            audience = str(token_claims.get("aud") or "").strip()
            if audience.casefold() == "https://outlook.office365.com":
                _add("ok", "Token audience", audience)
            else:
                _add(
                    "warn",
                    "Token audience",
                    f"{audience or '-'} (for SMTP anbefales https://outlook.office365.com)",
                )
            _add("info", "Token issuer", str(token_claims.get("iss") or "-").strip())
            _add("info", "Token expiry (UTC)", _format_diag_timestamp(token_claims.get("exp")))
            _add(
                "info",
                "Token app-id",
                str(token_claims.get("appid") or token_claims.get("azp") or "-").strip(),
            )
        else:
            _add("warn", "Token claims", "Kunne ikke dekode JWT payload.")

    smtp_probe_error = ""
    smtp_auth_attempted = False
    smtp_host = str(settings_payload.get("host") or "").strip()
    smtp_port = int(settings_payload.get("port") or 587)
    smtp_timeout = float(settings_payload.get("timeout_seconds") or 12.0)
    smtp_use_ssl = bool(settings_payload.get("use_ssl"))
    smtp_use_tls = bool(settings_payload.get("use_tls"))
    smtp_username = str(settings_payload.get("oauth2_username") or settings_payload.get("username") or "").strip()

    if smtp_host and access_token and auth_method == "oauth2":
        smtp_client: smtplib.SMTP | None = None
        try:
            if smtp_use_ssl:
                smtp_client = smtplib.SMTP_SSL(smtp_host, port=smtp_port, timeout=smtp_timeout)
            else:
                smtp_client = smtplib.SMTP(smtp_host, port=smtp_port, timeout=smtp_timeout)
                smtp_client.ehlo()
                if smtp_use_tls:
                    smtp_client.starttls()
                    smtp_client.ehlo()
            capabilities = ""
            try:
                capabilities = str(getattr(smtp_client, "esmtp_features", {}).get("auth") or "").upper()
            except Exception:
                capabilities = ""
            if "XOAUTH2" in capabilities:
                _add("ok", "SMTP capabilities", f"AUTH annonserer XOAUTH2 ({capabilities}).")
            elif capabilities:
                _add("warn", "SMTP capabilities", f"AUTH annonserer: {capabilities}")
            else:
                _add("warn", "SMTP capabilities", "Ingen AUTH-capabilities lest ut.")

            smtp_auth_attempted = True
            _smtp_auth_xoauth2(smtp_client, username=smtp_username, access_token=access_token)
            _add("ok", "SMTP XOAUTH2", "Autentisering mot SMTP lykkes.")
        except Exception as exc:
            smtp_probe_error = str(exc).strip()
            _add("fail", "SMTP XOAUTH2", smtp_probe_error or "Ukjent SMTP-feil.")
        finally:
            try:
                if smtp_client is not None:
                    smtp_client.quit()
            except Exception:
                pass
    elif auth_method == "oauth2" and not access_token:
        _add("warn", "SMTP XOAUTH2", "Hoppe over SMTP-probe fordi token ikke ble hentet.")
    elif auth_method == "oauth2":
        _add("warn", "SMTP XOAUTH2", "Hoppe over SMTP-probe fordi SMTP host mangler.")

    return {
        "generated_at": format_datetime_display(datetime.now(timezone.utc)),
        "results": results,
        "summary": {
            "auth_method": auth_method,
            "email_enabled": bool(settings_payload.get("enabled")),
            "oauth2_ready": bool(settings_payload.get("oauth2_ready")),
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_use_tls": smtp_use_tls,
            "smtp_use_ssl": smtp_use_ssl,
            "oauth2_scope": str(settings_payload.get("oauth2_scope") or "").strip(),
            "oauth2_tenant_id_masked": _mask_text(str(settings_payload.get("oauth2_tenant_id") or "").strip()),
            "oauth2_client_id_masked": _mask_text(str(settings_payload.get("oauth2_client_id") or "").strip()),
            "oauth2_username": smtp_username or "-",
            "token_fetch_error": token_fetch_error,
            "smtp_probe_error": smtp_probe_error,
            "smtp_auth_attempted": smtp_auth_attempted,
            "token_claims": token_claims or {},
        },
    }


def _send_email_notification(row: NotificationOutboxEvent) -> None:
    settings_payload = _notification_email_settings()
    if not bool(settings_payload.get("enabled")):
        raise RuntimeError("SMTP er ikke konfigurert/aktivert.")

    recipient = str(row.recipient_email or "").strip()
    if not _is_valid_email(recipient):
        raise RuntimeError("Ugyldig mottakeradresse.")

    sender = str(settings_payload.get("sender") or "").strip()
    host = str(settings_payload.get("host") or "").strip()
    if not sender or not host:
        raise RuntimeError("SMTP mangler sender eller host.")

    subject = str(row.title or "Varsel fra bugsystem").strip()[:255] or "Varsel fra bugsystem"
    body = _build_notification_email_body(row)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)

    username = str(settings_payload.get("username") or "").strip()
    password = str(settings_payload.get("password") or "").strip()
    auth_method = str(settings_payload.get("auth_method") or "basic").strip().casefold()
    oauth2_username = str(settings_payload.get("oauth2_username") or username or sender).strip()
    port = int(settings_payload.get("port") or 587)
    timeout_seconds = float(settings_payload.get("timeout_seconds") or 12.0)
    use_ssl = bool(settings_payload.get("use_ssl"))
    use_tls = bool(settings_payload.get("use_tls"))

    oauth2_token: str | None = None
    if auth_method == "oauth2":
        oauth2_token = _fetch_smtp_oauth2_access_token(settings_payload)

    if use_ssl:
        with smtplib.SMTP_SSL(host, port=port, timeout=timeout_seconds) as smtp:
            if auth_method == "oauth2":
                _smtp_auth_xoauth2(smtp, username=oauth2_username, access_token=str(oauth2_token or ""))
            elif username:
                smtp.login(username, password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(host, port=port, timeout=timeout_seconds) as smtp:
        smtp.ehlo()
        if use_tls:
            smtp.starttls()
            smtp.ehlo()
        if auth_method == "oauth2":
            _smtp_auth_xoauth2(smtp, username=oauth2_username, access_token=str(oauth2_token or ""))
        elif username:
            smtp.login(username, password)
        smtp.send_message(message)


def _dispatch_notification_outbox(db, *, max_items: int = 200) -> None:
    db.flush()
    pending_rows = list(
        db.scalars(
            select(NotificationOutboxEvent)
            .where(NotificationOutboxEvent.status == "pending")
            .order_by(NotificationOutboxEvent.created_at.asc(), NotificationOutboxEvent.id.asc())
            .limit(max(1, min(1000, int(max_items))))
        ).all()
    )
    if not pending_rows:
        return

    max_attempts_raw = str(_config_value("NOTIFY_MAX_ATTEMPTS", "3") or "3").strip()
    try:
        max_attempts = int(max_attempts_raw)
    except (TypeError, ValueError):
        max_attempts = 3
    max_attempts = max(1, min(10, max_attempts))

    now = datetime.now(timezone.utc)
    for row in pending_rows:
        row.attempts = int(row.attempts or 0) + 1
        row.updated_at = now
        try:
            channel = str(row.channel or "").strip().casefold()
            if channel == "in_app":
                _queue_in_app_notification(
                    db,
                    recipient_email=str(row.recipient_email or ""),
                    event_type=str(row.event_type or "event"),
                    bug_id=int(row.bug_id) if row.bug_id is not None else None,
                    title=str(row.title or "Varsel"),
                    message=str(row.message or ""),
                    actor_email=str(row.actor_email or "") or None,
                    dedupe_key=str(row.dedupe_key or ""),
                    payload=_notification_payload_dict(row.payload_json),
                )
            elif channel == "email":
                _send_email_notification(row)
            else:
                row.status = "failed"
                row.last_error = f"Unsupported channel: {row.channel}"
                continue
            row.status = "delivered"
            row.delivered_at = now
            row.last_error = None
        except Exception as exc:
            row.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
            if int(row.attempts or 0) >= max_attempts:
                row.status = "failed"
            else:
                row.status = "pending"


def _admin_notification_recipients() -> set[str]:
    return {
        _normalize_email(item)
        for item in (_admin_emails() | _db_admin_emails())
        if _is_valid_email(_normalize_email(item))
    }


def _emit_notifications_for_bug_created(
    db,
    *,
    bug: Bug,
    actor_email: str,
    history_id: int | None,
    notifications_enabled: bool = True,
) -> None:
    if not notifications_enabled:
        return
    actor_norm = _normalize_email(actor_email)
    bug_id = int(bug.id)
    bug_title = str(bug.title or f"Bug #{bug_id}")
    assignee_norm = _normalize_email(bug.assignee_id)

    if assignee_norm:
        _queue_notification_outbox_event(
            db,
            recipient_email=assignee_norm,
            event_type="bug_assigned",
            bug_id=bug_id,
            title=f"Ny tildeling: bug #{bug_id}",
            message=f"Du er tildelt «{bug_title}».",
            actor_email=actor_email,
            dedupe_key=_notification_dedupe_key(
                event_type="bug_assigned",
                recipient_email=assignee_norm,
                bug_id=bug_id,
                history_id=history_id,
                suffix="created",
            ),
            payload={"status": normalize_bug_status(bug.status), "severity": str(bug.severity or "")},
        )

    if str(bug.severity or "").strip().casefold() == "critical":
        for admin_email in _admin_notification_recipients():
            if admin_email == actor_norm:
                continue
            _queue_notification_outbox_event(
                db,
                recipient_email=admin_email,
                event_type="bug_critical",
                bug_id=bug_id,
                title=f"Kritisk bug opprettet #{bug_id}",
                message=f"Kritisk bug opprettet: «{bug_title}».",
                actor_email=actor_email,
                dedupe_key=_notification_dedupe_key(
                    event_type="bug_critical",
                    recipient_email=admin_email,
                    bug_id=bug_id,
                    history_id=history_id,
                    suffix="created",
                ),
                payload={"status": normalize_bug_status(bug.status), "severity": str(bug.severity or "")},
            )


def _emit_notifications_for_comment(
    db,
    *,
    bug: Bug,
    actor_email: str,
    comment_id: int | None,
    comment_body: str,
    notifications_enabled: bool = True,
) -> None:
    if not notifications_enabled:
        return
    actor_norm = _normalize_email(actor_email)
    bug_id = int(bug.id)
    bug_title = str(bug.title or f"Bug #{bug_id}")
    reporter_norm = _normalize_email(bug.reporter_id)
    assignee_norm = _normalize_email(bug.assignee_id)
    recipients = {
        recipient
        for recipient in {reporter_norm, assignee_norm}
        if recipient and recipient != actor_norm
    }
    if not recipients:
        return

    preview = str(comment_body or "").strip().replace("\n", " ")
    if len(preview) > 140:
        preview = f"{preview[:137]}..."

    for recipient in recipients:
        _queue_notification_outbox_event(
            db,
            recipient_email=recipient,
            event_type="bug_comment_added",
            bug_id=bug_id,
            title=f"Ny kommentar på bug #{bug_id}",
            message=f"{actor_email} kommenterte «{bug_title}». {preview}",
            actor_email=actor_email,
            dedupe_key=_notification_dedupe_key(
                event_type="bug_comment_added",
                recipient_email=recipient,
                bug_id=bug_id,
                comment_id=comment_id,
            ),
            payload={"comment_id": int(comment_id or 0), "status": normalize_bug_status(bug.status)},
        )


def _emit_notifications_for_update(
    db,
    *,
    bug: Bug,
    actor_email: str,
    history_id: int | None,
    previous_status: str,
    previous_assignee: str | None,
    previous_severity: str,
    previous_overdue: bool,
    new_status: str,
    new_assignee: str | None,
    new_severity: str,
    new_overdue: bool,
    notifications_enabled: bool = True,
) -> None:
    if not notifications_enabled:
        return
    actor_norm = _normalize_email(actor_email)
    bug_id = int(bug.id)
    bug_title = str(bug.title or f"Bug #{bug_id}")
    reporter_norm = _normalize_email(bug.reporter_id)
    previous_assignee_norm = _normalize_email(previous_assignee)
    new_assignee_norm = _normalize_email(new_assignee)

    assignee_changed = previous_assignee_norm != new_assignee_norm
    status_changed = previous_status != new_status
    severity_escalated_to_critical = previous_severity != "critical" and new_severity == "critical"

    if assignee_changed:
        if new_assignee_norm:
            _queue_notification_outbox_event(
                db,
                recipient_email=new_assignee_norm,
                event_type="bug_assigned",
                bug_id=bug_id,
                title=f"Ny tildeling: bug #{bug_id}",
                message=f"Du er tildelt «{bug_title}».",
                actor_email=actor_email,
                dedupe_key=_notification_dedupe_key(
                    event_type="bug_assigned",
                    recipient_email=new_assignee_norm,
                    bug_id=bug_id,
                    history_id=history_id,
                    suffix=f"to:{new_assignee_norm}",
                ),
                payload={"status": new_status, "severity": new_severity},
            )
        if previous_assignee_norm and previous_assignee_norm != actor_norm and previous_assignee_norm != new_assignee_norm:
            _queue_notification_outbox_event(
                db,
                recipient_email=previous_assignee_norm,
                event_type="bug_unassigned",
                bug_id=bug_id,
                title=f"Tildeling fjernet: bug #{bug_id}",
                message=f"Du er ikke lenger tildelt «{bug_title}».",
                actor_email=actor_email,
                dedupe_key=_notification_dedupe_key(
                    event_type="bug_unassigned",
                    recipient_email=previous_assignee_norm,
                    bug_id=bug_id,
                    history_id=history_id,
                    suffix=f"from:{previous_assignee_norm}",
                ),
                payload={"status": new_status, "severity": new_severity},
            )
        if reporter_norm and reporter_norm != actor_norm:
            _queue_notification_outbox_event(
                db,
                recipient_email=reporter_norm,
                event_type="bug_assignee_changed",
                bug_id=bug_id,
                title=f"Ny ansvarlig på bug #{bug_id}",
                message=f"Ansvarlig endret til: {new_assignee_norm or 'ikke tildelt'}.",
                actor_email=actor_email,
                dedupe_key=_notification_dedupe_key(
                    event_type="bug_assignee_changed",
                    recipient_email=reporter_norm,
                    bug_id=bug_id,
                    history_id=history_id,
                ),
                payload={"status": new_status, "severity": new_severity},
            )

    if status_changed:
        event_type = "bug_status_changed"
        if new_status == "resolved":
            event_type = "bug_resolved"
        elif previous_status == "resolved" and new_status == "open":
            event_type = "bug_reopened"

        targets = {
            recipient
            for recipient in {reporter_norm, new_assignee_norm}
            if recipient and recipient != actor_norm
        }
        status_text = status_label(new_status)
        for recipient in targets:
            _queue_notification_outbox_event(
                db,
                recipient_email=recipient,
                event_type=event_type,
                bug_id=bug_id,
                title=f"Status endret på bug #{bug_id}",
                message=f"Status for «{bug_title}» er nå {status_text}.",
                actor_email=actor_email,
                dedupe_key=_notification_dedupe_key(
                    event_type=event_type,
                    recipient_email=recipient,
                    bug_id=bug_id,
                    history_id=history_id,
                    suffix=f"{previous_status}->{new_status}",
                ),
                payload={"status": new_status, "severity": new_severity},
            )

    if severity_escalated_to_critical:
        for admin_email in _admin_notification_recipients():
            if admin_email == actor_norm:
                continue
            _queue_notification_outbox_event(
                db,
                recipient_email=admin_email,
                event_type="bug_critical",
                bug_id=bug_id,
                title=f"Kritisk bug: #{bug_id}",
                message=f"Bug «{bug_title}» er satt til kritisk alvorlighet.",
                actor_email=actor_email,
                dedupe_key=_notification_dedupe_key(
                    event_type="bug_critical",
                    recipient_email=admin_email,
                    bug_id=bug_id,
                    history_id=history_id,
                    suffix="severity",
                ),
                payload={"status": new_status, "severity": new_severity},
            )

    if new_status != "resolved" and new_overdue and not previous_overdue:
        for admin_email in _admin_notification_recipients():
            if admin_email == actor_norm:
                continue
            _queue_notification_outbox_event(
                db,
                recipient_email=admin_email,
                event_type="bug_sla_overdue",
                bug_id=bug_id,
                title=f"SLA-brudd på bug #{bug_id}",
                message=f"Bug «{bug_title}» har passert SLA-frist.",
                actor_email=actor_email,
                dedupe_key=_notification_dedupe_key(
                    event_type="bug_sla_overdue",
                    recipient_email=admin_email,
                    bug_id=bug_id,
                    history_id=history_id,
                ),
                payload={"status": new_status, "severity": new_severity},
            )


def _list_notifications_for_user(*, user_email: str, unread_only: bool, limit: int) -> list[InAppNotification]:
    normalized = _normalize_email(user_email)
    if not normalized:
        return []
    max_rows = max(1, min(100, int(limit)))
    with db_session() as db:
        query = (
            select(InAppNotification)
            .where(func.lower(InAppNotification.recipient_email) == normalized)
            .order_by(InAppNotification.created_at.desc(), InAppNotification.id.desc())
            .limit(max_rows)
        )
        if unread_only:
            query = query.where(InAppNotification.is_read.is_(False))
        return list(db.scalars(query).all())


def _count_unread_notifications(user_email: str) -> int:
    normalized = _normalize_email(user_email)
    if not normalized:
        return 0
    with db_session() as db:
        value = db.scalar(
            select(func.count(InAppNotification.id)).where(
                func.lower(InAppNotification.recipient_email) == normalized,
                InAppNotification.is_read.is_(False),
            )
        )
    return int(value or 0)


def _mark_notification_as_read(*, notification_id: int, user_email: str) -> str | None:
    normalized = _normalize_email(user_email)
    if not normalized:
        return "Ugyldig bruker."
    try:
        with db_session() as db:
            row = db.get(InAppNotification, int(notification_id))
            if row is None:
                return "Varslet finnes ikke."
            if _normalize_email(row.recipient_email) != normalized:
                return "Du har ikke tilgang til dette varslet."
            if not bool(row.is_read):
                row.is_read = True
                row.read_at = datetime.now(timezone.utc)
                _commit_with_retry(db, operation="Oppdatering av varselstatus")
    except RuntimeError as exc:
        return str(exc)
    except SQLAlchemyError as exc:
        return format_user_error("Kunne ikke oppdatere varsel", exc, fallback="Prøv igjen.")
    return None


def _mark_all_notifications_as_read(user_email: str) -> str | None:
    normalized = _normalize_email(user_email)
    if not normalized:
        return "Ugyldig bruker."
    try:
        with db_session() as db:
            rows = db.scalars(
                select(InAppNotification).where(
                    func.lower(InAppNotification.recipient_email) == normalized,
                    InAppNotification.is_read.is_(False),
                )
            ).all()
            if not rows:
                return None
            timestamp = datetime.now(timezone.utc)
            for row in rows:
                row.is_read = True
                row.read_at = timestamp
            _commit_with_retry(db, operation="Oppdatering av varselstatus")
    except RuntimeError as exc:
        return str(exc)
    except SQLAlchemyError as exc:
        return format_user_error("Kunne ikke oppdatere varsler", exc, fallback="Prøv igjen.")
    return None


def _notification_outbox_status_counts() -> dict[str, int]:
    counts = {"pending": 0, "failed": 0, "delivered": 0}
    with db_session() as db:
        rows = db.execute(
            select(NotificationOutboxEvent.status, func.count(NotificationOutboxEvent.id)).group_by(
                NotificationOutboxEvent.status
            )
        ).all()
    for status, amount in rows:
        normalized_status = str(status or "").strip().casefold()
        if normalized_status in counts:
            counts[normalized_status] = int(amount or 0)
    counts["total"] = sum(int(value) for value in counts.values())
    return counts


def _list_notification_outbox_rows(*, status: str | None, limit: int = 20) -> list[NotificationOutboxEvent]:
    max_rows = max(1, min(200, int(limit)))
    with db_session() as db:
        query = select(NotificationOutboxEvent).order_by(NotificationOutboxEvent.id.desc()).limit(max_rows)
        if status:
            query = query.where(NotificationOutboxEvent.status == str(status).strip().casefold())
        return list(db.scalars(query).all())


def _run_notification_outbox_now(*, max_items: int = 300) -> str | None:
    try:
        with db_session() as db:
            _dispatch_notification_outbox(db, max_items=max_items)
            _commit_with_retry(db, operation="Kjøring av varselkø")
    except RuntimeError as exc:
        return str(exc)
    except SQLAlchemyError as exc:
        return format_user_error("Kunne ikke kjøre varselkø", exc, fallback="Prøv igjen.")
    return None


def _resend_failed_notification_outbox(*, max_rows: int = 200) -> tuple[int, str | None]:
    max_count = max(1, min(500, int(max_rows)))
    try:
        with db_session() as db:
            failed_rows = list(
                db.scalars(
                    select(NotificationOutboxEvent)
                    .where(NotificationOutboxEvent.status == "failed")
                    .order_by(NotificationOutboxEvent.id.asc())
                    .limit(max_count)
                ).all()
            )
            if not failed_rows:
                return 0, None
            now = datetime.now(timezone.utc)
            for row in failed_rows:
                row.status = "pending"
                row.attempts = 0
                row.last_error = None
                row.delivered_at = None
                row.updated_at = now
            _dispatch_notification_outbox(db, max_items=max_count * 2)
            _commit_with_retry(db, operation="Resend av feilede varsler")
            return len(failed_rows), None
    except RuntimeError as exc:
        return 0, str(exc)
    except SQLAlchemyError as exc:
        return 0, format_user_error("Kunne ikke sende feilede varsler på nytt", exc, fallback="Prøv igjen.")


def _render_notification_outbox_admin_sidebar(*, user: dict[str, str], prefix: str) -> None:
    if str(user.get("role") or "").strip().casefold() != "admin":
        return
    if not _is_entra_session(user):
        with st.sidebar.expander("Varselkø", expanded=False):
            st.caption("Varselkø er tilgjengelig kun for admin med Entra-innlogging.")
        return

    counts = _notification_outbox_status_counts()
    with st.sidebar.expander("Varselkø", expanded=False):
        c1, c2, c3 = st.columns(3)
        c1.metric("Pending", int(counts.get("pending", 0)))
        c2.metric("Failed", int(counts.get("failed", 0)))
        c3.metric("Delivered", int(counts.get("delivered", 0)))
        st.caption(f"Totalt i outbox: {int(counts.get('total', 0))}")

        action_left, action_right = st.columns(2)
        with action_left:
            run_now_clicked = st.button(
                "Kjør kø nå",
                key=f"{prefix}_outbox_run_now",
                use_container_width=True,
            )
        with action_right:
            resend_clicked = st.button(
                "Resend feilede",
                key=f"{prefix}_outbox_resend_failed",
                use_container_width=True,
                disabled=int(counts.get("failed", 0)) <= 0,
            )

        if run_now_clicked:
            run_error = _run_notification_outbox_now(max_items=500)
            if run_error:
                st.error(run_error)
            else:
                st.success("Varselkø kjørt.")
            st.rerun()

        if resend_clicked:
            resent_count, resend_error = _resend_failed_notification_outbox(max_rows=500)
            if resend_error:
                st.error(resend_error)
            elif resent_count <= 0:
                st.info("Ingen feilede varsler å sende på nytt.")
            else:
                st.success(f"Forsøkte resend av {resent_count} feilede varsler.")
            st.rerun()

        failed_rows = _list_notification_outbox_rows(status="failed", limit=10)
        if failed_rows:
            st.caption("Siste feilede")
            for row in failed_rows:
                st.write(
                    f"#{row.id} | {row.channel} | {row.recipient_email} | bug #{row.bug_id or '-'} | "
                    f"attempts {int(row.attempts or 0)}"
                )
                if row.last_error:
                    st.caption(str(row.last_error)[:220])
        else:
            st.caption("Ingen feilede varsler.")

        st.divider()
        st.caption("SMTP OAuth2 diagnose")
        diag_button = st.button(
            "Kjør SMTP OAuth2 diagnose",
            key=f"{prefix}_smtp_oauth2_diagnose_run",
            use_container_width=True,
        )
        diag_state_key = f"{prefix}_smtp_oauth2_diagnose_result"
        if diag_button:
            st.session_state[diag_state_key] = _run_smtp_oauth2_diagnostic()
            st.rerun()

        diag_result = st.session_state.get(diag_state_key)
        if isinstance(diag_result, Mapping):
            st.caption(f"Sist kjørt: {diag_result.get('generated_at') or '-'}")
            status_icon = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "info": "INFO"}
            for item in list(diag_result.get("results") or []):
                if not isinstance(item, Mapping):
                    continue
                state = str(item.get("status") or "info").strip().casefold()
                icon = status_icon.get(state, "INFO")
                title = str(item.get("title") or "").strip()
                detail = str(item.get("detail") or "").strip()
                st.write(f"[{icon}] {title}: {detail}")
            summary = diag_result.get("summary")
            if isinstance(summary, Mapping):
                st.caption("Diagnose-detajler")
                st.code(
                    json.dumps(
                        {
                            "auth_method": summary.get("auth_method"),
                            "email_enabled": summary.get("email_enabled"),
                            "oauth2_ready": summary.get("oauth2_ready"),
                            "smtp_host": summary.get("smtp_host"),
                            "smtp_port": summary.get("smtp_port"),
                            "smtp_use_tls": summary.get("smtp_use_tls"),
                            "smtp_use_ssl": summary.get("smtp_use_ssl"),
                            "oauth2_scope": summary.get("oauth2_scope"),
                            "oauth2_tenant_id": summary.get("oauth2_tenant_id_masked"),
                            "oauth2_client_id": summary.get("oauth2_client_id_masked"),
                            "oauth2_username": summary.get("oauth2_username"),
                            "smtp_auth_attempted": summary.get("smtp_auth_attempted"),
                            "token_claims": summary.get("token_claims"),
                            "token_fetch_error": summary.get("token_fetch_error"),
                            "smtp_probe_error": summary.get("smtp_probe_error"),
                        },
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                    language="json",
                )


def _render_in_app_notifications_sidebar(*, user: dict[str, str], prefix: str) -> None:
    notifications_enabled = _notifications_enabled_for_user(user)
    email_enabled = _notification_email_enabled()
    if notifications_enabled:
        try:
            with db_session() as db:
                _dispatch_notification_outbox(db, max_items=500)
                _commit_with_retry(db, operation="Oppdatering av varselkø")
        except Exception as exc:
            logger.warning("Could not drain notification outbox in sidebar: %s", exc.__class__.__name__)

    unread_count = _count_unread_notifications(user["email"]) if notifications_enabled else 0
    label = f"Varsler ({unread_count})" if unread_count > 0 else "Varsler"
    with st.sidebar.expander(label, expanded=False):
        if not notifications_enabled:
            st.caption("Varsler er tilgjengelig kun for Entra-innlogging.")
            return
        st.caption("Rollebaserte hendelser for dine bugs og tildelinger.")
        st.caption("Kanaler: In-app + e-post" if email_enabled else "Kanaler: In-app (e-post er ikke aktivert).")
        unread_only = st.checkbox(
            "Kun uleste",
            key=f"{prefix}_notifications_unread_only",
            value=True,
        )
        limit = st.selectbox(
            "Antall",
            options=[10, 20, 50],
            index=1,
            key=f"{prefix}_notifications_limit",
        )
        top_left, top_right = st.columns(2)
        with top_left:
            if st.button("Oppdater", key=f"{prefix}_notifications_refresh", use_container_width=True):
                st.rerun()
        with top_right:
            if st.button(
                "Marker alle lest",
                key=f"{prefix}_notifications_mark_all",
                use_container_width=True,
                disabled=unread_count <= 0,
            ):
                error = _mark_all_notifications_as_read(user["email"])
                if error:
                    st.error(error)
                else:
                    st.success("Alle varsler er markert som lest.")
                    st.rerun()

        notifications = _list_notifications_for_user(
            user_email=user["email"],
            unread_only=bool(unread_only),
            limit=int(limit),
        )
        if not notifications:
            st.caption("Ingen varsler å vise.")
            return

        for row in notifications:
            bullet = "🔵" if not bool(row.is_read) else "⚪"
            bug_text = f"Bug #{row.bug_id}" if row.bug_id else "System"
            st.write(f"{bullet} {bug_text} - {row.title}")
            st.caption(f"{format_datetime_display(row.created_at)} | {row.message}")
            if not bool(row.is_read):
                if st.button(
                    "Marker lest",
                    key=f"{prefix}_notification_mark_read_{row.id}",
                    use_container_width=True,
                ):
                    error = _mark_notification_as_read(notification_id=int(row.id), user_email=user["email"])
                    if error:
                        st.error(error)
                    else:
                        st.rerun()


def _current_user() -> dict[str, str] | None:
    email = str(st.session_state.get("email") or "").strip()
    role = str(st.session_state.get("role") or "").strip()
    if not email or not role:
        return None
    auth_provider = str(st.session_state.get("auth_provider") or "").strip().casefold()
    if not auth_provider:
        if bool(getattr(st.user, "is_logged_in", False)):
            oidc_email = str(getattr(st.user, "email", "") or "").strip().casefold()
            if oidc_email and oidc_email == email.casefold():
                auth_provider = "entra"
        if not auth_provider:
            try:
                with db_session() as db:
                    db_user = db.get(User, email.casefold())
                    auth_provider = str(getattr(db_user, "auth_provider", "") or "").strip().casefold()
            except Exception:
                auth_provider = ""
        if auth_provider:
            st.session_state["auth_provider"] = auth_provider
    return {"email": email, "role": role, "auth_provider": auth_provider}


def _set_user(email: str) -> None:
    role = _role_for_email(email)
    try:
        with db_session() as db:
            _ensure_user_exists(db, email=email, role=role)
    except RuntimeError as exc:
        logger.warning("Unable to persist user during login: %s", exc)
        st.session_state["_auth_error"] = str(exc)
        return
    st.session_state["email"] = email
    st.session_state["role"] = role
    st.session_state["auth_provider"] = "entra"


def _is_entra_session(user: dict[str, str] | None = None) -> bool:
    current = user or _current_user()
    if not current:
        return False
    auth_provider = str(current.get("auth_provider") or "").strip().casefold()
    if auth_provider == "entra":
        return True
    if bool(getattr(st.user, "is_logged_in", False)):
        oidc_email = str(getattr(st.user, "email", "") or "").strip().casefold()
        return bool(oidc_email and oidc_email == str(current.get("email", "")).strip().casefold())
    return False


def _devops_access_state(user: dict[str, str] | None = None) -> tuple[bool, str]:
    current_user = user or _current_user() or {}
    role = str(current_user.get("role") or "").strip().casefold()
    if role not in {"admin", "assignee"}:
        return False, "DevOps er kun tilgjengelig for Assignee og Admin."
    enabled = _devops_ui_enabled()
    if not enabled:
        return False, "DevOps-integrasjon er slått av. Aktiver den i Admin -> DevOps-innstillinger."
    if not _is_entra_session(current_user):
        return False, "DevOps krever Entra-innlogging. Test/lokal innlogging er sperret for DevOps."
    return True, ""


def _devops_admin_manage_access_state(user: dict[str, str] | None = None) -> tuple[bool, str]:
    current_user = user or _current_user() or {}
    role = str(current_user.get("role") or "").strip().casefold()
    if role != "admin":
        return False, "Kun admin kan konfigurere DevOps-innstillinger."
    if not _is_entra_session(current_user):
        return False, "DevOps-innstillinger krever Entra-innlogging. Test/lokal innlogging er sperret."
    return True, ""


def _ensure_test_login_user() -> None:
    enable_test_login, test_login_email, test_login_password = _resolve_test_login_settings()
    if not enable_test_login:
        return
    if not _is_valid_email(test_login_email) or not test_login_password:
        return
    try:
        _upsert_test_login_user(
            email=test_login_email,
            password=test_login_password,
            force_password_refresh=False,
            create_operation="Oppretting av testbruker",
            update_operation="Oppdatering av testbruker",
        )
    except Exception as exc:
        logger.warning("Failed to ensure test login user: %s", exc)


def _auth_gate() -> bool:
    _ensure_test_login_user()
    if st.session_state.get("_auth_error"):
        st.sidebar.error(str(st.session_state.get("_auth_error")))
        st.session_state.pop("_auth_error", None)
    base_kwargs = {
        "allow_local_login": _allow_local_login,
        "current_user": _current_user,
        "set_user": _set_user,
        "db_session": db_session,
        "verify_password": verify_password,
        "user_model": User,
        "logger": logger,
    }
    enable_test_login, test_login_email, test_login_password = _resolve_test_login_settings()
    extended_kwargs = {
        **base_kwargs,
        "local_default_email": test_login_email or "admin@example.com",
        "local_default_password": test_login_password or "admin123",
        "enable_test_login": enable_test_login,
    }
    try:
        return _render_auth_gate(**extended_kwargs)
    except TypeError:
        logger.warning("auth_ui.render_auth_gate has legacy signature; retrying without test-login kwargs")
        return _render_auth_gate(**base_kwargs)


def _load_bugs_for_user(user: dict[str, str]) -> list[Bug]:
    with db_session() as db:
        query = select(Bug).where(Bug.deleted_at.is_(None)).order_by(Bug.created_at.desc())
        bugs = list(db.scalars(query).unique().all())
        if user["role"] in {"admin", "assignee"}:
            return bugs
        return [bug for bug in bugs if (bug.reporter_id or "").casefold() == user["email"].casefold()]


def _load_deleted_bugs_for_admin(*, limit: int = 100) -> list[Bug]:
    with db_session() as db:
        query = (
            select(Bug)
            .where(Bug.deleted_at.is_not(None))
            .order_by(Bug.deleted_at.desc(), Bug.id.desc())
            .limit(max(1, int(limit)))
        )
        return list(db.scalars(query).unique().all())


def _is_deleted_bug(bug: Bug) -> bool:
    return bool(getattr(bug, "deleted_at", None))


def _load_bugs_for_user_cached(user: dict[str, str], ttl_seconds: int = 8) -> list[Bug]:
    cache_key = _bug_cache_key(user)
    cached = cached_value(cache_key, ttl_seconds, lambda: _load_bugs_for_user(user))
    if isinstance(cached, list):
        return cached
    return _load_bugs_for_user(user)


_ADVANCED_SIDEBAR_SECTIONS: dict[str, list[str]] = {
    "reporter": [
        "Filtrering",
        "Lagrede filter",
        "AI-innstillinger",
        "System og drift",
        "Varsler",
        "Eksport",
        "TODO",
    ],
    "assignee": [
        "Filtrering",
        "Lagrede filter",
        "AI-innstillinger",
        "System og drift",
        "Varsler",
        "Arbeidskø-filtre",
        "Arbeidskø",
        "Mulige duplikater",
        "Eksport",
        "TODO",
    ],
    "admin": [
        "Filtrering",
        "Lagrede filter",
        "AI-innstillinger",
        "System og drift",
        "Varselkø",
        "Varsler",
        "Arbeidskø-filtre",
        "Admin-filtrering",
        "Arbeidskø",
        "Mulige duplikater",
        "Eksport",
        "DevOps-innstillinger",
        "Admin-tilganger",
        "TODO",
    ],
}


def _advanced_sidebar_sections(prefix: str) -> list[str]:
    return list(_ADVANCED_SIDEBAR_SECTIONS.get(prefix, _ADVANCED_SIDEBAR_SECTIONS["reporter"]))


def _render_sidebar_advanced_controller(prefix: str) -> None:
    mode_key = f"{prefix}_advanced_mode"
    section_key = f"{prefix}_advanced_section"
    sections = _advanced_sidebar_sections(prefix)
    selector_options = ["Alle"] + sections

    st.sidebar.toggle(
        "Avansert modus",
        key=mode_key,
        value=bool(st.session_state.get(mode_key, False)),
        help="Når aktiv vises avanserte sidebarmoduler. Velg én seksjon om gangen for mindre støy.",
    )
    if not st.session_state.get(mode_key):
        st.session_state[section_key] = "Alle"
        return

    current_value = str(st.session_state.get(section_key, "Alle") or "Alle")
    if current_value not in selector_options:
        current_value = "Alle"
    st.sidebar.selectbox(
        "Avansert seksjon",
        options=selector_options,
        index=selector_options.index(current_value),
        key=section_key,
        help="Velg hvilken avansert seksjon som skal vises i sidebaren.",
    )


def _sidebar_should_render(prefix: str, section_name: str) -> bool:
    if not bool(st.session_state.get(f"{prefix}_advanced_mode", False)):
        return False
    selected = str(st.session_state.get(f"{prefix}_advanced_section", "Alle") or "Alle")
    return selected == "Alle" or selected == section_name


def _search_label_for_prefix(prefix: str) -> str:
    search_labels = {
        "reporter": "Søk i dine bugs (vektorsøk)",
        "assignee": "Søk i bugs (vektorsøk)",
        "admin": "Søk i bugs (vektorsøk)",
    }
    return search_labels.get(prefix, "Søk i bugs (vektorsøk)")


def _render_page_sidebar_sections(*, user: dict[str, str], prefix: str, bugs: list[Bug]) -> bool:
    _render_sidebar_advanced_controller(prefix)
    show_filtering = _sidebar_should_render(prefix, "Filtrering")
    if show_filtering:
        render_sidebar_bug_filters(prefix, bugs)
    if _sidebar_should_render(prefix, "Lagrede filter"):
        _render_saved_filter_views_sidebar(user=user, prefix=prefix)
    if _sidebar_should_render(prefix, "AI-innstillinger"):
        _render_ai_and_embedding_sidebar_settings(prefix=prefix)
    if _sidebar_should_render(prefix, "System og drift"):
        _render_system_and_ops_sidebar(jobs=_background_jobs_snapshot(), telemetry=_runtime_performance_snapshot())
    if prefix == "admin" and _sidebar_should_render(prefix, "Varselkø"):
        _render_notification_outbox_admin_sidebar(user=user, prefix=prefix)
    if _sidebar_should_render(prefix, "Varsler"):
        _render_in_app_notifications_sidebar(user=user, prefix=prefix)
    return show_filtering


def _run_page_vector_search(*, user: dict[str, str], prefix: str, query: str, bugs: list[Bug]) -> tuple[list[Bug], bool]:
    if not query:
        return bugs, False
    search_settings = _current_search_settings()
    started = time.perf_counter()
    try:
        with db_session() as db:
            db_user = _get_or_create_user(db, email=user["email"], role=user["role"])
            semantic_results = search_visible_bugs(
                db,
                current_user=db_user,
                query=query,
                limit=200,
                embedding_provider=search_settings["embedding_provider"],
                embedding_model=search_settings["embedding_model"],
            )
        st.caption(f"Viser vektorsøk-resultater for: {query}")
        return semantic_results, True
    except (SQLAlchemyError, DetachedInstanceError, RuntimeError, ValueError) as exc:
        logger.warning(
            "Vector search failed for prefix=%s query_len=%s error=%s",
            prefix,
            len(query),
            exc.__class__.__name__,
        )
        st.warning(format_user_error("Vektorsøk feilet", exc, fallback="Bruker lokal søkefallback i denne visningen."))
        return bugs, False
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        _record_runtime_metric(f"search_{prefix}_ms", elapsed_ms)


def _reset_filter_state_for_hidden_filtering(prefix: str) -> None:
    st.session_state[f"{prefix}_filter_status_mode"] = "all"
    st.session_state[f"{prefix}_filter_severity"] = []
    st.session_state[f"{prefix}_filter_tags"] = []
    st.session_state[f"{prefix}_sort_mode"] = "Nyeste først"


def _prepare_page_bug_list(*, user: dict[str, str], prefix: str) -> list[Bug]:
    bugs = _load_bugs_for_user_cached(user)
    query = render_sidebar_search(prefix, label=_search_label_for_prefix(prefix))
    show_filtering = _render_page_sidebar_sections(user=user, prefix=prefix, bugs=bugs)
    candidate_bugs, vector_search_active = _run_page_vector_search(user=user, prefix=prefix, query=query, bugs=bugs)

    st.session_state[f"{prefix}_vector_search_active"] = vector_search_active
    if not show_filtering:
        _reset_filter_state_for_hidden_filtering(prefix)
    filtered_bugs = apply_sidebar_bug_filters(
        bugs=candidate_bugs,
        prefix=prefix,
        apply_query_filter=not vector_search_active,
    )
    return filtered_bugs


def _sidebar_render_once(key: str) -> bool:
    """Return True the first time a sidebar block is requested in this rerun."""
    registry = st.session_state.setdefault("_sidebar_render_once_registry", set())
    if not isinstance(registry, set):
        registry = set()
        st.session_state["_sidebar_render_once_registry"] = registry
    normalized = str(key or "").strip()
    if not normalized:
        return True
    if normalized in registry:
        return False
    registry.add(normalized)
    st.session_state["_sidebar_render_once_registry"] = registry
    return True


def _severity_priority(severity: str | None) -> int:
    order = {
        "critical": 4,
        "high": 3,
        "medium": 2,
        "low": 1,
    }
    return order.get(str(severity or "").strip().casefold(), 0)


def _bug_sort_timestamp(bug: Bug) -> float:
    value = bug.updated_at or bug.created_at
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return float(value.timestamp())


def _prioritize_assignee_bugs(bugs: list[Bug], *, user_email: str) -> list[Bug]:
    user_email_norm = _normalize_email(user_email)

    def _sort_key(item: Bug) -> tuple[int, int, int, float]:
        mine = _normalize_email(item.assignee_id) == user_email_norm
        is_closed = normalize_bug_status(item.status) == "resolved"
        return (
            0 if mine else 1,
            1 if is_closed else 0,
            -_severity_priority(item.severity),
            -_bug_sort_timestamp(item),
        )

    return sorted(list(bugs), key=_sort_key)


def _create_bug(
    user: dict[str, str],
    *,
    title: str,
    description: str,
    severity: str,
    category: str,
    environment: str | None,
    tags: str | None,
    notify_emails: str | None,
    assignee_id: str | None,
    attachments: list | None = None,
    allowed_assignees: set[str] | None = None,
) -> str | None:
    title_clean = title.strip()
    description_clean = description.strip()
    if not title_clean or not description_clean:
        return "Tittel og beskrivelse er påkrevd."
    severity_clean = severity if severity in SEVERITY_OPTIONS else "medium"
    category_clean = category if category in CATEGORY_OPTIONS else "software"
    assignee_clean = (assignee_id or "").strip().casefold() or None
    environment_clean = str(environment or "").strip() or None
    tags_clean = str(tags or "").strip() or None
    notify_candidates = _parse_email_list(notify_emails)
    invalid_notify = [entry for entry in notify_candidates if not _is_valid_email(entry)]
    if invalid_notify:
        return f"Ugyldige e-postadresser i varsling: {', '.join(invalid_notify)}"
    notify_clean = ", ".join(notify_candidates) if notify_candidates else None

    if assignee_clean and not _is_valid_email(assignee_clean):
        return "Tildelt e-postadresse er ugyldig."
    if assignee_clean and allowed_assignees is not None and assignee_clean not in allowed_assignees:
        return "Tildelt bruker er ikke i listen over gyldige tildelbare brukere."

    try:
        with db_session() as db:
            _ensure_user_exists(db, email=user["email"], role=user["role"])
            if assignee_clean:
                _ensure_user_exists(db, email=assignee_clean, role=_role_for_email(assignee_clean))
            bug = Bug(
                title=title_clean,
                description=description_clean,
                category=category_clean,
                severity=severity_clean,
                status="open",
                environment=environment_clean,
                tags=tags_clean,
                notify_emails=notify_clean,
                reporter_id=user["email"],
                assignee_id=assignee_clean,
                reporting_date=datetime.now(timezone.utc),
            )
            db.add(bug)
            db.flush()

            uploaded_files = list(attachments or [])
            upload_errors: list[str] = []
            if len(uploaded_files) > MAX_ATTACHMENTS_PER_UPLOAD:
                upload_errors.append(f"Maks {MAX_ATTACHMENTS_PER_UPLOAD} vedlegg per innsending.")
                uploaded_files = uploaded_files[:MAX_ATTACHMENTS_PER_UPLOAD]

            for upload in uploaded_files:
                file_name = str(getattr(upload, "name", "") or "").strip()
                if not file_name:
                    continue
                content = upload.getvalue()
                if not isinstance(content, (bytes, bytearray)):
                    continue
                if len(content) > MAX_ATTACHMENT_BYTES:
                    upload_errors.append(f"{file_name}: for stor fil (>{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB).")
                    continue

                try:
                    storage_ref = _ATTACHMENT_STORAGE.store_bytes(
                        payload=bytes(content),
                        file_name=file_name,
                        bug_id=bug.id,
                    )
                except AttachmentStorageError as exc:
                    upload_errors.append(f"{file_name}: lagring feilet ({exc}).")
                    continue

                db.add(
                    Attachment(
                        bug_id=bug.id,
                        filename=file_name,
                        content_type=str(getattr(upload, "type", "") or None),
                        storage_path=str(storage_ref),
                        uploaded_by=user["email"],
                    )
                )
            created_history = _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="created",
                details=f"Bug opprettet av {user['email']}",
            )
            if upload_errors:
                _write_history(
                    db,
                    bug_id=bug.id,
                    actor_email=user["email"],
                    action="attachment_warning",
                    details=" | ".join(upload_errors)[:1000],
                )
            db.flush()
            _emit_notifications_for_bug_created(
                db,
                bug=bug,
                actor_email=user["email"],
                history_id=int(created_history.id) if created_history.id is not None else None,
                notifications_enabled=_notifications_enabled_for_user(user),
            )
            _dispatch_notification_outbox(db)
            _mark_bug_search_index_dirty(db, bug_id=bug.id)
            _commit_with_retry(db, operation="Oppretting av bug")
    except RuntimeError as exc:
        return str(exc)
    except SQLAlchemyError as exc:
        return format_user_error("Kunne ikke opprette bug", exc, fallback="Databasen svarte med en feil.")
    _clear_bug_cache()
    return None


def _upload_attachments_for_bug(user: dict[str, str], bug_id: int, attachments: list | None) -> list[str]:
    uploaded_files = list(attachments or [])
    if not uploaded_files:
        return []

    upload_errors: list[str] = []
    if len(uploaded_files) > MAX_ATTACHMENTS_PER_UPLOAD:
        upload_errors.append(f"Maks {MAX_ATTACHMENTS_PER_UPLOAD} vedlegg per innsending.")
        uploaded_files = uploaded_files[:MAX_ATTACHMENTS_PER_UPLOAD]

    uploaded_count = 0
    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if not bug:
                return ["Fant ikke bug."]
            if _is_deleted_bug(bug):
                return ["Bugen er i papirkurv og kan ikke oppdateres."]
            if normalize_bug_status(bug.status) == "resolved":
                return ["Bugen er løst og kan ikke oppdateres. Sett status tilbake til Åpen først."]

        for upload in uploaded_files:
            file_name = str(getattr(upload, "name", "") or "").strip()
            if not file_name:
                continue
            content = upload.getvalue()
            if not isinstance(content, (bytes, bytearray)):
                upload_errors.append(f"{file_name}: kunne ikke lese filinnhold.")
                continue
            if len(content) > MAX_ATTACHMENT_BYTES:
                upload_errors.append(f"{file_name}: for stor fil (>{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB).")
                continue

            try:
                storage_ref = _ATTACHMENT_STORAGE.store_bytes(
                    payload=bytes(content),
                    file_name=file_name,
                    bug_id=bug.id,
                )
            except AttachmentStorageError as exc:
                upload_errors.append(f"{file_name}: lagring feilet ({exc}).")
                continue

            db.add(
                Attachment(
                    bug_id=bug.id,
                    filename=file_name,
                    content_type=str(getattr(upload, "type", "") or None),
                    storage_path=str(storage_ref),
                    uploaded_by=user["email"],
                )
            )
            uploaded_count += 1

        if uploaded_count > 0:
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="attachment_uploaded",
                details=f"{uploaded_count} vedlegg lastet opp.",
            )
        if upload_errors:
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="attachment_warning",
                details=" | ".join(upload_errors)[:1000],
            )
            if uploaded_count > 0:
                _mark_bug_search_index_dirty(db, bug_id=bug.id)
            if uploaded_count > 0 or upload_errors:
                _commit_with_retry(db, operation="Opplasting av vedlegg")
    except RuntimeError as exc:
        return [str(exc)]
    except SQLAlchemyError as exc:
        return [format_user_error("Kunne ikke laste opp vedlegg", exc, fallback="Databasen svarte med en feil.")]
    _clear_bug_cache()
    return upload_errors


def _add_comment(user: dict[str, str], bug_id: int, body: str) -> str | None:
    text = body.strip()
    if not text:
        return "Kommentaren er tom."
    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if not bug:
                return "Fant ikke bug."
            if _is_deleted_bug(bug):
                return "Bugen er i papirkurv og kan ikke oppdateres."
            if normalize_bug_status(bug.status) == "resolved":
                return "Bugen er løst og kan ikke oppdateres. Sett status tilbake til Åpen først."
            comment_row = BugComment(
                bug_id=bug.id,
                author_email=user["email"],
                author_role=user["role"],
                body=text,
            )
            db.add(comment_row)
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="comment_added",
                details=text[:400],
            )
            db.flush()
            _emit_notifications_for_comment(
                db,
                bug=bug,
                actor_email=user["email"],
                comment_id=int(comment_row.id) if comment_row.id is not None else None,
                comment_body=text,
                notifications_enabled=_notifications_enabled_for_user(user),
            )
            _dispatch_notification_outbox(db)
            _mark_bug_search_index_dirty(db, bug_id=bug.id)
            _commit_with_retry(db, operation="Lagring av kommentar")
    except RuntimeError as exc:
        return str(exc)
    except SQLAlchemyError as exc:
        return format_user_error("Kunne ikke lagre kommentar", exc, fallback="Databasen svarte med en feil.")
    _clear_bug_cache()
    return None


def _update_bug(
    user: dict[str, str],
    *,
    bug_id: int,
    status: str,
    severity: str,
    assignee_id: str | None,
    category: str | None = None,
    environment: str | None = None,
    tags: str | None = None,
    notify_emails: str | None = None,
    description: str | None = None,
    reporter_satisfaction: str | None = None,
) -> str | None:
    assignee_clean = (assignee_id or "").strip().casefold() or None
    notify_candidates = _parse_email_list(notify_emails)
    invalid_notify = [entry for entry in notify_candidates if not _is_valid_email(entry)]
    status_clean = normalize_bug_status(status)
    severity_clean = severity if severity in SEVERITY_OPTIONS else "medium"
    category_clean = category if (category in CATEGORY_OPTIONS) else None
    notify_clean = ", ".join(notify_candidates) if notify_candidates else None
    description_clean = str(description or "").strip()
    satisfaction_clean: str | None = None
    if reporter_satisfaction is not None:
        candidate = str(reporter_satisfaction or "").strip()
        satisfaction_clean = candidate if candidate in REPORTER_SATISFACTION_OPTIONS else None

    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if not bug:
                return "Fant ikke bug."
            if _is_deleted_bug(bug):
                return "Bugen ligger i papirkurv. Gjenopprett bugen før oppdatering."

            current_status = normalize_bug_status(bug.status)
            previous_status = current_status
            previous_assignee = _normalize_email(bug.assignee_id)
            previous_severity = str(bug.severity or "").strip().casefold()
            previous_overdue = False
            if previous_status != "resolved":
                previous_overdue = bool(_bug_sla_snapshot(bug).get("overdue"))
            if current_status == "resolved":
                if status_clean != "open":
                    return "Bugen er løst og kan ikke oppdateres. Sett status tilbake til Åpen først."
                if not _policy_allows(
                    policy_key="policy.reopen_roles",
                    user_role=user["role"],
                    default_roles={"admin", "assignee", "reporter"},
                ):
                    return "Du har ikke rettighet til å gjenåpne løste bugs."
                bug.status = "open"
                bug.closed_at = None
                reopened_history = _write_history(
                    db,
                    bug_id=bug.id,
                    actor_email=user["email"],
                    action="reopened",
                    details="Bug satt tilbake til Åpen.",
                )
                db.flush()
                _emit_notifications_for_update(
                    db,
                    bug=bug,
                    actor_email=user["email"],
                    history_id=int(reopened_history.id) if reopened_history.id is not None else None,
                    previous_status=previous_status,
                    previous_assignee=previous_assignee,
                    previous_severity=previous_severity,
                    previous_overdue=previous_overdue,
                    new_status=normalize_bug_status(bug.status),
                    new_assignee=_normalize_email(bug.assignee_id),
                    new_severity=str(bug.severity or "").strip().casefold(),
                    new_overdue=bool(_bug_sla_snapshot(bug).get("overdue")),
                    notifications_enabled=_notifications_enabled_for_user(user),
                )
                _dispatch_notification_outbox(db)
                _mark_bug_search_index_dirty(db, bug_id=bug.id)
                _commit_with_retry(db, operation="Gjenåpning av bug")
                _clear_bug_cache()
                return None

            if assignee_clean and not _is_valid_email(assignee_clean):
                return "Tildelt e-postadresse er ugyldig."
            if invalid_notify:
                return f"Ugyldige e-postadresser i varsling: {', '.join(invalid_notify)}"

            if assignee_clean:
                _ensure_user_exists(db, email=assignee_clean, role=_role_for_email(assignee_clean))
            bug.status = status_clean
            bug.severity = severity_clean
            bug.assignee_id = assignee_clean
            if category_clean:
                bug.category = category_clean
            if environment is not None:
                bug.environment = str(environment).strip() or None
            if tags is not None:
                bug.tags = str(tags).strip() or None
            if notify_emails is not None:
                bug.notify_emails = notify_clean
            if description is not None and description_clean:
                bug.description = description_clean
            if reporter_satisfaction is not None:
                bug.reporter_satisfaction = satisfaction_clean

            if status_clean == "resolved" and bug.closed_at is None:
                bug.closed_at = datetime.now(timezone.utc)
            if status_clean != "resolved":
                bug.closed_at = None
            updated_history = _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="updated",
                details=(
                    f"status={status_clean}, severity={severity_clean}, "
                    f"assignee={assignee_clean or '-'}, satisfaction={satisfaction_clean or '-'}"
                ),
            )
            db.flush()
            new_overdue = False
            if status_clean != "resolved":
                new_overdue = bool(_bug_sla_snapshot(bug).get("overdue"))
            _emit_notifications_for_update(
                db,
                bug=bug,
                actor_email=user["email"],
                history_id=int(updated_history.id) if updated_history.id is not None else None,
                previous_status=previous_status,
                previous_assignee=previous_assignee,
                previous_severity=previous_severity,
                previous_overdue=previous_overdue,
                new_status=status_clean,
                new_assignee=assignee_clean,
                new_severity=str(severity_clean).strip().casefold(),
                new_overdue=new_overdue,
                notifications_enabled=_notifications_enabled_for_user(user),
            )
            _dispatch_notification_outbox(db)
            _mark_bug_search_index_dirty(db, bug_id=bug.id)
            _commit_with_retry(db, operation="Oppdatering av bug")
    except RuntimeError as exc:
        return str(exc)
    except SQLAlchemyError as exc:
        return format_user_error("Kunne ikke oppdatere bug", exc, fallback="Databasen svarte med en feil.")
    _clear_bug_cache()
    return None


def _delete_bug(user: dict[str, str], bug_id: int) -> str | None:
    if not _policy_allows(
        policy_key="policy.delete_roles",
        user_role=user["role"],
        default_roles={"admin", "assignee"},
    ):
        return "Du har ikke rettighet til å slette bugs."
    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if not bug:
                return "Fant ikke bug."
            if _is_deleted_bug(bug):
                return "Bugen ligger allerede i papirkurv."
            bug.deleted_at = datetime.now(timezone.utc)
            bug.deleted_by = user["email"]
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="soft_deleted",
                details=f"Flyttet til papirkurv av {user['email']}.",
            )
            index_row = db.get(BugSearchIndex, bug.id)
            if index_row is not None:
                db.delete(index_row)
            _commit_with_retry(db, operation="Flytting til papirkurv")
    except RuntimeError as exc:
        return str(exc)
    except SQLAlchemyError as exc:
        return format_user_error("Kunne ikke flytte bug til papirkurv", exc, fallback="Databasen svarte med en feil.")
    _clear_bug_cache()
    return None


def _restore_deleted_bug(user: dict[str, str], bug_id: int) -> str | None:
    if str(user.get("role") or "").strip().casefold() != "admin":
        return "Kun admin kan gjenopprette bugs fra papirkurv."
    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if not bug:
                return "Fant ikke bug."
            if not _is_deleted_bug(bug):
                return "Bugen ligger ikke i papirkurv."
            bug.deleted_at = None
            bug.deleted_by = None
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="restored",
                details="Gjenopprettet fra papirkurv.",
            )
            _mark_bug_search_index_dirty(db, bug_id=bug.id)
            _commit_with_retry(db, operation="Gjenoppretting av bug")
    except RuntimeError as exc:
        return str(exc)
    except SQLAlchemyError as exc:
        return format_user_error("Kunne ikke gjenopprette bug", exc, fallback="Databasen svarte med en feil.")
    _clear_bug_cache()
    return None


def _hard_delete_bug(user: dict[str, str], bug_id: int) -> str | None:
    if not _policy_allows(
        policy_key="policy.hard_delete_roles",
        user_role=user["role"],
        default_roles={"admin"},
    ):
        return "Du har ikke rettighet til permanent sletting."
    try:
        with db_session() as db:
            bug = db.get(Bug, bug_id)
            if not bug:
                return "Fant ikke bug."
            attachments = list(bug.attachments or [])
            for item in attachments:
                storage_ref = str(item.storage_path or "").strip()
                if not storage_ref:
                    continue
                try:
                    _ATTACHMENT_STORAGE.delete(storage_ref)
                except AttachmentStorageError:
                    pass
            db.delete(bug)
            _commit_with_retry(db, operation="Permanent sletting av bug")
    except RuntimeError as exc:
        return str(exc)
    except SQLAlchemyError as exc:
        return format_user_error("Kunne ikke slette bug permanent", exc, fallback="Databasen svarte med en feil.")
    _clear_bug_cache()
    return None


def _render_bug_thread(
    bug: Bug,
    *,
    title: str = "Samtale",
    collapsed: bool = False,
    dedupe_consecutive: bool = True,
) -> None:
    comments = _resolve_bug_comments(bug)
    comments.sort(key=lambda item: item.created_at or datetime.min)
    if dedupe_consecutive:
        deduped_comments: list[BugComment] = []
        previous_signature: tuple[str, str, str] | None = None
        for item in comments:
            signature = (
                str(item.author_role or ""),
                str(item.author_email or ""),
                str(item.body or "").strip(),
            )
            if signature == previous_signature:
                continue
            deduped_comments.append(item)
            previous_signature = signature
        comments = deduped_comments

    if not comments:
        st.caption("Ingen samtale ennå.")
        return

    def _render_body() -> None:
        for comment in comments:
            ts = format_datetime_display(comment.created_at)
            st.markdown(f"**{comment.author_role} ({comment.author_email})** • {ts}")
            st.write(comment.body)

    if collapsed:
        with st.expander(title, expanded=False):
            _render_body()
    else:
        st.caption(title)
        _render_body()


def _render_bug_history(
    bug: Bug,
    *,
    title: str = "Endringshistorikk",
    collapsed: bool = True,
) -> None:
    entries = _resolve_bug_history_entries(bug)
    entries.sort(key=lambda item: item.created_at or datetime.min, reverse=True)
    if not entries:
        st.caption("Ingen endringshistorikk ennå.")
        return

    def _render_body() -> None:
        for item in entries:
            ts = format_datetime_display(item.created_at)
            action = str(item.action or "updated")
            details = str(item.details or "").strip() or "-"
            actor = str(item.actor_email or "-")
            st.markdown(f"**{ts}** • `{action}` • `{actor}`")
            st.caption(details[:800])

    if collapsed:
        with st.expander(title, expanded=False):
            _render_body()
    else:
        st.caption(title)
        _render_body()


def _render_attachments(bug: Bug, *, key_prefix: str) -> None:
    attachments = _resolve_bug_attachments(bug)
    attachments.sort(key=lambda item: item.created_at or datetime.min, reverse=True)
    if not attachments:
        st.caption("Ingen vedlegg.")
        return

    st.caption("Vedlegg")
    for idx, item in enumerate(attachments):
        file_name = str(item.filename or "vedlegg")
        created = format_datetime_display(item.created_at)
        storage_ref = str(item.storage_path or "")
        label = f"{file_name} ({created})"
        data = _ATTACHMENT_STORAGE.read_bytes(storage_ref)
        if data is None:
            st.warning(f"{label} - fil mangler på disk.")
            continue
        st.download_button(
            f"Last ned {file_name}",
            data=data,
            file_name=file_name,
            mime=str(item.content_type or "application/octet-stream"),
            key=f"{key_prefix}_dl_{item.id}_{idx}",
            use_container_width=True,
        )


def _resolve_bug_comments(bug: Bug) -> list[BugComment]:
    bug_id = int(bug.id)
    comments_cache = st.session_state.setdefault("_bug_comments_cache", {})
    cached = comments_cache.get(bug_id)
    if isinstance(cached, list):
        return list(cached)
    try:
        rows = list(bug.comments or [])
        comments_cache[bug_id] = rows
        return rows
    except DetachedInstanceError:
        with db_session() as db:
            rows = db.scalars(
                select(BugComment)
                .where(BugComment.bug_id == bug_id)
                .order_by(BugComment.created_at.asc(), BugComment.id.asc())
            ).all()
            normalized = list(rows)
            comments_cache[bug_id] = normalized
            return normalized


def _resolve_bug_history_entries(bug: Bug) -> list[BugHistory]:
    bug_id = int(bug.id)
    history_cache = st.session_state.setdefault("_bug_history_cache", {})
    cached = history_cache.get(bug_id)
    if isinstance(cached, list):
        return list(cached)
    try:
        rows = list(bug.history or [])
        history_cache[bug_id] = rows
        return rows
    except DetachedInstanceError:
        with db_session() as db:
            rows = db.scalars(
                select(BugHistory)
                .where(BugHistory.bug_id == bug_id)
                .order_by(BugHistory.created_at.desc(), BugHistory.id.desc())
            ).all()
            normalized = list(rows)
            history_cache[bug_id] = normalized
            return normalized


def _resolve_bug_attachments(bug: Bug) -> list[Attachment]:
    bug_id = int(bug.id)
    attachment_cache = st.session_state.setdefault("_bug_attachments_cache", {})
    cached = attachment_cache.get(bug_id)
    if isinstance(cached, list):
        return list(cached)
    try:
        rows = list(bug.attachments or [])
        attachment_cache[bug_id] = rows
        return rows
    except DetachedInstanceError:
        with db_session() as db:
            rows = db.scalars(
                select(Attachment)
                .where(Attachment.bug_id == bug_id)
                .order_by(Attachment.created_at.desc(), Attachment.id.desc())
            ).all()
            normalized = list(rows)
            attachment_cache[bug_id] = normalized
            return normalized


def _prefetch_bug_details(bugs: list[Bug]) -> None:
    bug_ids = sorted({int(getattr(item, "id", 0) or 0) for item in bugs if int(getattr(item, "id", 0) or 0) > 0})
    if not bug_ids:
        return

    comments_cache = st.session_state.setdefault("_bug_comments_cache", {})
    history_cache = st.session_state.setdefault("_bug_history_cache", {})
    attachments_cache = st.session_state.setdefault("_bug_attachments_cache", {})

    missing_comment_ids = [bug_id for bug_id in bug_ids if bug_id not in comments_cache]
    missing_history_ids = [bug_id for bug_id in bug_ids if bug_id not in history_cache]
    missing_attachment_ids = [bug_id for bug_id in bug_ids if bug_id not in attachments_cache]
    if not missing_comment_ids and not missing_history_ids and not missing_attachment_ids:
        return

    with db_session() as db:
        if missing_comment_ids:
            rows = db.scalars(
                select(BugComment)
                .where(BugComment.bug_id.in_(missing_comment_ids))
                .order_by(BugComment.bug_id.asc(), BugComment.created_at.asc(), BugComment.id.asc())
            ).all()
            grouped: dict[int, list[BugComment]] = {bug_id: [] for bug_id in missing_comment_ids}
            for row in rows:
                grouped.setdefault(int(row.bug_id), []).append(row)
            comments_cache.update(grouped)

        if missing_history_ids:
            rows = db.scalars(
                select(BugHistory)
                .where(BugHistory.bug_id.in_(missing_history_ids))
                .order_by(BugHistory.bug_id.asc(), BugHistory.created_at.desc(), BugHistory.id.desc())
            ).all()
            grouped: dict[int, list[BugHistory]] = {bug_id: [] for bug_id in missing_history_ids}
            for row in rows:
                grouped.setdefault(int(row.bug_id), []).append(row)
            history_cache.update(grouped)

        if missing_attachment_ids:
            rows = db.scalars(
                select(Attachment)
                .where(Attachment.bug_id.in_(missing_attachment_ids))
                .order_by(Attachment.bug_id.asc(), Attachment.created_at.desc(), Attachment.id.desc())
            ).all()
            grouped: dict[int, list[Attachment]] = {bug_id: [] for bug_id in missing_attachment_ids}
            for row in rows:
                grouped.setdefault(int(row.bug_id), []).append(row)
            attachments_cache.update(grouped)


def _page_render_deps() -> dict[str, Any]:
    return dict(globals())


def _render_reporter_page(user: dict[str, str]) -> None:
    started = time.perf_counter()
    _render_reporter_page_module(user, **_page_render_deps())
    _record_runtime_metric("page_reporter_ms", (time.perf_counter() - started) * 1000.0)


def _render_assignee_page(user: dict[str, str]) -> None:
    started = time.perf_counter()
    _render_assignee_page_module(user, **_page_render_deps())
    _record_runtime_metric("page_assignee_ms", (time.perf_counter() - started) * 1000.0)


def _render_admin_page(user: dict[str, str]) -> None:
    started = time.perf_counter()
    _render_admin_page_module(user, **_page_render_deps())
    _record_runtime_metric("page_admin_ms", (time.perf_counter() - started) * 1000.0)


def main() -> None:
    init_started_at = time.perf_counter()
    st.set_page_config(page_title="AI-drevet bugsystem", layout="wide")
    apply_shared_app_style()
    try:
        with st.spinner("Starter app og klargjør data ..."):
            _init_local_data()
    except Exception as exc:
        logger.exception("CloudTest startup failed")
        detail = str(exc).strip()
        if detail:
            st.error(f"Oppstart feilet: {exc.__class__.__name__}: {detail}")
        else:
            st.error(
                format_user_error(
                    "Oppstart feilet",
                    exc,
                    fallback="Sjekk DATABASE_URL/pgvector-oppsett og prøv igjen.",
                )
            )
        return

    init_duration = time.perf_counter() - init_started_at

    render_sidebar_logo(app_title="AI-drevet bugsystem")
    if init_duration >= 2.0:
        st.sidebar.caption(f"Oppstartstid: {init_duration:.1f} s")
    st.title("AI-drevet bugsystem")
    if not _auth_gate():
        return

    user = _current_user()
    if not user:
        return

    # Reset per-rerun sidebar section registry.
    st.session_state["_sidebar_render_once_registry"] = set()

    pages = ["Reporter"]
    if user["role"] in {"assignee", "admin"}:
        pages.append("Assignee")
    if user["role"] == "admin":
        pages.append("Admin")

    selected_page = st.radio(
        "Visning",
        options=pages,
        index=0,
        horizontal=True,
        key="top_page_selector",
        label_visibility="collapsed",
    )
    selected_prefix = "reporter"
    if selected_page == "Reporter":
        _render_reporter_page(user)
        selected_prefix = "reporter"
    elif selected_page == "Assignee":
        _render_assignee_page(user)
        selected_prefix = "assignee"
    else:
        _render_admin_page(user)
        selected_prefix = "admin"

    # Keep TODO as the final sidebar block regardless of active page.
    if _sidebar_should_render(selected_prefix, "TODO"):
        devops_allowed, devops_reason = _devops_access_state(user)
        devops_enabled = _devops_ui_enabled()
        _render_todo_sidebar(
            devops_enabled=devops_enabled,
            devops_allowed=devops_allowed,
            devops_reason=devops_reason,
        )


if __name__ == "__main__":
    main()


