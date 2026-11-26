from time import time_ns
from typing import TYPE_CHECKING

from centrifuge import Client
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.rustapp.exceptions import RustAppHTTPError
from app.crud.rustapp_requests import record_request

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection


class RustAppRealtimeClient(Client):
    def __init__(
        self,
        address: str,
        *,
        request_log: async_sessionmaker[AsyncSession],
        account: str | None = None,
        **options,
    ) -> None:
        super().__init__(address, **options)
        self._request_log = request_log
        self._account = account
        self.last_ping_at = 0

    async def _handle_ping(self) -> None:
        # Centrifuge 0.7.0 invokes this on each server heartbeat.
        self.last_ping_at = time_ns() // 1_000_000
        await super()._handle_ping()

    async def _open_transport(self) -> "ClientConnection":
        # Centrifuge 0.7.0 opens the transport here for both initial and retry attempts.
        try:
            await record_request(self._request_log, self._account)
        except RustAppHTTPError as exc:
            raise OSError("Действует общая пауза RustApp") from exc
        except (SQLAlchemyError, OSError) as exc:
            raise OSError("Не удалось записать время подключения к RustApp") from exc
        return await super()._open_transport()
