import logging
from collections.abc import AsyncIterator
from ipaddress import ip_address

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.clients.rustapp.client import RustAppClient
from app.clients.rustapp.exceptions import (
    RustAppHTTPError,
    RustAppResponseError,
    RustAppTransportError,
)
from app.clients.rustapp.moderation import Profile, is_player, read, results
from app.core.config import ModerationSettings
from app.crud.moderation import archive, latest, now_ms
from app.crud.player_data import stored_sessions
from app.crud.players import whitelist_for_playtime, whitelisted
from app.models import Decision, ReferenceBan
from app.services.moderation.collect import (
    cache_profile,
    cache_sessions,
    fetch_profile,
    fetch_sessions,
)
from app.services.moderation.evaluate import PRIORITY, describe
from app.services.moderation.facts import MINUTE
from app.services.moderation.playtime import WHITELIST_PLAYTIME_MS, played_ms

logger = logging.getLogger(__name__)

MARKER = "rustapp-addon:"


async def usable_ban(db: AsyncSession, project: int, ban: dict) -> bool:
    if (
        ban.get("computed_is_active") is not True
        or str(ban.get("reason", "")).strip().casefold() != "чит"
    ):
        return False
    marker = str(ban.get("comment", "")).partition(" | ")[0]
    query = select(Decision).where(
        Decision.project_id == project,
        Decision.steam_id == ban["steam_id"],
        Decision.state.in_(("sending", "confirmed")),
    )
    if marker.startswith(MARKER):
        parts = marker.removeprefix(MARKER).split(":")
        if len(parts) != 2 or not parts[0].isascii() or not parts[0].isdigit():
            return False
        decision = await db.scalar(query.where(Decision.id == int(parts[0])))
        if decision is None or decision.primary_pattern != parts[1]:
            return False
    else:
        # An edited panel comment must not turn a registration-derived ban into a manual seed.
        decision = await db.scalar(query.where(Decision.external_id == ban["id"]))
    return decision is None or decision.primary_pattern not in {"P01", "P02"}


async def sync_bans(
    db: AsyncSession, client: RustAppClient, project: int, *, initial_pages: int = 1
) -> None:
    # On startup collect the most recent page. On recovery bridge the gap to the last known ID.
    last_seen = await db.scalar(
        select(ReferenceBan.ban_id)
        .where(ReferenceBan.project_id == project)
        .order_by(ReferenceBan.at.desc(), ReferenceBan.ban_id.desc())
        .limit(1)
    )
    page = 0
    until = now_ms()
    while True:
        data = await read(
            client,
            "/ban/browse",
            project_id=project,
            page=page,
            limit=50,
            sort_by="created",
            created_to=until,
        )
        rows = results(data, page=page)
        for ban in rows:
            if ban.get("project_id") != project or not is_player(ban.get("steam_id")):
                raise RustAppResponseError("Некорректная запись бана проекта")
            existing = await db.get(ReferenceBan, (project, ban["id"]))
            if existing and existing.payload == ban:
                continue
            eligible = await usable_ban(db, project, ban)
            signed_at = server = None
            pirate = False
            if eligible:
                sid = ban["steam_id"]
                sessions = await stored_sessions(db, project, sid)
                cached_profile = await latest(db, project, sid, "details")
                if cached_profile is not None:
                    signed_at = Profile.model_validate(cached_profile.payload["player"]).signed_at
                try:
                    if signed_at is None and not await whitelisted(db, project, sid):
                        profile, details = await fetch_profile(client, project, sid)
                        signed_at = profile.signed_at
                        await cache_profile(db, project, sid, details)
                    cached_history = await latest(db, project, sid, "session-history")
                    if (
                        cached_history is None or cached_history.observed_at < ban["created_at"]
                    ) and not await whitelisted(db, project, sid):
                        sessions, history = await fetch_sessions(client, project, sid)
                        await cache_sessions(db, project, sid, history)
                except RustAppHTTPError as exc:
                    if exc.status_code not in (404, 422):
                        raise
                prior = [
                    session
                    for session in sessions
                    if 0 <= ban["created_at"] - session.connected_at <= 30 * 1440 * MINUTE
                    and (not ban.get("server_ids") or session.server_id in ban["server_ids"])
                ]
                if prior:
                    server = prior[-1].server_id
                    pirate = prior[-1].is_license is False
            values = dict(
                project_id=project,
                ban_id=ban["id"],
                steam_id=ban["steam_id"],
                at=ban["created_at"],
                signed_at=signed_at,
                server_id=server,
                pirated=pirate,
                eligible=eligible,
                payload=ban,
            )
            stmt = insert(ReferenceBan).values(**values)
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["project_id", "ban_id"],
                    set_={
                        column: value
                        for column, value in values.items()
                        if column not in {"project_id", "ban_id"}
                    },
                )
            )
        if (
            (last_seen is None and page + 1 >= initial_pages)
            or len(rows) < 50
            or any(ban["id"] == last_seen for ban in rows)
        ):
            return
        page += 1


async def player_bans(
    db: AsyncSession, client: RustAppClient, project: int, sid: str
) -> AsyncIterator[dict]:
    params = dict(project_id=project, steam_id=sid, sort_by="created", limit=50)
    page = 0
    while True:
        data = await read(client, "/ban/browse", page=page, **params)
        rows = results(data, page=page)
        if any(ban.get("project_id") != project or ban.get("steam_id") != sid for ban in rows):
            raise RustAppResponseError("Несовпадение владельца бана")
        await archive(db, project, sid, f"ban-history:{page}", data)
        for ban in rows:
            yield ban
        if len(rows) < 50:
            return
        page += 1


async def valid_reference(
    db: AsyncSession, client: RustAppClient, project: int, pattern: str, evidence: dict
) -> bool:
    ids = [ban["id"] for ban in evidence["bans"]] if pattern == "P02" else evidence["prior_bans"]
    for ban_id in ids:
        known = await db.get(ReferenceBan, (project, ban_id))
        if known is not None and await whitelisted(db, project, known.steam_id):
            continue
        rows = results(
            await read(
                client,
                "/ban/browse",
                page=0,
                limit=1,
                project_id=project,
                **{"ban_ids[]": ban_id},
                sort_by="created",
            )
        )
        for ban in rows:
            if ban.get("id") == ban_id and ban.get("project_id") == project:
                if await usable_ban(db, project, ban):
                    return True
    return False


def covers_requested_ban(ban: dict, expected_ip: str) -> bool:
    if (
        ban.get("computed_is_active") is not True
        or ban.get("server_ids") != []
        or ban.get("expired_at") is not None
        or ban.get("ban_ip_active") is not True
        or not isinstance(ban.get("ban_ip"), str)
    ):
        return False
    try:
        return str(ip_address(ban["ban_ip"])) == expected_ip
    except ValueError:
        return False


async def deliver(
    sessions: async_sessionmaker[AsyncSession],
    client: RustAppClient,
    project: int,
    settings: ModerationSettings,
) -> None:
    if not settings.enforce:
        return
    # Claim durably before sending. An ambiguous PUT is reconciled by marker, never repeated.
    async with sessions.begin() as db:
        other = aliased(Decision)
        unresolved = (
            select(other.id)
            .where(
                other.project_id == Decision.project_id,
                other.steam_id == Decision.steam_id,
                other.state == "sending",
            )
            .exists()
        )
        row = await db.scalar(
            select(Decision)
            .where(
                Decision.project_id == project,
                Decision.server_id == settings.server_id,
                Decision.state == "pending",
                ~unresolved,
            )
            .order_by(Decision.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return
        sid = row.steam_id
        key = row.id
        if await whitelisted(db, project, sid, lock=True):
            row.state = "cancelled"
            return
        history, history_data = await fetch_sessions(client, project, sid)
        if await cache_sessions(db, project, sid, history_data):
            row.state = "cancelled"
            return
        current = next(
            (session for session in reversed(history) if session.server_id == row.server_id), None
        )
        if (
            not current
            or current.is_license is not False
            or current.disconnected_at is not None
            or current.connected_at != row.connected_at
        ):
            row.state = "cancelled"
            return
        if (
            played_ms(await stored_sessions(db, project, sid), settings.server_id, now_ms())
            > WHITELIST_PLAYTIME_MS
        ):
            await whitelist_for_playtime(db, project, sid)
            row.state = "cancelled"
            return
        allowed = {
            code: match
            for code, match in row.matches.items()
            if getattr(settings.patterns, code).action == "ban"
            and match["settings"]["action"] == "ban"
        }
        if "P01" not in allowed:
            allowed.pop("P02", None)
        for code in PRIORITY:
            if code not in allowed or code == "P01":
                continue
            if code in {"P02", "P04"} and not await valid_reference(
                db, client, project, code, allowed[code]["evidence"]
            ):
                continue
            pattern = code
            break
        else:
            row.state = "cancelled"
            return
        row.primary_pattern = pattern
        marker = f"{MARKER}{key}:{pattern}"
        reason = describe(pattern, allowed[pattern])
        target_ip = current.ban_ip
        if target_ip is None:
            row.state = "failed"
            logger.error(
                "Бан не отправлен: у текущей сессии нет корректного IP | решение=%s "
                "SteamID=%s проект=%s паттерн=%s | %s",
                key,
                sid,
                project,
                pattern,
                reason,
            )
            return
        async for ban in player_bans(db, client, project, sid):
            if covers_requested_ban(ban, target_ip):
                row.state = "superseded"
                row.external_id = ban["id"]
                logger.info(
                    "Уже действует бессрочный бан по SteamID и IP на всём проекте | "
                    "бан=%s решение=%s SteamID=%s паттерн=%s | %s",
                    ban["id"],
                    key,
                    sid,
                    pattern,
                    reason,
                )
                return
        row.ban_ip = target_ip
        row.state = "sending"
        body = {
            "steam_id": sid,
            "ban_ip_active": True,
            "ban_ip": target_ip,
            "reason": "чит",
            "proofs": [],
            "server_ids": [],
            "comment": f"{marker} | {reason}",
            "destroy_buildings": False,
        }
    try:
        async with sessions.begin() as db:
            if await whitelisted(db, project, sid, lock=True):
                claimed = await db.get(Decision, key, with_for_update=True)
                claimed.state = "cancelled"
                return
            logger.info(
                "Отправка бессрочного бана на все серверы проекта по SteamID и IP | "
                "решение=%s SteamID=%s проект=%s паттерн=%s | %s",
                key,
                sid,
                project,
                pattern,
                reason,
            )
            await client.put_player_ban(sid, {"bans": [body], "override_broadcast": False})
    except RustAppTransportError:
        logger.warning(
            "Результат отправки неизвестен, ожидаем сверки | решение=%s SteamID=%s паттерн=%s | %s",
            key,
            sid,
            pattern,
            reason,
        )
        raise
    except RustAppHTTPError as exc:
        logger.warning(
            "RustApp вернул HTTP %s при отправке бана | решение=%s SteamID=%s паттерн=%s | %s",
            exc.status_code,
            key,
            sid,
            pattern,
            reason,
        )
        if exc.status_code in (400, 401, 403, 404, 422, 429):
            async with sessions.begin() as db:
                claimed = await db.get(Decision, key, with_for_update=True)
                if claimed.state == "sending":
                    claimed.state = "pending" if exc.status_code == 429 else "failed"
        raise


async def reconciliation_bans(
    db: AsyncSession, client: RustAppClient, project: int, steam_id: str
) -> AsyncIterator[dict]:
    if await whitelisted(db, project, steam_id, lock=True):
        cached_bans = await db.scalars(
            select(ReferenceBan.payload).where(
                ReferenceBan.project_id == project,
                ReferenceBan.steam_id == steam_id,
            )
        )
        for ban in cached_bans:
            yield ban
    else:
        async for ban in player_bans(db, client, project, steam_id):
            yield ban


async def reconcile(db: AsyncSession, client: RustAppClient, project: int, server: int) -> None:
    sending = await db.scalars(
        select(Decision)
        .where(
            Decision.project_id == project,
            Decision.server_id == server,
            Decision.state == "sending",
        )
        .order_by(Decision.id)
    )
    for row in sending:
        marker = f"{MARKER}{row.id}:{row.primary_pattern}"
        async for ban in reconciliation_bans(db, client, project, row.steam_id):
            if str(ban.get("comment", "")).partition(" | ")[0] == marker:
                row.external_id = ban["id"]
                reason = describe(row.primary_pattern, row.matches[row.primary_pattern])
                if row.ban_ip is not None and not covers_requested_ban(ban, row.ban_ip):
                    row.state = "failed"
                    logger.error(
                        "Параметры бана в RustApp расходятся с запросом | бан=%s "
                        "решение=%s SteamID=%s паттерн=%s | %s",
                        ban["id"],
                        row.id,
                        row.steam_id,
                        row.primary_pattern,
                        reason,
                    )
                    break
                row.state = "confirmed"
                logger.info(
                    "Блокировка подтверждена RustApp | бан=%s решение=%s SteamID=%s "
                    "проект=%s паттерн=%s | %s",
                    ban["id"],
                    row.id,
                    row.steam_id,
                    project,
                    row.primary_pattern,
                    reason,
                )
                break
