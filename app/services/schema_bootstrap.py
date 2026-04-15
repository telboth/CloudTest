from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.database import engine
from app.core.logging import get_logger

logger = get_logger("app.schema_bootstrap")


def run_local_schema_upgrades() -> None:
    if not settings.database_is_sqlite:
        logger.info("Skipping local schema bootstrap because backend=%s", settings.database_backend)
        return

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "bugs" not in table_names:
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

    user_columns = {column["name"] for column in inspector.get_columns("users")} if "users" in table_names else set()
    _ensure_column(user_columns, "auth_provider", "ALTER TABLE users ADD COLUMN auth_provider VARCHAR(50)")
    _ensure_column(user_columns, "entra_oid", "ALTER TABLE users ADD COLUMN entra_oid VARCHAR(255)")

    search_index_columns = (
        {column["name"] for column in inspector.get_columns("bug_search_index")}
        if "bug_search_index" in table_names
        else set()
    )
    _ensure_column(
        search_index_columns,
        "needs_reindex",
        "ALTER TABLE bug_search_index ADD COLUMN needs_reindex INTEGER NOT NULL DEFAULT 1",
    )
    _ensure_column(
        search_index_columns,
        "last_error",
        "ALTER TABLE bug_search_index ADD COLUMN last_error TEXT",
    )
    _ensure_column(
        search_index_columns,
        "indexed_at",
        "ALTER TABLE bug_search_index ADD COLUMN indexed_at DATETIME",
    )

    _ensure_ddl(
        """
        CREATE TABLE IF NOT EXISTS app_runtime_meta (
            key VARCHAR(100) PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    _ensure_ddl(
        "CREATE VIRTUAL TABLE IF NOT EXISTS bug_search_fts USING fts5(bug_id UNINDEXED, search_text)"
    )
    _ensure_ddl(
        "CREATE INDEX IF NOT EXISTS ix_bug_search_index_needs_reindex ON bug_search_index(needs_reindex)"
    )
    _ensure_ddl(
        "CREATE INDEX IF NOT EXISTS ix_bug_search_index_indexed_at ON bug_search_index(indexed_at)"
    )
    _ensure_ddl(
        """
        CREATE TABLE IF NOT EXISTS in_app_notifications (
            id INTEGER PRIMARY KEY,
            recipient_email VARCHAR(255) NOT NULL,
            event_type VARCHAR(80) NOT NULL,
            bug_id INTEGER NULL REFERENCES bugs(id) ON DELETE SET NULL,
            title VARCHAR(255) NOT NULL,
            message TEXT NOT NULL,
            payload_json TEXT NULL,
            dedupe_key VARCHAR(255) NOT NULL,
            actor_email VARCHAR(255) NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            read_at DATETIME NULL
        )
        """
    )
    _ensure_ddl(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_in_app_notifications_dedupe_key ON in_app_notifications(dedupe_key)"
    )
    _ensure_ddl(
        "CREATE INDEX IF NOT EXISTS ix_in_app_notifications_recipient_read_created ON in_app_notifications(recipient_email, is_read, created_at)"
    )
    _ensure_ddl(
        "CREATE INDEX IF NOT EXISTS ix_in_app_notifications_bug_id_created_at ON in_app_notifications(bug_id, created_at)"
    )
    _ensure_sqlite_vec_table()


def _ensure_column(existing_columns: set[str], column_name: str, ddl: str) -> None:
    if column_name in existing_columns:
        return
    logger.warning("Applying legacy local schema bootstrap for missing column=%s", column_name)
    with engine.begin() as connection:
        connection.execute(text(ddl))


def _ensure_ddl(ddl: str) -> None:
    try:
        with engine.begin() as connection:
            connection.execute(text(ddl))
    except SQLAlchemyError as exc:
        logger.warning("Local schema bootstrap DDL failed: %s", exc)


def _ensure_sqlite_vec_table() -> None:
    sqlite_vec_lock_active = bool(
        getattr(settings, "sqlite_vec_lock_active", None)
        if getattr(settings, "sqlite_vec_lock_active", None) is not None
        else (getattr(settings, "database_is_sqlite", False) and getattr(settings, "sqlite_vec_enabled", False))
    )
    if not sqlite_vec_lock_active:
        return

    dimensions = max(1, int(getattr(settings, "sqlite_vec_dimensions", 1536) or 1536))
    table_name = str(getattr(settings, "sqlite_vec_table_name", "bug_search_vec") or "bug_search_vec").strip() or "bug_search_vec"
    ddl = (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} "
        f"USING vec0(bug_id INTEGER PRIMARY KEY, embedding float[{dimensions}])"
    )
    try:
        with engine.begin() as connection:
            connection.execute(text(ddl))
    except SQLAlchemyError as exc:
        logger.warning(
            "Could not create sqlite-vec table '%s'. Falling back to non-native vector search. error=%s",
            table_name,
            exc,
        )
