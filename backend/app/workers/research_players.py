import asyncio
import logging

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.clients.rustapp.exceptions import (
    ResearchDeferred,
    RustAppError,
    RustAppHTTPError,
    RustAppResponseError,
)
from app.clients.rustapp.moderation import read
from app.clients.rustapp.retry_after import parse_retry_after
from app.core.config import DatabaseSettings, ModerationSettings, get_settings
from app.core.logging import configure_console_logging
from app.crud.moderation import now_ms
from app.crud.rustapp_requests import RESEARCH_INTERVAL_MS, account_key
from app.db.session import open_database_sessions
from app.models import Player, PlayerPlaytime, PlaytimeDay, RustAppGate
from app.services.moderation.playtime import WHITELIST_PLAYTIME_MS
from app.services.research.cases import sync_cases
from app.services.research.playtime import DAY, observed_hours_lower_bound, refresh_hours
from app.services.research.queue import claim_hours, claim_source, hydrate, reschedule, store_work

logger = logging.getLogger(__name__)


async def maintain(sessions, project: int, server: int) -> None:
    now = now_ms()
    async with sessions.begin() as db:
        await refresh_hours(db, project, now)
    async with sessions.begin() as db:
        protected = select(PlayerPlaytime.steam_id).where(
            PlayerPlaytime.project_id == project,
            PlayerPlaytime.project_ms >= WHITELIST_PLAYTIME_MS,
            # By excluding the oldest calendar day at the live boundary, ordinary
            # checks also work for a partially filled cache. Recheck it here.
            PlayerPlaytime.project_day_end == now // DAY * DAY,
        )
        total = (
            select(PlaytimeDay.steam_id)
            .where(
                PlaytimeDay.project_id == project,
                PlaytimeDay.day >= now - 365 * DAY,
                PlaytimeDay.day + DAY <= now,
                PlaytimeDay.steam_id.in_(protected),
            )
            .group_by(PlaytimeDay.steam_id)
            .having(func.sum(PlaytimeDay.played_ms) >= WHITELIST_PLAYTIME_MS)
        )
        observed = select(PlayerPlaytime.steam_id).where(
            PlayerPlaytime.project_id == project,
            observed_hours_lower_bound(now) >= WHITELIST_PLAYTIME_MS,
            PlayerPlaytime.calculated_at >= now - 180_000,
        )
        await db.execute(
            update(Player)
            .where(
                Player.project_id == project,
                ~Player.is_whitelisted,
                (Player.steam_id.in_(total) | Player.steam_id.in_(observed)),
            )
            .values(is_whitelisted=True, whitelist_reason="playtime")
        )
        await sync_cases(db, project, server, now)
    async with sessions.begin() as db:
        await hydrate(db, project, server)


async def request_step(sessions, client, project: int, server: int, account: str) -> int:
    now = now_ms()
    # One account-wide advisory lock spans only this read; no queue row lock spans HTTP.
    async with sessions.begin() as leader:
        if not await leader.scalar(
            select(func.pg_try_advisory_xact_lock(8411, func.hashtext(account)))
        ):
            return now + 15_000
        async with sessions.begin() as db:
            planners = (
                (claim_hours, claim_source)
                if now // RESEARCH_INTERVAL_MS % 3 == 0
                else (claim_source, claim_hours)
            )
            job = await planners[0](db, project, server, now)
            if job is None:
                job = await planners[1](db, project, server, now)
        if job is None:
            return now + 15_000
        sent, terminal = True, False
        try:
            raw = await read(client, f"/players/{job.steam_id}/{job.source}", **job.params)
            async with sessions.begin() as db:
                await store_work(db, project, job, raw, now_ms())
            logger.info(
                "Исследование: сохранён раздел %s | SteamID=%s срез=%s",
                job.source,
                job.steam_id,
                job.case_id,
            )
            return now + RESEARCH_INTERVAL_MS
        except ResearchDeferred as exc:
            due, global_due, reason, sent = exc.until, exc.until, str(exc), False
        except RustAppHTTPError as exc:
            delay = parse_retry_after(exc.retry_after)
            due = now_ms() + max(
                RESEARCH_INTERVAL_MS,
                int((delay or 0) * 1000),
                min(3600_000, RESEARCH_INTERVAL_MS * 2 ** min(job.attempts, 5)),
            )
            reason = f"HTTP {exc.status_code}"
            terminal = exc.status_code in (400, 404, 422)
            global_due = due if exc.status_code == 429 else now + RESEARCH_INTERVAL_MS
            if exc.status_code in (401, 403):
                async with sessions.begin() as db:
                    await db.execute(
                        update(RustAppGate)
                        .where(RustAppGate.account_key == account)
                        .values(research_denied=True)
                    )
                global_due = now + DAY
        except (RustAppError, ValueError, KeyError, TypeError, AttributeError) as exc:
            reason = type(exc).__name__
            terminal = (
                isinstance(
                    exc, (RustAppResponseError, ValueError, KeyError, TypeError, AttributeError)
                )
                and job.attempts >= 2
            )
            due = now_ms() + min(3600_000, RESEARCH_INTERVAL_MS * 2 ** min(job.attempts, 5))
            global_due = now + RESEARCH_INTERVAL_MS
        async with sessions.begin() as db:
            await reschedule(
                db, project, job, now, due=due, reason=reason, sent=sent, terminal=terminal
            )
        logger.log(
            logging.WARNING if sent else logging.DEBUG,
            "Исследование: %s | SteamID=%s раздел=%s",
            reason,
            job.steam_id,
            job.source,
        )
        return global_due


async def run() -> None:
    cfg = get_settings()
    account = account_key(cfg.rustapp_access_token.get_secret_value())
    project, server = cfg.rustapp_project_id, ModerationSettings().server_id
    logger.info("Исследовательский сбор запущен | проект=%s сервер=%s", project, server)
    async with (
        open_database_sessions(DatabaseSettings()) as sessions,
        create_rustapp_http_client(cfg, request_log=sessions, research=True) as http,
    ):
        client = RustAppClient(http)
        next_request = 0
        while True:
            try:
                await maintain(sessions, project, server)
                if now_ms() >= next_request:
                    next_request = await request_step(sessions, client, project, server, account)
            except (SQLAlchemyError, OSError) as exc:
                logger.warning(
                    "Исследование: PostgreSQL временно недоступна | %s", type(exc).__name__
                )
            await asyncio.sleep(30)


if __name__ == "__main__":
    configure_console_logging()
    asyncio.run(run())
