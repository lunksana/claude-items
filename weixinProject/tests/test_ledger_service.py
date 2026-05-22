from decimal import Decimal

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from app.models.commission import CommissionLedger
from app.models.user import User, UserRole
from app.services.ledger_service import record_commission_for_order


async def _make_agent(session, openid: str, inviter_id: int | None = None) -> User:
    user = User(openid=openid, role=UserRole.AGENT.value, inviter_id=inviter_id)
    session.add(user)
    await session.flush()
    return user


async def test_agent_with_inviter_writes_two_tiers(session):
    """B (invited by A) makes a sale -> tier 1 to B, tier 2 to A."""
    a = await _make_agent(session, "openid_A")
    b = await _make_agent(session, "openid_B", inviter_id=a.id)

    entries = await record_commission_for_order(
        session,
        order_id=1001,
        order_amount=Decimal("100.00"),
        seller=b,
        tier1_rate=Decimal("0.10"),
        tier2_rate=Decimal("0.05"),
    )

    assert len(entries) == 2
    assert {(e.agent_id, e.tier, e.amount) for e in entries} == {
        (b.id, 1, Decimal("10.00")),
        (a.id, 2, Decimal("5.00")),
    }


async def test_top_level_agent_writes_only_tier1(session):
    """An agent with no inviter -> only tier 1 entry."""
    a = await _make_agent(session, "openid_A")

    entries = await record_commission_for_order(
        session, order_id=1002, order_amount=Decimal("100.00"), seller=a,
    )

    assert len(entries) == 1
    assert entries[0].tier == 1
    assert entries[0].agent_id == a.id


async def test_three_level_chain_grandparent_gets_nothing(session):
    """A -> B -> C chain: when C sells, A (grandparent) must NOT be paid."""
    a = await _make_agent(session, "openid_A")
    b = await _make_agent(session, "openid_B", inviter_id=a.id)
    c = await _make_agent(session, "openid_C", inviter_id=b.id)

    entries = await record_commission_for_order(
        session, order_id=1003, order_amount=Decimal("100.00"), seller=c,
    )

    recipient_ids = {e.agent_id for e in entries}
    assert c.id in recipient_ids
    assert b.id in recipient_ids
    assert a.id not in recipient_ids, "grandparent must NEVER receive commission"


async def test_db_check_constraint_rejects_tier_3(session):
    """DB-level safety net: a raw INSERT with tier=3 must fail."""
    a = await _make_agent(session, "openid_A")

    with pytest.raises(IntegrityError):
        await session.execute(
            insert(CommissionLedger).values(
                order_id=999, agent_id=a.id, tier=3, amount=Decimal("1.00"),
            )
        )
        await session.flush()
