from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.commission import CommissionLedger, LedgerStatus
from app.models.user import User


async def record_commission_for_order(
    session: AsyncSession,
    *,
    order_id: int,
    order_amount: Decimal,
    seller: User,
    tier1_rate: Decimal | None = None,
    tier2_rate: Decimal | None = None,
) -> list[CommissionLedger]:
    # 二级分销 enforcement: writes at most TWO ledger rows — one for the
    # seller (tier 1) and one for the seller's DIRECT inviter (tier 2) if
    # one exists. seller.inviter.inviter is intentionally never read.
    # Three-tier distribution is banned by WeChat / PRC law; the
    # CommissionLedger.tier CHECK constraint guards the DB layer, and
    # this function is the only sanctioned write path at the app layer.
    tier1_rate = tier1_rate if tier1_rate is not None else settings.commission_tier1_rate
    tier2_rate = tier2_rate if tier2_rate is not None else settings.commission_tier2_rate

    entries: list[CommissionLedger] = [
        CommissionLedger(
            order_id=order_id,
            agent_id=seller.id,
            tier=1,
            amount=order_amount * tier1_rate,
            status=LedgerStatus.PENDING.value,
        )
    ]

    if seller.inviter_id is not None:
        entries.append(
            CommissionLedger(
                order_id=order_id,
                agent_id=seller.inviter_id,
                tier=2,
                amount=order_amount * tier2_rate,
                status=LedgerStatus.PENDING.value,
            )
        )

    session.add_all(entries)
    await session.flush()
    return entries
