import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crud.moderation import claim_ban
from app.models import Decision


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_dispatch_has_no_quota_and_claims_one_decision_per_player():
    project = uuid4().int % (2**30) + 1
    address = "192.0.2.1"
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)

    async def claim(decision_id):
        async with sessions.begin() as db:
            return await claim_ban(db, project, decision_id, "P05", address)

    try:
        async with sessions.begin() as db:
            decisions = [
                Decision(
                    project_id=project,
                    steam_id=str(76561198000000000 + index % 5),
                    server_id=163,
                    connected_at=index,
                    action="ban",
                    state="pending",
                    primary_pattern="P05",
                    matches={},
                    ban_ip=address,
                )
                for index in range(6)
            ]
            db.add_all(decisions)
            await db.flush()
            ids = [decision.id for decision in decisions]
        permits = await asyncio.gather(*(claim(key) for key in ids))
        assert permits[1:5] == [True] * 4
        assert permits[0] != permits[5]
        assert not await claim(ids[1])
    finally:
        await engine.dispose()
