from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LedgerStatus(StrEnum):
    PENDING = "pending"
    SETTLED = "settled"
    CLAWED_BACK = "clawed_back"


class CommissionLedger(Base):
    # Append-only commission ledger.
    # tier 1 = direct sale by the agent.
    # tier 2 = sale by the agent's direct invitee.
    # tier 3+ is 传销 under PRC law and banned by WeChat — the CHECK
    # constraint below is the DB-level safety net; ledger_service is the
    # only sanctioned write path at the application level.
    __tablename__ = "commission_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tier: Mapped[int] = mapped_column()
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(16), default=LedgerStatus.PENDING.value)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint("tier IN (1, 2)", name="ck_commission_ledger_tier_max_2"),
    )
