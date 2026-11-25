"""PostgreSQL integration test. The URL must point to a migrated, dedicated test database."""

import json
import os
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.clients.rustapp.exceptions import RustAppTimeoutError
from app.core.config import ModerationSettings, Settings
from app.crud.moderation import enqueue, now_ms
from app.models import CollectionJob, CombatKill, Decision, Observation
from app.services.moderation.bans import deliver, reconcile, sync_bans
from app.services.moderation.collect import collect


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_join_archive_decide_and_reconcile_timeout():
    project = uuid4().int % (2**30) + 1
    sid = "76561198000000000"
    now = now_ms()
    start = now - 100_000
    session = dict(
        project_id=project,
        steam_id=sid,
        server_id=163,
        connected_at=start,
        joined_at=start + 100,
        is_license=False,
    )
    feed = [
        dict(
            id=i,
            created_at=start + 1000 + i,
            server_id=163,
            project_id=project,
            initiator={"steam_id": sid},
            target={"steam_id": str(int(sid) + i)},
            distance=51,
            kill_bone="head",
            weapon="rifle.ak",
        )
        for i in range(1, 4)
    ]
    submitted = []
    bans = []

    def handle(req):
        path = req.url.path
        if req.method == "PUT":
            body = json.loads(req.content)["bans"][0]
            submitted.append(body)
            bans.append(
                {
                    **body,
                    "project_id": project,
                    "id": 789,
                    "created_at": now,
                    "computed_is_active": True,
                }
            )
            raise httpx2.ReadTimeout("Response lost", request=req)
        if path.endswith("/details"):
            data = {"player": {"project_id": project, "steam_id": sid, "created_at": start}}
        elif path.endswith("/checks"):
            data = {"total": 5, "results": [{"private": "omit"}]}
        elif path.endswith("/stats"):
            data = {"kills": {}}
        else:
            records = {
                "session-history": [session],
                "kill-feed": feed,
                "reports": [],
                "browse": bans,
            }
            data = {"page": 0, "results": records[path.split("/")[-1]]}
        return httpx2.Response(200, json=data)

    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    config = Settings(rustapp_access_token="test", rustapp_project_id=project)
    settings = ModerationSettings(enforce=True)
    try:
        async with create_rustapp_http_client(
            config, transport=httpx2.MockTransport(handle)
        ) as http:
            client = RustAppClient(http)
            async with sessions.begin() as db:
                await enqueue(db, project, [sid, sid], seed=False)
            for _ in range(2):
                async with sessions.begin() as db:
                    job = await db.get(CollectionJob, (project, sid), with_for_update=True)
                    await collect(db, client, job, settings)
            async with sessions.begin() as db:
                row = (
                    await db.scalars(select(Decision).where(Decision.project_id == project))
                ).one()
                assert row.primary_pattern == "P05" and row.state == "pending"
                assert set(row.matches) == {"P05", "P09"}
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(CombatKill)
                        .where(CombatKill.project_id == project)
                    )
                    == 3
                )
                observations = (
                    await db.scalars(select(Observation).where(Observation.project_id == project))
                ).all()
                assert len(observations) == 6
                assert next(o.payload for o in observations if o.source == "checks") == {"total": 5}
            with pytest.raises(RustAppTimeoutError):
                await deliver(sessions, client, project, settings)
            await deliver(sessions, client, project, settings)
            async with sessions.begin() as db:
                await reconcile(db, client, project, 163)
                await sync_bans(db, client, project)
                row = await db.scalar(select(Decision).where(Decision.project_id == project))
                assert row.state == "confirmed" and row.external_id == 789
                from app.models import ReferenceBan

                assert (await db.get(ReferenceBan, (project, 789))).eligible
            assert len(submitted) == 1 and submitted[0]["server_ids"] == [163]
    finally:
        await engine.dispose()


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_registration_ban_cannot_seed_next_registration_after_comment_edit():
    from app.services.moderation.bans import usable_ban

    project = uuid4().int % (2**30) + 1
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    try:
        async with sessions.begin() as db:
            sid = "76561198000000001"
            decision = Decision(
                project_id=project,
                steam_id=sid,
                server_id=163,
                connected_at=1,
                action="ban",
                state="confirmed",
                primary_pattern="P02",
                matches={},
                external_id=123,
            )
            db.add(decision)
            await db.flush()
            ban = dict(
                project_id=project, steam_id=sid, id=123, computed_is_active=True, reason="чит"
            )
            assert not await usable_ban(
                db, project, {**ban, "comment": f"rustapp-addon:{decision.id}:P02"}
            )
            assert not await usable_ban(db, project, {**ban, "comment": "изменено в панели"})
            assert await usable_ban(
                db, project, {**ban, "id": 124, "comment": "проверено модератором"}
            )
            assert not await usable_ban(
                db, project, {**ban, "id": 124, "computed_is_active": False}
            )
    finally:
        await engine.dispose()
