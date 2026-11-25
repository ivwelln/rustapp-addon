from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.rustapp_request import RustAppRequest


async def record_request(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions.begin() as db:
        db.add(RustAppRequest())
