import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from ipaddress import ip_address
from time import time_ns

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.ban_guard.client import BanGuard
from app.clients.rustapp.client import RustAppClient
from app.clients.rustapp.exceptions import (
    PlayerNotOnline,
    RustAppHTTPError,
    RustAppRequestNotSent,
    RustAppResponseError,
    RustAppTransportError,
)
from app.clients.rustapp.moderation import is_player, read, results
from app.core.config import ModerationSettings
from app.crud.moderation import archive, now_ms
from app.crud.players import whitelist_for_playtime, whitelisted
from app.crud.presence import player_is_afk, presence_is_current, save_presence
from app.models import Decision, PlayerData, PlayerPresence, ReferenceBan
from app.services.moderation.collect import (
    cache_profile,
    fetch_profile,
)
from app.services.moderation.evaluate import PRIORITY, describe
from app.services.moderation.facts import MINUTE
from app.services.moderation.playtime import WHITELIST_PLAYTIME_MS, known_sessions, played_ms

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
        signed_date = PlayerData.responses["details"]["payload"]["player"]["steam_data"][
            "signed_at"
        ]
        stored = (
            {
                record.ban_id: (record, signed_at)
                for record, signed_at in (
                    await db.execute(
                        select(ReferenceBan, signed_date)
                        .outerjoin(
                            PlayerData,
                            (PlayerData.project_id == ReferenceBan.project_id)
                            & (PlayerData.steam_id == ReferenceBan.steam_id),
                        )
                        .where(
                            ReferenceBan.project_id == project,
                            ReferenceBan.ban_id.in_([ban["id"] for ban in rows]),
                        )
                    )
                )
            }
            if rows
            else {}
        )
        for ban in rows:
            existing, cached_date = stored.get(ban["id"], (None, None))
            if type(cached_date) is not int or cached_date <= 0:
                cached_date = None
            if existing and existing.payload == ban:
                if existing.eligible and existing.signed_at is None:
                    existing.signed_at = cached_date
                continue
            eligible = await usable_ban(db, project, ban)
            signed_at = server = None
            pirate = False
            if eligible:
                sid = ban["steam_id"]
                presence = await db.get(PlayerPresence, (project, sid))
                sessions = await known_sessions(db, project, sid, presence, until)
                if existing is None:
                    cached_date = await db.scalar(
                        select(signed_date).where(
                            PlayerData.project_id == project, PlayerData.steam_id == sid
                        )
                    )
                signed_at = cached_date if type(cached_date) is int and cached_date > 0 else None
                try:
                    if (
                        signed_at is None
                        and presence is not None
                        and presence.status == "online"
                        and not await whitelisted(db, project, sid)
                    ):
                        profile, details = await fetch_profile(client, project, sid)
                        signed_at = profile.signed_at
                        await cache_profile(db, project, sid, details)
                except PlayerNotOnline:
                    pass
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
) -> int | None:
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
                    return ban_id
    return None


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
    guard: BanGuard,
) -> None:
    if not settings.enforce:
        return
    # The guard commits the claim before sending, so ambiguous PUTs keep their slot.
    async with sessions.begin() as db:
        if not await presence_is_current(db, project, now_ms()):
            return
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
                ~player_is_afk(Decision.project_id, Decision.steam_id),
            )
            .order_by(Decision.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return
        if not await guard.available():
            return
        sid = row.steam_id
        key = row.id
        if await whitelisted(db, project, sid, lock=True):
            row.state = "cancelled"
            return
        current = await db.get(PlayerPresence, (project, sid))
        if current is None or current.status != "online" or current.session_key != row.connected_at:
            row.state = "cancelled"
            return
        now = now_ms()
        if (
            played_ms(await known_sessions(db, project, sid, current, now), settings.server_id, now)
            > WHITELIST_PLAYTIME_MS
        ):
            await whitelist_for_playtime(db, project, sid)
            row.state = "cancelled"
            return
        profile_observed_at = time_ns()
        profile, details = await fetch_profile(client, project, sid)
        if (
            await cache_profile(db, project, sid, details)
            or profile.last_no_license is not True
            or profile.server_id != row.server_id
        ):
            row.state = "cancelled"
            return
        if profile.status == "afk":
            await save_presence(db, project, {sid: "afk"}, profile_observed_at)
            return
        if profile.status != "online":
            row.state = "cancelled"
            return
        allowed = {
            code: match
            for code, match in row.matches.items()
            if getattr(settings.patterns, code).action == "ban"
            and match["settings"]["action"] == "ban"
            and (code != "P07" or "window_start" in match["evidence"])
        }
        if "P01" not in allowed:
            allowed.pop("P02", None)
        for code in PRIORITY:
            if code not in allowed or code == "P01":
                continue
            reference_ban_id = None
            if code in {"P02", "P04"}:
                reference_ban_id = await valid_reference(
                    db, client, project, code, allowed[code]["evidence"]
                )
                if reference_ban_id is None:
                    continue
            pattern = code
            break
        else:
            row.state = "cancelled"
            return
        row.primary_pattern = pattern
        row.reference_ban_id = reference_ban_id
        marker = f"{MARKER}{key}:{pattern}"
        reason = describe(pattern, allowed[pattern], reference_ban_id=reference_ban_id)
        try:
            target_ip = str(ip_address(profile.ip))
        except ValueError:
            target_ip = None
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
    if not await guard.claim(key, pattern, target_ip):
        return
    try:
        async with sessions.begin() as db:
            trusted = await whitelisted(db, project, sid, lock=True)
            claimed = await db.get(Decision, key, with_for_update=True)
            if claimed.state != "sending":
                return
            if trusted:
                claimed.state = "cancelled"
                return
            presence = await db.get(PlayerPresence, (project, sid))
            if presence is None or presence.session_key != claimed.connected_at:
                claimed.state = "cancelled"
                return
            if presence.status != "online":
                claimed.state = "pending"
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
    except (PlayerNotOnline, RustAppRequestNotSent):
        async with sessions.begin() as db:
            claimed = await db.get(Decision, key, with_for_update=True)
            if claimed.state == "sending":
                claimed.state = "pending"
        raise
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


async def reconcile(
    db: AsyncSession, client: RustAppClient, project: int, server: int, *, cached_only: bool = False
) -> set[int]:
    sending = await db.scalars(
        select(Decision)
        .where(
            Decision.project_id == project,
            Decision.server_id == server,
            Decision.state == "sending",
        )
        .order_by(Decision.id)
    )
    needs_project_sync: set[int] = set()
    for row in sending:
        marker = f"{MARKER}{row.id}:{row.primary_pattern}"
        ban = await db.scalar(
            select(ReferenceBan.payload)
            .where(
                ReferenceBan.project_id == project,
                ReferenceBan.steam_id == row.steam_id,
                func.split_part(ReferenceBan.payload["comment"].astext, " | ", 1) == marker,
            )
            .order_by(ReferenceBan.at.desc())
            .limit(1)
        )
        if ban is None:
            presence = await db.get(PlayerPresence, (project, row.steam_id))
            query_player = (
                not cached_only
                and presence is not None
                and presence.status == "online"
                and await presence_is_current(db, project, now_ms())
                and not await whitelisted(db, project, row.steam_id, lock=True)
            )
            if not query_player:
                needs_project_sync.add(row.id)
                continue
            try:
                async with aclosing(player_bans(db, client, project, row.steam_id)) as candidates:
                    async for candidate in candidates:
                        if str(candidate.get("comment", "")).partition(" | ")[0] == marker:
                            ban = candidate
                            break
            except PlayerNotOnline:
                needs_project_sync.add(row.id)
            if ban is None:
                continue
        row.external_id = ban["id"]
        reason = describe(
            row.primary_pattern,
            row.matches[row.primary_pattern],
            reference_ban_id=row.reference_ban_id,
        )
        if row.ban_ip is not None and not covers_requested_ban(ban, row.ban_ip):
            row.state = "failed"
            logger.error(
                "Параметры бана в RustApp расходятся с запросом | бан=%s "
                "решение=%s SteamID=%s паттерн=%s | %s",
                row.external_id,
                row.id,
                row.steam_id,
                row.primary_pattern,
                reason,
            )
            continue
        row.state = "confirmed"
        logger.info(
            "Блокировка подтверждена RustApp | бан=%s решение=%s SteamID=%s "
            "проект=%s паттерн=%s | %s",
            row.external_id,
            row.id,
            row.steam_id,
            project,
            row.primary_pattern,
            reason,
        )
    return needs_project_sync
