from __future__ import annotations

import os
import sys
import json
import time
import re
import shutil
import sqlite3
import tempfile
import zipfile
from io import BytesIO
from difflib import SequenceMatcher
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping
from statistics import mean
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

CLOUD_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = CLOUD_ROOT.parent


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

    def _truthy(value: str | None) -> bool:
        return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}

    def _host_from_database_url(database_url: str) -> str:
        try:
            return str(urlparse(database_url).hostname or "").strip().casefold()
        except Exception:
            return ""

    running_on_streamlit_cloud = _truthy(os.getenv("STREAMLIT_CLOUD"))
    allow_sqlite_fallback = _truthy(os.getenv("CLOUD_TEST_ALLOW_SQLITE_FALLBACK"))
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
    render_bug_list_controls,
    render_bug_status_summary,
    render_sidebar_bug_filters,
    render_sidebar_refresh_button,
    render_sidebar_search,
    status_label,
)
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.core.logging import get_logger
from app.core.security import get_password_hash, verify_password
from app.models.background_job import BackgroundJob
from app.models.bug import AppRuntimeMeta, Attachment, Bug, BugComment, BugHistory, BugSearchIndex
from app.models.user import User
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
    selected_ai_model as _selected_ai_model,
)
from page_admin import render_admin_page as _render_admin_page_module
from page_assignee import render_assignee_page as _render_assignee_page_module
from page_reporter import render_reporter_page as _render_reporter_page_module

logger = get_logger("cloud_test.unified")
_ATTACHMENT_STORAGE = build_attachment_storage()


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
    value = str(os.getenv("BUGSEARCH_DISABLE_PGVECTOR", "")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


def _cloud_test_mode_enabled() -> bool:
    value = str(os.getenv("STREAMLIT_CLOUD_TEST_MODE", "")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


def _legacy_schema_bootstrap_enabled() -> bool:
    value = str(os.getenv("CLOUDTEST_ENABLE_LEGACY_SCHEMA_BOOTSTRAP", "")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


def _allow_migration_failure_fallback() -> bool:
    value = str(os.getenv("CLOUDTEST_ALLOW_MIGRATION_FALLBACK", "true")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


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
        c1, c2 = st.columns(2)
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
        "reporter_ai_debug_details": "",
        "reporter_ai_file_extract_summary": "",
        "reporter_similar_results": [],
        "reporter_similar_query": "",
        "reporter_typeahead_suggestion": "",
        "reporter_typeahead_error": "",
        "reporter_typeahead_source": "",
        "reporter_duplicate_exact_id": None,
        "reporter_duplicate_candidates": [],
        "reporter_confirm_unique_bug": False,
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
    st.session_state["reporter_typeahead_suggestion"] = ""
    st.session_state["reporter_typeahead_error"] = ""
    st.session_state["reporter_typeahead_source"] = ""
    st.session_state["reporter_duplicate_exact_id"] = None
    st.session_state["reporter_duplicate_candidates"] = []
    st.session_state["reporter_duplicate_checked"] = False
    st.session_state["reporter_confirm_unique_bug"] = False
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


def _build_assignable_emails() -> list[str]:
    with db_session() as db:
        query = select(User.email, User.role)
        rows = db.execute(query).all()
    allowed_roles = {"assignee", "admin"}
    return sorted(
        {
            _normalize_email(email)
            for email, role in rows
            if _normalize_email(email) and str(role or "").strip().casefold() in allowed_roles
        }
    )


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


def _apply_reporter_ai_draft(payload: dict) -> None:
    st.session_state["reporter_create_title"] = str(payload.get("title", "") or "").strip()
    st.session_state["reporter_create_description"] = str(payload.get("description", "") or "").strip()

    severity = str(payload.get("severity", "") or "").strip().casefold()
    category = str(payload.get("category", "") or "").strip().casefold()
    assignee_email = _normalize_email(str(payload.get("assignee_email", "") or ""))
    notify_value = payload.get("notify_emails", "")
    environment = str(payload.get("environment", "") or "").strip()
    tags_value = payload.get("tags", "")

    if severity in SEVERITY_OPTIONS:
        st.session_state["reporter_create_severity"] = severity
    if category in CATEGORY_OPTIONS:
        st.session_state["reporter_create_category"] = category
    if assignee_email:
        st.session_state["reporter_create_assignee"] = assignee_email

    if isinstance(notify_value, list):
        st.session_state["reporter_create_notify_emails"] = ", ".join(_parse_email_list(", ".join(str(x) for x in notify_value)))
    else:
        st.session_state["reporter_create_notify_emails"] = ", ".join(_parse_email_list(str(notify_value)))

    st.session_state["reporter_create_environment"] = environment
    if isinstance(tags_value, list):
        st.session_state["reporter_create_tags"] = ", ".join(str(x).strip() for x in tags_value if str(x).strip())
    else:
        st.session_state["reporter_create_tags"] = str(tags_value or "").strip()


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

    if len(title) < 3:
        return "Tittel må være minst 3 tegn."
    if len(description) < 10:
        return "Beskrivelse må være minst 10 tegn."
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
    enable_test_login = str(_config_value("CLOUD_TEST_ENABLE_TEST_LOGIN", "true")).strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    test_login_email = _normalize_email(_config_value("CLOUD_TEST_LOCAL_TEST_EMAIL", "admin@example.com"))
    test_login_password = _config_value("CLOUD_TEST_LOCAL_TEST_PASSWORD", "admin123")
    if enable_test_login and _is_valid_email(test_login_email) and test_login_password:
        with db_session() as db:
            test_user = db.get(User, test_login_email)
            if test_user:
                test_user.role = "admin"
                test_user.auth_provider = "local"
                test_user.password_hash = get_password_hash(test_login_password)
            else:
                db.add(
                    User(
                        email=test_login_email,
                        full_name="Local Test Admin",
                        password_hash=get_password_hash(test_login_password),
                        role="admin",
                        auth_provider="local",
                    )
                )
            _commit_with_retry(db, operation="Oppretting av lokal test-innlogging")

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


def _write_history(db, *, bug_id: int, actor_email: str, action: str, details: str) -> None:
    db.add(BugHistory(bug_id=bug_id, action=action, details=details, actor_email=actor_email))


def _current_user() -> dict[str, str] | None:
    email = str(st.session_state.get("email") or "").strip()
    role = str(st.session_state.get("role") or "").strip()
    if not email or not role:
        return None
    return {"email": email, "role": role}


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


def _auth_gate() -> bool:
    if st.session_state.get("_auth_error"):
        st.sidebar.error(str(st.session_state.get("_auth_error")))
        st.session_state.pop("_auth_error", None)
    return _render_auth_gate(
        allow_local_login=_allow_local_login,
        current_user=_current_user,
        set_user=_set_user,
        db_session=db_session,
        verify_password=verify_password,
        user_model=User,
        local_default_email=_config_value("CLOUD_TEST_LOCAL_TEST_EMAIL", "admin@example.com"),
        local_default_password=_config_value("CLOUD_TEST_LOCAL_TEST_PASSWORD", "admin123"),
        enable_test_login=str(_config_value("CLOUD_TEST_ENABLE_TEST_LOGIN", "true")).strip().casefold()
        in {"1", "true", "yes", "on"},
        logger=logger,
    )


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


def _prepare_page_bug_list(*, user: dict[str, str], prefix: str) -> list[Bug]:
    _render_ai_and_embedding_sidebar_settings(prefix=prefix)
    _render_system_and_ops_sidebar(jobs=_background_jobs_snapshot(), telemetry=_runtime_performance_snapshot())
    if render_sidebar_refresh_button(prefix):
        _clear_bug_cache()
        st.rerun()

    bugs = _load_bugs_for_user_cached(user)
    search_labels = {
        "reporter": "Søk i dine bugs (vektorsøk)",
        "assignee": "Søk i bugs (vektorsøk)",
        "admin": "Søk i bugs (vektorsøk)",
    }
    query = render_sidebar_search(prefix, label=search_labels.get(prefix, "Søk i bugs (vektorsøk)"))
    render_sidebar_bug_filters(prefix, bugs)

    candidate_bugs = bugs
    vector_search_active = False
    if query:
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
            candidate_bugs = semantic_results
            vector_search_active = True
            st.caption(f"Viser vektorsøk-resultater for: {query}")
        except (SQLAlchemyError, DetachedInstanceError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Vector search failed for prefix=%s query_len=%s error=%s",
                prefix,
                len(query),
                exc.__class__.__name__,
            )
            st.warning(format_user_error("Vektorsøk feilet", exc, fallback="Bruker lokal søkefallback i denne visningen."))
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            _record_runtime_metric(f"search_{prefix}_ms", elapsed_ms)

    st.session_state[f"{prefix}_vector_search_active"] = vector_search_active
    return apply_sidebar_bug_filters(
        bugs=candidate_bugs,
        prefix=prefix,
        apply_query_filter=not vector_search_active,
    )


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
            _write_history(
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
            db.add(
                BugComment(
                    bug_id=bug.id,
                    author_email=user["email"],
                    author_role=user["role"],
                    body=text,
                )
            )
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="comment_added",
                details=text[:400],
            )
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
                _write_history(
                    db,
                    bug_id=bug.id,
                    actor_email=user["email"],
                    action="reopened",
                    details="Bug satt tilbake til Åpen.",
                )
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
            _write_history(
                db,
                bug_id=bug.id,
                actor_email=user["email"],
                action="updated",
                details=(
                    f"status={status_clean}, severity={severity_clean}, "
                    f"assignee={assignee_clean or '-'}, satisfaction={satisfaction_clean or '-'}"
                ),
            )
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

    pages = ["Reporter"]
    if user["role"] in {"assignee", "admin"}:
        pages.append("Assignee")
    if user["role"] == "admin":
        pages.append("Admin")

    page = st.sidebar.radio("Arbeidsflate", pages, index=0)
    if page == "Reporter":
        _render_reporter_page(user)
    elif page == "Assignee":
        _render_assignee_page(user)
    else:
        _render_admin_page(user)


if __name__ == "__main__":
    main()


