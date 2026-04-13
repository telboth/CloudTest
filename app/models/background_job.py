from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class BackgroundJob(Base):
    __tablename__ = "background_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    job_type: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("users.email"), nullable=True, index=True)
    bug_id: Mapped[int | None] = mapped_column(ForeignKey("bugs.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    requester = relationship("User")
    bug = relationship("Bug")
