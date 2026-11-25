import asyncio
import logging

import httpx2
from pydantic import ValidationError
from sqlalchemy import select

from app.ban_guard.client import BanGuard
from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.clients.rustapp.exceptions import RustAppError, RustAppHTTPError
from app.clients.rustapp.retry_after import parse_retry_after
from app.core.config import DatabaseSettings, ModerationSettings, get_settings
from app.core.logging import configure_console_logging
from app.crud.moderation import now_ms
from app.db.session import open_database_sessions
from app.models import CollectionJob
from app.services.moderation.bans import deliver, reconcile, sync_bans
from app.services.moderation.collect import collect

logger = logging.getLogger(__name__)


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

        http.event_hooks["request"].insert(0, pace)
        sync_due = 0.0
        while True:
            delay = 0
            job_key = None
            try:
                if loop.time() >= sync_due:
                    sync_due = loop.time() + 60
                    logger.debug("Модерация: сверка истории банов и отправленных решений")
                    async with sessions.begin() as db:
                        await sync_bans(
                            db, client, project, initial_pages=settings.initial_ban_pages
                        )
                    async with sessions.begin() as db:
                        await reconcile(db, client, project, settings.server_id)
                async with sessions.begin() as db:
                    job = await db.scalar(
                        select(CollectionJob)
                        .where(
                            CollectionJob.project_id == project, CollectionJob.due_at <= now_ms()
                        )
                        .order_by(CollectionJob.due_at)
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
                        await collect(db, client, job, settings)
                        next_check = job.due_at
                    else:
                        delay = 2
                if job_key is not None:
                    logger.info(
                        "Модерация: проверка завершена | SteamID=%s следующая проверка=%s",
                        job_key[1],
                        f"через {max(0, (next_check - now_ms()) / 1000):.0f} с"
                        if next_check is not None
                        else "снята с расписания",
                    )
                await deliver(sessions, client, project, settings, guard)
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
            if job_key and delay >= 5:
                async with sessions.begin() as db:
                    job = await db.get(CollectionJob, job_key, with_for_update=True)
                    job.due_at = now_ms() + int(delay * 1000)
            await asyncio.sleep(delay)


if __name__ == "__main__":
    configure_console_logging()
    asyncio.run(run())
