from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.player_data import stored_sessions
from app.crud.presence import PRESENT, confirmed_presence_at
from app.models import ObservedPlay, PlayerPresence
from app.services.moderation.facts import MINUTE, Session

YEAR_MS = 365 * 24 * 60 * MINUTE
WHITELIST_PLAYTIME_MS = 3 * 60 * MINUTE


def played_ms(sessions: Iterable[Session], server_id: int | None, now: int) -> int:
    """Count non-overlapping intervals across the project or on one requested server."""
    ordered = sorted(sessions, key=lambda session: session.connected_at)
    covered_until = now - YEAR_MS
    total = 0
    for index, session in enumerate(ordered):
        if session.joined_at is None or (server_id is not None and session.server_id != server_id):
            continue
        following = ordered[index + 1].connected_at if index + 1 < len(ordered) else None
        # A stale open record followed by another connection has no reliable end time.
        if session.disconnected_at is None and following is not None:
            continue
        end = min(session.disconnected_at or now, following or now, now)
        start = max(session.joined_at, covered_until)
        if end > start:
            total += end - start
            covered_until = end
    return total


async def known_sessions(
    db: AsyncSession,
    project: int,
    steam_id: str,
    presence: PlayerPresence | None,
    now: int,
) -> list[Session]:
    records = [
        session
        for session in await stored_sessions(db, project, steam_id)
        if session.disconnected_at is not None
    ]
    observed = await db.scalars(
        select(ObservedPlay).where(
            ObservedPlay.project_id == project,
            ObservedPlay.steam_id == steam_id,
            ObservedPlay.ended_at > now - YEAR_MS,
        )
    )
    records.extend(
        Session(
            project_id=project,
            steam_id=steam_id,
            server_id=record.server_id,
            connected_at=record.started_at,
            joined_at=record.started_at,
            disconnected_at=record.ended_at,
            is_license=None if record.is_pirated is None else not record.is_pirated,
        )
        for record in observed
    )
    if (
        presence is not None
        and presence.status in PRESENT
        and presence.server_id is not None
        and presence.server_since is not None
    ):
        confirmed = await confirmed_presence_at(db, project)
        if confirmed is not None and confirmed > presence.server_since:
            records.append(
                Session(
                    project_id=project,
                    steam_id=steam_id,
                    server_id=presence.server_id,
                    connected_at=presence.server_since,
                    joined_at=presence.server_since,
                    disconnected_at=min(confirmed, now),
                    is_license=None if presence.is_pirated is None else not presence.is_pirated,
                )
            )
    return sorted(records, key=lambda item: item.connected_at)
