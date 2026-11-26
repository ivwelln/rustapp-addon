from collections.abc import Mapping

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.moderation import CollectionJob
from app.models.player_data import ObservedPlay, PlayerPresence, PresenceHeartbeat
from app.schemas.rustapp.realtime import PlayerStatus

PRESENT = ("online", "afk")
PRESENCE_TIMEOUT_MS = 60_000


async def confirm_presence_connection(db: AsyncSession, project: int, at: int) -> None:
    statement = insert(PresenceHeartbeat).values(project_id=project, confirmed_at=at)
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[PresenceHeartbeat.project_id],
            set_={"confirmed_at": at},
            where=PresenceHeartbeat.confirmed_at < at,
        )
    )


async def confirmed_presence_at(db: AsyncSession, project: int) -> int | None:
    return await db.scalar(
        select(PresenceHeartbeat.confirmed_at).where(PresenceHeartbeat.project_id == project)
    )


async def presence_is_current(db: AsyncSession, project: int, now: int) -> bool:
    confirmed = await confirmed_presence_at(db, project)
    return confirmed is not None and now - PRESENCE_TIMEOUT_MS <= confirmed <= now


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


def player_is_online(project, steam_id):
    return (
        select(PlayerPresence.steam_id)
        .where(
            PlayerPresence.project_id == project,
            PlayerPresence.steam_id == steam_id,
            PlayerPresence.status == "online",
        )
        .exists()
    )


async def remove_offline_jobs(db: AsyncSession, project: int, observed_at: int) -> None:
    offline = select(PlayerPresence.steam_id).where(
        PlayerPresence.project_id == project,
        PlayerPresence.observed_at == observed_at,
        PlayerPresence.status == "offline",
    )
    await db.execute(
        update(CollectionJob)
        .where(
            CollectionJob.project_id == project,
            CollectionJob.steam_id.in_(offline),
            CollectionJob.due_at.is_not(None),
        )
        .values(due_at=None)
    )


async def is_afk(db: AsyncSession, project: int, steam_id: str) -> bool:
    return bool(await db.scalar(select(player_is_afk(project, steam_id))))


def end_server_observation(
    db: AsyncSession,
    player: PlayerPresence,
    end: int,
) -> None:
    start = player.server_since
    if player.server_id is not None and start is not None and start < end:
        db.add(
            ObservedPlay(
                project_id=player.project_id,
                steam_id=player.steam_id,
                started_at=start,
                ended_at=end,
                server_id=player.server_id,
                is_pirated=player.is_pirated,
            )
        )
    player.server_id = player.server_since = player.server_verified_at = player.is_pirated = None


def observe_server(
    db: AsyncSession,
    player: PlayerPresence,
    server: int,
    pirated: bool | None,
    at: int,
) -> None:
    if (
        player.status not in PRESENT
        or player.active_since is None
        or at < player.active_since
        or (player.server_verified_at is not None and at < player.server_verified_at)
    ):
        return
    if player.server_id != server:
        if player.server_id is not None:
            player.active_since = at
            player.joined_at = player.connecting_at = None
        end_server_observation(db, player, player.server_verified_at or at)
        player.server_id = server
        player.server_since = at
    player.server_verified_at = at
    if pirated is not None:
        player.is_pirated = pirated


def transition(
    db: AsyncSession,
    player: PlayerPresence,
    status: PlayerStatus | None,
    observed_at: int,
    *,
    complete_snapshot: bool,
) -> None:
    at = observed_at // 1_000_000
    previous = player.status
    if status in PRESENT:
        if player.active_since is None:
            player.active_since = at
            if player.first_active_at is None:
                player.first_active_at = at
            if not complete_snapshot and previous in ("joining", "offline"):
                player.joined_at = at
            player.is_afk = status == "afk"
        else:
            player.is_afk = status == "afk"
    else:
        end = (player.server_verified_at or at) if complete_snapshot else at
        end_server_observation(db, player, end)
        player.active_since = player.joined_at = None
        player.connecting_at = (
            player.connecting_at or at if status == "joining" and not complete_snapshot else None
        )
    player.status = status
    player.observed_at = observed_at


async def save_presence(
    db: AsyncSession,
    project: int,
    statuses: Mapping[str, PlayerStatus],
    observed_at: int,
    *,
    complete_snapshot: bool = False,
    server_info: Mapping[str, tuple[int, bool | None]] | None = None,
) -> tuple[set[str], set[str]]:
    # Serialize snapshot and WebSocket writes for the project, including newly observed IDs.
    await db.execute(select(func.pg_advisory_xact_lock(7419, project)))
    query = select(PlayerPresence).where(PlayerPresence.project_id == project)
    if not complete_snapshot:
        query = query.where(PlayerPresence.steam_id.in_(tuple(statuses)))
    elif statuses:
        query = query.where(
            PlayerPresence.steam_id.in_(tuple(statuses)) | PlayerPresence.status.in_(PRESENT)
        )
    else:
        query = query.where(PlayerPresence.status.in_(PRESENT))
    known = {
        player.steam_id: player
        for player in await db.scalars(query.order_by(PlayerPresence.steam_id).with_for_update())
    }
    if complete_snapshot:
        for steam_id, player in known.items():
            if steam_id not in statuses and player.observed_at < observed_at:
                transition(db, player, "offline", observed_at, complete_snapshot=True)

    online: set[str] = set()
    resumed: set[str] = set()
    for steam_id, status in statuses.items():
        player = known.get(steam_id)
        if player is None:
            player = PlayerPresence(
                project_id=project, steam_id=steam_id, is_afk=False, observed_at=0
            )
            db.add(player)
        if player.observed_at >= observed_at:
            continue
        previously_afk = player.is_afk
        transition(db, player, status, observed_at, complete_snapshot=complete_snapshot)
        if server_info and steam_id in server_info:
            server, pirated = server_info[steam_id]
            observe_server(db, player, server, pirated, observed_at // 1_000_000)
        if status == "online":
            online.add(steam_id)
            if previously_afk:
                resumed.add(steam_id)
    return online, resumed


async def invalidate_presence(db: AsyncSession, project: int, observed_at: int) -> None:
    confirmed = await db.scalar(
        delete(PresenceHeartbeat)
        .where(PresenceHeartbeat.project_id == project)
        .returning(PresenceHeartbeat.confirmed_at)
    )
    players = await db.scalars(
        select(PlayerPresence)
        .where(
            PlayerPresence.project_id == project,
            PlayerPresence.status.in_((*PRESENT, "joining")),
            PlayerPresence.observed_at < observed_at,
        )
        .with_for_update()
    )
    for player in players:
        if confirmed is not None:
            end_server_observation(db, player, min(confirmed, observed_at // 1_000_000))
        transition(db, player, None, observed_at, complete_snapshot=True)
