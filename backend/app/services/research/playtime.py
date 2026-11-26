from decimal import Decimal
from itertools import groupby

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rustapp.exceptions import RustAppResponseError
from app.crud.presence import PRESENT
from app.models import Player, PlayerPlaytime, PlayerPresence, PlaytimeDay
from app.services.moderation.playtime import YEAR_MS, known_sessions, played_ms

DAY = 86_400_000
HOUR = 3_600_000
CONTROL_MS = 20 * HOUR


def observed_hours_lower_bound(now: int):
    # Up to this much recorded activity may since have rolled out of the year.
    age = func.greatest(0, now - PlayerPlaytime.calculated_at)
    return func.greatest(0, PlayerPlaytime.observed_ms - age)


def daily_playtime(payload: dict, sid: str) -> dict[int, int]:
    combat = payload.get("kills")
    if not isinstance(combat, dict) or combat.get("steam_id") not in (None, sid):
        raise RustAppResponseError("Некорректная принадлежность статистики игроку")
    play = payload.get("playTime")
    buckets = play.get("stats") if isinstance(play, dict) else None
    if not isinstance(buckets, dict) or not buckets:
        raise RustAppResponseError("Нет дневной разбивки игрового времени")
    days = {}
    for key, item in buckets.items():
        day = item.get("date") if isinstance(item, dict) else None
        hours = item.get("hours_played") if isinstance(item, dict) else None
        if (
            type(day) is not int
            or str(day) != key
            or day % DAY
            or type(hours) not in (int, float)
            or not 0 <= hours <= 24
        ):
            raise RustAppResponseError("Некорректный день статистики RustApp")
        days[day] = int(round(Decimal(str(hours)) * HOUR))
    return days


async def cache_days(
    db: AsyncSession,
    project: int,
    sid: str,
    payload: dict,
    params: dict | None,
    observed_at: int,
) -> int:
    days = daily_playtime(payload, sid)
    # Historical moderation responses did not record their exact range. Only interior UTC
    # days are known to be complete. A missing bucket is never evidence for zero hours.
    start = params["time_from"] if params else min(days) + DAY
    end = min(params["time_to"] + 1 if params else max(days), observed_at)
    rows = [
        dict(project_id=project, steam_id=sid, day=day, played_ms=duration, observed_at=observed_at)
        for day, duration in sorted(days.items())
        if start <= day and day + DAY <= end
    ]
    if not rows:
        return 0
    statement = insert(PlaytimeDay).values(rows)
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[PlaytimeDay.project_id, PlaytimeDay.steam_id, PlaytimeDay.day],
            set_={"played_ms": statement.excluded.played_ms, "observed_at": observed_at},
            where=PlaytimeDay.observed_at < observed_at,
        )
    )
    await db.execute(
        update(PlayerPlaytime)
        .where(
            PlayerPlaytime.project_id == project,
            PlayerPlaytime.steam_id == sid,
        )
        .values(next_calculation=0)
    )
    return len(rows)


async def missing_range(db: AsyncSession, project: int, sid: str, now: int) -> dict | None:
    end = now // DAY * DAY
    start = end - YEAR_MS
    present = set(
        await db.scalars(
            select(PlaytimeDay.day).where(
                PlaytimeDay.project_id == project,
                PlaytimeDay.steam_id == sid,
                PlaytimeDay.day >= start,
                PlaytimeDay.day < end,
            )
        )
    )
    missing = (day for day in range(start, end, DAY) if day not in present)
    gaps = [
        [day for _, day in group]
        for _, group in groupby(
            enumerate(missing),
            key=lambda pair: pair[1] - pair[0] * DAY,
        )
    ]
    if not gaps:
        return None
    gap = max(gaps, key=len)
    return {"time_from": gap[0], "time_to": gap[-1] + DAY - 1}


async def refresh_hours(db: AsyncSession, project: int, now: int) -> None:
    # Seed cheaply, then recompute a bounded batch. Exits wake the local cache early.
    await db.execute(
        insert(PlayerPlaytime)
        .from_select(
            ["project_id", "steam_id"],
            select(Player.project_id, Player.steam_id)
            .where(
                Player.project_id == project,
                ~select(PlayerPlaytime.steam_id)
                .where(
                    PlayerPlaytime.project_id == project,
                    PlayerPlaytime.steam_id == Player.steam_id,
                )
                .exists(),
            )
            .order_by(Player.steam_id)
            .limit(128),
        )
        .on_conflict_do_nothing()
    )
    rows = (
        await db.execute(
            select(PlayerPlaytime, PlayerPresence)
            .outerjoin(
                PlayerPresence,
                (PlayerPresence.project_id == PlayerPlaytime.project_id)
                & (PlayerPresence.steam_id == PlayerPlaytime.steam_id),
            )
            .where(
                PlayerPlaytime.project_id == project,
                or_(
                    PlayerPlaytime.next_calculation <= now,
                    (PlayerPresence.status == "offline")
                    & (PlayerPresence.observed_at > PlayerPlaytime.calculated_at * 1_000_000),
                ),
            )
            .order_by(PlayerPlaytime.next_calculation, PlayerPlaytime.steam_id)
            .limit(64)
            .with_for_update(of=PlayerPlaytime, skip_locked=True)
        )
    ).all()
    end = now // DAY * DAY
    for cache, presence in rows:
        history = await known_sessions(db, project, cache.steam_id, presence, now)
        cache.observed_ms = played_ms(history, server_id=None, now=now) if history else None
        cache.calculated_at = now
        cache.next_calculation = (
            now + 180_000 if presence and presence.status in PRESENT else end + DAY
        )
        count, total = (
            await db.execute(
                select(
                    func.count(),
                    func.sum(PlaytimeDay.played_ms),
                ).where(
                    PlaytimeDay.project_id == project,
                    PlaytimeDay.steam_id == cache.steam_id,
                    PlaytimeDay.day >= end - YEAR_MS,
                    PlaytimeDay.day < end,
                )
            )
        ).one()
        cache.project_days = count
        cache.project_ms = int(total) if count else None
        cache.project_day_end = end
