from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class InAppNotification(Base):
    __tablename__ = "in_app_notifications"
    __table_args__ = (
        Index(
            "ix_in_app_notifications_recipient_read_created",
            "recipient_email",
            "is_read",
            "created_at",
        ),
        Index("ix_in_app_notifications_bug_id_created_at", "bug_id", "created_at"),
        Index("ix_in_app_notifications_dedupe_key", "dedupe_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    recipient_email: Mapped[str] = mapped_column(String(255), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    bug_id: Mapped[int | None] = mapped_column(
        ForeignKey("bugs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    bug = relationship("Bug")


class NotificationOutboxEvent(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        Index("ix_notification_outbox_status_created", "status", "created_at"),
        Index("ix_notification_outbox_recipient_status", "recipient_email", "status"),
        Index("ix_notification_outbox_bug_id_created", "bug_id", "created_at"),
        Index("ix_notification_outbox_dedupe_key", "dedupe_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="in_app", server_default=text("'in_app'"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default=text("'pending'"))
    recipient_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    bug_id: Mapped[int | None] = mapped_column(
        ForeignKey("bugs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    bug = relationship("Bug")
