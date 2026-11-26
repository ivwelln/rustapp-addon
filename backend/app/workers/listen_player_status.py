import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from time import time_ns

from centrifuge import (
    CentrifugeError,
    ClientEventHandler,
    ConnectingContext,
    DisconnectedContext,
    ErrorContext,
    PublicationContext,
    SubscribedContext,
    SubscribingContext,
    SubscriptionEventHandler,
    UnauthorizedError,
    UnsubscribedContext,
)
from pydantic import ValidationError
from pydantic_settings import SettingsError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.rustapp.client import (
    RUSTAPP_CORE_URL,
    RustAppClient,
    create_rustapp_http_client,
)
from app.clients.rustapp.exceptions import RustAppError, RustAppHTTPError, RustAppResponseError
from app.clients.rustapp.handshake import fetch_centrifugo_token
from app.clients.rustapp.players import fetch_players_page
from app.clients.rustapp.realtime import RustAppRealtimeClient
from app.clients.rustapp.retry_after import parse_retry_after
from app.core.config import DatabaseSettings, get_settings
from app.core.logging import configure_console_logging
from app.crud.moderation import enqueue
from app.crud.presence import (
    PRESENCE_TIMEOUT_MS,
    confirm_presence_connection,
    invalidate_presence,
    remove_offline_jobs,
    save_presence,
)
from app.crud.rustapp_requests import account_key
from app.db.session import open_database_sessions
from app.schemas.rustapp.players import MAX_PLAYERS_PAGE_SIZE, RustAppPlayer
from app.schemas.rustapp.realtime import PlayerStatus, RustAppPlayerStatusUpdate
from app.services.rustapp.player_sessions import PlayerSessionTracker
from app.services.rustapp.player_snapshots import save_player_snapshot

logger = logging.getLogger(__name__)

RUSTAPP_WS_URL = "wss://centrifugo.rustapp.io/connection/websocket"
TOKEN_RETRY_FALLBACK_SECONDS = 300.0
SNAPSHOT_RETRY_FALLBACK_SECONDS = 300.0
SNAPSHOT_MIN_RETRY_SECONDS = 30.0
SNAPSHOT_DB_RETRY_SECONDS = 30
SNAPSHOT_MIN_INTERVAL_SECONDS = 60


class ConnectionEvents(ClientEventHandler):
    def __init__(self, stopped: asyncio.Event) -> None:
        self._stopped = stopped

    async def on_connecting(self, ctx: ConnectingContext) -> None:
        logger.info("Подключение к RustApp WebSocket, код %d", ctx.code)

    async def on_disconnected(self, ctx: DisconnectedContext) -> None:
        logger.info("WebSocket отключён, код %d", ctx.code)
        self._stopped.set()

    async def on_error(self, ctx: ErrorContext) -> None:
        logger.warning("Ошибка WebSocket: код %d, %s", ctx.code, type(ctx.error).__name__)


class PlayerStatusEvents(SubscriptionEventHandler):
    def __init__(
        self,
        stopped: asyncio.Event,
        *,
        court_client: RustAppClient,
        db_sessions: async_sessionmaker[AsyncSession],
        project_id: int,
        tasks: asyncio.TaskGroup,
    ) -> None:
        self._stopped = stopped
        self._sessions = PlayerSessionTracker()
        self._court_client = court_client
        self._db_sessions = db_sessions
        self._project_id = project_id
        self._tasks = tasks
        self._snapshot_loaded = False
        self._snapshot_retry_not_before = 0.0
        self._snapshot_refresh_not_before = 0.0
        self._snapshot_task: asyncio.Task[None] | None = None
        self._snapshot_queued = False
        self._last_signal_at = 0
        self._subscribed = False

    def cancel_snapshot(self) -> None:
        if self._snapshot_task is not None:
            self._snapshot_task.cancel()
            self._snapshot_task = None

    async def on_subscribing(self, ctx: SubscribingContext) -> None:
        self._subscribed = False
        self.cancel_snapshot()
        self._snapshot_queued = False

    async def on_subscribed(self, ctx: SubscribedContext) -> None:
        if not ctx.recovered:
            self._sessions.reset()
            self._snapshot_loaded = False
            await self.clear_presence()

        if not self._snapshot_loaded:
            self.cancel_snapshot()
            self._snapshot_task = self._tasks.create_task(self._load_snapshot())

        self._last_signal_at = time_ns() // 1_000_000
        self._subscribed = True
        logger.info("Подписка активна: %s", ctx.channel)

    async def on_unsubscribed(self, ctx: UnsubscribedContext) -> None:
        self._subscribed = False
        self.cancel_snapshot()
        logger.info("Подписка завершилась: %d", ctx.code)
        self._stopped.set()

    async def _load_snapshot(self, *, refresh: bool = False) -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        retry_delay = SNAPSHOT_MIN_RETRY_SECONDS

        try:
            while self._snapshot_task is task:
                not_before = self._snapshot_retry_not_before
                if refresh:
                    not_before = max(not_before, self._snapshot_refresh_not_before)
                remaining = not_before - loop.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue

                observed_at = datetime.now(UTC)
                presence_at = time_ns()
                self._snapshot_queued = False
                try:
                    self._snapshot_refresh_not_before = loop.time() + SNAPSHOT_MIN_INTERVAL_SECONDS
                    players = await self._fetch_snapshot()
                except RustAppError as exc:
                    if self._snapshot_task is not task:
                        return
                    delay = retry_delay
                    if isinstance(exc, RustAppHTTPError):
                        retry_after = parse_retry_after(exc.retry_after)
                        if retry_after is not None:
                            delay = max(delay, retry_after)
                        elif exc.status_code in (401, 403, 429):
                            delay = SNAPSHOT_RETRY_FALLBACK_SECONDS
                    retry_delay = min(retry_delay * 2, SNAPSHOT_RETRY_FALLBACK_SECONDS)
                    self._snapshot_retry_not_before = loop.time() + delay
                    logger.warning("HTTP-сверка игроков не удалась: %s. Пауза %.1f с.", exc, delay)
                    continue

                if self._snapshot_task is task:
                    statuses = {
                        player.steam_id: player.status
                        for player in players
                        if player.status in ("online", "afk", "joining", "offline")
                    }
                    self._sessions.seed_missing(statuses)
                    await self._persist_snapshot(
                        [player for player in players if player.status in ("online", "afk")],
                        observed_at=observed_at,
                    )
                    servers = {
                        player.steam_id: (player.server_id, player.last_no_license)
                        for player in players
                        if player.server_id is not None and player.status in ("online", "afk")
                    }
                    await self._update_presence(
                        statuses,
                        presence_at,
                        seed=True,
                        initial=not refresh,
                        server_info=servers,
                    )
                    self._snapshot_loaded = True
                return
        finally:
            if self._snapshot_task is task:
                self._snapshot_task = None
                if self._snapshot_queued and self._snapshot_loaded:
                    self._snapshot_task = self._tasks.create_task(self._load_snapshot(refresh=True))

    async def monitor_connection(self, client: RustAppRealtimeClient) -> None:
        saved = 0
        while not self._stopped.is_set():
            if self._subscribed:
                confirmed = max(self._last_signal_at, client.last_ping_at)
                if time_ns() // 1_000_000 - confirmed > PRESENCE_TIMEOUT_MS or (
                    saved and confirmed - saved > PRESENCE_TIMEOUT_MS
                ):
                    logger.warning("RustApp WebSocket: время ожидания подтверждения связи истекло")
                    self._stopped.set()
                    return
                if self._snapshot_loaded and confirmed > saved:
                    try:
                        async with self._db_sessions.begin() as db:
                            await confirm_presence_connection(db, self._project_id, confirmed)
                    except (OSError, SQLAlchemyError) as exc:
                        logger.warning(
                            "Не удалось подтвердить связь слушателя (%s)", type(exc).__name__
                        )
                    else:
                        saved = confirmed
            await asyncio.sleep(5)

    async def clear_presence(self) -> None:
        try:
            async with self._db_sessions.begin() as db:
                await invalidate_presence(db, self._project_id, time_ns())
        except (OSError, SQLAlchemyError) as exc:
            logger.warning("Не удалось очистить онлайн-статусы (%s)", type(exc).__name__)

    async def _fetch_snapshot(self) -> list[RustAppPlayer]:
        players: dict[str, RustAppPlayer] = {}
        page_number = 0
        while True:
            page = await fetch_players_page(
                self._court_client, page=page_number, limit=MAX_PLAYERS_PAGE_SIZE
            )
            if page.page != page_number:
                raise RustAppResponseError("RustApp вернул страницу с неожиданным номером")
            if any(player.project_id != self._project_id for player in page.results):
                raise RustAppResponseError("RustApp вернул игроков из другого проекта")
            previous_count = len(players)
            players.update((player.steam_id, player) for player in page.results)
            if len(page.results) < page.limit:
                logger.info("HTTP-сверка завершена | игроков в снимке: %d", len(players))
                return list(players.values())
            if len(players) == previous_count:
                raise RustAppResponseError("RustApp повторил страницу списка игроков")
            page_number += 1

    async def _persist_snapshot(
        self, players: list[RustAppPlayer], *, observed_at: datetime
    ) -> None:
        while True:
            try:
                count = await save_player_snapshot(
                    self._db_sessions,
                    project_id=self._project_id,
                    players=players,
                    observed_at=observed_at,
                )
            except (OSError, SQLAlchemyError) as exc:
                logger.warning(
                    "Запись снимка в PostgreSQL не удалась (%s). Повтор через %d с.",
                    type(exc).__name__,
                    SNAPSHOT_DB_RETRY_SECONDS,
                )
                await asyncio.sleep(SNAPSHOT_DB_RETRY_SECONDS)
            else:
                logger.info("Снимок обработан в PostgreSQL | Игроков: %d", count)
                return

    async def _update_presence(
        self,
        statuses: Mapping[str, PlayerStatus],
        observed_at: int,
        *,
        joined: tuple[str, ...] = (),
        discovered: tuple[str, ...] = (),
        seed: bool = False,
        initial: bool = False,
        server_info: Mapping[str, tuple[int, bool | None]] | None = None,
    ) -> None:
        while True:
            try:
                async with self._db_sessions.begin() as db:
                    online, resumed = await save_presence(
                        db,
                        self._project_id,
                        statuses,
                        observed_at,
                        complete_snapshot=seed,
                        server_info=server_info,
                    )
            except (SQLAlchemyError, OSError) as exc:
                logger.warning("Не удалось сохранить статусы игроков (%s)", type(exc).__name__)
                await asyncio.sleep(SNAPSHOT_DB_RETRY_SECONDS)
            else:
                break

        checks = online if seed else online.intersection(joined)
        checks |= online.intersection(discovered) - resumed
        restart = resumed - set(joined)
        while True:
            try:
                async with self._db_sessions.begin() as db:
                    if seed or "offline" in statuses.values():
                        await remove_offline_jobs(db, self._project_id, observed_at)
                    await enqueue(db, self._project_id, checks - restart, seed=seed and not initial)
                    await enqueue(db, self._project_id, restart, seed=False)
            except (SQLAlchemyError, OSError) as exc:
                logger.warning("Не удалось обновить очередь игроков (%s)", type(exc).__name__)
                await asyncio.sleep(SNAPSHOT_DB_RETRY_SECONDS)
            else:
                if restart:
                    logger.info(
                        "RustApp: проверки возобновлены после AFK | игроков=%d", len(restart)
                    )
                return

    async def on_publication(self, ctx: PublicationContext) -> None:
        try:
            update = RustAppPlayerStatusUpdate.model_validate(ctx.pub.data)
        except ValidationError:
            logger.warning("Неизвестный формат публикации статусов")
            return

        logger.debug("RustApp WebSocket: получены статусы | игроков=%d", len(update.players))
        observed_at = time_ns()
        joined, discovered = self._sessions.apply(update.players)
        await self._update_presence(
            update.players, observed_at, joined=tuple(joined), discovered=tuple(discovered)
        )
        self._last_signal_at = max(self._last_signal_at, observed_at // 1_000_000)
        if joined or discovered:
            if self._snapshot_task is not None:
                self._snapshot_queued = True
            elif self._snapshot_loaded:
                self._snapshot_task = self._tasks.create_task(self._load_snapshot(refresh=True))
        if "offline" in update.players.values():
            logger.debug("RustApp: обработаны выходы игроков")
        for steam_id in joined:
            logger.info("Подтверждён заход  |  SteamID:  %s", steam_id)


async def run_listener() -> int:
    try:
        settings = get_settings()
    except (SettingsError, ValidationError):
        logger.info("Не удалось загрузить настройки RustApp")
        return 1

    try:
        database_settings = DatabaseSettings()
    except (SettingsError, ValidationError):
        logger.error("Не удалось загрузить настройки PostgreSQL")
        return 1

    stopped = asyncio.Event()
    channel = f"project:player-status/{settings.rustapp_project_id}"

    async with (
        open_database_sessions(database_settings) as db_sessions,
        create_rustapp_http_client(
            settings, base_url=RUSTAPP_CORE_URL, request_log=db_sessions
        ) as http,
        create_rustapp_http_client(settings, request_log=db_sessions) as court_http,
        asyncio.TaskGroup() as tasks,
    ):
        core_client = RustAppClient(http)

        token_request_lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        retry_not_before = 0.0

        async def get_token() -> str:
            nonlocal retry_not_before

            async with token_request_lock:
                remaining = retry_not_before - loop.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                try:
                    return await fetch_centrifugo_token(core_client)
                except RustAppHTTPError as exc:
                    if exc.status_code in (401, 402, 403):
                        logger.error(
                            "RustApp отклонил выдучу WS-токена | HTTP: %d", exc.status_code
                        )
                        raise UnauthorizedError() from None

                    if exc.status_code == 429 or (
                        exc.status_code == 503 and exc.retry_after is not None
                    ):
                        delay = parse_retry_after(exc.retry_after)
                        if delay is None:
                            delay = TOKEN_RETRY_FALLBACK_SECONDS
                        retry_not_before = loop.time() + delay
                        logger.warning(
                            "Выдача WS-токена: HTTP %d. Пауза %.1f с.",
                            exc.status_code,
                            delay,
                        )
                    raise

        # WebSocket Client
        client = RustAppRealtimeClient(
            RUSTAPP_WS_URL,
            request_log=db_sessions,
            account=account_key(settings.rustapp_access_token.get_secret_value()),
            events=ConnectionEvents(stopped),
            get_token=get_token,
            headers={"Origin": "https://rustapp.io"},
            proxy=None,
            min_reconnect_delay=2.0,
            max_reconnect_delay=60.0,
        )
        status_events = PlayerStatusEvents(
            stopped,
            court_client=RustAppClient(court_http),
            db_sessions=db_sessions,
            project_id=settings.rustapp_project_id,
            tasks=tasks,
        )
        subscription = client.new_subscription(channel, events=status_events)
        heartbeat = tasks.create_task(status_events.monitor_connection(client))

        try:
            await client.connect()
            if not stopped.is_set():
                await subscription.subscribe()
                await stopped.wait()
        except CentrifugeError as exc:
            logger.error("Слушатель RustApp завершился: %s", type(exc).__name__)
        finally:
            heartbeat.cancel()
            status_events.cancel_snapshot()
            await client.disconnect()
            await status_events.clear_presence()

    return 1


if __name__ == "__main__":
    configure_console_logging()
    try:
        raise SystemExit(asyncio.run(run_listener()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
