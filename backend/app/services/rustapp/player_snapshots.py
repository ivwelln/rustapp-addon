from collections.abc import Iterable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crud.players import ClientKind, upsert_player_client_kinds
from app.schemas.rustapp.players import RustAppPlayer


async def save_player_snapshot(
    sessions: async_sessionmaker[AsyncSession],
    *,
    project_id: int,
    players: Iterable[RustAppPlayer],
    observed_at: datetime,
) -> int:
    client_kinds: dict[str, ClientKind] = {}
    for player in players:
        if player.last_no_license is None:
            client_kinds.setdefault(player.steam_id, "unknown")
        else:
            client_kinds[player.steam_id] = "pirated" if player.last_no_license else "licensed"

    async with sessions.begin() as session:
        await upsert_player_client_kinds(
            session,
            project_id=project_id,
            client_kinds=client_kinds,
            observed_at=observed_at,
        )

    return len(client_kinds)
