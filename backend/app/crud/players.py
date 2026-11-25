from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, case, func, or_, select, update
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
                "is_whitelisted": client_kind == "licensed",
                "whitelist_reason": "license" if client_kind == "licensed" else None,
                "client_kind_observed_at": observed_at if client_kind != "unknown" else None,
            }
            for steam_id, client_kind in sorted(client_kinds.items())
        ]
    )
    incoming = statement.excluded
    newer = and_(
        incoming.client_kind_observed_at.is_not(None),
        or_(
            Player.client_kind_observed_at.is_(None),
            Player.client_kind_observed_at < incoming.client_kind_observed_at,
        ),
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Player.project_id, Player.steam_id],
            set_={
                "last_client_kind": case(
                    (newer, incoming.last_client_kind), else_=Player.last_client_kind
                ),
                "client_kind_observed_at": case(
                    (newer, incoming.client_kind_observed_at), else_=Player.client_kind_observed_at
                ),
                "is_whitelisted": or_(Player.is_whitelisted, incoming.is_whitelisted),
                "whitelist_reason": func.coalesce(
                    Player.whitelist_reason, incoming.whitelist_reason
                ),
            },
            where=or_(newer, and_(incoming.is_whitelisted, ~Player.is_whitelisted)),
        )
    )


async def whitelisted(db: AsyncSession, project: int, steam_id: str, *, lock: bool = False) -> bool:
    query = select(Player.is_whitelisted).where(
        Player.project_id == project, Player.steam_id == steam_id
    )
    if lock:
        await db.execute(
            insert(Player)
            .values(project_id=project, steam_id=steam_id)
            .on_conflict_do_nothing(index_elements=[Player.project_id, Player.steam_id])
        )
        query = query.with_for_update()
    return await db.scalar(query) is True


async def whitelist_for_playtime(db: AsyncSession, project: int, steam_id: str) -> None:
    await db.execute(
        update(Player)
        .where(Player.project_id == project, Player.steam_id == steam_id, ~Player.is_whitelisted)
        .values(is_whitelisted=True, whitelist_reason="playtime")
    )
