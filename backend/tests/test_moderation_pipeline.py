"""PostgreSQL integration test. The URL must point to a migrated, dedicated test database."""

import json
import logging
import os
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.clients.rustapp.exceptions import RustAppRequestNotSent, RustAppTimeoutError
from app.core.config import ModerationSettings, Settings
from app.crud.moderation import archive, enqueue, now_ms
from app.crud.moderation_control import set_autoban
from app.crud.presence import confirm_presence_connection, save_presence
from app.models import (
    CollectionJob,
    CombatKill,
    Decision,
    Observation,
    Player,
    PlayerPlaytime,
    PlayerSession,
    ReferenceBan,
)
from app.services.moderation.bans import covers_requested_ban, deliver, reconcile, sync_bans
from app.services.moderation.collect import collect


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
@pytest.mark.parametrize(
    "outcome",
    [
        "confirmed",
        "confirmed_after_exit",
        "wrong_ip",
        "missing_ip",
        "already_banned",
        "observe",
        "pause_before_send",
        "new_session",
        "other_server",
        "became_licensed",
        "unknown_license",
        "three_hours",
        "protected_on_join",
        "protected_on_recheck",
        "long_playtime",
        "incomplete_year",
        "stats_error",
        "local_counter_failure",
    ],
)
async def test_join_archive_decide_and_reconcile_timeout(outcome, caplog):
    caplog.set_level(logging.INFO, logger="app")
    session_ip = None if outcome == "missing_ip" else "192.0.2.1"
    project = uuid4().int % (2**30) + 1
    sid = "76561198000000000"
    now = now_ms()
    start = now - 100_000
    profile = dict(
        project_id=project,
        steam_id=sid,
        server_id=163,
        status="online",
        last_no_license=True,
        ip=session_ip,
        created_at=start,
    )
    requests = []
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
    before_delivery = False
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
        assert not path.endswith("/session-history")
        requests.append(path)
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
            data = {"player": profile}
        elif path.endswith("/checks"):
            data = {"total": 5, "results": [{"private": "omit"}]}
        elif path.endswith("/stats"):
            start_at = int(req.url.params["time_from"])
            end_at = int(req.url.params["time_to"])
            day_ms = 86_400_000
            buckets = {
                str(day): {"date": day, "hours_played": 0}
                for day in range(start_at // day_ms * day_ms, end_at + 1, day_ms)
            }
            hours = (
                {"three_hours": 3, "long_playtime": 21.67}.get(outcome, 0) if before_delivery else 0
            )
            if outcome == "protected_on_join":
                hours = 21.67
            if outcome == "protected_on_recheck":
                hours = 3 if before_delivery else 2.5
            buckets[max(buckets)]["hours_played"] = hours
            if before_delivery and outcome == "incomplete_year":
                buckets.pop(min(buckets))
            if before_delivery and outcome == "stats_error":
                return httpx2.Response(503)
            data = {"kills": {"steam_id": sid}, "playTime": {"total": hours, "stats": buckets}}
        else:
            records = {
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
    settings = ModerationSettings()
    try:
        async with create_rustapp_http_client(
            config, transport=httpx2.MockTransport(handle)
        ) as http:
            client = RustAppClient(http)
            async with sessions.begin() as db:
                if outcome == "observe":
                    await set_autoban(db, project, False)
                await confirm_presence_connection(db, project, now)
                await save_presence(db, project, {sid: "joining"}, (start - 25) * 1_000_000)
                await save_presence(
                    db,
                    project,
                    {sid: "online"},
                    start * 1_000_000,
                    server_info={sid: (163, True)},
                )
                await enqueue(db, project, [sid, sid], seed=False)
            for _ in range(2):
                async with sessions.begin() as db:
                    job = await db.get(CollectionJob, (project, sid), with_for_update=True)
                    await collect(db, client, job, settings)
            if outcome == "protected_on_join":
                async with sessions() as db:
                    assert (await db.get(Player, (project, sid))).is_whitelisted
                    assert not await db.scalar(
                        select(Decision.id).where(Decision.project_id == project)
                    )
                    job = await db.get(CollectionJob, (project, sid))
                    assert job.due_at is None
                assert f"/players/{sid}/kill-feed" not in requests
                assert not submitted
                return
            if outcome == "protected_on_recheck":
                assert requests.count(f"/players/{sid}/stats") == 1
                async with sessions.begin() as db:
                    job = await db.get(CollectionJob, (project, sid))
                    cache = await db.get(PlayerPlaytime, (project, sid))
                    assert cache.annual_ms == 9_000_000
                    assert job.playtime_due_at == cache.annual_checked_at + 1_800_000
                    before_delivery = True
                    requests.clear()
                    job.playtime_due_at = now_ms() - 1
                    await collect(db, client, job, settings)
                    await db.refresh(cache)
                    assert cache.annual_ms == 10_800_000
                    assert (await db.get(Player, (project, sid))).is_whitelisted
                    assert job.due_at is None
                    assert requests == [f"/players/{sid}/stats"]
                return
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
                assert len(observations) == 6
                assert requests.count(f"/players/{sid}/details") == 1
                assert next(
                    observation.payload
                    for observation in observations
                    if observation.source == "checks"
                ) == {"total": 5}
            before_delivery = True
            if outcome == "stats_error":
                from app.clients.rustapp.exceptions import RustAppHTTPError

                with pytest.raises(RustAppHTTPError):
                    await deliver(sessions, client, project, settings)
                assert not submitted
                return
            if outcome in {"three_hours", "long_playtime", "incomplete_year"}:
                await deliver(sessions, client, project, settings)
                async with sessions() as db:
                    row = await db.scalar(select(Decision).where(Decision.project_id == project))
                    player = await db.get(Player, (project, sid))
                    assert row.state == "cancelled"
                    assert player.is_whitelisted == (outcome != "incomplete_year")
                    assert row.eligibility["outcome"] == (
                        "incomplete_stats" if outcome == "incomplete_year" else "protected"
                    )
                assert not submitted
                return
            if outcome in {"new_session", "other_server", "became_licensed", "unknown_license"}:
                if outcome == "new_session":
                    async with sessions.begin() as db:
                        await save_presence(db, project, {sid: "offline"}, (now - 1) * 1_000_000)
                        await save_presence(
                            db,
                            project,
                            {sid: "online"},
                            now * 1_000_000,
                            server_info={sid: (163, True)},
                        )
                if outcome == "other_server":
                    profile["server_id"] = 164
                if outcome == "became_licensed":
                    profile["last_no_license"] = False
                if outcome == "unknown_license":
                    profile["last_no_license"] = None
                await deliver(sessions, client, project, settings)
                async with sessions() as db:
                    row = await db.scalar(select(Decision).where(Decision.project_id == project))
                    assert row.state == "cancelled"
                assert not submitted
                return
            if outcome in {"missing_ip", "already_banned", "observe"}:
                await deliver(sessions, client, project, settings)
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
            if outcome == "local_counter_failure":

                async def fail_before_send(request):
                    if request.method == "PUT":
                        raise RustAppRequestNotSent("test counter failure")

                http.event_hooks["request"].append(fail_before_send)
                with pytest.raises(RustAppRequestNotSent):
                    await deliver(sessions, client, project, settings)
                async with sessions() as db:
                    row = await db.scalar(select(Decision).where(Decision.project_id == project))
                    assert row.state == "pending"
                assert submitted == []
                return
            if outcome == "pause_before_send":

                async def disable_during_stats(request):
                    if request.url.path.endswith("/stats"):
                        async with sessions.begin() as db:
                            await set_autoban(db, project, False)

                http.event_hooks["request"].append(disable_during_stats)
                await deliver(sessions, client, project, settings)
                assert not submitted
                async with sessions.begin() as db:
                    row = await db.scalar(select(Decision).where(Decision.project_id == project))
                    assert row.state == "pending"
                    await set_autoban(db, project, True)
                http.event_hooks["request"].remove(disable_during_stats)
            with pytest.raises(RustAppTimeoutError):
                await deliver(sessions, client, project, settings)
            assert "Отправка бессрочного бана" in caplog.text
            assert "Блокировка подтверждена" not in caplog.text
            await deliver(sessions, client, project, settings)
            async with sessions.begin() as db:
                if outcome == "confirmed_after_exit":
                    await save_presence(db, project, {sid: "offline"}, (now + 1) * 1_000_000)
                    requests.clear()
                    await sync_bans(db, client, project)
                    await reconcile(db, client, project, 163)
                    assert requests == ["/ban/browse"]
                else:
                    await reconcile(db, client, project, 163)
                    await sync_bans(db, client, project)
                row = await db.scalar(select(Decision).where(Decision.project_id == project))
                assert row.state == ("failed" if outcome == "wrong_ip" else "confirmed")
                assert row.external_id == 789 and row.ban_ip == session_ip
                reference = await db.get(ReferenceBan, (project, 789))
                assert reference.eligible == (outcome != "wrong_ip")
            assert len(submitted) == 1 and submitted[0]["reason"] == "Чит"
            assert submitted[0]["server_ids"] == [] and "expired_at" not in submitted[0]
            assert submitted[0]["ban_ip_active"] is True and submitted[0]["ban_ip"] == session_ip
            assert ("Блокировка подтверждена RustApp" in caplog.text) == (outcome != "wrong_ip")
            assert "паттерн=P05" in caplog.text and "жертв" in caplog.text and sid in caplog.text
            assert session_ip not in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_ban_sync_reads_existing_page_in_batch_and_backfills_cached_date():
    project = uuid4().int % (2**30) + 1
    steam_id = "76561198000000099"
    now = now_ms()
    rows = [
        {
            "id": index + 1,
            "created_at": now - index,
            "project_id": project,
            "steam_id": steam_id,
            "reason": "чит",
            "computed_is_active": True,
            "server_ids": [],
        }
        for index in range(50)
    ]

    def handle(request):
        assert request.url.path == "/ban/browse" and request.url.params["page"] == "0"
        return httpx2.Response(200, json={"page": 0, "results": rows})

    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    settings = Settings(_secrets_dir=None, rustapp_access_token="test", rustapp_project_id=project)
    selects = []

    def count_reads(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().startswith("SELECT"):
            selects.append(statement)

    try:
        async with create_rustapp_http_client(
            settings, transport=httpx2.MockTransport(handle)
        ) as http:
            client = RustAppClient(http)
            async with sessions.begin() as db:
                await sync_bans(db, client, project)
                await archive(
                    db, project, steam_id, "details", {"player": {"steam_data": {"signed_at": now}}}
                )
            event.listen(engine.sync_engine, "before_cursor_execute", count_reads)
            async with sessions.begin() as db:
                await sync_bans(db, client, project)
            assert len(selects) == 2
            async with sessions() as db:
                record = await db.get(ReferenceBan, (project, 50))
                assert record.signed_at == now
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
        "profile_license",
        "observed_playtime",
        "archived_playtime",
        "under_limit",
        "stale_listener",
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

    def handle(request):
        assert not request.url.path.endswith("/session-history")
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
        data = {
            "details": {
                "player": {
                    "project_id": project,
                    "steam_id": steam_id,
                    "created_at": joined_at,
                    "status": "online",
                    "server_id": 163,
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
            started_at = (
                joined_at if trust_reason in {"observed_playtime", "stale_listener"} else now - 500
            )
            confirmed_at = now if trust_reason != "stale_listener" else started_at + 1000
            await confirm_presence_connection(db, project, confirmed_at)
            await save_presence(
                db,
                project,
                {steam_id: "online"},
                started_at * 1_000_000,
                server_info={steam_id: (163, True)},
            )
            if trust_reason in {"archived_playtime", "under_limit"}:
                prior_start = now - 2 * duration
                prior_end = prior_start + (
                    duration + 10
                    if trust_reason == "archived_playtime"
                    else duration - 30 * 60 * 1000
                )
                db.add(
                    PlayerSession(
                        project_id=project,
                        steam_id=steam_id,
                        server_id=164,
                        connected_at=prior_start,
                        payload={
                            "project_id": project,
                            "steam_id": steam_id,
                            "server_id": 164,
                            "connected_at": prior_start,
                            "joined_at": prior_start,
                            "disconnected_at": prior_end,
                        },
                    )
                )
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
                if trust_reason == "stale_listener":
                    assert not player.is_whitelisted
                    assert data is None and requests == []
                    return
                if trust_reason in {"under_limit", "observed_playtime", "archived_playtime"}:
                    assert not player.is_whitelisted
                    assert set(data.responses) == {
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
                    assert job.playtime_due_at > now_ms()
                    requests.clear()
                    await collect(db, RustAppClient(http), job, ModerationSettings())
                    assert requests == ["kill-feed"]
                    # A new entry rechecks annual hours while short-lived detail data stays cached.
                    job.requested_at = now_ms() + 100
                    requests.clear()
                    await collect(db, RustAppClient(http), job, ModerationSettings())
                    assert requests == ["stats", "kill-feed"]
                else:
                    assert player.is_whitelisted and job.due_at is None
                    assert player.whitelist_reason == "license"
                    expected_requests = {
                        "snapshot_license": [],
                        "profile_license": ["stats", "details"],
                    }
                    assert requests == expected_requests[trust_reason]
                    await enqueue(db, project, [steam_id], seed=False)
                    assert job.due_at is None
            if trust_reason in {"snapshot_license", "profile_license"}:
                requests.clear()
                async with sessions.begin() as db:
                    await sync_bans(db, RustAppClient(http), project)
                assert requests == ["browse"]
    finally:
        await engine.dispose()
