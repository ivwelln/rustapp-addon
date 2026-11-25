from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.player_data import PlayerPresence
from app.schemas.rustapp.realtime import PlayerStatus


def player_is_afk(project, steam_id):
    return (
        select(PlayerPresence.steam_id)
        .where(
            PlayerPresence.project_id == project,
            PlayerPresence.steam_id == steam_id,
            PlayerPresence.is_afk,
        )
        .exists()
    )


async def is_afk(db: AsyncSession, project: int, steam_id: str) -> bool:
    return bool(await db.scalar(select(player_is_afk(project, steam_id))))


async def save_presence(
    db: AsyncSession, project: int, statuses: Mapping[str, PlayerStatus], observed_at: int
) -> tuple[set[str], set[str]]:
    values = [
        dict(project_id=project, steam_id=steam_id, is_afk=status == "afk", observed_at=observed_at)
        for steam_id, status in sorted(statuses.items())
        if status in ("online", "afk")
    ]
    if not values:
        return set(), set()
    previously_afk = set(
        await db.scalars(
            select(PlayerPresence.steam_id).where(
                PlayerPresence.project_id == project,
                PlayerPresence.steam_id.in_([value["steam_id"] for value in values]),
                PlayerPresence.is_afk,
            )
        )
    )
    statement = insert(PlayerPresence).values(values)
    incoming = statement.excluded
    accepted = await db.execute(
        statement.on_conflict_do_update(
            index_elements=[PlayerPresence.project_id, PlayerPresence.steam_id],
            set_={"is_afk": incoming.is_afk, "observed_at": incoming.observed_at},
            where=PlayerPresence.observed_at < incoming.observed_at,
        ).returning(PlayerPresence.steam_id, PlayerPresence.is_afk)
    )
    online = {steam_id for steam_id, paused in accepted if not paused}
    return online, online & previously_afk
