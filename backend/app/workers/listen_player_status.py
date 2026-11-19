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
from app.clients.rustapp.exceptions import RustAppHTTPError
from app.clients.rustapp.handshake import fetch_centrifugo_token
from app.clients.rustapp.retry_after import parse_retry_after
from app.core.config import get_settings
from app.schemas.rustapp.realtime import RustAppPlayerStatusUpdate
from app.services.rustapp.player_sessions import PlayerSessionTraker

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
    def __init__(self, stopped: asyncio.Event) -> None:
        self._stopped = stopped
        self._sessions = PlayerSessionTraker()

    async def on_subscribed(self, ctx: SubscribedContext) -> None:
        if not ctx.recovered:
            self._sessions.reset()
        logger.info("Подписка активна: %s", ctx.channel)

    async def on_unsubscribed(self, ctx: UnsubscribedContext) -> None:
        logger.info("Подписка завершилась: %d", ctx.code)
        self._stopped.set()

    async def on_publication(self, ctx: PublicationContext) -> None:
        try:
            update = RustAppPlayerStatusUpdate.model_validate(ctx.pub.data)
        except ValidationError:
            logger.warning("Неизвестный формат публикации статусов")
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

    async with create_rustapp_http_client(settings, base_url=RUSTAPP_CORE_URL) as http:
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
        subscription = client.new_subscription(channel, events=PlayerStatusEvents(stopped))

        try:
            await client.connect()
            if not stopped.is_set():
                await subscription.subscribe()
                await stopped.wait()
        except CentrifugeError as exc:
            logger.error("Слушатель RustApp завершился: %s", type(exc).__name__)
        finally:
            await client.disconnect()

    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        raise SystemExit(asyncio.run(run_listener()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
