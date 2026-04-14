"""CloudTest soft-delete + policy support

Revision ID: 20260414_ct_0003
Revises: 20260414_ct_0002
Create Date: 2026-04-14 14:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260414_ct_0003"
down_revision = "20260414_ct_0002"
branch_labels = None
depends_on = None


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
    _ensure_column("bugs", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    _ensure_column("bugs", sa.Column("deleted_by", sa.String(length=255), nullable=True))
    _ensure_index("ix_bugs_deleted_at", "bugs", ["deleted_at"])

    op.execute(
        """
        INSERT INTO app_runtime_meta(key, value)
        SELECT 'policy.delete_roles', 'admin,assignee'
        WHERE NOT EXISTS (SELECT 1 FROM app_runtime_meta WHERE key = 'policy.delete_roles')
        """
    )
    op.execute(
        """
        INSERT INTO app_runtime_meta(key, value)
        SELECT 'policy.reopen_roles', 'admin,assignee,reporter'
        WHERE NOT EXISTS (SELECT 1 FROM app_runtime_meta WHERE key = 'policy.reopen_roles')
        """
    )
    op.execute(
        """
        INSERT INTO app_runtime_meta(key, value)
        SELECT 'policy.hard_delete_roles', 'admin'
        WHERE NOT EXISTS (SELECT 1 FROM app_runtime_meta WHERE key = 'policy.hard_delete_roles')
        """
    )


def downgrade() -> None:
    # Intentional no-op in CloudTest.
    pass

