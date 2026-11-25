import asyncio
import os
from time import time_ns
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.core.config import ModerationSettings, Settings
from app.crud.moderation import enqueue, now_ms
from app.crud.presence import is_afk, player_is_afk
from app.models import CollectionJob
from app.services.moderation.collect import collect
from app.services.rustapp.player_sessions import PlayerSessionTracker
from app.workers.listen_player_status import PlayerStatusEvents


def test_online_after_afk_is_not_a_new_connection():
    tracker = PlayerSessionTracker()
    tracker.seed_missing({"player": "afk"})
    assert tracker.apply({"player": "online"}) == ([], [])
    tracker.apply({"player": "offline"})
    assert tracker.apply({"player": "online"}) == (["player"], [])


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_afk_pauses_requests_and_online_wakes_existing_job():
    project = uuid4().int % (2**30) + 1
    steam_id = "76561198000000100"
    startup_afk = "76561198000000101"
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    observed = time_ns()
    settings = Settings(_secrets_dir=None, rustapp_access_token="test", rustapp_project_id=project)

    def unexpected_http(request):
        pytest.fail("AFK не должен вызывать RustApp")

    try:
        async with (
            asyncio.TaskGroup() as tasks,
            create_rustapp_http_client(
                settings, transport=httpx2.MockTransport(unexpected_http)
            ) as http,
        ):
            client = RustAppClient(http)
            listener = PlayerStatusEvents(
                asyncio.Event(),
                court_client=client,
                db_sessions=sessions,
                project_id=project,
                tasks=tasks,
            )
            async with sessions.begin() as db:
                await enqueue(db, project, [steam_id], seed=False)
                job = await db.get(CollectionJob, (project, steam_id))
                original_request = job.requested_at
                job.collected_at = job.requested_at + 1
            await listener._update_presence(
                {steam_id: "afk", startup_afk: "afk"}, observed, seed=True
            )
            async with sessions.begin() as db:
                assert await db.get(CollectionJob, (project, startup_afk)) is None
                eligible = await db.scalar(
                    select(CollectionJob).where(
                        CollectionJob.project_id == project,
                        ~player_is_afk(CollectionJob.project_id, CollectionJob.steam_id),
                    )
                )
                assert eligible is None
                job = await db.get(CollectionJob, (project, steam_id))
                await collect(db, client, job, ModerationSettings())
                job.due_at = now_ms() + 90_000

            await listener._update_presence(
                {steam_id: "online", startup_afk: "online"}, observed + 1
            )
            await listener._update_presence({steam_id: "afk"}, observed - 1, seed=True)
            async with sessions() as db:
                assert not await is_afk(db, project, steam_id)
                job = await db.get(CollectionJob, (project, steam_id))
                assert job.due_at <= now_ms()
                assert job.requested_at == original_request
                assert await db.get(CollectionJob, (project, startup_afk)) is not None
    finally:
        await engine.dispose()
