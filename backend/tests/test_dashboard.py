import os
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crud.moderation import now_ms
from app.crud.player_data import save_response
from app.crud.presence import save_presence
from app.main import app
from app.models import CollectionJob, CombatKill, Decision, Observation, Player, PlayerReport
from app.services.dashboard.roster import DAY_MS, online_players


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_dashboard_manual_whitelist_and_live_interval(monkeypatch):
    project = uuid4().int % (2**30) + 1
    now = now_ms()
    normal, reported, licensed, manual = [str(76561198000000010 + index) for index in range(4)]
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    monkeypatch.setattr(app.state, "dashboard_sessions", sessions, raising=False)
    monkeypatch.setattr(app.state, "dashboard_project", project, raising=False)
    try:
        async with sessions.begin() as db:
            await save_presence(
                db,
                project,
                dict.fromkeys((normal, reported, licensed, manual), "online"),
                now * 1_000_000,
            )
            db.add(
                Player(
                    project_id=project,
                    steam_id=licensed,
                    is_whitelisted=True,
                    whitelist_reason="license",
                )
            )
            for steam_id, report_pending in ((normal, False), (reported, True)):
                db.add(
                    CollectionJob(
                        project_id=project,
                        steam_id=steam_id,
                        requested_at=now,
                        collected_at=now,
                        due_at=now + 150_000,
                        report_pending=report_pending,
                    )
                )
        async with httpx2.AsyncClient(
            base_url="http://127.0.0.1", transport=httpx2.ASGITransport(app=app)
        ) as client:
            snapshot_path = "/api/dashboard"
            interval_path = f"{snapshot_path}/poll-interval"
            whitelist_path = f"{snapshot_path}/players/{manual}/whitelist"
            assert (await client.get(snapshot_path)).json()["poll_seconds"] == 150
            assert (await client.put(interval_path, json={"seconds": 0})).status_code == 422
            changed = await client.put(interval_path, json={"seconds": 45})
            assert changed.json() == {"seconds": 45}
            assert (await client.get(snapshot_path)).json()["poll_seconds"] == 45
            added = await client.put(whitelist_path, json={"enabled": True})
            assert added.json() == {"trusted": True, "whitelist_reason": "manual"}
            roster = (await client.get(snapshot_path)).json()["players"]
            assert next(player for player in roster if player["steam_id"] == manual)["trusted"]
            automatic = await client.put(
                f"{snapshot_path}/players/{licensed}/whitelist", json={"enabled": False}
            )
            assert automatic.json() == {"trusted": True, "whitelist_reason": "license"}
            removed = await client.put(whitelist_path, json={"enabled": False})
            assert removed.json() == {"trusted": False, "whitelist_reason": None}
        async with sessions() as db:
            assert (await db.get(CollectionJob, (project, normal))).due_at == now + 45_000
            assert (await db.get(CollectionJob, (project, reported))).due_at == now + 150_000
            assert (await db.get(CollectionJob, (project, manual))).due_at <= now_ms()
    finally:
        await engine.dispose()


async def test_dashboard_is_local_and_assets_are_served():
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(base_url="http://127.0.0.1", transport=transport) as client:
        page = await client.get("/")
        assert page.status_code == 200 and "Игроки и модерация" in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert "https://avatars.akamai.steamstatic.com" in page.headers["content-security-policy"]
        assert "Правила модерации" in page.text and "P09" in page.text
        assert 'id="refresh"' not in page.text
        assert (await client.get("/dashboard-assets/rustapp.ico")).content.startswith(
            b"\x00\x00\x01\x00"
        )
        assert (await client.get("/dashboard-assets/dashboard.js")).status_code == 200
        assert (await client.get("/dashboard-assets/dashboard.css")).status_code == 200
        external = await client.get("/api/dashboard", headers={"Host": "other.example"})
        assert external.status_code == 403


async def test_dashboard_reports_unavailable_when_database_connection_fails(monkeypatch):
    @asynccontextmanager
    async def disconnected():
        raise OSError("test database connection failure")
        yield

    monkeypatch.setattr(app.state, "dashboard_sessions", disconnected, raising=False)
    monkeypatch.setattr(app.state, "dashboard_project", 1, raising=False)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(base_url="http://127.0.0.1", transport=transport) as client:
        assert (await client.get("/ready")).status_code == 503
        assert (await client.get("/api/dashboard")).status_code == 503


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_roster_only_shows_present_players_with_nontrusted_first():
    project = uuid4().int % (2**30) + 1
    now = now_ms()
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    steam_ids = ["76561198000001230", "76561198000001231", "76561198000001232"]
    active, trusted, offline = steam_ids
    avatar_url = f"https://avatars.akamai.steamstatic.com/{'a' * 40}_medium.jpg"
    try:
        async with sessions.begin() as db:
            await save_presence(
                db, project, {active: "online", trusted: "afk", offline: "online"}, now * 1_000_000
            )
            await save_presence(db, project, {offline: "offline"}, (now + 1) * 1_000_000)
            db.add(Player(project_id=project, steam_id=trusted, is_whitelisted=True))
            await save_response(
                db,
                project,
                active,
                "browse-full",
                now - 20,
                {
                    "steam_name": "Unnamed",
                    "steam_avatar": "https://untrusted.example/image.jpg",
                },
            )
            await save_response(
                db,
                project,
                active,
                "details",
                now,
                {
                    "player": {
                        "steam_name": "Zeta",
                        "steam_data": {"avatar_medium_url": avatar_url},
                        "ip": "192.0.2.4",
                        "ip_details": {
                            "provider": "Network",
                            "country_code": "RU",
                            "country_name": "Russia",
                        },
                    },
                },
            )
            await save_response(db, project, trusted, "browse-full", now, {"steam_name": "Alpha"})
            await save_response(db, project, active, "reports", now, {"total": 25, "results": []})
            db.add(
                PlayerReport(
                    project_id=project, steam_id=trusted, report_id=1, created_at=now, payload={}
                )
            )
            db.add_all(
                [
                    CombatKill(
                        project_id=project,
                        steam_id=active,
                        event_id=1,
                        server_id=163,
                        at=now - 10,
                        facts={},
                    ),
                    CombatKill(
                        project_id=project,
                        steam_id=active,
                        event_id=2,
                        server_id=163,
                        at=now - DAY_MS - 10,
                        facts={},
                    ),
                ]
            )
        async with sessions() as db:
            roster = await online_players(db, project, now)
        assert [player["steam_id"] for player in roster] == [active, trusted]
        assert [player["status"] for player in roster] == ["online", "afk"]
        assert [player["kills"] for player in roster] == [1, None]
        assert [player["reports"] for player in roster] == [25, 1]
        assert [player["avatar_url"] for player in roster] == [avatar_url, None]
        assert roster[0]["name"] == "Zeta" and roster[0]["provider"] == "Network"
        assert roster[0]["country_code"] == "RU" and "ip" not in roster[0]
    finally:
        await engine.dispose()


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_roster_prioritizes_only_eligible_pirates_with_observed_kd():
    project = uuid4().int % (2**30) + 1
    now = now_ms()
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    fixtures = [
        ("Alpha", 6, 2, True, False),
        ("Bravo", 5, 0, True, False),
        ("Charlie", 6, 0, False, False),
        ("Delta", 6, 0, None, False),
        ("Echo", 6, None, True, False),
        ("Tango", 6, 0, True, True),
        ("Yankee", 6, 0, None, False),
        ("Zulu", 6, 1, True, False),
    ]

    def event(event_id, victim, at, source_project=project):
        return {
            "id": event_id,
            "project_id": source_project,
            "created_at": at,
            "target": {"steam_id": victim},
        }

    try:
        async with sessions.begin() as db:
            for index, (name, kills, deaths, pirate, trusted) in enumerate(fixtures):
                steam_id = str(76561198000001240 + index)
                await save_presence(
                    db,
                    project,
                    {steam_id: "afk" if name == "Zulu" else "online"},
                    (now - 10_000) * 1_000_000,
                    server_info={steam_id: (163, pirate)},
                )
                db.add(
                    Player(
                        project_id=project,
                        steam_id=steam_id,
                        is_whitelisted=trusted,
                        last_client_kind="pirated" if name in {"Charlie", "Yankee"} else "unknown",
                    )
                )
                await save_response(db, project, steam_id, "browse-full", now, {"steam_name": name})
                if deaths is not None:
                    await save_response(
                        db, project, steam_id, "kill-feed", now - 2_000, {"results": []}
                    )
                db.add_all(
                    CombatKill(
                        project_id=project,
                        steam_id=steam_id,
                        event_id=100 * index + kill,
                        server_id=163,
                        at=now - 4_000,
                        facts={},
                    )
                    for kill in range(kills)
                )
                death_events = [
                    event(1_000 + 100 * index + death, steam_id, now - 4_000)
                    for death in range(deaths or 0)
                ]
                if name == "Zulu":
                    db.add(
                        CollectionJob(
                            project_id=project,
                            steam_id=steam_id,
                            requested_at=now - 8_000,
                            collected_at=now - 3_000,
                        )
                    )
                    death_events += [
                        death_events[0],
                        event(2000, steam_id, now - DAY_MS - 1),
                        event(2001, steam_id, now - 4_000, project + 1),
                        {**event(2002, steam_id, now), "created_at": "invalid"},
                    ]
                for _ in range(2):
                    db.add(
                        Observation(
                            project_id=project,
                            steam_id=steam_id,
                            source="kill-feed",
                            observed_at=now - 2_000,
                            payload={"results": death_events},
                        )
                    )
        async with sessions() as db:
            roster = await online_players(db, project, now)
        assert [player["name"] for player in roster] == [
            "Yankee",
            "Zulu",
            "Alpha",
            "Bravo",
            "Charlie",
            "Delta",
            "Echo",
            "Tango",
        ]
        assert [player["name"] for player in roster if player["priority"]] == ["Yankee", "Zulu"]
        assert roster[0]["deaths"] == 0
        zulu = roster[1]
        assert (zulu["kills"], zulu["deaths"], zulu["checked_at"]) == (6, 1, now - 2_000)
        assert next(player for player in roster if player["name"] == "Echo")["deaths"] is None
    finally:
        await engine.dispose()


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_decision_card_persists_local_verdict_and_shows_verified_reference(monkeypatch):
    project = uuid4().int % (2**30) + 1
    now = now_ms()
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    monkeypatch.setattr(app.state, "dashboard_sessions", sessions, raising=False)
    monkeypatch.setattr(app.state, "dashboard_project", project, raising=False)
    steam_id = "76561198000001231"
    try:
        async with sessions.begin() as db:
            decision = Decision(
                project_id=project,
                steam_id=steam_id,
                state="confirmed",
                action="ban",
                server_id=163,
                connected_at=now,
                joined_at=now,
                primary_pattern="P02",
                reference_ban_id=22,
                external_id=33,
                ban_ip="192.0.2.4",
                matches={
                    "P02": {
                        "at": now,
                        "settings": {"minutes": 60, "version": 1},
                        "evidence": {
                            "signed_at": now,
                            "bans": [
                                {"id": 11, "signed_at": now},
                                {"id": 22, "signed_at": now},
                            ],
                        },
                    }
                },
            )
            db.add(decision)
            db.add(Player(project_id=project, steam_id=steam_id))
            db.add(
                CombatKill(
                    project_id=project,
                    steam_id=steam_id,
                    event_id=1,
                    server_id=163,
                    at=now,
                    facts={},
                )
            )
            db.add(
                CombatKill(
                    project_id=project + 1,
                    steam_id=steam_id,
                    event_id=1,
                    server_id=163,
                    at=now,
                    facts={},
                )
            )
            await db.flush()
            path = f"/api/dashboard/decisions/{decision.id}"
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(base_url="http://127.0.0.1", transport=transport) as client:
            assert (await client.get("/ready")).json() == {"status": "ready"}
            initial = await client.get(path)
            assert initial.status_code == 200
            snapshot = await client.get("/api/dashboard")
            assert snapshot.json()["storage"] == {"players": 1, "kills": 1}
            assert snapshot.json()["autoban_enabled"] is True
            switch_path = "/api/dashboard/autoban"
            denied_switch = await client.put(
                switch_path, json={"enabled": False}, headers={"Origin": "https://other.example"}
            )
            assert denied_switch.status_code == 403
            assert (await client.put(switch_path, json={"enabled": "false"})).status_code == 422
            disabled = await client.put(
                switch_path, json={"enabled": False}, headers={"Origin": "http://127.0.0.1"}
            )
            assert disabled.json() == {"enabled": False}
            assert (await client.get("/api/dashboard")).json()["autoban_enabled"] is False
            assert (await client.put(switch_path, json={"enabled": True})).json() == {
                "enabled": True
            }
            assert initial.json()["joined_at"] == now
            assert initial.json()["reference_ban_id"] == 22 and "№22" in initial.json()["reason"]
            assert "192.0.2.4" not in initial.text
            data = {"verdict": "false_positive", "note": "Ручная проверка"}
            denied = await client.put(path, json=data, headers={"Origin": "https://other.example"})
            assert denied.status_code == 403
            saved = await client.put(path, json=data, headers={"Origin": "http://127.0.0.1"})
            assert saved.status_code == 200
            assert saved.json()["verdict_at"] >= now
            assert (await client.get(path)).json() == saved.json()
            assert saved.json()["state"] == "confirmed" and saved.json()["ban_id"] == 33
            app.state.dashboard_project = project + 1
            assert (await client.get(path)).status_code == 404
    finally:
        await engine.dispose()
