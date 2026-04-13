from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from app.core.config import settings


def validate_runtime_config() -> dict[str, Any]:
    checks = {
        "security": _security_check(),
        "database": _database_check(),
        "ai_text": _ai_text_check(),
        "embeddings": _embedding_check(),
        "entra": _entra_check(),
        "azure_devops": _azure_devops_check(),
        "urls": _url_check(),
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

    cloud_mode = str(os.getenv("STREAMLIT_CLOUD_TEST_MODE", "")).strip().casefold() in {"1", "true", "yes", "on"}
    running_on_streamlit_cloud = str(os.getenv("STREAMLIT_CLOUD", "")).strip().casefold() in {"1", "true", "yes", "on"}
    allow_sqlite_fallback = str(os.getenv("CLOUD_TEST_ALLOW_SQLITE_FALLBACK", "")).strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    backend = settings.database_backend
    if cloud_mode and backend != "postgresql":
        if backend == "sqlite" and allow_sqlite_fallback:
            return {
                "status": "degraded",
                "detail": (
                    "CloudTest mode is running on explicit SQLite fallback. "
                    "Use PostgreSQL for cloud/stable multi-user runtime."
                ),
            }
        if backend == "sqlite" and not running_on_streamlit_cloud:
            return {
                "status": "degraded",
                "detail": (
                    "CloudTest mode is running on local auto SQLite fallback. "
                    "Use PostgreSQL for cloud/stable multi-user runtime."
                ),
            }
        return {
            "status": "error",
            "detail": "CloudTest mode requires PostgreSQL. Update DATABASE_URL to a postgresql+psycopg URL.",
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
            "status": "error",
            "detail": "AI_PROVIDER is set to OpenAI, but OPENAI_API_KEY is missing.",
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
            "status": "error",
            "detail": "EMBEDDING_PROVIDER is set to OpenAI, but OPENAI_API_KEY is missing.",
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


def _entra_check() -> dict[str, str]:
    parts = [
        bool(settings.entra_tenant_id),
        bool(settings.entra_client_id),
        bool(settings.entra_client_secret),
    ]
    configured_count = sum(1 for part in parts if part)
    if configured_count == 0:
        return {
            "status": "degraded",
            "detail": "Entra ID is not configured.",
        }
    if configured_count != len(parts):
        return {
            "status": "error",
            "detail": "Entra ID configuration is incomplete. Set tenant, client ID and client secret together.",
        }
    return {
        "status": "ok",
        "detail": "Entra ID configuration is complete.",
    }


def _azure_devops_check() -> dict[str, str]:
    has_org = bool(settings.azure_devops_org)
    has_project = bool(settings.azure_devops_project)
    has_pat = bool(settings.azure_devops_pat)

    if not has_org and not has_project and not has_pat:
        return {
            "status": "degraded",
            "detail": "Azure DevOps is not configured.",
        }
    if has_org != has_project:
        return {
            "status": "error",
            "detail": "Azure DevOps configuration is incomplete. Set AZURE_DEVOPS_ORG and AZURE_DEVOPS_PROJECT together.",
        }
    if has_org and has_project and not has_pat:
        return {
            "status": "degraded",
            "detail": "Azure DevOps org/project is configured, but AZURE_DEVOPS_PAT is missing.",
        }
    return {
        "status": "ok",
        "detail": "Azure DevOps configuration looks valid.",
    }


def _url_check() -> dict[str, str]:
    urls = [
        ("API_BASE_URL", settings.api_base_url),
        ("ENTRA_REDIRECT_URI", settings.entra_redirect_uri),
        ("STREAMLIT_REPORTER_URL", settings.streamlit_reporter_url),
        ("STREAMLIT_ASSIGNEE_URL", settings.streamlit_assignee_url),
        ("STREAMLIT_ADMIN_URL", settings.streamlit_admin_url),
    ]

    invalid: list[str] = []
    for name, value in urls:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            invalid.append(name)

    if invalid:
        return {
            "status": "error",
            "detail": f"These URL settings are invalid: {', '.join(invalid)}.",
        }

    api_url = urlparse(settings.api_base_url)
    entra_url = urlparse(settings.entra_redirect_uri)
    if (api_url.hostname, api_url.port) != (entra_url.hostname, entra_url.port):
        return {
            "status": "degraded",
            "detail": "API_BASE_URL and ENTRA_REDIRECT_URI use different host/port combinations.",
        }

    return {
        "status": "ok",
        "detail": "Configured URLs look valid.",
    }

