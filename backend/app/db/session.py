import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import DatabaseSettings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def open_database_sessions(
    settings: DatabaseSettings,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        settings.db_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 10},
    )
    logger.info(
        "PostgreSQL: пул настроен | хост=%s порт=%d база=%s",
        settings.db_host,
        settings.db_port,
        settings.db_name,
    )
    if logger.isEnabledFor(logging.DEBUG):

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def start_query(connection, cursor, statement, parameters, context, executemany):
            context.query_started_at = perf_counter()

        @event.listens_for(engine.sync_engine, "after_cursor_execute")
        def finish_query(connection, cursor, statement, parameters, context, executemany):
            operation = next(iter(statement.split(maxsplit=1)), "").upper()
            if operation not in {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"}:
                operation = "SQL"
            logger.debug(
                "PostgreSQL: %s | %.1f мс | строки=%d",
                operation,
                (perf_counter() - context.query_started_at) * 1000,
                cursor.rowcount,
            )

    @event.listens_for(engine.sync_engine, "handle_error")
    def log_query_error(context):
        logger.warning(
            "PostgreSQL: операция завершилась ошибкой | тип=%s",
            type(context.original_exception).__name__,
        )

    try:
        yield async_sessionmaker(engine)
    finally:
        await engine.dispose()
        logger.debug("PostgreSQL: пул закрыт")
