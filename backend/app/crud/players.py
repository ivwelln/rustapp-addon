from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.player import Player

ClientKind = Literal["licensed", "pirated", "unknown"]


async def upsert_player_client_kinds(
    session: AsyncSession,
    *,
    project_id: int,
    client_kinds: Mapping[str, ClientKind],
    observed_at: datetime,
) -> None:
    if not client_kinds:
        return

    statement = insert(Player).values(
        [
            {
                "project_id": project_id,
                "steam_id": steam_id,
                "last_client_kind": client_kind,
                "client_kind_observed_at": observed_at if client_kind != "unknown" else None,
            }
            for steam_id, client_kind in client_kinds.items()
        ]
    )
    incoming = statement.excluded
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Player.project_id, Player.steam_id],
            set_={
                "last_client_kind": incoming.last_client_kind,
                "client_kind_observed_at": incoming.client_kind_observed_at,
            },
            where=and_(
                incoming.client_kind_observed_at.is_not(None),
                or_(
                    Player.client_kind_observed_at.is_(None),
                    Player.client_kind_observed_at < incoming.client_kind_observed_at,
                ),
            ),
        )
    )
