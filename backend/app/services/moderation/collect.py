from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rustapp.client import RustAppClient
from app.clients.rustapp.exceptions import RustAppHTTPError, RustAppResponseError
from app.clients.rustapp.moderation import Profile, parse_kill, read, results
from app.core.config import ModerationSettings
from app.crud.moderation import archive, latest, now_ms, reference_bans, save_decision, save_kill
from app.crud.players import upsert_player_client_kinds
from app.models import CollectionJob, CombatKill, Player
from app.services.moderation.evaluate import evaluate
from app.services.moderation.facts import MINUTE, Facts, Kill, Session


async def fetch_profile(client: RustAppClient, project: int, sid: str) -> tuple[Profile, dict]:
    data = await read(client, f"/players/{sid}/details")
    profile = Profile.model_validate(data["player"])
    if profile.project_id != project or profile.steam_id != sid:
        raise RustAppResponseError("Получен профиль другого игрока или проекта")
    return profile, data


async def fetch_sessions(
    client: RustAppClient, project: int, sid: str
) -> tuple[list[Session], dict]:
    data = await read(client, f"/players/{sid}/session-history", page=0, limit=20)
    records = [Session.model_validate(s) for s in results(data)]
    if any(s.steam_id != sid or s.project_id != project for s in records):
        raise RustAppResponseError("Получены чужие сессии")
    return sorted(records, key=lambda s: s.connected_at), data


async def collect(
    db: AsyncSession, client: RustAppClient, job: CollectionJob, settings: ModerationSettings
) -> None:
    project, sid = job.project_id, job.steam_id
    fresh = job.collected_at is None or job.requested_at > job.collected_at
    records, data = await fetch_sessions(client, project, sid)
    last_history = await latest(db, project, sid, "session-history")
    if records and last_history is not None:
        known_connections = {s["connected_at"] for s in results(last_history.payload)}
        fresh = fresh or records[-1].connected_at not in known_connections
    await archive(db, project, sid, "session-history", data)
    last_profile = await latest(db, project, sid, "details")
    if fresh or last_profile is None:
        profile, data = await fetch_profile(client, project, sid)
        await archive(db, project, sid, "details", data)
    else:
        profile = Profile.model_validate(last_profile.payload["player"])
    now = now_ms()
    base = f"/players/{sid}/"
    if fresh:
        for source, params in (
            ("stats", {"time_from": now - 30 * 1440 * MINUTE, "time_to": now}),
            ("checks", {"page": 0, "limit": 1}),
            ("reports", {"page": 0, "limit": 20, "variant": "received"}),
        ):
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
            await archive(db, project, sid, source, payload)
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
            await save_kill(db, project, row["server_id"], sid, kill)

    previous = job.collected_at
    eligible = [s for s in records if s.server_id == settings.server_id and s.connected_at <= now]
    job.collected_at = now
    current = eligible[-1] if eligible else None
    active = current is not None and current.disconnected_at is None
    # A confirmed entry can precede propagation to the HTTP session history.
    awaiting_session = now - job.requested_at < 5 * MINUTE and (
        not active or current.joined_at is None
    )
    job.due_at = now + settings.poll_seconds * 1000 if active or awaiting_session else None
    if not current:
        return
    kind = {True: "licensed", False: "pirated", None: "unknown"}[current.is_license]

    await upsert_player_client_kinds(
        db,
        project_id=project,
        client_kinds={sid: kind},
        observed_at=datetime.fromtimestamp(current.connected_at / 1000, UTC),
    )
    player = await db.get(Player, (project, sid))
    if current.is_license is not False or player.is_whitelisted or current.joined_at is None:
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
    kills = tuple(Kill.model_validate(k.facts) for k in stored)
    first = next((s for s in eligible if s.joined_at is not None), None)
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
    for k in kills:
        if k.created_at < start:
            continue
        for code, result in evaluate(replace(facts, at=k.created_at), rules, entry=False).items():
            matches.setdefault(code, result)
    await save_decision(db, current, matches, enforce=settings.enforce and active)
