import os
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crud.moderation import now_ms
from app.crud.player_data import save_response
from app.crud.presence import save_presence
from app.main import app
from app.models import CombatKill, Decision, Player, PlayerReport
from app.services.dashboard.roster import DAY_MS, online_players


async def test_dashboard_is_local_and_assets_are_served():
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(base_url="http://127.0.0.1", transport=transport) as client:
        page = await client.get("/")
        assert page.status_code == 200 and "Игроки и модерация" in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert "https://avatars.akamai.steamstatic.com" in page.headers["content-security-policy"]
        assert "Правила модерации" in page.text and "P09" in page.text
        assert (await client.get("/dashboard-assets/rustapp.ico")).content.startswith(
            b"\x00\x00\x01\x00"
        )
        assert (await client.get("/dashboard-assets/dashboard.js")).status_code == 200
        assert (await client.get("/dashboard-assets/dashboard.css")).status_code == 200
        external = await client.get("/api/dashboard", headers={"Host": "other.example"})
        assert external.status_code == 403


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
            initial = await client.get(path)
            assert initial.status_code == 200
            snapshot = await client.get("/api/dashboard")
            assert snapshot.json()["storage"] == {"players": 1, "kills": 1}
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
