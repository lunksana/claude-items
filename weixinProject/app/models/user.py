from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class UserRole(StrEnum):
    USER = "user"      # 普通用户
    AGENT = "agent"    # 代理商


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    openid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    unionid: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    nickname: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default=UserRole.USER.value)

    # Direct inviter only. Used by ledger_service to pay tier-2 commission.
    # The inviter's inviter is intentionally never read — see 二级分销 rule.
    inviter_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )
    inviter: Mapped["User | None"] = relationship(remote_side="User.id")

    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
