from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError


def is_database_error(exc: Exception) -> bool:
    return isinstance(exc, SQLAlchemyError)


def format_user_error(prefix: str, exc: Exception, *, fallback: str = "Uventet feil") -> str:
    reason = f"{exc.__class__.__name__}"
    if is_database_error(exc):
        reason = f"database-feil ({reason})"
    return f"{prefix}: {reason}. {fallback}".strip()
