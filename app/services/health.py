from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.services.ai_provider import get_ai_provider_status, get_embedding_provider_status
from app.services.config_validation import validate_runtime_config
from app.services.search import get_search_telemetry_snapshot


def get_live_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": settings.app_name,
    }


def get_ready_health() -> dict[str, Any]:
    checks = {
        "config": _config_check(),
        "database": _database_check(),
        "ai_text": _ai_text_check(),
        "embeddings": _embedding_check(),
        "search": _search_check(),
    }

    overall_status = "ok"
    if any(check["status"] == "error" for check in checks.values()):
        overall_status = "error"
    elif any(check["status"] == "degraded" for check in checks.values()):
        overall_status = "degraded"

    return {
        "status": overall_status,
        "service": settings.app_name,
        "checks": checks,
    }


def _config_check() -> dict[str, Any]:
    result = validate_runtime_config()
    return {
        "status": result["status"],
        "detail": "Runtime configuration validation completed.",
        "checks": result["checks"],
    }


def _database_check() -> dict[str, Any]:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "detail": "Database connection succeeded.",
        }
    except Exception as exc:
        return {
            "status": "error",
            "detail": f"Database connection failed: {exc.__class__.__name__}: {exc}",
        }


def _ai_text_check() -> dict[str, Any]:
    provider = (settings.ai_provider or "openai").strip().casefold()
    try:
        provider_status = get_ai_provider_status(ai_provider=provider, ai_model=settings.ai_model)
    except Exception as exc:
        return {
            "status": "error",
            "provider": provider,
            "detail": f"AI provider status check failed: {exc.__class__.__name__}: {exc}",
        }

    available = bool(provider_status.get("available"))
    model_available = bool(provider_status.get("model_available"))
    if available and model_available:
        status = "ok"
    elif available:
        status = "degraded"
    else:
        status = "degraded"

    return {
        "status": status,
        "provider": provider_status.get("provider"),
        "model": provider_status.get("model"),
        "detail": provider_status.get("detail"),
    }


def _embedding_check() -> dict[str, Any]:
    provider = (settings.embedding_provider or "openai").strip().casefold()
    try:
        provider_status = get_embedding_provider_status(
            embedding_provider=provider,
            embedding_model=settings.embedding_model if provider == "openai" else settings.local_embedding_model,
        )
    except Exception as exc:
        return {
            "status": "error",
            "provider": provider,
            "detail": f"Embedding status check failed: {exc.__class__.__name__}: {exc}",
        }

    available = bool(provider_status.get("available"))
    model_available = bool(provider_status.get("model_available"))
    if available and model_available:
        status = "ok"
    elif available:
        status = "degraded"
    else:
        status = "degraded"

    return {
        "status": status,
        "provider": provider_status.get("provider"),
        "model": provider_status.get("model"),
        "detail": provider_status.get("detail"),
    }


def _search_check() -> dict[str, Any]:
    telemetry = get_search_telemetry_snapshot()
    return {
        "status": "ok",
        "detail": "Search telemetry snapshot.",
        "telemetry": telemetry,
    }
