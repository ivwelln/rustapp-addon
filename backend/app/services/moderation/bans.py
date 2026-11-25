from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.clients.rustapp.client import RustAppClient
from app.clients.rustapp.exceptions import RustAppHTTPError, RustAppResponseError
from app.clients.rustapp.moderation import is_player, read, results
from app.core.config import ModerationSettings
from app.crud.moderation import archive, now_ms
from app.models import Decision, Player, ReferenceBan
from app.services.moderation.collect import fetch_profile, fetch_sessions
from app.services.moderation.evaluate import PRIORITY, describe
from app.services.moderation.facts import MINUTE

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
        for b in rows:
            if b.get("project_id") != project or not is_player(b.get("steam_id")):
                raise RustAppResponseError("Некорректная запись бана проекта")
            existing = await db.get(ReferenceBan, (project, b["id"]))
            if existing and existing.payload == b:
                continue
            eligible = await usable_ban(db, project, b)
            signed_at = server = None
            pirate = False
            if eligible:
                sid = b["steam_id"]
                sessions = []
                try:
                    profile, details = await fetch_profile(client, project, sid)
                    signed_at = profile.signed_at
                    await archive(db, project, sid, "details", details)
                    sessions, history = await fetch_sessions(client, project, sid)
                    await archive(db, project, sid, "session-history", history)
                except RustAppHTTPError as exc:
                    if exc.status_code not in (404, 422):
                        raise
                prior = [
                    s
                    for s in sessions
                    if 0 <= b["created_at"] - s.connected_at <= 30 * 1440 * MINUTE
                    and (not b.get("server_ids") or s.server_id in b["server_ids"])
                ]
                if prior:
                    server = prior[-1].server_id
                    pirate = prior[-1].is_license is False
            values = dict(
                project_id=project,
                ban_id=b["id"],
                steam_id=b["steam_id"],
                at=b["created_at"],
                signed_at=signed_at,
                server_id=server,
                pirated=pirate,
                eligible=eligible,
                payload=b,
            )
            stmt = insert(ReferenceBan).values(**values)
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["project_id", "ban_id"],
                    set_={k: v for k, v in values.items() if k not in {"project_id", "ban_id"}},
                )
            )
        if (
            (last_seen is None and page + 1 >= initial_pages)
            or len(rows) < 50
            or any(b["id"] == last_seen for b in rows)
        ):
            return
        page += 1


async def player_bans(client: RustAppClient, project: int, sid: str) -> AsyncIterator[dict]:
    params = dict(project_id=project, steam_id=sid, sort_by="created", limit=50)
    page = 0
    while True:
        rows = results(await read(client, "/ban/browse", page=page, **params), page=page)
        for b in rows:
            if b.get("project_id") != project or b.get("steam_id") != sid:
                raise RustAppResponseError("Несовпадение владельца бана")
            yield b
        if len(rows) < 50:
            return
        page += 1


async def valid_reference(
    db: AsyncSession, client: RustAppClient, project: int, pattern: str, evidence: dict
) -> bool:
    ids = [b["id"] for b in evidence["bans"]] if pattern == "P02" else evidence["prior_bans"]
    for ban_id in ids:
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
        player = await db.get(Player, (project, sid))
        if not player or player.is_whitelisted:
            row.state = "cancelled"
            return
        history, _ = await fetch_sessions(client, project, sid)
        current = next((s for s in reversed(history) if s.server_id == row.server_id), None)
        if (
            not current
            or current.is_license is not False
            or current.disconnected_at is not None
            or current.connected_at != row.connected_at
        ):
            row.state = "cancelled"
            return
        async for b in player_bans(client, project, sid):
            if b.get("computed_is_active") and (
                not b.get("server_ids") or row.server_id in b["server_ids"]
            ):
                row.state = "superseded"
                row.external_id = b["id"]
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
        row.state = "sending"
        body = {
            "steam_id": sid,
            "ban_ip_active": False,
            "reason": "чит",
            "proofs": [],
            "server_ids": [row.server_id],
            "comment": f"{marker} | {describe(pattern, allowed[pattern])}",
            "destroy_buildings": False,
        }
        if settings.ban_minutes:
            body["expired_at"] = now_ms() + settings.ban_minutes * MINUTE
    try:
        await client.put_player_ban(sid, {"bans": [body], "override_broadcast": False})
    except RustAppHTTPError as exc:
        if exc.status_code in (400, 401, 403, 404, 422, 429):
            async with sessions.begin() as db:
                claimed = await db.get(Decision, key, with_for_update=True)
                if claimed.state == "sending":
                    claimed.state = "pending" if exc.status_code == 429 else "failed"
        raise


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
        async for b in player_bans(client, project, row.steam_id):
            if str(b.get("comment", "")).partition(" | ")[0] == marker:
                row.state = "confirmed"
                row.external_id = b["id"]
                break
