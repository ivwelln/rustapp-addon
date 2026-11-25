import asyncio
import logging
from datetime import UTC, datetime

from centrifuge import (
    CentrifugeError,
    Client,
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
from app.clients.rustapp.retry_after import parse_retry_after
from app.core.config import DatabaseSettings, get_settings
from app.crud.moderation import enqueue
from app.db.session import open_database_sessions
from app.schemas.rustapp.players import MAX_PLAYERS_PAGE_SIZE, RustAppPlayer, RustAppPlayersPage
from app.schemas.rustapp.realtime import PlayerStatus, RustAppPlayerStatusUpdate
from app.services.rustapp.player_sessions import PlayerSessionTracker
from app.services.rustapp.player_snapshots import save_player_snapshot

logger = logging.getLogger(__name__)

RUSTAPP_WS_URL = "wss://centrifugo.rustapp.io/connection/websocket"
TOKEN_RETRY_FALLBACK_SECONDS = 300.0
SNAPSHOT_RETRY_FALLBACK_SECONDS = 300.0
SNAPSHOT_MIN_RETRY_SECONDS = 30.0
SNAPSHOT_DB_RETRY_SECONDS = 30


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
        self._snapshot_task: asyncio.Task[None] | None = None

    def cancel_snapshot(self) -> None:
        if self._snapshot_task is not None:
            self._snapshot_task.cancel()
            self._snapshot_task = None

    async def on_subscribing(self, ctx: SubscribingContext) -> None:
        self.cancel_snapshot()

    async def on_subscribed(self, ctx: SubscribedContext) -> None:
        if not ctx.recovered:
            self._sessions.reset()
            self._snapshot_loaded = False

        if not self._snapshot_loaded:
            self.cancel_snapshot()
            self._snapshot_task = self._tasks.create_task(self._load_snapshot())

        logger.info("Подписка активна: %s", ctx.channel)

    async def on_unsubscribed(self, ctx: UnsubscribedContext) -> None:
        self.cancel_snapshot()
        logger.info("Подписка завершилась: %d", ctx.code)
        self._stopped.set()

    async def _load_snapshot(self) -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()

        try:
            while self._snapshot_task is task:
                remaining = self._snapshot_retry_not_before - loop.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue

                observed_at = datetime.now(UTC)
                try:
                    page = await fetch_players_page(
                        self._court_client, page=0, limit=MAX_PLAYERS_PAGE_SIZE
                    )
                except RustAppHTTPError as exc:
                    if self._snapshot_task is not task:
                        return
                    if exc.status_code != 429 and (
                        exc.status_code != 503 or exc.retry_after is None
                    ):
                        raise

                    delay = parse_retry_after(exc.retry_after)
                    if delay is None:
                        delay = SNAPSHOT_RETRY_FALLBACK_SECONDS
                    delay = max(SNAPSHOT_MIN_RETRY_SECONDS, delay)
                    self._snapshot_retry_not_before = loop.time() + delay
                    logger.warning("HTTP-сверка: HTTP %d. Пауза %.1f с.", exc.status_code, delay)
                    continue

                if self._snapshot_task is task:
                    players = self._apply_snapshot(page)
                    await self._persist_snapshot(players, observed_at=observed_at)
                    await self._enqueue_players([p.steam_id for p in players], seed=True)
                    self._snapshot_loaded = True
                return
        except RustAppError as exc:
            if self._snapshot_task is task:
                logger.warning("HTTP-сверка игроков не удалась: %s", exc)
        finally:
            if self._snapshot_task is task:
                self._snapshot_task = None

    def _apply_snapshot(self, page: RustAppPlayersPage) -> list[RustAppPlayer]:
        if page.page != 0:
            raise RustAppResponseError("RustApp вернул страницу с неожиданным номером")
        if any(player.project_id != self._project_id for player in page.results):
            raise RustAppResponseError("RustApp вернул игроков из другого проекта")

        snapshot: dict[str, PlayerStatus] = {}
        players: list[RustAppPlayer] = []
        for player in page.results:
            status = player.status
            if status == "online" or status == "afk":
                snapshot[player.steam_id] = status
                players.append(player)

        if len(page.results) >= page.limit:
            logger.warning("Страница HTTP-сверки заполнена. Список может быть неполным.")

        self._sessions.seed_missing(snapshot)
        logger.info("HTTP-сверка завершена | Игроков в снимке: %d", len(snapshot))
        return players

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

    async def _enqueue_players(self, players: list[str], *, seed: bool) -> None:
        while True:
            try:
                async with self._db_sessions.begin() as db:
                    await enqueue(db, self._project_id, players, seed=seed)
            except (SQLAlchemyError, OSError) as exc:
                logger.warning("Не удалось поставить игроков в очередь (%s)", type(exc).__name__)
                await asyncio.sleep(SNAPSHOT_DB_RETRY_SECONDS)
            else:
                return

    async def on_publication(self, ctx: PublicationContext) -> None:
        try:
            update = RustAppPlayerStatusUpdate.model_validate(ctx.pub.data)
        except ValidationError:
            logger.warning("Неизвестный формат публикации статусов")
            return

        logger.debug("Статусы игроков: %s", update.players)
        joined = self._sessions.apply(update.players)
        if joined:
            await self._enqueue_players(joined, seed=False)
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
        create_rustapp_http_client(settings, base_url=RUSTAPP_CORE_URL) as http,
        create_rustapp_http_client(settings) as court_http,
        open_database_sessions(database_settings) as db_sessions,
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
        client = Client(
            RUSTAPP_WS_URL,
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

        try:
            await client.connect()
            if not stopped.is_set():
                await subscription.subscribe()
                await stopped.wait()
        except CentrifugeError as exc:
            logger.error("Слушатель RustApp завершился: %s", type(exc).__name__)
        finally:
            status_events.cancel_snapshot()
            await client.disconnect()

    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        raise SystemExit(asyncio.run(run_listener()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
