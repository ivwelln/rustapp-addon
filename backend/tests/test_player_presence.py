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
from app.crud.moderation import archive, enqueue, now_ms
from app.crud.moderation_control import DEFAULT_POLL_SECONDS
from app.crud.presence import (
    confirm_presence_connection,
    is_afk,
    player_is_afk,
    player_is_online,
    save_presence,
)
from app.models import CollectionJob, ObservedPlay, Player, PlayerPresence, PlayerReport
from app.schemas.rustapp.players import RustAppPlayer
from app.services.moderation.collect import collect
from app.services.moderation.playtime import YEAR_MS
from app.services.research.playtime import DAY
from app.services.rustapp.player_sessions import PlayerSessionTracker
from app.workers import listen_player_status
from app.workers.listen_player_status import PlayerStatusEvents, ReportEvents


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_report_publication_prioritizes_only_present_players_outside_whitelist(monkeypatch):
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    project = uuid4().int % (2**30) + 1
    online, afk, trusted, offline = [str(76561198000000000 + index) for index in range(4)]
    at = now_ms()
    monkeypatch.setattr("app.crud.moderation.now_ms", lambda: at)
    handler = ReportEvents(sessions, project)

    async def publish(report_id: int, steam_id: str, report_project: int = project) -> None:
        payload = {
            "id": report_id,
            "project_id": report_project,
            "created_at": at,
            "target": {"steam_id": steam_id},
        }
        await handler.on_publication(SimpleNamespace(pub=SimpleNamespace(data=payload)))

    try:
        async with sessions.begin() as db:
            db.add(Player(project_id=project, steam_id=trusted, is_whitelisted=True))
            await save_presence(
                db,
                project,
                {online: "online", afk: "afk", trusted: "online", offline: "offline"},
                at * 1_000_000,
            )
            db.add(
                CollectionJob(
                    project_id=project,
                    steam_id=online,
                    requested_at=at - 60_000,
                    collected_at=at - 30_000,
                    due_at=at + 120_000,
                )
            )

        await publish(1, online, project + 1)
        for report_id, steam_id in enumerate((online, afk, trusted, offline), start=1):
            await publish(report_id, steam_id)

        async with sessions.begin() as db:
            job = await db.get(CollectionJob, (project, online))
            assert job.report_pending and job.due_at == at
            assert job.requested_at == at - 60_000
            assert (await db.get(PlayerReport, (project, 1))).payload["target_steam_id"] == online
            assert (await db.get(CollectionJob, (project, afk))).report_pending
            assert not await db.scalar(select(player_is_online(project, afk)))
            for steam_id in (trusted, offline):
                assert await db.get(CollectionJob, (project, steam_id)) is None
            job.report_pending = False
            job.due_at = at + 150_000

        await publish(1, online)
        async with sessions() as db:
            job = await db.get(CollectionJob, (project, online))
            assert not job.report_pending and job.due_at == at + 150_000
    finally:
        await engine.dispose()


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
@pytest.mark.parametrize("intake", ["startup", "websocket"])
async def test_first_check_refreshes_annual_hours_before_combat(intake, monkeypatch):
    project = uuid4().int % (2**30) + 1
    steam_id = "76561198000000102"
    now = now_ms()
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    settings = Settings(_secrets_dir=None, rustapp_access_token="test", rustapp_project_id=project)
    requests = []
    profile = {
        "project_id": project,
        "steam_id": steam_id,
        "server_id": 164,
        "status": "online",
        "last_no_license": True,
        "created_at": now - YEAR_MS,
    }

    def reply(request):
        requests.append(request.url.path)
        assert request.url.path == f"/players/{steam_id}/stats"
        start = int(request.url.params["time_from"])
        end = int(request.url.params["time_to"])
        assert end - start + 1 == YEAR_MS
        buckets = {
            str(day): {"date": day, "hours_played": 0}
            for day in range(start // DAY * DAY, end + 1, DAY)
        }
        buckets[str(end // DAY * DAY - 60 * DAY)]["hours_played"] = 3
        return httpx2.Response(
            200,
            json={"kills": {"steam_id": steam_id}, "playTime": {"total": 3, "stats": buckets}},
        )

    try:
        async with sessions.begin() as db:
            db.add(
                CollectionJob(
                    project_id=project,
                    steam_id=steam_id,
                    requested_at=now - 2_000,
                    collected_at=now - 1_000,
                    due_at=now + 150_000,
                )
            )
            # A current short-window response must not suppress the annual intake check.
            await archive(db, project, steam_id, "stats", {"playTime": {"total": 0}})
            await archive(db, project, steam_id, "details", {"player": profile})
        async with (
            asyncio.TaskGroup() as tasks,
            create_rustapp_http_client(settings, transport=httpx2.MockTransport(reply)) as http,
        ):
            client = RustAppClient(http)
            listener = PlayerStatusEvents(
                asyncio.Event(),
                court_client=client,
                db_sessions=sessions,
                project_id=project,
                tasks=tasks,
            )
            if intake == "startup":
                snapshot = AsyncMock(return_value=[RustAppPlayer.model_validate(profile)])
                monkeypatch.setattr(listener, "_fetch_snapshot", snapshot)
                await listener.on_subscribed(SimpleNamespace(recovered=False, channel="test"))
                await asyncio.wait_for(listener._snapshot_task, timeout=2)
            else:
                publication = SimpleNamespace(
                    pub=SimpleNamespace(data={"players": {steam_id: "online"}})
                )
                await listener.on_publication(publication)
                await listener.on_publication(publication)
            async with sessions.begin() as db:
                await confirm_presence_connection(db, project, now_ms())
                job = await db.get(CollectionJob, (project, steam_id))
                assert job.requested_at > job.collected_at and job.due_at <= now_ms()
                await collect(db, client, job, ModerationSettings())
                player = await db.get(Player, (project, steam_id))
                assert player.is_whitelisted and player.whitelist_reason == "playtime"
                assert job.due_at is None
                await collect(db, client, job, ModerationSettings())
        assert requests == [f"/players/{steam_id}/stats"]
    finally:
        await engine.dispose()


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
                job.due_at = now_ms() + DEFAULT_POLL_SECONDS * 1000

            await listener._update_presence(
                {steam_id: "online", startup_afk: "online"}, observed + 1
            )
            await listener._update_presence({steam_id: "afk"}, observed - 1, seed=True)
            async with sessions() as db:
                assert not await is_afk(db, project, steam_id)
                job = await db.get(CollectionJob, (project, steam_id))
                assert job.due_at <= now_ms()
                assert job.requested_at > original_request
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
