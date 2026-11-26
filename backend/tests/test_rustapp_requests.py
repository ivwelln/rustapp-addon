import os

import httpx2
import pytest
from centrifuge import Client
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.clients.rustapp.exceptions import RustAppHTTPError, RustAppTimeoutError
from app.clients.rustapp.realtime import RustAppRealtimeClient
from app.core.config import Settings
from app.models import RustAppRequest


@pytest.mark.skipif(not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL")
async def test_request_times_include_errors_and_websocket_attempts(monkeypatch):
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)

    async def count() -> int:
        async with sessions() as db:
            return await db.scalar(select(func.count()).select_from(RustAppRequest))

    async def http_reply(request):
        if request.method == "PUT":
            assert await count() == before + 2
            raise httpx2.ReadTimeout("test", request=request)
        assert await count() == before + 1
        return httpx2.Response(429, headers={"Retry-After": "0"})

    async def websocket_reply(_client):
        assert await count() == before + 3
        raise OSError("test")

    try:
        before = await count()
        async with create_rustapp_http_client(
            Settings(rustapp_access_token="test"),
            transport=httpx2.MockTransport(http_reply),
            request_log=sessions,
        ) as http:
            client = RustAppClient(http)
            with pytest.raises(RustAppHTTPError):
                await client.get("/ban/browse")
            with pytest.raises(RustAppTimeoutError):
                await client.put_player_ban("76561198000000000", {})
        monkeypatch.setattr(Client, "_open_transport", websocket_reply)
        socket = RustAppRealtimeClient("wss://centrifugo.rustapp.io", request_log=sessions)
        with pytest.raises(OSError, match="test"):
            await socket._open_transport()
        async with sessions() as db:
            times = await db.scalars(
                select(RustAppRequest.requested_at).order_by(RustAppRequest.id.desc()).limit(3)
            )
            recorded = list(times)
            assert len(recorded) == 3 and all(time.tzinfo for time in recorded)
    finally:
        await engine.dispose()
