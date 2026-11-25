import asyncio
import os
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ban_guard.api import app
from app.ban_guard.client import BanGuard
from app.models import Decision


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_guard_limits_parallel_and_uncertain_bans():
    project = uuid4().int % (2**30) + 1
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    app.state.sessions = sessions
    try:
        address = "192.0.2.1"
        async with sessions.begin() as db:
            decisions = [
                Decision(
                    project_id=project,
                    steam_id=str(76561198000000000 + number),
                    server_id=163,
                    connected_at=1,
                    action="ban",
                    state="pending",
                    primary_pattern="P05",
                    matches={},
                    ban_ip=address,
                )
                for number in range(6)
            ]
            db.add_all(decisions)
            await db.flush()
            ids = [decision.id for decision in decisions]
        async with httpx2.AsyncClient(
            base_url="http://ban-guard", transport=httpx2.ASGITransport(app=app)
        ) as http:
            guard = BanGuard(http, project)
            permits = await asyncio.gather(*(guard.claim(key, "P05", address) for key in ids))
            claimed = [key for key, allowed in zip(ids, permits, strict=True) if allowed]
            waiting = [key for key, allowed in zip(ids, permits, strict=True) if not allowed]
            assert len(claimed) == 3
            assert not await guard.available()
            async with sessions.begin() as db:
                failed = await db.get(Decision, claimed[0])
                failed.state = "failed"
            assert await guard.available()
            assert not await guard.claim(claimed[1], "P05", address)
            assert await guard.claim(waiting[0], "P05", address)
            async with sessions.begin() as db:
                confirmed = await db.get(Decision, waiting[0])
                confirmed.state = "confirmed"
                confirmed.external_id = 100
                mismatched = await db.get(Decision, claimed[1])
                mismatched.state = "failed"
                mismatched.external_id = 101
            after_restart = BanGuard(http, project)
            assert not await after_restart.available()
            assert not await after_restart.claim(waiting[1], "P05", address)
    finally:
        await engine.dispose()


async def test_unavailable_guard_refuses_requests():
    def unavailable(request):
        raise httpx2.ConnectError("unavailable", request=request)

    async with httpx2.AsyncClient(
        base_url="http://ban-guard", transport=httpx2.MockTransport(unavailable)
    ) as http:
        guard = BanGuard(http, 9867)
        assert not await guard.available()
        assert not await guard.claim(1, "P05", "192.0.2.1")
