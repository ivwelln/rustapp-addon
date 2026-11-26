"""Reuse the private pilot export and existing DB observations without network access."""

import argparse
import asyncio
import json
import logging
import re
import sqlite3
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rustapp.exceptions import RustAppResponseError
from app.clients.rustapp.moderation import is_player
from app.core.config import DatabaseSettings, ModerationSettings
from app.core.logging import configure_console_logging
from app.crud.moderation import now_ms
from app.db.session import open_database_sessions
from app.models import Observation
from app.services.research.cases import add_case, ban_label, sync_cases
from app.services.research.playtime import DAY, cache_days
from app.services.research.queue import hydrate

logger = logging.getLogger(__name__)
ARCHIVE_KEY = re.compile(r"[a-f0-9]{64}")
SOURCES = {"details", "stats", "checks", "reports", "kill-feed", "ip-history"}


async def save_archived(
    db: AsyncSession, project: int, sid: str, source: str, raw: dict, params: dict, at: int
) -> bool:
    if not is_player(sid):
        return False
    if source in ("details", "browse-full"):
        profile = raw.get("player") if source == "details" else raw
        if not profile or (profile.get("steam_id"), profile.get("project_id")) != (sid, project):
            return False
        raw = {"player": profile} if source == "details" else profile
    if source == "checks":
        count = raw.get("total")
        if type(count) is not int or count < 0:
            return False
        raw = {"total": count}
    name = "research/" + source
    existing = await db.scalar(
        select(Observation.id)
        .where(
            Observation.project_id == project,
            Observation.steam_id == sid,
            Observation.source == name,
            Observation.observed_at == at,
            Observation.request_params == params,
        )
        .limit(1)
    )
    if existing:
        return False
    if source == "stats":
        try:
            await cache_days(db, project, sid, raw, params, at)
        except RustAppResponseError:
            return False
    db.add(
        Observation(
            project_id=project,
            steam_id=sid,
            source=name,
            request_params=params,
            observed_at=at,
            payload=raw,
        )
    )
    return True


async def import_pilot(db: AsyncSession, folder: Path, project: int, server: int) -> int:
    cohort = json.loads((folder / "cohorts.json").read_text())
    if (cohort["project_id"], cohort["server_id"]) != (project, server):
        raise ValueError("Архив относится к другому проекту или серверу")
    prior_sessions = {}
    imported = 0
    index = sqlite3.connect((folder / "index.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        for key, path, encoded, at in index.execute(
            "SELECT key,path,params,fetched_at_ms FROM responses ORDER BY fetched_at_ms"
        ):
            params = json.loads(encoded)
            if params.get("page", 0) != 0 or not ARCHIVE_KEY.fullmatch(key):
                continue
            parts = path.strip("/").split("/")
            if path == "/players/browse-full":
                raw = json.loads((folder / "raw" / f"{key}.json").read_bytes())
                for profile in raw.get("results", []):
                    imported += await save_archived(
                        db, project, profile.get("steam_id"), "browse-full", profile, {}, at
                    )
                continue
            if len(parts) != 3 or parts[0] != "players" or not is_player(parts[1]):
                continue
            _, sid, source = parts
            if source not in SOURCES and source != "session-history":
                continue
            raw = json.loads((folder / "raw" / f"{key}.json").read_bytes())
            if source == "session-history":
                # Use existing evidence of client type at the ban; never copy session histories.
                prior_sessions.setdefault(sid, []).extend(
                    row
                    for row in raw.get("results", [])
                    if row.get("project_id") == project and row.get("steam_id") == sid
                )
            else:
                imported += await save_archived(db, project, sid, source, raw, params, at)
    finally:
        index.close()
    for ban in cohort["bans"]:
        sid, at = ban["steam_id"], ban["created_at"]
        if ban["project_id"] != project or ban_label(ban, None) == "excluded":
            continue
        prior = max(
            (
                row
                for row in prior_sessions.get(sid, ())
                if 0 <= at - row["connected_at"] <= 30 * DAY
            ),
            key=lambda row: row["connected_at"],
            default=None,
        )
        if (
            not prior
            or prior.get("is_license") is not False
            or prior.get("server_id") != server
            or (ban.get("server_ids") and server not in ban["server_ids"])
        ):
            continue
        # A recorded session before a ban does not establish the first-ever entry.
        await add_case(
            db,
            project,
            server,
            sid,
            cohort="ban",
            episode=ban["id"],
            cutoff=at,
            entry=None,
            label=ban_label(ban, None),
        )
    return imported


async def import_data(sessions, project: int, server: int, folder: Path | None = None) -> int:
    # Page through local observations without loading all JSON responses into memory.
    cursor = 0
    while True:
        async with sessions.begin() as db:
            rows = list(
                await db.scalars(
                    select(Observation)
                    .where(
                        Observation.project_id == project,
                        Observation.id > cursor,
                        or_(
                            Observation.source == "stats",
                            Observation.source == "research-stats-window",
                        ),
                    )
                    .order_by(Observation.id)
                    .limit(64)
                )
            )
            for record in rows:
                raw, params = record.payload, record.request_params
                if record.source == "research-stats-window":
                    raw, params = raw.get("response", {}), raw.get("params")
                try:
                    await cache_days(db, project, record.steam_id, raw, params, record.observed_at)
                except RustAppResponseError:
                    continue
            if not rows:
                break
            cursor = rows[-1].id
    async with sessions.begin() as db:
        await db.execute(select(func.pg_advisory_xact_lock(8412, project)))
        imported = await import_pilot(db, folder, project, server) if folder else 0
        await sync_cases(db, project, server, now_ms())
    while True:
        async with sessions.begin() as db:
            if not await hydrate(db, project, server):
                break
    return imported


async def run(folder: Path | None, project: int) -> None:
    async with open_database_sessions(DatabaseSettings()) as sessions:
        count = await import_data(sessions, project, ModerationSettings().server_id, folder)
        logger.info("Локальный импорт завершён | новых ответов=%d", count)


if __name__ == "__main__":
    configure_console_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, nargs="?")
    parser.add_argument("--project", type=int, default=9867)
    args = parser.parse_args()
    asyncio.run(run(args.folder, args.project))
