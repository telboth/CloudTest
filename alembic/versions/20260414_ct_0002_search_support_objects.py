"""CloudTest search support objects (fts/ann/meta)

Revision ID: 20260414_ct_0002
Revises: 20260414_ct_0001
Create Date: 2026-04-14 09:35:00
"""

from __future__ import annotations

from alembic import op

from app.core.config import settings

# revision identifiers, used by Alembic.
revision = "20260414_ct_0002"
down_revision = "20260414_ct_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Ensure runtime-meta keys exist for maintenance visibility.
    op.execute(
        """
        INSERT INTO app_runtime_meta(key, value)
        SELECT 'search.last_reindex_at', NULL
        WHERE NOT EXISTS (SELECT 1 FROM app_runtime_meta WHERE key = 'search.last_reindex_at')
        """
    )
    op.execute(
        """
        INSERT INTO app_runtime_meta(key, value)
        SELECT 'search.last_reindex_count', '0'
        WHERE NOT EXISTS (SELECT 1 FROM app_runtime_meta WHERE key = 'search.last_reindex_count')
        """
    )

    # Optional SQLite search helper structures.
    if dialect == "sqlite":
        op.execute("CREATE VIRTUAL TABLE IF NOT EXISTS bug_search_fts USING fts5(bug_id UNINDEXED, search_text)")
        sqlite_vec_enabled = bool(getattr(settings, "sqlite_vec_enabled", True))
        if sqlite_vec_enabled:
            dimensions = max(1, int(getattr(settings, "sqlite_vec_dimensions", 1536) or 1536))
            table_name = str(getattr(settings, "sqlite_vec_table_name", "bug_search_vec") or "bug_search_vec").strip() or "bug_search_vec"
            try:
                op.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} "
                    f"USING vec0(bug_id INTEGER PRIMARY KEY, embedding float[{dimensions}])"
                )
            except Exception:
                # sqlite-vec can be unavailable in some environments; keep migration non-blocking.
                pass
        return

    # PostgreSQL ANN indexes for pgvector.
    if dialect == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_bug_search_index_embedding_1536_ivfflat
            ON bug_search_index
            USING ivfflat ((embedding::vector(1536)) vector_cosine_ops)
            WITH (lists = 100)
            WHERE embedding IS NOT NULL AND embedding_dimensions = 1536
            """
        )
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_bug_search_index_embedding_384_ivfflat
            ON bug_search_index
            USING ivfflat ((embedding::vector(384)) vector_cosine_ops)
            WITH (lists = 50)
            WHERE embedding IS NOT NULL AND embedding_dimensions = 384
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_bug_search_index_embedding_1536_ivfflat")
        op.execute("DROP INDEX IF EXISTS ix_bug_search_index_embedding_384_ivfflat")
