from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), primary_key=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(50), index=True)
    auth_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entra_oid: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    reported_bugs = relationship(
        "Bug",
        back_populates="reporter",
        foreign_keys="Bug.reporter_id",
    )
    assigned_bugs = relationship(
        "Bug",
        back_populates="assignee",
        foreign_keys="Bug.assignee_id",
    )
