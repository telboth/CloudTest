from sqlalchemy import inspect, text

from app.core.config import settings
from app.core.database import engine
from app.core.logging import get_logger

logger = get_logger("app.schema_bootstrap")


def run_local_schema_upgrades() -> None:
    if not settings.database_is_sqlite:
        logger.info("Skipping local schema bootstrap because backend=%s", settings.database_backend)
        return

    inspector = inspect(engine)
    if "bugs" not in inspector.get_table_names():
        logger.info("Skipping local schema bootstrap because bugs table does not exist yet.")
        return

    bug_columns = {column["name"] for column in inspector.get_columns("bugs")}
    _ensure_column(bug_columns, "reporter_satisfaction", "ALTER TABLE bugs ADD COLUMN reporter_satisfaction VARCHAR(50)")
    _ensure_column(bug_columns, "notify_emails", "ALTER TABLE bugs ADD COLUMN notify_emails VARCHAR(500)")
    _ensure_column(bug_columns, "reporting_date", "ALTER TABLE bugs ADD COLUMN reporting_date DATETIME")
    _ensure_column(bug_columns, "sentiment_label", "ALTER TABLE bugs ADD COLUMN sentiment_label VARCHAR(20)")
    _ensure_column(bug_columns, "sentiment_summary", "ALTER TABLE bugs ADD COLUMN sentiment_summary VARCHAR(255)")
    _ensure_column(bug_columns, "sentiment_analyzed_at", "ALTER TABLE bugs ADD COLUMN sentiment_analyzed_at DATETIME")
    _ensure_column(bug_columns, "bug_summary", "ALTER TABLE bugs ADD COLUMN bug_summary TEXT")
    _ensure_column(bug_columns, "bug_summary_updated_at", "ALTER TABLE bugs ADD COLUMN bug_summary_updated_at DATETIME")
    _ensure_column(bug_columns, "ado_work_item_id", "ALTER TABLE bugs ADD COLUMN ado_work_item_id INTEGER")
    _ensure_column(bug_columns, "ado_work_item_url", "ALTER TABLE bugs ADD COLUMN ado_work_item_url VARCHAR(500)")
    _ensure_column(bug_columns, "ado_sync_status", "ALTER TABLE bugs ADD COLUMN ado_sync_status VARCHAR(50)")
    _ensure_column(bug_columns, "ado_synced_at", "ALTER TABLE bugs ADD COLUMN ado_synced_at DATETIME")

    user_columns = {column["name"] for column in inspector.get_columns("users")} if "users" in inspector.get_table_names() else set()
    _ensure_column(user_columns, "auth_provider", "ALTER TABLE users ADD COLUMN auth_provider VARCHAR(50)")
    _ensure_column(user_columns, "entra_oid", "ALTER TABLE users ADD COLUMN entra_oid VARCHAR(255)")


def _ensure_column(existing_columns: set[str], column_name: str, ddl: str) -> None:
    if column_name in existing_columns:
        return
    logger.warning("Applying legacy local schema bootstrap for missing column=%s", column_name)
    with engine.begin() as connection:
        connection.execute(text(ddl))
