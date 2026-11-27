import asyncio
import logging

import httpx2
from pydantic import ValidationError
from sqlalchemy import select

from app.ban_guard.client import BanGuard
from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.clients.rustapp.exceptions import (
    PlayerNotOnline,
    RustAppError,
    RustAppHTTPError,
    RustAppRequestNotSent,
)
from app.clients.rustapp.retry_after import parse_retry_after
from app.core.config import DatabaseSettings, ModerationSettings, get_settings
from app.core.logging import configure_console_logging
from app.crud.moderation import now_ms
from app.crud.presence import player_is_online, presence_is_current
from app.db.session import open_database_sessions
from app.models import CollectionJob, Decision, PlayerPresence
from app.services.moderation.bans import deliver, reconcile, sync_bans
from app.services.moderation.collect import collect
from app.services.moderation.eligibility import hours_proof_is_current

logger = logging.getLogger(__name__)
BAN_SYNC_SECONDS = 5 * 60
BAN_RECONCILE_SECONDS = 60


async def run() -> None:
    config = get_settings()
    settings = ModerationSettings()
    project = config.rustapp_project_id
    logger.info(
        "Модерация запущена | проект=%d сервер=%d интервал=%d с пауза RustApp=%.1f с автобаны=%s",
        project,
        settings.server_id,
        settings.poll_seconds,
        settings.request_interval,
        "включены" if settings.enforce else "выключены",
    )
    async with (
        open_database_sessions(DatabaseSettings()) as sessions,
        create_rustapp_http_client(config, request_log=sessions) as http,
        httpx2.AsyncClient(
            base_url=settings.ban_guard_url, timeout=3, trust_env=False
        ) as guard_http,
    ):
        client = RustAppClient(http)
        guard = BanGuard(guard_http, project)
        loop = asyncio.get_running_loop()
        next_request = 0.0

        async def pace(request):
            nonlocal next_request
            await asyncio.sleep(max(0, next_request - loop.time()))
            next_request = loop.time() + settings.request_interval

        async def skip_inactive_request(request: httpx2.Request) -> None:
            parts = request.url.path.strip("/").split("/")
            steam_id = parts[1] if len(parts) == 3 and parts[0] == "players" else None
            if request.url.path == "/ban/browse":
                steam_id = request.url.params.get("steam_id")
            if steam_id is None:
                return
            async with sessions() as db:
                presence = await db.get(PlayerPresence, (project, steam_id))
                if (
                    presence is None
                    or presence.status != "online"
                    or (
                        request.method == "PUT"
                        and (
                            presence.is_pirated is not True
                            or presence.server_id != settings.server_id
                        )
                    )
                    or not await presence_is_current(db, project, now_ms())
                ):
                    logger.debug("RustApp: персональный запрос пропущен | SteamID=%s", steam_id)
                    raise PlayerNotOnline
                if request.method == "PUT":
                    # The rate limiter may have waited since deliver() checked this session.
                    decision = await db.scalar(
                        select(Decision).where(
                            Decision.project_id == project,
                            Decision.steam_id == steam_id,
                            Decision.server_id == presence.server_id,
                            Decision.connected_at == presence.session_key,
                            Decision.state == "sending",
                        )
                    )
                    if decision is None or not hours_proof_is_current(
                        decision.eligibility, now_ms()
                    ):
                        raise RustAppRequestNotSent(
                            "Перед отправкой требуется новая проверка часов и сессии"
                        )

        http.event_hooks["request"][:0] = [pace, skip_inactive_request]
        sync_due = 0.0
        reconcile_due = 0.0
        expedited: set[int] = set()
        while True:
            delay = 0
            job_key = None
            finished = False
            try:
                sync_project = loop.time() >= sync_due
                if loop.time() >= reconcile_due:
                    reconcile_due = loop.time() + BAN_RECONCILE_SECONDS
                    if not sync_project:
                        async with sessions.begin() as db:
                            missing = await reconcile(db, client, project, settings.server_id)
                        sync_project = bool(missing - expedited)
                        expedited = missing
                if sync_project:
                    sync_due = loop.time() + BAN_RECONCILE_SECONDS
                    logger.debug("Модерация: общая сверка истории банов")
                    async with sessions.begin() as db:
                        await sync_bans(
                            db, client, project, initial_pages=settings.initial_ban_pages
                        )
                    sync_due = loop.time() + BAN_SYNC_SECONDS
                    async with sessions.begin() as db:
                        expedited = await reconcile(
                            db, client, project, settings.server_id, cached_only=True
                        )
                async with sessions.begin() as db:
                    job = None
                    if await presence_is_current(db, project, now_ms()):
                        job = await db.scalar(
                            select(CollectionJob)
                            .where(
                                CollectionJob.project_id == project,
                                CollectionJob.due_at <= now_ms(),
                                player_is_online(CollectionJob.project_id, CollectionJob.steam_id),
                            )
                            .order_by(
                                CollectionJob.report_pending.desc(),
                                (
                                    CollectionJob.collected_at.is_(None)
                                    | (CollectionJob.requested_at > CollectionJob.collected_at)
                                ).desc(),
                                CollectionJob.due_at,
                            )
                            .limit(1)
                            .with_for_update(skip_locked=True)
                        )
                    if job:
                        job_key = (job.project_id, job.steam_id)
                        logger.debug(
                            "PostgreSQL: взята задача игрока SteamID=%s | ожидание=%.1f с",
                            job.steam_id,
                            (now_ms() - job.due_at) / 1000,
                        )
                        try:
                            await collect(db, client, job, settings)
                        except PlayerNotOnline:
                            logger.debug("Модерация: сбор приостановлен | SteamID=%s", job.steam_id)
                        else:
                            next_check = job.due_at
                            finished = True
                    else:
                        delay = 2
                if finished and job_key is not None:
                    logger.info(
                        "Модерация: проверка завершена | SteamID=%s следующая проверка=%s",
                        job_key[1],
                        f"через {max(0, (next_check - now_ms()) / 1000):.0f} с"
                        if next_check is not None
                        else "снята с расписания",
                    )
                await deliver(sessions, client, project, settings, guard)
            except PlayerNotOnline:
                logger.debug("Модерация: отправка отложена до подтверждения онлайна")
            except RustAppHTTPError as exc:
                if exc.status_code in (401, 403):
                    raise
                delay = max(
                    5, parse_retry_after(exc.retry_after) or (60 if exc.status_code == 429 else 5)
                )
                logger.warning("Панель вернула HTTP %d, пауза %.1f с", exc.status_code, delay)
            except (RustAppError, ValidationError, ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "Модерация: ошибка обработки | SteamID=%s тип=%s пауза=5 с",
                    job_key[1] if job_key else "—",
                    type(exc).__name__,
                )
                delay = 5
            if job_key and not finished and delay >= 5:
                async with sessions.begin() as db:
                    job = await db.get(CollectionJob, job_key, with_for_update=True)
                    if job is not None and job.due_at is not None:
                        job.due_at = now_ms() + int(delay * 1000)
            await asyncio.sleep(delay)


if __name__ == "__main__":
    configure_console_logging()
    asyncio.run(run())
