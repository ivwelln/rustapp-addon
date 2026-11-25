from typing import TYPE_CHECKING

from centrifuge import Client
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crud.rustapp_requests import record_request

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection


class RustAppRealtimeClient(Client):
    def __init__(
        self, address: str, *, request_log: async_sessionmaker[AsyncSession], **options
    ) -> None:
        super().__init__(address, **options)
        self._request_log = request_log

    async def _open_transport(self) -> "ClientConnection":
        # Centrifuge 0.7.0 opens the transport here for both initial and retry attempts.
        try:
            await record_request(self._request_log)
        except (SQLAlchemyError, OSError) as exc:
            raise OSError("Не удалось записать время подключения к RustApp") from exc
        return await super()._open_transport()
