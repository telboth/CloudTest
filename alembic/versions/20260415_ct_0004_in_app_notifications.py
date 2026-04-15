"""CloudTest in-app notifications

Revision ID: 20260415_ct_0004
Revises: 20260414_ct_0003
Create Date: 2026-04-15 11:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260415_ct_0004"
down_revision = "20260414_ct_0003"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in set(inspector.get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return False
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def _ensure_index(index_name: str, table_name: str, columns: list[str], *, unique: bool = False) -> None:
    if _index_exists(table_name, index_name):
        return
    op.create_index(index_name, table_name, columns, unique=unique)


def upgrade() -> None:
    if not _table_exists("in_app_notifications"):
        op.create_table(
            "in_app_notifications",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("recipient_email", sa.String(length=255), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column("bug_id", sa.Integer(), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("dedupe_key", sa.String(length=255), nullable=False),
            sa.Column("actor_email", sa.String(length=255), nullable=True),
            sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["bug_id"], ["bugs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )

    _ensure_index("ix_in_app_notifications_id", "in_app_notifications", ["id"])
    _ensure_index("ix_in_app_notifications_recipient_email", "in_app_notifications", ["recipient_email"])
    _ensure_index("ix_in_app_notifications_event_type", "in_app_notifications", ["event_type"])
    _ensure_index("ix_in_app_notifications_bug_id", "in_app_notifications", ["bug_id"])
    _ensure_index("ix_in_app_notifications_created_at", "in_app_notifications", ["created_at"])
    _ensure_index(
        "ix_in_app_notifications_recipient_read_created",
        "in_app_notifications",
        ["recipient_email", "is_read", "created_at"],
    )
    _ensure_index(
        "ix_in_app_notifications_bug_id_created_at",
        "in_app_notifications",
        ["bug_id", "created_at"],
    )
    _ensure_index(
        "ix_in_app_notifications_dedupe_key",
        "in_app_notifications",
        ["dedupe_key"],
        unique=True,
    )


def downgrade() -> None:
    # Intentional no-op in CloudTest.
    pass
