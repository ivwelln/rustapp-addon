import logging
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rustapp.exceptions import RustAppResponseError
from app.crud.presence import presence_is_current
from app.models import Player, PlayerPlaytime, PlayerPresence, ResearchCase, ResearchSource
from app.services.research.cases import ELIGIBLE, VERSION
from app.services.research.playtime import DAY, cache_days, missing_range
from app.services.research.snapshots import Snapshot, cached_sample, finish, prepare

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Work:
    steam_id: str
    source: str
    params: dict
    case_id: int | None
    lease: str
    attempts: int


def pending(project: int, server: int):
    return (
        select(ResearchSource, ResearchCase)
        .join(ResearchCase)
        .where(
            ResearchCase.project_id == project,
            ResearchCase.server_id == server,
            ResearchCase.version == VERSION,
            ResearchCase.label.in_(ELIGIBLE),
        )
    )


async def hydrate(db: AsyncSession, project: int, server: int) -> int:
    rows = (
        await db.execute(
            pending(project, server)
            .where(ResearchSource.status == "queued")
            .order_by(ResearchCase.id, ResearchSource.source)
            .limit(48)
            .with_for_update(of=ResearchSource, skip_locked=True)
        )
    ).all()
    for source, case in rows:
        if found := await cached_sample(db, case, source):
            finish(source, *found)
        else:
            source.status = "needs_fetch"
    return len(rows)


async def claim_source(db: AsyncSession, project: int, server: int, now: int) -> Work | None:
    # Give both cohorts turns, even when a large historical import is waiting.
    source_history = ResearchSource.__table__.alias("history")
    case_history = ResearchCase.__table__.alias("history_cases")
    last_attempt = (
        select(func.max(source_history.c.attempted_at))
        .select_from(
            source_history.join(case_history, source_history.c.case_id == case_history.c.id)
        )
        .where(
            case_history.c.project_id == project,
            case_history.c.server_id == server,
            case_history.c.cohort == ResearchCase.cohort,
        )
        .correlate(ResearchCase)
        .scalar_subquery()
    )
    query = (
        pending(project, server)
        .where(
            ResearchSource.status.in_(("needs_fetch", "leased")),
            ResearchSource.next_at <= now,
        )
        .order_by(
            last_attempt.nulls_first(),
            ResearchSource.next_at,
            ResearchCase.id,
            ResearchSource.source,
        )
        .limit(1)
        .with_for_update(of=ResearchSource, skip_locked=True)
    )
    row = (await db.execute(query)).first()
    if row is None:
        return None
    source, case = row
    if found := await cached_sample(db, case, source):
        finish(source, *found)
        return None
    source.lease = uuid4().hex
    source.status, source.next_at = "leased", now + 300_000
    return Work(case.steam_id, source.source, source.params, case.id, source.lease, source.attempts)


async def claim_hours(db: AsyncSession, project: int, server: int, now: int) -> Work | None:
    player_cases = select(ResearchCase.id).where(
        ResearchCase.project_id == project,
        ResearchCase.server_id == server,
        ResearchCase.steam_id == PlayerPlaytime.steam_id,
        ResearchCase.version == VERSION,
    )
    selected = player_cases.where(ResearchCase.label.in_(ELIGIBLE)).exists()
    awaiting = player_cases.where(ResearchCase.label == "awaiting_playtime").exists()
    online = PlayerPresence.status == "online"
    regular = (
        online & (~Player.is_whitelisted | ~selected)
        if await presence_is_current(db, project, now)
        else False
    )
    query = (
        select(PlayerPlaytime)
        .join(
            Player,
            (Player.project_id == PlayerPlaytime.project_id)
            & (Player.steam_id == PlayerPlaytime.steam_id),
        )
        .outerjoin(
            PlayerPresence,
            (PlayerPresence.project_id == PlayerPlaytime.project_id)
            & (PlayerPresence.steam_id == PlayerPlaytime.steam_id),
        )
        .where(
            PlayerPlaytime.project_id == project,
            Player.last_client_kind == "pirated",
            PlayerPlaytime.stats_after <= now,
            or_(regular, (selected | awaiting) & PlayerPlaytime.stats_checked_at.is_(None)),
        )
        .order_by(PlayerPlaytime.stats_after, PlayerPlaytime.steam_id)
        .limit(1)
        .with_for_update(of=PlayerPlaytime, skip_locked=True)
    )
    cache = await db.scalar(query)
    if cache is None:
        return None
    params = await missing_range(db, project, cache.steam_id, now)
    if not params:
        cache.stats_checked_at, cache.stats_after = now, now // DAY * DAY + DAY
        return None
    cache.stats_after = now + 300_000
    return Work(cache.steam_id, "stats", params, None, "", 0)


async def store_work(db: AsyncSession, project: int, job: Work, raw: dict, now: int):
    if job.case_id is None:
        cache = await db.get(PlayerPlaytime, (project, job.steam_id), with_for_update=True)
        days = await cache_days(db, project, job.steam_id, raw, job.params, now)
        expected = (job.params["time_to"] + 1 - job.params["time_from"]) // DAY
        if not days or days != expected:
            raise RustAppResponseError("Неполный ответ на интервал статистики")
        remaining = await missing_range(db, project, job.steam_id, now)
        if remaining is None:
            cache.stats_checked_at = now
        cache.stats_after = now + (DAY if remaining is None else 180_000)
        return
    case = await db.get(ResearchCase, job.case_id)
    source = await db.get(ResearchSource, (job.case_id, job.source), with_for_update=True)
    if source.lease != job.lease:
        return
    snapshot = Snapshot(raw, job.params, now, "rustapp")
    sample = prepare(case, source, snapshot)
    if sample is None:
        raise RustAppResponseError("Ответ не соответствует запросу исследовательского среза")
    if job.source == "stats":
        await cache_days(db, project, job.steam_id, raw, job.params, now)
    finish(source, snapshot, sample)
    source.attempted_at, source.attempts = now, source.attempts + 1


async def reschedule(
    db: AsyncSession,
    project: int,
    job: Work,
    now: int,
    *,
    due: int,
    reason: str,
    sent: bool,
    terminal: bool = False,
) -> None:
    if job.case_id is None:
        cache = await db.get(PlayerPlaytime, (project, job.steam_id), with_for_update=True)
        cache.stats_after = due if not terminal else now + 7 * DAY
        return
    source = await db.get(ResearchSource, (job.case_id, job.source), with_for_update=True)
    if source.lease != job.lease:
        return
    source.lease = None
    source.next_at = due
    if sent:
        source.attempts += 1
        source.attempted_at = now
        source.error = reason
    source.status = "unavailable" if terminal else "needs_fetch"
