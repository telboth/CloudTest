import json
import os
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.core.database import Base

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - optional dependency at runtime
    Vector = None  # type: ignore[assignment]


def _pgvector_enabled_for_dialect(dialect_name: str) -> bool:
    if dialect_name != "postgresql" or Vector is None:
        return False
    disabled = str(os.getenv("BUGSEARCH_DISABLE_PGVECTOR", "")).strip().casefold()
    return disabled not in {"1", "true", "yes", "on"}


class EmbeddingType(TypeDecorator):
    impl = Text
    cache_ok = True

    def __init__(self, dimensions: int | None = None):
        super().__init__()
        self.dimensions = dimensions

    def load_dialect_impl(self, dialect):
        if _pgvector_enabled_for_dialect(dialect.name):
            return dialect.type_descriptor(Vector(self.dimensions))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if _pgvector_enabled_for_dialect(dialect.name):
            return value
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if _pgvector_enabled_for_dialect(dialect.name):
            return list(value)
        return json.loads(value)


class Bug(Base):
    __tablename__ = "bugs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(100), default="software")
    severity: Mapped[str] = mapped_column(String(50), default="medium")
    status: Mapped[str] = mapped_column(String(50), default="open", index=True)
    environment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    repro_steps: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notify_emails: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reporter_satisfaction: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sentiment_label: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sentiment_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bug_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    bug_summary_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    workaround: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    reporting_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ado_work_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ado_work_item_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ado_sync_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ado_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reporter_id: Mapped[str] = mapped_column(ForeignKey("users.email"), index=True)
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("users.email"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reporter = relationship("User", foreign_keys=[reporter_id], back_populates="reported_bugs")
    assignee = relationship("User", foreign_keys=[assignee_id], back_populates="assigned_bugs")
    attachments = relationship("Attachment", back_populates="bug", cascade="all, delete-orphan")
    comments = relationship("BugComment", back_populates="bug", cascade="all, delete-orphan")
    history = relationship("BugHistory", back_populates="bug", cascade="all, delete-orphan")
    view_states = relationship("BugViewState", back_populates="bug", cascade="all, delete-orphan")


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    bug_id: Mapped[int] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    storage_path: Mapped[str] = mapped_column(String(500))
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.email"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bug = relationship("Bug", back_populates="attachments")


class BugHistory(Base):
    __tablename__ = "bug_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    bug_id: Mapped[int] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[str] = mapped_column(Text)
    actor_email: Mapped[str] = mapped_column(ForeignKey("users.email"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bug = relationship("Bug", back_populates="history")


class BugComment(Base):
    __tablename__ = "bug_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    bug_id: Mapped[int] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), index=True)
    author_email: Mapped[str] = mapped_column(ForeignKey("users.email"))
    author_role: Mapped[str] = mapped_column(String(50))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bug = relationship("Bug", back_populates="comments")


class BugSearchIndex(Base):
    __tablename__ = "bug_search_index"

    bug_id: Mapped[int] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedding_provider: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    search_text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    bug = relationship("Bug")


class BugViewState(Base):
    __tablename__ = "bug_view_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    bug_id: Mapped[int] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), index=True)
    user_email: Mapped[str] = mapped_column(ForeignKey("users.email"), index=True)
    last_viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    bug = relationship("Bug", back_populates="view_states")
