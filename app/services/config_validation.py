from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.core.config import settings


def validate_runtime_config() -> dict[str, Any]:
    checks = {
        "security": _security_check(),
        "database": _database_check(),
        "ai_text": _ai_text_check(),
        "embeddings": _embedding_check(),
    }

    status = "ok"
    if any(check["status"] == "error" for check in checks.values()):
        status = "error"
    elif any(check["status"] == "degraded" for check in checks.values()):
        status = "degraded"

    return {
        "status": status,
        "checks": checks,
    }


def _security_check() -> dict[str, str]:
    issues: list[str] = []
    status = "ok"

    if settings.secret_key == "change-me-for-real-use":
        status = "degraded"
        issues.append("SECRET_KEY uses the default placeholder value.")

    if settings.default_admin_password == "admin123":
        status = "degraded"
        issues.append("DEFAULT_ADMIN_PASSWORD still uses the demo default.")

    if settings.default_admin_email == "admin@example.com":
        status = "degraded"
        issues.append("DEFAULT_ADMIN_EMAIL still uses the demo default.")

    return {
        "status": status,
        "detail": " ".join(issues) if issues else "Security-related configuration looks valid.",
    }


def _database_check() -> dict[str, str]:
    database_url = (settings.database_url or "").strip()
    if not database_url:
        return {
            "status": "error",
            "detail": "DATABASE_URL is missing.",
        }

    backend = settings.database_backend
    if backend == "sqlite":
        return {
            "status": "ok",
            "detail": "SQLite-profil aktiv (lokal/lettvektsdrift).",
        }

    if backend == "postgresql":
        parsed = urlparse(database_url)
        hostname = (parsed.hostname or "").strip().casefold()
        external_host = hostname not in {"", "localhost", "127.0.0.1"}
        if external_host and "sslmode=" not in database_url.casefold():
            return {
                "status": "degraded",
                "detail": (
                    "PostgreSQL is configured, but sslmode is not set. "
                    "For cloud databases, append '?sslmode=require' to DATABASE_URL."
                ),
            }

    if backend == "other":
        return {
            "status": "degraded",
            "detail": f"Ukjent database-backend i DATABASE_URL ({backend}).",
        }

    return {
        "status": "ok",
        "detail": f"Database configuration is present (backend: {backend}).",
    }


def _ai_text_check() -> dict[str, str]:
    provider = (settings.ai_provider or "").strip().casefold()
    if provider not in {"openai", "ollama"}:
        return {
            "status": "error",
            "detail": f"AI_PROVIDER '{settings.ai_provider}' is not supported.",
        }
    if provider == "openai" and not settings.openai_api_key:
        return {
            "status": "degraded",
            "detail": "AI_PROVIDER=OpenAI, men OPENAI_API_KEY mangler. AI-funksjoner blir utilgjengelige.",
        }
    if provider == "ollama" and not settings.ollama_base_url:
        return {
            "status": "error",
            "detail": "AI_PROVIDER is set to Ollama, but OLLAMA_BASE_URL is missing.",
        }
    return {
        "status": "ok",
        "detail": f"Text AI configuration is valid for provider '{provider}'.",
    }


def _embedding_check() -> dict[str, str]:
    provider = (settings.embedding_provider or "").strip().casefold()
    if provider not in {"openai", "local"}:
        return {
            "status": "error",
            "detail": f"EMBEDDING_PROVIDER '{settings.embedding_provider}' is not supported.",
        }
    if provider == "openai" and not settings.openai_api_key:
        return {
            "status": "degraded",
            "detail": "EMBEDDING_PROVIDER=OpenAI, men OPENAI_API_KEY mangler. Vektorsøk blir begrenset/fallback.",
        }
    if provider == "local" and not settings.local_embedding_model:
        return {
            "status": "error",
            "detail": "EMBEDDING_PROVIDER is set to local, but LOCAL_EMBEDDING_MODEL is missing.",
        }
    model_name = settings.local_embedding_model if provider == "local" else settings.embedding_model
    lock_detail = "locked" if settings.embedding_lock_enabled else "runtime overrides allowed"
    return {
        "status": "ok",
        "detail": (
            f"Embedding configuration is valid for provider '{provider}' "
            f"model '{model_name}' ({lock_detail})."
        ),
    }



