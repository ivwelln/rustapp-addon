import asyncio
import logging

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

from app.clients.rustapp.client import (
    RUSTAPP_CORE_URL,
    RustAppClient,
    create_rustapp_http_client,
)
from app.clients.rustapp.exceptions import RustAppError, RustAppHTTPError, RustAppResponseError
from app.clients.rustapp.handshake import fetch_centrifugo_token
from app.clients.rustapp.players import fetch_players_page
from app.clients.rustapp.retry_after import parse_retry_after
from app.core.config import get_settings
from app.schemas.rustapp.players import MAX_PLAYERS_PAGE_SIZE
from app.schemas.rustapp.realtime import PlayerStatus, RustAppPlayerStatusUpdate
from app.services.rustapp.player_sessions import PlayerSessionTracker

logger = logging.getLogger(__name__)

RUSTAPP_WS_URL = "wss://centrifugo.rustapp.io/connection/websocket"
TOKEN_RETRY_FALLBACK_SECONDS = 300.0


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
        project_id: int,
        tasks: asyncio.TaskGroup,
    ) -> None:
        self._stopped = stopped
        self._sessions = PlayerSessionTracker()
        self._court_client = court_client
        self._project_id = project_id
        self._tasks = tasks
        self._pending_statuses: dict[str, PlayerStatus] | None = None
        self._snapshot_task: asyncio.Task[None] | None = None

    def cancel_snapshot(self) -> None:
        if self._snapshot_task is not None:
            self._snapshot_task.cancel()
            self._snapshot_task = None

    async def on_subscribing(self, ctx: SubscribingContext) -> None:
        self.cancel_snapshot()

    async def on_subscribed(self, ctx: SubscribedContext) -> None:
        if not ctx.recovered or self._pending_statuses is not None:
            self.cancel_snapshot()
            pending: dict[str, PlayerStatus] = {}
            self._pending_statuses = pending
            self._snapshot_task = self._tasks.create_task(self._load_snapshot(pending))
        logger.info("Подписка активна: %s", ctx.channel)

    async def on_unsubscribed(self, ctx: UnsubscribedContext) -> None:
        self.cancel_snapshot()
        logger.info("Подписка завершилась: %d", ctx.code)
        self._stopped.set()

    async def _load_snapshot(self, pending: dict[str, PlayerStatus]) -> None:
        snapshot: dict[str, PlayerStatus] = {}
        loaded = False

        try:
            page = await fetch_players_page(self._court_client, page=0, limit=MAX_PLAYERS_PAGE_SIZE)
            if page.page != 0:
                raise RustAppResponseError("RustApp вернул страницу с неожиданным номером")
            if any(player.project_id != self._project_id for player in page.results):
                raise RustAppResponseError("RustApp вернул игроков из другого проекта")
        except RustAppError as exc:
            logger.warning("HTTP-сверка игроков не удалась: %s", exc)
        else:
            for player in page.results:
                status = player.status
                if status == "online" or status == "afk":
                    snapshot[player.steam_id] = status

            if len(page.results) >= page.limit:
                logger.warning("Страница HTTP-сверки заполнена. Список может быть неполным.")
            loaded = True

        if self._snapshot_task is not asyncio.current_task():
            return

        self._sessions.seed(snapshot | pending)
        self._pending_statuses = None
        self._snapshot_task = None
        if loaded:
            logger.info("HTTP-сверка завершена | Игроков в снимке: %d", len(snapshot))

    async def on_publication(self, ctx: PublicationContext) -> None:
        try:
            update = RustAppPlayerStatusUpdate.model_validate(ctx.pub.data)
        except ValidationError:
            logger.warning("Неизвестный формат публикации статусов")
            return

        if self._pending_statuses is not None:
            self._pending_statuses.update(update.players)
            return

        logger.debug("Статусы игроков: %s", update.players)
        for steam_id in self._sessions.apply(update.players):
            logger.info("Подтверждён заход  |  SteamID:  %s", steam_id)


async def run_listener() -> int:
    try:
        settings = get_settings()
    except (SettingsError, ValidationError):
        logger.info("Не удалось загрузить настройки RustApp")
        return 1

    stopped = asyncio.Event()
    channel = f"project:player-status/{settings.rustapp_project_id}"

    async with (
        create_rustapp_http_client(settings, base_url=RUSTAPP_CORE_URL) as http,
        create_rustapp_http_client(settings) as court_http,
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
