"""CloudTest notification outbox

Revision ID: 20260415_ct_0005
Revises: 20260415_ct_0004
Create Date: 2026-04-15 14:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260415_ct_0005"
down_revision = "20260415_ct_0004"
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
    if not _table_exists("notification_outbox"):
        op.create_table(
            "notification_outbox",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column("channel", sa.String(length=32), nullable=False, server_default=sa.text("'in_app'")),
            sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
            sa.Column("recipient_email", sa.String(length=255), nullable=False),
            sa.Column("bug_id", sa.Integer(), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("actor_email", sa.String(length=255), nullable=True),
            sa.Column("dedupe_key", sa.String(length=255), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["bug_id"], ["bugs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )

    _ensure_index("ix_notification_outbox_id", "notification_outbox", ["id"])
    _ensure_index("ix_notification_outbox_event_type", "notification_outbox", ["event_type"])
    _ensure_index("ix_notification_outbox_recipient_email", "notification_outbox", ["recipient_email"])
    _ensure_index("ix_notification_outbox_bug_id", "notification_outbox", ["bug_id"])
    _ensure_index("ix_notification_outbox_created_at", "notification_outbox", ["created_at"])
    _ensure_index("ix_notification_outbox_status_created", "notification_outbox", ["status", "created_at"])
    _ensure_index("ix_notification_outbox_recipient_status", "notification_outbox", ["recipient_email", "status"])
    _ensure_index("ix_notification_outbox_bug_id_created", "notification_outbox", ["bug_id", "created_at"])
    _ensure_index("ix_notification_outbox_dedupe_key", "notification_outbox", ["dedupe_key"], unique=True)


def downgrade() -> None:
    # Intentional no-op in CloudTest.
    pass

