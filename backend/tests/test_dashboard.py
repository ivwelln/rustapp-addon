import os
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crud.moderation import now_ms
from app.crud.player_data import save_response
from app.crud.presence import save_presence
from app.main import app
from app.models import CombatKill, Player
from app.services.dashboard.roster import DAY_MS, online_players


async def test_dashboard_is_local_and_assets_are_served():
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(base_url="http://127.0.0.1", transport=transport) as client:
        page = await client.get("/")
        assert page.status_code == 200 and "Игроки и модерация" in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
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
        assert roster[0]["name"] == "Zeta" and roster[0]["provider"] == "Network"
        assert roster[0]["country_code"] == "RU" and "ip" not in roster[0]
    finally:
        await engine.dispose()
