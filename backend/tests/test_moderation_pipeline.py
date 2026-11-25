"""PostgreSQL integration test. The URL must point to a migrated, dedicated test database."""

import json
import logging
import os
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ban_guard.api import app as ban_guard_api
from app.ban_guard.client import BanGuard
from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.clients.rustapp.exceptions import RustAppTimeoutError
from app.core.config import ModerationSettings, Settings
from app.crud.moderation import enqueue, now_ms
from app.models import CollectionJob, CombatKill, Decision, Observation
from app.services.moderation.bans import covers_requested_ban, deliver, reconcile, sync_bans
from app.services.moderation.collect import collect


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
@pytest.mark.parametrize(
    "outcome", ["confirmed", "wrong_ip", "missing_ip", "already_banned", "observe"]
)
async def test_join_archive_decide_and_reconcile_timeout(outcome, caplog):
    caplog.set_level(logging.INFO, logger="app")
    session_ip = None if outcome == "missing_ip" else "192.0.2.1"
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
        ip=session_ip,
    )
    feed = [
        dict(
            id=event_number,
            created_at=start + 1000 + event_number,
            server_id=163,
            project_id=project,
            initiator={"steam_id": sid},
            target={"steam_id": str(int(sid) + event_number)},
            distance=51,
            kill_bone="head",
            weapon="rifle.ak",
        )
        for event_number in range(1, 4)
    ]
    submitted = []
    bans = [
        dict(
            id=788,
            created_at=start,
            project_id=project,
            steam_id=sid,
            computed_is_active=True,
            reason="тест",
            server_ids=[] if outcome == "already_banned" else [163],
            ban_ip_active=True,
            ban_ip="192.0.2.1",
        )
    ]

    def handle(req):
        path = req.url.path
        if req.method == "PUT":
            body = json.loads(req.content)["bans"][0]
            submitted.append(body)
            bans.append(
                {
                    **body,
                    "ban_ip": "192.0.2.2" if outcome == "wrong_ip" else body["ban_ip"],
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
                "ip-history": [],
                "browse": bans,
            }
            data = {"page": 0, "results": records[path.split("/")[-1]]}
        return httpx2.Response(200, json=data)

    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    config = Settings(rustapp_access_token="test", rustapp_project_id=project)
    settings = ModerationSettings(enforce=outcome != "observe")
    ban_guard_api.state.sessions = sessions
    try:
        async with (
            create_rustapp_http_client(config, transport=httpx2.MockTransport(handle)) as http,
            httpx2.AsyncClient(
                base_url="http://ban-guard", transport=httpx2.ASGITransport(app=ban_guard_api)
            ) as guard_http,
        ):
            guard = BanGuard(guard_http, project)
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
                assert row.primary_pattern == "P05"
                assert row.state == ("recorded" if outcome == "observe" else "pending")
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
                assert len(observations) == 7
                assert next(
                    observation.payload
                    for observation in observations
                    if observation.source == "checks"
                ) == {"total": 5}
            if outcome in {"missing_ip", "already_banned", "observe"}:
                await deliver(sessions, client, project, settings, guard)
                expected = {
                    "missing_ip": "failed",
                    "already_banned": "superseded",
                    "observe": "recorded",
                }
                async with sessions() as db:
                    row = await db.scalar(select(Decision).where(Decision.project_id == project))
                    assert row.state == expected[outcome]
                assert not submitted
                assert "паттерн=P05" in caplog.text and "жертв" in caplog.text
                assert "Блокировка подтверждена" not in caplog.text
                return
            with pytest.raises(RustAppTimeoutError):
                await deliver(sessions, client, project, settings, guard)
            assert "Отправка бессрочного бана" in caplog.text
            assert "Блокировка подтверждена" not in caplog.text
            await deliver(sessions, client, project, settings, guard)
            async with sessions.begin() as db:
                await reconcile(db, client, project, 163)
                await sync_bans(db, client, project)
                row = await db.scalar(select(Decision).where(Decision.project_id == project))
                assert row.state == ("failed" if outcome == "wrong_ip" else "confirmed")
                assert row.external_id == 789 and row.ban_ip == session_ip
                from app.models import ReferenceBan

                assert (await db.get(ReferenceBan, (project, 789))).eligible == (
                    outcome == "confirmed"
                )
            assert len(submitted) == 1
            assert submitted[0]["server_ids"] == [] and "expired_at" not in submitted[0]
            assert submitted[0]["ban_ip_active"] is True and submitted[0]["ban_ip"] == session_ip
            assert ("Блокировка подтверждена RustApp" in caplog.text) == (outcome == "confirmed")
            assert "паттерн=P05" in caplog.text and "жертв" in caplog.text and sid in caplog.text
            assert session_ip not in caplog.text
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


@pytest.mark.parametrize(
    "changed_field",
    [
        {"server_ids": [163]},
        {"expired_at": 1795641400000},
        {"ban_ip_active": False},
        {"ban_ip": "192.0.2.2"},
        {"ban_ip": None},
        {"computed_is_active": False},
    ],
)
def test_partial_ban_does_not_cover_requested_ban(changed_field):
    target_ip = "192.0.2.1"
    permanent = {
        "server_ids": [],
        "ban_ip": target_ip,
        "computed_is_active": True,
        "ban_ip_active": True,
    }
    assert covers_requested_ban(permanent, target_ip)
    assert not covers_requested_ban({**permanent, **changed_field}, target_ip)


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
@pytest.mark.parametrize(
    "trust_reason",
    [
        "snapshot_license",
        "session_license",
        "profile_license",
        "playtime",
        "older_page",
        "at_limit",
    ],
)
async def test_whitelist_stops_player_requests(trust_reason):
    from datetime import UTC, datetime

    from app.models import Player, PlayerData, PlayerIP, PlayerReport
    from app.schemas.rustapp.players import RustAppPlayer
    from app.services.rustapp.player_snapshots import save_player_snapshot

    project = uuid4().int % (2**30) + 1
    steam_id = "76561198000000099"
    now = now_ms()
    duration = 3 * 60 * 60 * 1000
    joined_at = now - duration - 1000
    ip = "192.0.2.5"
    requests = []
    session = {
        "project_id": project,
        "server_id": 163,
        "steam_id": steam_id,
        "ip": ip,
        "is_license": trust_reason == "session_license",
        "connected_at": joined_at - 1000,
        "joined_at": joined_at,
        "disconnected_at": None if trust_reason == "playtime" else joined_at + duration,
    }

    if trust_reason == "older_page":
        recent_sessions = [
            {
                **session,
                "server_id": 164,
                "connected_at": now - index * 60_000,
                "joined_at": now - index * 60_000 + 50,
                "disconnected_at": now - index * 60_000 + 100,
            }
            for index in range(1, 21)
        ]
        old_joined_at = joined_at - 30 * 60_000
        old_session = {
            **session,
            "connected_at": old_joined_at - 1000,
            "joined_at": old_joined_at,
            "disconnected_at": old_joined_at + duration + 1,
        }
    else:
        recent_sessions = [session]

    def handle(request):
        requests.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path == "/ban/browse":
            assert "steam_id" not in request.url.params
            return httpx2.Response(
                200,
                json={
                    "page": 0,
                    "results": [
                        {
                            "id": 25,
                            "project_id": project,
                            "steam_id": steam_id,
                            "created_at": now,
                            "reason": "чит",
                            "computed_is_active": True,
                            "server_ids": [],
                        }
                    ],
                },
            )
        if requests[-1] == "session-history" and request.url.params["page"] == "1":
            assert trust_reason == "older_page"
            return httpx2.Response(200, json={"page": 1, "results": [old_session]})
        data = {
            "session-history": {"page": 0, "results": recent_sessions},
            "details": {
                "player": {
                    "project_id": project,
                    "steam_id": steam_id,
                    "created_at": joined_at,
                    "last_no_license": False if trust_reason == "profile_license" else True,
                }
            },
            "stats": {"kills": {}},
            "checks": {"total": 2},
            "reports": {
                "page": 0,
                "results": [
                    {"id": 33, "target_steam_id": steam_id, "created_at": now, "reason": "чит"}
                ],
            },
            "ip-history": {
                "page": 0,
                "results": [
                    {"ip": ip, "created_at": joined_at, "project_id": project},
                    {"ip": "192.0.2.99", "created_at": joined_at, "project_id": -1},
                ],
            },
            "kill-feed": {"page": 0, "results": []},
        }
        return httpx2.Response(200, json=data[requests[-1]])

    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    config = Settings(rustapp_access_token="test", rustapp_project_id=project)
    try:
        # Simulate a job already queued before a newer snapshot marks the player as trusted.
        async with sessions.begin() as db:
            await enqueue(db, project, [steam_id], seed=False)
        if trust_reason == "snapshot_license":
            await save_player_snapshot(
                sessions,
                project_id=project,
                observed_at=datetime.now(UTC),
                players=[
                    RustAppPlayer(
                        steam_id=steam_id,
                        project_id=project,
                        last_no_license=False,
                        status="online",
                    )
                ],
            )
        async with create_rustapp_http_client(
            config, transport=httpx2.MockTransport(handle)
        ) as http:
            async with sessions.begin() as db:
                job = await db.get(CollectionJob, (project, steam_id), with_for_update=True)
                await collect(db, RustAppClient(http), job, ModerationSettings())
            async with sessions.begin() as db:
                job = await db.get(CollectionJob, (project, steam_id))
                player = await db.get(Player, (project, steam_id))
                data = await db.get(PlayerData, (project, steam_id))
                if trust_reason == "at_limit":
                    assert not player.is_whitelisted
                    assert set(data.responses) == {
                        "session-history",
                        "details",
                        "stats",
                        "checks",
                        "reports",
                        "ip-history",
                        "kill-feed",
                    }
                    assert data.responses["checks"]["payload"] == {"total": 2}
                    assert (await db.get(PlayerReport, (project, 33))).steam_id == steam_id
                    assert await db.get(PlayerIP, (project, steam_id, ip))
                    assert await db.get(PlayerIP, (project, steam_id, "192.0.2.99")) is None
                    # A rejoin in the cache window refreshes only the two fast-changing sources.
                    job.requested_at = now_ms() + 100
                    requests.clear()
                    await collect(db, RustAppClient(http), job, ModerationSettings())
                    assert requests == ["session-history", "kill-feed"]
                else:
                    assert player.is_whitelisted and job.due_at is None
                    expected = (
                        "playtime" if trust_reason in {"playtime", "older_page"} else "license"
                    )
                    assert player.whitelist_reason == expected
                    expected_requests = {
                        "snapshot_license": [],
                        "profile_license": ["session-history", "details"],
                        "session_license": ["session-history"],
                        "playtime": ["session-history"],
                        "older_page": ["session-history", "session-history"],
                    }
                    assert requests == expected_requests[trust_reason]
                    await enqueue(db, project, [steam_id], seed=False)
                    assert job.due_at is None
            if trust_reason != "at_limit":
                requests.clear()
                async with sessions.begin() as db:
                    await sync_bans(db, RustAppClient(http), project)
                assert requests == ["browse"]
    finally:
        await engine.dispose()
