from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rustapp.client import RustAppClient
from app.clients.rustapp.exceptions import RustAppHTTPError, RustAppResponseError
from app.clients.rustapp.moderation import Profile, parse_kill, read, results
from app.core.config import ModerationSettings
from app.crud.moderation import archive, latest, now_ms, reference_bans, save_decision, save_kill
from app.crud.player_data import save_ip, save_reports, save_sessions, stored_sessions
from app.crud.players import upsert_player_client_kinds, whitelist_for_playtime, whitelisted
from app.models import CollectionJob, CombatKill, PlayerData
from app.services.moderation.evaluate import evaluate
from app.services.moderation.facts import MINUTE, Facts, Kill, Session
from app.services.moderation.playtime import WHITELIST_PLAYTIME_MS, YEAR_MS, played_ms

SESSION_PAGE_SIZE = 20
JOIN_DATA_TTL = 10 * MINUTE


async def fetch_profile(client: RustAppClient, project: int, sid: str) -> tuple[Profile, dict]:
    data = await read(client, f"/players/{sid}/details")
    profile = Profile.model_validate(data["player"])
    if profile.project_id != project or profile.steam_id != sid:
        raise RustAppResponseError("Получен профиль другого игрока или проекта")
    return profile, data


async def fetch_sessions(
    client: RustAppClient, project: int, sid: str, *, page: int = 0
) -> tuple[list[Session], dict]:
    data = await read(client, f"/players/{sid}/session-history", page=page, limit=SESSION_PAGE_SIZE)
    records = [Session.model_validate(session) for session in results(data, page=page)]
    if any(session.steam_id != sid or session.project_id != project for session in records):
        raise RustAppResponseError("Получены чужие сессии")
    return sorted(records, key=lambda session: session.connected_at), data


async def cache_profile(db: AsyncSession, project: int, sid: str, data: dict) -> bool:
    await archive(db, project, sid, "details", data)
    raw_player = data["player"]
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


async def cache_sessions(
    db: AsyncSession, project: int, sid: str, data: dict, *, page: int = 0
) -> bool:
    await archive(
        db, project, sid, "session-history" if page == 0 else f"session-history:{page}", data
    )
    rows = results(data, page=page)
    await save_sessions(db, rows)
    now = now_ms()
    latest_license_at = max(
        (
            row["connected_at"]
            for row in rows
            if row.get("is_license") is True and row["connected_at"] <= now
        ),
        default=None,
    )
    if latest_license_at is None:
        return False
    await upsert_player_client_kinds(
        db,
        project_id=project,
        client_kinds={sid: "licensed"},
        observed_at=datetime.fromtimestamp(latest_license_at / 1000, UTC),
    )
    return True


async def collect_playtime(
    db: AsyncSession,
    client: RustAppClient,
    job: CollectionJob,
    server: int,
    records: list[Session],
    *,
    fresh: bool,
    now: int,
) -> bool:
    project, sid = job.project_id, job.steam_id
    newest = records[-1].connected_at if records else now
    by_connection = {
        (session.server_id, session.connected_at): session
        for session in await stored_sessions(db, project, sid)
    }
    page = 0
    while played_ms(by_connection.values(), server, now) <= WHITELIST_PLAYTIME_MS:
        covered_at = job.session_history_covered_at
        if (
            len(records) < SESSION_PAGE_SIZE
            or records[0].connected_at <= now - YEAR_MS
            or (covered_at is not None and records[0].connected_at <= covered_at)
        ):
            job.session_history_covered_at = newest
            return False
        if not fresh:
            return False
        oldest_on_previous_page = records[0].connected_at
        page += 1
        records, data = await fetch_sessions(client, project, sid, page=page)
        if records and records[0].connected_at >= oldest_on_previous_page:
            return False
        if await cache_sessions(db, project, sid, data, page=page):
            return True
        by_connection.update(
            {(session.server_id, session.connected_at): session for session in records}
        )
    return True


async def collect(
    db: AsyncSession, client: RustAppClient, job: CollectionJob, settings: ModerationSettings
) -> None:
    project, sid = job.project_id, job.steam_id
    if await whitelisted(db, project, sid, lock=True):
        job.due_at = None
        return
    fresh = job.collected_at is None or job.requested_at > job.collected_at
    records, session_data = await fetch_sessions(client, project, sid)
    last_history = await latest(db, project, sid, "session-history")
    if records and last_history is not None:
        known_connections = {session["connected_at"] for session in results(last_history.payload)}
        fresh = fresh or records[-1].connected_at not in known_connections
    is_licensed = await cache_sessions(db, project, sid, session_data)
    now = now_ms()
    if is_licensed:
        job.collected_at, job.due_at = now, None
        return
    if await collect_playtime(
        db,
        client,
        job,
        settings.server_id,
        records,
        fresh=fresh,
        now=now,
    ):
        await whitelist_for_playtime(db, project, sid)
        job.collected_at, job.due_at = now, None
        return
    player_data = await db.get(PlayerData, (project, sid))
    cached = player_data.responses if player_data else {}

    def needs_refresh(source: str) -> bool:
        return now - cached.get(source, {}).get("observed_at", 0) >= JOIN_DATA_TTL

    last_profile = cached.get("details", {}).get("payload")
    if last_profile is None or (fresh and needs_refresh("details")):
        profile, data = await fetch_profile(client, project, sid)
        if await cache_profile(db, project, sid, data):
            job.collected_at, job.due_at = now, None
            return
    else:
        profile = Profile.model_validate(last_profile["player"])
    now = now_ms()
    base = f"/players/{sid}/"
    eligible = [
        session
        for session in records
        if session.server_id == settings.server_id and session.connected_at <= now
    ]
    current = eligible[-1] if eligible else None
    active = current is not None and current.disconnected_at is None
    # RustApp may publish the confirmed entry before the HTTP session appears.
    awaiting_session = now - job.requested_at < 5 * MINUTE and (
        current is None
        or (active and current.joined_at is None)
        or (not active and current.disconnected_at < job.requested_at)
    )
    for source, params in (
        ("stats", {"time_from": now - 30 * 1440 * MINUTE, "time_to": now}),
        ("checks", {"page": 0, "limit": 1}),
        ("reports", {"page": 0, "limit": 20, "variant": "received"}),
        ("ip-history", {"page": 0, "limit": 20}),
    ):
        if (not fresh and (source != "reports" or not active)) or not needs_refresh(source):
            continue
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
        await archive(db, project, sid, source, payload)
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
    if (
        not fresh
        and not active
        and last_history is not None
        and session_data == last_history.payload
    ):
        job.collected_at = now
        job.due_at = now + settings.poll_seconds * 1000 if awaiting_session else None
        return
    data = await read(
        client,
        base + "kill-feed",
        page=0,
        limit=25,
        time_from=now - 30 * 1440 * MINUTE,
        time_to=now,
    )
    await archive(db, project, sid, "kill-feed", data)
    for row in results(data):
        if row["project_id"] != project:
            raise RustAppResponseError("Получено событие другого проекта")
        if (kill := parse_kill(row, sid)) is not None:
            await save_kill(db, project, row["server_id"], sid, kill, row)

    previous = job.collected_at
    job.collected_at = now
    job.due_at = (
        now + settings.poll_seconds * 1000
        if (active and current.joined_at is not None) or awaiting_session
        else None
    )
    if not current:
        return
    kind = {True: "licensed", False: "pirated", None: "unknown"}[current.is_license]

    await upsert_player_client_kinds(
        db,
        project_id=project,
        client_kinds={sid: kind},
        observed_at=datetime.fromtimestamp(current.connected_at / 1000, UTC),
    )
    if current.is_license is not False or current.joined_at is None:
        return
    # A delayed final poll may still archive the evidence, but never dispatch for a closed session.
    if not active and (current.disconnected_at or 0) < job.requested_at:
        return
    start = current.joined_at
    end = min(current.disconnected_at or now, now)
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
    lower = min(start, previous or job.requested_at) - span
    lower = max(lower, (previous or job.requested_at) - 2 * span)
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
    first = next((session for session in eligible if session.joined_at is not None), None)
    # A full page can hide earlier sessions unless it reaches the creation of the project profile.
    complete_start = len(records) < 20 or records[0].connected_at <= profile.created_at
    first_join = first == current and complete_start
    new_on_project = 0 <= start - profile.created_at <= 60 * MINUTE
    references = await reference_bans(db, project, profile.signed_at, current, rules)
    facts = Facts(
        sid,
        current,
        profile.created_at,
        first_join and new_on_project,
        profile.signed_at,
        profile.private_without_date,
        start,
        kills,
        references,
    )
    matches = evaluate(facts, rules, entry=True) if first_join else {}
    # Revisit the recent window after reconnects or late publication of a hit log.
    for kill in kills:
        if kill.created_at < start:
            continue
        for code, result in evaluate(
            replace(facts, at=kill.created_at), rules, entry=False
        ).items():
            matches.setdefault(code, result)
    await save_decision(db, current, matches, enforce=settings.enforce and active)
