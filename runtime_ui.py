from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any

import streamlit as st

from app.core.config import settings
from app.services.ai_provider import get_ai_provider_status, get_embedding_provider_status
from app.services.health import get_ready_health

SEVERITY_OPTIONS = ["low", "medium", "high", "critical"]
STATUS_OPTIONS = ["open", "resolved"]
CATEGORY_OPTIONS = ["software", "hardware", "network", "security", "other"]
REPORTER_SATISFACTION_OPTIONS = [
    "Very satisfied",
    "Satisfied",
    "Neutral",
    "Dissatisfied",
    "Very dissatisfied",
]
MAX_ATTACHMENTS_PER_UPLOAD = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_AI_EXTRACTED_TEXT_CHARS = 20000

AI_MODEL_OPTIONS = ["gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1", "gpt-5-mini"]
EMBEDDING_PROVIDER_OPTIONS = ["openai", "local"]
EMBEDDING_MODEL_OPTIONS = {
    "openai": ["text-embedding-3-small", "text-embedding-3-large"],
    "local": [
        "sentence-transformers/all-MiniLM-L6-v2",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    ],
}


def _sqlite_vec_lock_active() -> bool:
    explicit = getattr(settings, "sqlite_vec_lock_active", None)
    if explicit is not None:
        return bool(explicit)
    return bool(getattr(settings, "database_is_sqlite", False) and getattr(settings, "sqlite_vec_enabled", False))


def _sqlite_vec_embedding_provider() -> str:
    provider = str(getattr(settings, "sqlite_vec_embedding_provider", "openai") or "openai").strip().casefold()
    if provider not in {"openai", "local"}:
        provider = "openai"
    return provider


def _sqlite_vec_embedding_model(provider: str) -> str:
    provider = provider if provider in {"openai", "local"} else "openai"
    default_model = settings.local_embedding_model if provider == "local" else settings.embedding_model
    configured_model = str(getattr(settings, "sqlite_vec_embedding_model", default_model) or default_model).strip()
    return configured_model or default_model


def truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def config_value(name: str, default: str = "") -> str:
    env_value = os.getenv(name)
    if env_value is not None and str(env_value).strip():
        return str(env_value).strip()

    key_lower = name.casefold()
    try:
        app_cfg = st.secrets.get("app", {})
        if isinstance(app_cfg, Mapping):
            app_value = app_cfg.get(key_lower, app_cfg.get(name))
            if app_value is not None and str(app_value).strip():
                return str(app_value).strip()

        root_value = st.secrets.get(name, st.secrets.get(key_lower))
        if root_value is not None and str(root_value).strip():
            return str(root_value).strip()
    except Exception:
        pass

    return default


def allow_local_login() -> bool:
    return truthy(config_value("CLOUD_TEST_ALLOW_LOCAL_LOGIN"), default=False)


def selected_ai_model() -> str:
    env_default = config_value("OPENAI_MODEL", settings.openai_model or settings.ai_model or "gpt-4o-mini")
    current = str(st.session_state.get("global_ai_model", env_default) or env_default).strip()
    if not current:
        current = "gpt-4o-mini"
    return current


def selected_embedding_provider() -> str:
    if _sqlite_vec_lock_active():
        return _sqlite_vec_embedding_provider()

    env_default = str(
        config_value(
            "EMBEDDING_PROVIDER",
            settings.embedding_provider or "openai",
        )
    ).strip().casefold()
    if env_default not in {"openai", "local"}:
        env_default = "openai"
    current = str(st.session_state.get("global_embedding_provider", env_default) or env_default).strip().casefold()
    if current not in {"openai", "local"}:
        current = "openai"
    return current


def selected_embedding_model(provider: str | None = None) -> str:
    if _sqlite_vec_lock_active():
        resolved_provider = (provider or selected_embedding_provider()).strip().casefold()
        if resolved_provider not in {"openai", "local"}:
            resolved_provider = "openai"
        return _sqlite_vec_embedding_model(resolved_provider)

    resolved_provider = (provider or selected_embedding_provider()).strip().casefold()
    if resolved_provider not in {"openai", "local"}:
        resolved_provider = "openai"
    env_default = config_value(
        "EMBEDDING_MODEL",
        settings.embedding_model if resolved_provider == "openai" else settings.local_embedding_model,
    )
    model_options = EMBEDDING_MODEL_OPTIONS.get(resolved_provider, [])
    current = str(st.session_state.get("global_embedding_model", env_default) or env_default).strip()
    if model_options and current not in model_options:
        current = model_options[0]
    if not current and model_options:
        current = model_options[0]
    return current


def current_search_settings() -> dict[str, str]:
    provider = selected_embedding_provider()
    model = selected_embedding_model(provider)
    return {
        "ai_provider": "openai",
        "ai_model": selected_ai_model(),
        "embedding_provider": provider,
        "embedding_model": model,
    }


def render_ai_and_embedding_sidebar_settings(*, prefix: str) -> None:
    settings_payload = current_search_settings()
    ai_model = settings_payload["ai_model"]
    embedding_provider = settings_payload["embedding_provider"]
    with st.sidebar.expander("AI-innstillinger", expanded=False):
        st.caption("AI")
        st.selectbox(
            "AI-modell",
            options=AI_MODEL_OPTIONS,
            index=AI_MODEL_OPTIONS.index(ai_model) if ai_model in AI_MODEL_OPTIONS else 0,
            key="global_ai_model",
            help="Modellen brukes for AI-utkast, løsningsforslag, oppsummering og sentimentanalyse.",
        )
        run_ai_status_check = st.toggle(
            "Sjekk AI-status",
            key=f"{prefix}_ai_status_check",
            value=False,
            help="Kjører en rask kontroll av om valgt AI-modell er tilgjengelig.",
        )
        if run_ai_status_check:
            try:
                ai_status = get_ai_provider_status(ai_provider="openai", ai_model=selected_ai_model())
                available = bool(ai_status.get("available"))
                model_available = bool(ai_status.get("model_available", True))
                detail = str(ai_status.get("detail") or "")
                if available and model_available:
                    st.success(detail or "AI-modellen er tilgjengelig.")
                elif available:
                    st.warning(detail or "AI-leverandøren svarer, men modellen er ikke bekreftet.")
                else:
                    st.error(detail or "AI-leverandøren er ikke tilgjengelig.")
            except Exception as exc:
                st.error(f"AI-status feilet: {exc.__class__.__name__}: {exc}")

        st.divider()
        st.caption("Embedding")
        sqlite_vec_locked = _sqlite_vec_lock_active()
        if sqlite_vec_locked:
            st.info(
                "SQLite+sqlite-vec er aktiv: embedding-leverandør og modell er låst for konsistent indeks."
            )
        st.selectbox(
            "Embedding-leverandør",
            options=EMBEDDING_PROVIDER_OPTIONS,
            index=EMBEDDING_PROVIDER_OPTIONS.index(embedding_provider)
            if embedding_provider in EMBEDDING_PROVIDER_OPTIONS
            else 0,
            key="global_embedding_provider",
            help="Bestemmer hvordan semantisk vektorsøk kjøres.",
            disabled=sqlite_vec_locked,
        )

        selected_provider = selected_embedding_provider()
        provider_models = EMBEDDING_MODEL_OPTIONS.get(selected_provider, [])
        resolved_embedding_model = selected_embedding_model(selected_provider)
        if provider_models:
            st.selectbox(
                "Embedding-modell",
                options=provider_models,
                index=provider_models.index(resolved_embedding_model)
                if resolved_embedding_model in provider_models
                else 0,
                key="global_embedding_model",
                help="Modellen brukes for vektorsøk og lignende-bug-søk.",
                disabled=sqlite_vec_locked,
            )
        else:
            st.text_input(
                "Embedding-modell",
                value=resolved_embedding_model,
                key="global_embedding_model",
                disabled=sqlite_vec_locked,
            )

        run_embedding_status_check = st.toggle(
            "Sjekk embedding-status",
            key=f"{prefix}_embedding_status_check",
            value=False,
            help="Sjekker at embedding-oppsettet er tilgjengelig.",
        )
        if run_embedding_status_check:
            try:
                embedding_status = get_embedding_provider_status(
                    embedding_provider=selected_embedding_provider(),
                    embedding_model=selected_embedding_model(selected_embedding_provider()),
                )
                available = bool(embedding_status.get("available"))
                model_available = bool(embedding_status.get("model_available", True))
                detail = str(embedding_status.get("detail") or "")
                if available and model_available:
                    st.success(detail or "Embedding-oppsettet er tilgjengelig.")
                elif available:
                    st.warning(detail or "Embedding-leverandøren svarer, men modellen er ikke bekreftet.")
                else:
                    st.error(detail or "Embedding-oppsettet er ikke tilgjengelig.")
            except Exception as exc:
                st.error(f"Embedding-status feilet: {exc.__class__.__name__}: {exc}")


def _format_health_status(status: str) -> tuple[str, str]:
    normalized = str(status or "").strip().casefold()
    icon = {"ok": "🟢", "degraded": "🟡", "error": "🔴"}.get(normalized, "⚪")
    label = {"ok": "OK", "degraded": "Redusert", "error": "Feil"}.get(normalized, normalized or "unknown")
    return icon, label


def _get_cached_ready_health(*, max_age_seconds: int = 45) -> dict[str, Any] | None:
    payload = st.session_state.get("_system_health_payload")
    captured_at = float(st.session_state.get("_system_health_captured_at", 0.0) or 0.0)
    if not isinstance(payload, dict):
        return None
    if (time.time() - captured_at) > max(1, int(max_age_seconds)):
        return None
    return payload


def _refresh_ready_health() -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = get_ready_health()
    except Exception as exc:
        return None, f"Kunne ikke hente systemstatus: {exc.__class__.__name__}: {exc}"
    st.session_state["_system_health_payload"] = payload
    st.session_state["_system_health_captured_at"] = time.time()
    return payload, None


def render_system_and_ops_sidebar(*, jobs: list[dict[str, Any]], telemetry: dict[str, float] | None = None) -> None:
    with st.sidebar.expander("TODO", expanded=False):
        st.write("1. Automatisk varsel når bug opprettes, tildeles, kommenteres eller lukkes (e-post/Teams/Slack).")
        st.write("2. Koble opp mot DevOPS")
        st.write("3. Rollebaserte in-app varsler (f.eks. ny bug tildelt meg).")

    with st.sidebar.expander("System og drift", expanded=False):
        h1, h2 = st.columns([1.2, 1])
        refresh_clicked = h1.button(
            "Hent health-status",
            key="sidebar_health_refresh",
            use_container_width=True,
            help="Kjører runtime health-sjekker nå. Dette kan ta noen sekunder.",
        )
        auto_mode = h2.toggle(
            "Auto",
            key="sidebar_health_auto",
            value=False,
            help="Når aktiv, hentes health-status automatisk når cache har utløpt.",
        )

        ready = _get_cached_ready_health()
        if refresh_clicked or (auto_mode and ready is None):
            refreshed, error = _refresh_ready_health()
            if error:
                st.error(error)
            elif isinstance(refreshed, dict):
                ready = refreshed

        if isinstance(ready, dict):
            overall_status = str(ready.get("status") or "unknown")
            icon, label = _format_health_status(overall_status)
            st.write(f"Totalstatus: {icon} {label}")

            checks = ready.get("checks") or {}
            if isinstance(checks, dict):
                check_labels = {
                    "config": "Konfigurasjon",
                    "database": "Database",
                    "ai_text": "AI-tekst",
                    "embeddings": "Embeddings",
                    "search": "Søk",
                }
                for key in ("config", "database", "ai_text", "embeddings", "search"):
                    payload = checks.get(key)
                    if not isinstance(payload, dict):
                        continue
                    c_icon, c_label = _format_health_status(str(payload.get("status") or "unknown"))
                    st.write(f"{c_icon} {check_labels.get(key, key)}: {c_label}")
                    detail = str(payload.get("detail") or "").strip()
                    if detail:
                        st.caption(detail)

                    if key == "config":
                        nested = payload.get("checks")
                        if isinstance(nested, dict):
                            for nested_name, nested_payload in nested.items():
                                if not isinstance(nested_payload, dict):
                                    continue
                                n_icon, n_label = _format_health_status(str(nested_payload.get("status") or "unknown"))
                                n_detail = str(nested_payload.get("detail") or "").strip()
                                st.write(f"  {n_icon} {nested_name}: {n_label}")
                                if n_detail:
                                    st.caption(f"  {n_detail}")

                    if key == "search":
                        telemetry = payload.get("telemetry")
                        if isinstance(telemetry, dict):
                            st.caption(
                                "Søk: "
                                f"queries={int(float(telemetry.get('total_queries', 0.0)))} | "
                                f"avg_latency_ms={round(float(telemetry.get('avg_latency_ms', 0.0)), 1)} | "
                                f"fallback_rate={round(float(telemetry.get('fallback_rate', 0.0)) * 100, 1)}%"
                            )
        else:
            st.caption("Health-status er ikke lastet ennå. Trykk «Hent health-status» ved behov.")

        st.divider()
        st.caption("Siste bakgrunnsjobber")
        if not jobs:
            st.caption("Ingen bakgrunnsjobber registrert ennå.")
            return

        ordered_jobs = list(jobs)
        ordered_jobs.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        running_count = sum(1 for job in ordered_jobs if str(job.get("status") or "") == "running")
        failed_count = sum(1 for job in ordered_jobs if str(job.get("status") or "") == "failed")
        pending_count = sum(1 for job in ordered_jobs if str(job.get("status") or "") == "pending")
        c1, c2, c3 = st.columns(3)
        c1.metric("Kjører", running_count)
        c2.metric("Feilet", failed_count)
        c3.metric("Venter", pending_count)

        for job in ordered_jobs[:8]:
            job_status = str(job.get("status") or "unknown").strip().casefold()
            j_icon = {
                "pending": "🟡",
                "running": "🟠",
                "completed": "🟢",
                "failed": "🔴",
            }.get(job_status, "⚪")
            label = str(job.get("label") or job.get("job_key") or "job")
            bug_id = job.get("bug_id")
            created = str(job.get("created_at") or "")
            line = f"{j_icon} {label} ({job_status or 'unknown'})"
            if bug_id:
                line += f" • Bug #{bug_id}"
            st.write(line)
            if created:
                st.caption(created)
            queue_latency_ms = job.get("queue_latency_ms")
            run_duration_ms = job.get("run_duration_ms")
            if queue_latency_ms is not None or run_duration_ms is not None:
                st.caption(
                    f"kø={round(float(queue_latency_ms or 0.0), 1)} ms | kjøring={round(float(run_duration_ms or 0.0), 1)} ms"
                )
            error_text = str(job.get("error") or "").strip()
            if error_text and str(job.get("status") or "") == "failed":
                st.caption(error_text)

        if isinstance(telemetry, dict) and telemetry:
            st.divider()
            st.caption("Ytelse (runtime)")
            t1, t2, t3 = st.columns(3)
            t1.metric("Søk snitt (ms)", round(float(telemetry.get("search_avg_ms", 0.0) or 0.0), 1))
            t2.metric("AI venting snitt (ms)", round(float(telemetry.get("ai_wait_avg_ms", 0.0) or 0.0), 1))
            t3.metric("Admin side (ms)", round(float(telemetry.get("page_admin_ms", 0.0) or 0.0), 1))
