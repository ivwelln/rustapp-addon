import logging
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rustapp.client import RustAppClient
from app.clients.rustapp.exceptions import RustAppHTTPError, RustAppResponseError
from app.clients.rustapp.moderation import Profile, parse_kill, read, results
from app.core.config import ModerationSettings
from app.crud.moderation import archive, now_ms, reference_bans, save_decision, save_kill
from app.crud.player_data import save_ip, save_reports
from app.crud.players import upsert_player_client_kinds, whitelist_for_playtime, whitelisted
from app.crud.presence import PRESENT, observe_server, presence_is_current
from app.models import CollectionJob, CombatKill, PlayerData, PlayerPresence, ReferenceBan
from app.services.moderation.eligibility import cached_project_ms
from app.services.moderation.evaluate import evaluate
from app.services.moderation.facts import MINUTE, Facts, Kill, Session
from app.services.moderation.kill_feed import PAGE_LIMIT, update_coverage
from app.services.moderation.playtime import WHITELIST_PLAYTIME_MS, known_sessions, played_ms
from app.services.research.playtime import DAY, HOUR, cache_days, daily_playtime, missing_range

logger = logging.getLogger(__name__)

JOIN_DATA_TTL = 10 * MINUTE


async def fetch_profile(client: RustAppClient, project: int, sid: str) -> tuple[Profile, dict]:
    data = await read(client, f"/players/{sid}/details")
    profile = Profile.model_validate(data["player"])
    if profile.project_id != project or profile.steam_id != sid:
        raise RustAppResponseError("Получен профиль другого игрока или проекта")
    return profile, data


async def cache_profile(db: AsyncSession, project: int, sid: str, data: dict) -> bool:
    await archive(db, project, sid, "details", data)
    raw_player = data["player"]
    signed_at = (raw_player.get("steam_data") or {}).get("signed_at")
    if type(signed_at) is int and signed_at > 0:
        await db.execute(
            update(ReferenceBan)
            .where(
                ReferenceBan.project_id == project,
                ReferenceBan.steam_id == sid,
                ReferenceBan.eligible,
                ReferenceBan.signed_at.is_(None),
            )
            .values(signed_at=signed_at)
        )
    await save_ip(
        db,
        project,
        sid,
        raw_player.get("ip"),
        raw_player.get("last_online_at") or now_ms(),
        raw_player.get("ip_details") or {},
    )
    if raw_player.get("last_no_license") is not False:
        return False
    await upsert_player_client_kinds(
        db, project_id=project, client_kinds={sid: "licensed"}, observed_at=datetime.now(UTC)
    )
    return True


async def collect(
    db: AsyncSession, client: RustAppClient, job: CollectionJob, settings: ModerationSettings
) -> None:
    project, sid = job.project_id, job.steam_id
    if await whitelisted(db, project, sid, lock=True):
        job.due_at = None
        logger.debug("Модерация: пропуск по белому списку | SteamID=%s", sid)
        return

    now = now_ms()
    if not await presence_is_current(db, project, now):
        return
    presence = await db.get(PlayerPresence, (project, sid))
    if presence is None or presence.active_since is None:
        awaiting_entry = presence is None or presence.status in ("joining", None)
        job.due_at = (
            now + settings.poll_seconds * 1000
            if awaiting_entry and now - job.requested_at < 5 * MINUTE
            else None
        )
        return
    if presence.status != "online":
        return
    active_since = presence.active_since
    previous = job.collected_at
    fresh = previous is None or job.requested_at > previous or active_since > previous
    history = await known_sessions(db, project, sid, presence, now)
    if (
        played_ms(history, server_id=None, now=now) >= WHITELIST_PLAYTIME_MS
        or await cached_project_ms(db, project, sid, now) >= WHITELIST_PLAYTIME_MS
    ):
        await whitelist_for_playtime(db, project, sid)
        logger.info("Белый список: подтверждено игровое время | SteamID=%s", sid)
        job.collected_at, job.due_at = now, None
        return
    player_data = await db.get(PlayerData, (project, sid))
    cached = player_data.responses if player_data else {}

    def needs_refresh(source: str) -> bool:
        return now - cached.get(source, {}).get("observed_at", 0) >= JOIN_DATA_TTL

    last_profile = cached.get("details", {}).get("payload")
    observed_profile_at: int | None = None
    if last_profile is None or (fresh and needs_refresh("details")) or presence.server_id is None:
        observed_profile_at = now_ms()
        profile, data = await fetch_profile(client, project, sid)
        if await cache_profile(db, project, sid, data):
            job.collected_at, job.due_at = now, None
            logger.info("Белый список: добавлен игрок по лицензии из профиля | SteamID=%s", sid)
            return
    else:
        profile = Profile.model_validate(last_profile["player"])
        logger.debug("PostgreSQL: профиль использован из кэша | SteamID=%s", sid)
    now = now_ms()
    base = f"/players/{sid}/"
    for source, params in (
        ("stats", {"time_from": now - 30 * 1440 * MINUTE, "time_to": now}),
        ("checks", {"page": 0, "limit": 1}),
        ("reports", {"page": 0, "limit": 20, "variant": "received"}),
        ("ip-history", {"page": 0, "limit": 20}),
    ):
        if (not fresh and source != "reports") or not needs_refresh(source):
            continue
        if source == "stats":
            previous_stats = cached.get("stats", {})
            if previous_stats.get(
                "observed_at", 0
            ) >= now // DAY * DAY and "playTime" in previous_stats.get("payload", {}):
                continue
            gap = await missing_range(db, project, sid, now)
            if gap and gap["time_to"] - gap["time_from"] > 30 * DAY:
                params = {"time_from": gap["time_from"], "time_to": now}
        try:
            payload = await read(client, base + source, **params)
        except RustAppHTTPError as exc:
            if exc.status_code not in (400, 404, 422):
                raise
            await archive(db, project, sid, source, {"http_status": exc.status_code})
            continue
        if source == "checks":
            count = payload.get("total")
            if type(count) is not int or count < 0:
                raise RustAppResponseError("Отсутствует число проверок")
            payload = {"total": count}
        if source == "ip-history":
            payload = {
                **payload,
                "results": [
                    address
                    for address in results(payload)
                    if address.get("project_id", project) == project
                    and address.get("steam_id", sid) == sid
                ],
            }
        await archive(db, project, sid, source, payload, params=params)
        if source == "stats":
            try:
                observed = now_ms()
                await cache_days(db, project, sid, payload, params, observed)
                current_days = daily_playtime(payload, sid)
                confirmed = sum(
                    duration
                    for day, duration in current_days.items()
                    if observed - 365 * DAY <= day <= observed
                )
                if (
                    max(confirmed, await cached_project_ms(db, project, sid, observed))
                    >= WHITELIST_PLAYTIME_MS
                ):
                    await whitelist_for_playtime(db, project, sid)
                    job.collected_at, job.due_at = observed, None
                    logger.info(
                        "Белый список по часам RustApp | SteamID=%s часы от %.2f",
                        sid,
                        confirmed / HOUR,
                    )
                    return
            except RustAppResponseError:
                logger.debug("Статистика без подтверждённых дневных часов | SteamID=%s", sid)
        if source == "reports":
            await save_reports(db, project, sid, results(payload))
        if source == "ip-history":
            for address in results(payload):
                await save_ip(
                    db,
                    project,
                    sid,
                    address.get("ip"),
                    address.get("last_seen_at") or address.get("created_at") or now,
                    address,
                )
    feed_start = now - 30 * 1440 * MINUTE
    data = await read(
        client,
        base + "kill-feed",
        page=0,
        limit=PAGE_LIMIT,
        time_from=feed_start,
        time_to=now,
    )
    await archive(
        db,
        project,
        sid,
        "kill-feed",
        data,
        params={
            "page": 0,
            "limit": PAGE_LIMIT,
            "time_from": feed_start,
            "time_to": now,
        },
    )
    rows = results(data)
    update_coverage(job, data, rows, start=feed_start, end=now)
    for row in rows:
        if row["project_id"] != project:
            raise RustAppResponseError("Получено событие другого проекта")
        if (kill := parse_kill(row, sid)) is not None:
            await save_kill(db, project, row["server_id"], sid, kill, row)

    job.collected_at = now
    job.due_at = now + settings.poll_seconds * 1000
    presence = await db.get(
        PlayerPresence, (project, sid), populate_existing=True, with_for_update=True
    )
    if (
        presence is None
        or presence.active_since != active_since
        or presence.status != "online"
        or not await presence_is_current(db, project, now_ms())
    ):
        return
    if observed_profile_at is not None:
        if profile.status not in PRESENT:
            return
        if profile.server_id is not None:
            observe_server(
                db, presence, profile.server_id, profile.last_no_license, observed_profile_at
            )
    if (
        presence.active_since != active_since
        or presence.server_id != settings.server_id
        or presence.is_pirated is not True
    ):
        return
    current = Session(
        project_id=project,
        steam_id=sid,
        server_id=settings.server_id,
        connected_at=presence.session_key,
        joined_at=presence.joined_at,
        is_license=False,
    )
    start = active_since
    end = now
    rules = settings.patterns
    span = (
        max(
            rules.P05.minutes,
            rules.P06.minutes,
            rules.P07.minutes,
            rules.P09.minutes,
            rules.P03.minutes,
        )
        * MINUTE
    )
    lower = max(start, (previous or job.requested_at) - span)
    stored = await db.scalars(
        select(CombatKill)
        .where(
            CombatKill.project_id == project,
            CombatKill.steam_id == sid,
            CombatKill.server_id == current.server_id,
            CombatKill.at.between(lower, end),
        )
        .order_by(CombatKill.at, CombatKill.event_id)
    )
    kills = tuple(Kill.model_validate(record.facts) for record in stored)
    first_join = (
        presence.joined_at is not None
        and presence.first_active_at == active_since
        and 0 <= presence.joined_at - profile.created_at <= 60 * MINUTE
        and not any(session.connected_at < active_since for session in history)
    )
    references = await reference_bans(db, project, profile.signed_at, current, rules)
    facts = Facts(
        sid,
        current,
        profile.created_at,
        first_join,
        profile.signed_at,
        profile.private_without_date,
        start,
        kills,
        references,
        connection_observed=presence.connecting_at is not None,
    )
    matches = evaluate(facts, rules, entry=True) if first_join else {}
    covered_from = job.kill_feed_covered_from
    if covered_from is not None:
        covered_from = max(covered_from, lower)
    # Revisit the recent window after reconnects or late publication of a hit log.
    for kill in kills:
        if kill.created_at < start:
            continue
        covered = (
            covered_from
            if job.kill_feed_covered_through is not None
            and kill.created_at <= job.kill_feed_covered_through
            else None
        )
        event_facts = replace(facts, at=kill.created_at, kill_feed_covered_from=covered)
        for code, result in evaluate(event_facts, rules, entry=False).items():
            matches.setdefault(code, result)
    logger.debug(
        "Модерация: паттерны проверены | SteamID=%s убийств=%d совпадения=%s",
        sid,
        len(kills),
        ", ".join(sorted(matches)) or "нет",
    )
    await save_decision(db, current, matches, enforce=settings.enforce)
