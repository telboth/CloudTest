"""CloudTest baseline schema (idempotent)

Revision ID: 20260414_ct_0001
Revises: None
Create Date: 2026-04-14 09:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.models.bug import EmbeddingType

# revision identifiers, used by Alembic.
revision = "20260414_ct_0001"
down_revision = None
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in set(inspector.get_table_names())


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _ensure_column(table_name: str, column: sa.Column) -> None:
    if _column_exists(table_name, str(column.name)):
        return
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(column)


def _ensure_index(index_name: str, table_name: str, columns: list[str], *, unique: bool = False) -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {idx["name"] for idx in inspector.get_indexes(table_name)}
    if index_name in existing:
        return
    op.create_index(index_name, table_name, columns, unique=unique)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    if not _table_exists("users"):
        op.create_table(
            "users",
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("full_name", sa.String(length=255), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=50), nullable=False),
            sa.Column("auth_provider", sa.String(length=50), nullable=True),
            sa.Column("entra_oid", sa.String(length=255), nullable=True),
            sa.PrimaryKeyConstraint("email"),
        )
    _ensure_column("users", sa.Column("auth_provider", sa.String(length=50), nullable=True))
    _ensure_column("users", sa.Column("entra_oid", sa.String(length=255), nullable=True))
    _ensure_index("ix_users_email", "users", ["email"])
    _ensure_index("ix_users_entra_oid", "users", ["entra_oid"])
    _ensure_index("ix_users_role", "users", ["role"])

    if not _table_exists("bugs"):
        op.create_table(
            "bugs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("category", sa.String(length=100), nullable=False),
            sa.Column("severity", sa.String(length=50), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("environment", sa.String(length=255), nullable=True),
            sa.Column("repro_steps", sa.Text(), nullable=True),
            sa.Column("tags", sa.String(length=255), nullable=True),
            sa.Column("notify_emails", sa.String(length=500), nullable=True),
            sa.Column("reporter_satisfaction", sa.String(length=50), nullable=True),
            sa.Column("sentiment_label", sa.String(length=20), nullable=True),
            sa.Column("sentiment_summary", sa.Text(), nullable=True),
            sa.Column("sentiment_analyzed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("bug_summary", sa.Text(), nullable=True),
            sa.Column("bug_summary_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("workaround", sa.Text(), nullable=True),
            sa.Column("resolution_summary", sa.Text(), nullable=True),
            sa.Column("reporting_date", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ado_work_item_id", sa.Integer(), nullable=True),
            sa.Column("ado_work_item_url", sa.String(length=500), nullable=True),
            sa.Column("ado_sync_status", sa.String(length=50), nullable=True),
            sa.Column("ado_synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reporter_id", sa.String(length=255), nullable=False),
            sa.Column("assignee_id", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["assignee_id"], ["users.email"]),
            sa.ForeignKeyConstraint(["reporter_id"], ["users.email"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_column("bugs", sa.Column("notify_emails", sa.String(length=500), nullable=True))
    _ensure_column("bugs", sa.Column("reporter_satisfaction", sa.String(length=50), nullable=True))
    _ensure_column("bugs", sa.Column("sentiment_label", sa.String(length=20), nullable=True))
    _ensure_column("bugs", sa.Column("sentiment_summary", sa.Text(), nullable=True))
    _ensure_column("bugs", sa.Column("sentiment_analyzed_at", sa.DateTime(timezone=True), nullable=True))
    _ensure_column("bugs", sa.Column("bug_summary", sa.Text(), nullable=True))
    _ensure_column("bugs", sa.Column("bug_summary_updated_at", sa.DateTime(timezone=True), nullable=True))
    _ensure_column("bugs", sa.Column("workaround", sa.Text(), nullable=True))
    _ensure_column("bugs", sa.Column("resolution_summary", sa.Text(), nullable=True))
    _ensure_column("bugs", sa.Column("reporting_date", sa.DateTime(timezone=True), nullable=True))
    _ensure_column("bugs", sa.Column("ado_work_item_id", sa.Integer(), nullable=True))
    _ensure_column("bugs", sa.Column("ado_work_item_url", sa.String(length=500), nullable=True))
    _ensure_column("bugs", sa.Column("ado_sync_status", sa.String(length=50), nullable=True))
    _ensure_column("bugs", sa.Column("ado_synced_at", sa.DateTime(timezone=True), nullable=True))
    _ensure_index("ix_bugs_id", "bugs", ["id"])
    _ensure_index("ix_bugs_title", "bugs", ["title"])
    _ensure_index("ix_bugs_status", "bugs", ["status"])
    _ensure_index("ix_bugs_reporter_id", "bugs", ["reporter_id"])
    _ensure_index("ix_bugs_assignee_id", "bugs", ["assignee_id"])
    _ensure_index("ix_bugs_ado_work_item_id", "bugs", ["ado_work_item_id"])

    if not _table_exists("attachments"):
        op.create_table(
            "attachments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("bug_id", sa.Integer(), nullable=False),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("content_type", sa.String(length=100), nullable=True),
            sa.Column("storage_path", sa.String(length=500), nullable=False),
            sa.Column("uploaded_by", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["bug_id"], ["bugs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["uploaded_by"], ["users.email"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_attachments_id", "attachments", ["id"])
    _ensure_index("ix_attachments_bug_id", "attachments", ["bug_id"])

    if not _table_exists("bug_comments"):
        op.create_table(
            "bug_comments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("bug_id", sa.Integer(), nullable=False),
            sa.Column("author_email", sa.String(length=255), nullable=False),
            sa.Column("author_role", sa.String(length=50), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["author_email"], ["users.email"]),
            sa.ForeignKeyConstraint(["bug_id"], ["bugs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_bug_comments_id", "bug_comments", ["id"])
    _ensure_index("ix_bug_comments_bug_id", "bug_comments", ["bug_id"])

    if not _table_exists("bug_history"):
        op.create_table(
            "bug_history",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("bug_id", sa.Integer(), nullable=False),
            sa.Column("action", sa.String(length=100), nullable=False),
            sa.Column("details", sa.Text(), nullable=False),
            sa.Column("actor_email", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["actor_email"], ["users.email"]),
            sa.ForeignKeyConstraint(["bug_id"], ["bugs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_bug_history_id", "bug_history", ["id"])
    _ensure_index("ix_bug_history_bug_id", "bug_history", ["bug_id"])

    if not _table_exists("bug_search_index"):
        op.create_table(
            "bug_search_index",
            sa.Column("bug_id", sa.Integer(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("embedding_provider", sa.String(length=50), nullable=True),
            sa.Column("embedding_model", sa.String(length=255), nullable=True),
            sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
            sa.Column("search_text", sa.Text(), nullable=False),
            sa.Column("needs_reindex", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("embedding", EmbeddingType(None), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["bug_id"], ["bugs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("bug_id"),
        )
    _ensure_column("bug_search_index", sa.Column("embedding_provider", sa.String(length=50), nullable=True))
    _ensure_column("bug_search_index", sa.Column("embedding_model", sa.String(length=255), nullable=True))
    _ensure_column("bug_search_index", sa.Column("embedding_dimensions", sa.Integer(), nullable=True))
    _ensure_column("bug_search_index", sa.Column("needs_reindex", sa.Integer(), nullable=False, server_default=sa.text("1")))
    _ensure_column("bug_search_index", sa.Column("last_error", sa.Text(), nullable=True))
    _ensure_column("bug_search_index", sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE bug_search_index SET needs_reindex = 1 WHERE needs_reindex IS NULL")
    _ensure_index("ix_bug_search_index_content_hash", "bug_search_index", ["content_hash"])
    _ensure_index("ix_bug_search_index_embedding_provider", "bug_search_index", ["embedding_provider"])
    _ensure_index("ix_bug_search_index_embedding_dimensions", "bug_search_index", ["embedding_dimensions"])
    _ensure_index("ix_bug_search_index_needs_reindex", "bug_search_index", ["needs_reindex"])
    _ensure_index("ix_bug_search_index_indexed_at", "bug_search_index", ["indexed_at"])

    if not _table_exists("background_jobs"):
        op.create_table(
            "background_jobs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("job_type", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default=sa.text("'pending'")),
            sa.Column("payload_json", sa.JSON(), nullable=True),
            sa.Column("result_json", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("requested_by", sa.String(length=255), nullable=True),
            sa.Column("bug_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["requested_by"], ["users.email"]),
            sa.ForeignKeyConstraint(["bug_id"], ["bugs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_background_jobs_id", "background_jobs", ["id"])
    _ensure_index("ix_background_jobs_job_type", "background_jobs", ["job_type"])
    _ensure_index("ix_background_jobs_status", "background_jobs", ["status"])
    _ensure_index("ix_background_jobs_requested_by", "background_jobs", ["requested_by"])
    _ensure_index("ix_background_jobs_bug_id", "background_jobs", ["bug_id"])

    if not _table_exists("bug_view_states"):
        op.create_table(
            "bug_view_states",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("bug_id", sa.Integer(), nullable=False),
            sa.Column("user_email", sa.String(length=255), nullable=False),
            sa.Column("last_viewed_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["bug_id"], ["bugs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_email"], ["users.email"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_bug_view_states_id", "bug_view_states", ["id"])
    _ensure_index("ix_bug_view_states_bug_id", "bug_view_states", ["bug_id"])
    _ensure_index("ix_bug_view_states_user_email", "bug_view_states", ["user_email"])
    _ensure_index("ix_bug_view_states_last_viewed_at", "bug_view_states", ["last_viewed_at"])
    _ensure_index("ix_bug_view_states_bug_user", "bug_view_states", ["bug_id", "user_email"], unique=True)

    if not _table_exists("app_runtime_meta"):
        op.create_table(
            "app_runtime_meta",
            sa.Column("key", sa.String(length=100), nullable=False),
            sa.Column("value", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.PrimaryKeyConstraint("key"),
        )


def downgrade() -> None:
    # Intentional no-op to avoid accidental destructive rollback in local/cloud test environments.
    pass
