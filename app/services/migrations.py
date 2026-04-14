from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("app.migrations")

CLOUD_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = CLOUD_ROOT / "alembic.ini"
ALEMBIC_SCRIPT = CLOUD_ROOT / "alembic"


def run_cloudtest_migrations(*, target_revision: str = "head") -> None:
    if not ALEMBIC_INI.exists():
        raise RuntimeError(f"Fant ikke Alembic-konfigurasjon: {ALEMBIC_INI}")
    if not ALEMBIC_SCRIPT.exists():
        raise RuntimeError(f"Fant ikke Alembic script_location: {ALEMBIC_SCRIPT}")

    try:
        from alembic import command
        from alembic.config import Config
    except Exception as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError(
            "Alembic er ikke installert i miljøet. Legg til 'alembic' i requirements og installer avhengigheter."
        ) from exc

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    logger.info("Running CloudTest migrations target=%s backend=%s", target_revision, settings.database_backend)
    command.upgrade(config, target_revision)
