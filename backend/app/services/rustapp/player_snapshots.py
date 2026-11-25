from collections.abc import Iterable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crud.moderation import archive, now_ms
from app.crud.player_data import save_ip
from app.crud.players import ClientKind, upsert_player_client_kinds
from app.schemas.rustapp.players import RustAppPlayer


async def save_player_snapshot(
    sessions: async_sessionmaker[AsyncSession],
    *,
    project_id: int,
    players: Iterable[RustAppPlayer],
    observed_at: datetime,
) -> int:
    rows = sorted(players, key=lambda player: player.steam_id)
    client_kinds: dict[str, ClientKind] = {}
    for player in rows:
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
        for player in rows:
            payload = player.model_dump(exclude_unset=True)
            await archive(session, project_id, player.steam_id, "browse-full", payload)
            await save_ip(
                session,
                project_id,
                player.steam_id,
                payload.get("ip"),
                payload.get("last_online_at") or now_ms(),
                payload.get("ip_details") or {},
            )

    return len(client_kinds)
