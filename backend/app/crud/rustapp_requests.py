from hashlib import sha256
from math import ceil
from time import time_ns

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.rustapp.exceptions import ResearchDeferred, RustAppHTTPError
from app.crud.presence import player_is_online, presence_is_current
from app.models import CollectionJob, RustAppGate, RustAppRequest

DAY_MS = 86_400_000
RESEARCH_INTERVAL_MS = 180_000
RESEARCH_DAILY_LIMIT = 120


def account_key(token: str) -> str:
    return sha256(token.encode()).hexdigest()


async def pause_account(
    sessions: async_sessionmaker[AsyncSession], account: str, seconds: float
) -> None:
    until = time_ns() // 1_000_000 + ceil(min(seconds, 2**52) * 1000)
    statement = insert(RustAppGate).values(account_key=account, blocked_until=until)
    async with sessions.begin() as db:
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[RustAppGate.account_key],
                set_={"blocked_until": func.greatest(RustAppGate.blocked_until, until)},
            )
        )


async def record_request(
    sessions: async_sessionmaker[AsyncSession],
    account: str | None = None,
    *,
    research_project: int | None = None,
) -> None:
    async with sessions.begin() as db:
        if account is not None:
            if research_project is not None:
                await db.execute(
                    insert(RustAppGate).values(account_key=account).on_conflict_do_nothing()
                )
            gate = await db.get(RustAppGate, account, with_for_update=research_project is not None)
            now = time_ns() // 1_000_000
            if gate and gate.blocked_until > now:
                raise RustAppHTTPError(
                    429, retry_after=str(ceil((gate.blocked_until - now) / 1000))
                )
            if research_project is not None:
                if gate.research_denied:
                    raise ResearchDeferred(now + DAY_MS, "RustApp отклонил доступ для этого токена")
                starts = [at for at in gate.research_starts if at > now - DAY_MS]
                if len(starts) >= RESEARCH_DAILY_LIMIT:
                    raise ResearchDeferred(starts[0] + DAY_MS, "Исчерпан суточный бюджет")
                if starts and starts[-1] + RESEARCH_INTERVAL_MS > now:
                    raise ResearchDeferred(starts[-1] + RESEARCH_INTERVAL_MS, "Интервал запросов")
                late = (
                    select(CollectionJob.steam_id)
                    .where(
                        CollectionJob.project_id == research_project,
                        CollectionJob.due_at < now - 30_000,
                        player_is_online(CollectionJob.project_id, CollectionJob.steam_id),
                    )
                    .limit(1)
                )
                if await presence_is_current(db, research_project, now) and await db.scalar(late):
                    raise ResearchDeferred(now + 30_000, "Приоритет очереди модерации")
                gate.research_starts = [*starts, now]
        db.add(RustAppRequest())
