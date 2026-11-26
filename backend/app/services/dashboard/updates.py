import asyncio
import logging
from contextlib import suppress

import asyncpg

from app.core.config import DatabaseSettings

logger = logging.getLogger(__name__)


class DashboardUpdates:
    def __init__(self) -> None:
        self.changed = asyncio.Event()

    def notify(self, *_notification: object) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    async def listen(self, settings: DatabaseSettings) -> None:
        while True:
            connection = None
            try:
                connection = await asyncpg.connect(
                    host=settings.db_host,
                    port=settings.db_port,
                    user=settings.db_user,
                    password=settings.db_password.get_secret_value(),
                    database=settings.db_name,
                    timeout=5,
                )
                disconnected = asyncio.Event()
                connection.add_termination_listener(
                    lambda _connection, signal=disconnected: signal.set()
                )
                await connection.add_listener("rustapp_dashboard", self.notify)
                self.notify()
                await disconnected.wait()
            except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
                logger.warning("Мониторинг: прервана подписка PostgreSQL (%s)", type(exc).__name__)
            finally:
                if connection is not None and not connection.is_closed():
                    with suppress(TimeoutError, OSError, asyncpg.PostgresError):
                        await connection.close(timeout=2)
            await asyncio.sleep(2)
