from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Decision

BAN_LIMIT = 3
LOCK_NAMESPACE = 912_438


async def slots_used(db: AsyncSession, project_id: int) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(Decision)
        .where(
            Decision.project_id == project_id,
            or_(
                Decision.state.in_(("sending", "confirmed")),
                and_(Decision.state == "failed", Decision.external_id.is_not(None)),
            ),
        )
    )
    return count or 0


async def claim(
    db: AsyncSession, project_id: int, decision_id: int, pattern: str, ban_ip: str
) -> tuple[bool, int]:
    await db.execute(select(func.pg_advisory_xact_lock(LOCK_NAMESPACE, project_id)))
    used = await slots_used(db, project_id)
    if used >= BAN_LIMIT:
        return False, used
    other = aliased(Decision)
    in_flight = (
        select(other.id)
        .where(
            other.project_id == project_id,
            other.steam_id == Decision.steam_id,
            other.state == "sending",
        )
        .exists()
    )
    decision = await db.scalar(
        update(Decision)
        .where(
            Decision.project_id == project_id,
            Decision.id == decision_id,
            Decision.state == "pending",
            Decision.action == "ban",
            Decision.primary_pattern == pattern,
            Decision.ban_ip == ban_ip,
            ~in_flight,
        )
        .values(state="sending")
        .returning(Decision.id)
    )
    return (True, used + 1) if decision is not None else (False, used)
