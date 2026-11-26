import asyncio
import os
from time import time_ns
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.clients.rustapp.exceptions import RustAppTransportError
from app.core.config import ModerationSettings, Settings
from app.crud.moderation import enqueue, now_ms
from app.crud.presence import is_afk, player_is_afk, player_is_online
from app.models import CollectionJob, ObservedPlay, PlayerPresence
from app.services.moderation.collect import collect
from app.services.rustapp.player_sessions import PlayerSessionTracker
from app.workers import listen_player_status
from app.workers.listen_player_status import PlayerStatusEvents


def test_online_after_afk_is_not_a_new_connection():
    tracker = PlayerSessionTracker()
    tracker.seed_missing({"player": "afk"})
    assert tracker.apply({"player": "online"}) == ([], [])
    tracker.apply({"player": "offline"})
    assert tracker.apply({"player": "online"}) == (["player"], [])


async def test_initial_snapshot_retries_without_waiting_for_new_player(monkeypatch):
    monkeypatch.setattr(listen_player_status, "SNAPSHOT_MIN_RETRY_SECONDS", 0.001)
    async with asyncio.TaskGroup() as tasks:
        listener = PlayerStatusEvents(
            asyncio.Event(), court_client=None, db_sessions=None, project_id=1, tasks=tasks
        )
        fetch = AsyncMock(side_effect=[RustAppTransportError("test connection failure"), []])
        monkeypatch.setattr(listener, "_fetch_snapshot", fetch)
        monkeypatch.setattr(listener, "_persist_snapshot", AsyncMock())
        monkeypatch.setattr(listener, "_update_presence", AsyncMock())
        monkeypatch.setattr(listener, "clear_presence", AsyncMock())
        await listener.on_subscribed(SimpleNamespace(recovered=False, channel="test"))
        await asyncio.wait_for(listener._snapshot_task, timeout=1)
        assert fetch.await_count == 2
        assert listener._snapshot_loaded is True


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
                job.due_at = now_ms() + ModerationSettings().poll_seconds * 1000

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

            await listener._update_presence(
                {steam_id: "online"},
                observed + 2,
                seed=True,
                server_info={steam_id: (163, True)},
            )
            await listener._update_presence({steam_id: "offline"}, observed + 90_000_000_000)
            async with sessions.begin() as db:
                player = await db.get(PlayerPresence, (project, steam_id))
                assert player.status == "offline" and player.session_key is None
                assert not await db.scalar(select(player_is_online(project, steam_id)))
                task = await db.get(CollectionJob, (project, steam_id))
                assert task.due_at is None
                await collect(db, client, task, ModerationSettings())
                assert task.due_at is None
                recorded = await db.scalar(
                    select(ObservedPlay).where(
                        ObservedPlay.project_id == project,
                        ObservedPlay.steam_id == steam_id,
                    )
                )
                assert recorded.server_id == 163 and recorded.ended_at > recorded.started_at
    finally:
        await engine.dispose()
