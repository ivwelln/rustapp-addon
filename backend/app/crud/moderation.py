import logging
from collections.abc import Iterable
from time import time_ns

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.moderation import CollectionJob, CombatKill, Decision, Observation, ReferenceBan
from app.services.moderation.evaluate import describe, outcome
from app.services.moderation.facts import MINUTE, Ban, Kill, Patterns, Session

logger = logging.getLogger(__name__)


def now_ms() -> int:
    return time_ns() // 1_000_000


async def enqueue(db: AsyncSession, project: int, players: Iterable[str], *, seed: bool) -> None:
    at = now_ms()
    values = [
        dict(project_id=project, steam_id=sid, requested_at=at, due_at=at)
        for sid in sorted(set(players))
    ]
    if not values:
        return
    stmt = insert(CollectionJob).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_id", "steam_id"],
        set_={"due_at": func.least(CollectionJob.due_at, at), "requested_at": at},
        where=(CollectionJob.due_at.is_(None) if seed else CollectionJob.requested_at < at),
    )
    await db.execute(stmt)


async def latest(db: AsyncSession, project: int, sid: str, source: str) -> Observation | None:
    return await db.scalar(
        select(Observation)
        .where(
            Observation.project_id == project,
            Observation.steam_id == sid,
            Observation.source == source,
        )
        .order_by(Observation.id.desc())
        .limit(1)
    )


async def archive(db: AsyncSession, project: int, sid: str, source: str, data: dict) -> None:
    last = await latest(db, project, sid, source)
    if last is None or last.payload != data:
        db.add(
            Observation(
                project_id=project, steam_id=sid, source=source, observed_at=now_ms(), payload=data
            )
        )


async def save_kill(db: AsyncSession, project: int, server: int, sid: str, kill: Kill) -> None:
    stmt = insert(CombatKill).values(
        project_id=project,
        server_id=server,
        steam_id=sid,
        event_id=kill.id,
        at=kill.created_at,
        facts=kill.model_dump(),
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["project_id", "event_id"],
            set_={"facts": stmt.excluded.facts},
            where=CombatKill.facts != stmt.excluded.facts,
        )
    )


async def reference_bans(
    db: AsyncSession, project: int, signed_at: int | None, session: Session, rules: Patterns
) -> tuple[Ban, ...]:
    cutoff = session.connected_at - rules.P04.minutes * MINUTE
    query = ReferenceBan.at.between(cutoff, session.connected_at)
    if signed_at is not None:
        width = rules.P01.tolerance_seconds * 1000
        query = or_(query, ReferenceBan.signed_at.between(signed_at - width, signed_at + width))
    records = await db.scalars(
        select(ReferenceBan)
        .where(
            ReferenceBan.project_id == project,
            ReferenceBan.eligible,
            ReferenceBan.steam_id != session.steam_id,
            query,
        )
        .order_by(ReferenceBan.ban_id)
    )
    return tuple(
        Ban(
            record.ban_id,
            record.steam_id,
            record.at,
            record.signed_at,
            record.server_id,
            record.pirated,
        )
        for record in records
    )


async def save_decision(
    db: AsyncSession, session: Session, matches: dict, *, enforce: bool
) -> None:
    if not matches:
        return
    decision = await db.scalar(
        select(Decision)
        .where(
            Decision.project_id == session.project_id,
            Decision.steam_id == session.steam_id,
            Decision.server_id == session.server_id,
            Decision.connected_at == session.connected_at,
        )
        .with_for_update()
    )
    if decision is None:
        decision = Decision(
            project_id=session.project_id,
            steam_id=session.steam_id,
            server_id=session.server_id,
            connected_at=session.connected_at,
            matches={},
            state="recorded",
        )
        db.add(decision)
    # Keep the first evidence and freeze the dispatch reason once an action has been claimed.
    if decision.state in {"sending", "confirmed", "superseded", "failed", "cancelled"}:
        return
    previous_primary = decision.primary_pattern
    previous = decision.matches
    decision.matches = {**matches, **previous}
    decision.primary_pattern, decision.action = outcome(decision.matches)
    if not enforce and decision.action == "ban" and decision.primary_pattern != previous_primary:
        await db.flush()
        logger.info(
            "Наблюдение: правило предлагает блокировку | решение=%s SteamID=%s "
            "проект=%s паттерн=%s | %s",
            decision.id,
            session.steam_id,
            session.project_id,
            decision.primary_pattern,
            describe(decision.primary_pattern, decision.matches[decision.primary_pattern]),
        )
    if (
        decision.action == "ban"
        and enforce
        and decision.state == "recorded"
        and matches.keys() - previous.keys()
    ):
        decision.state = "pending"
