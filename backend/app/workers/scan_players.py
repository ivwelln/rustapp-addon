import asyncio
import logging
from argparse import ArgumentParser

from pydantic import ValidationError
from pydantic_settings import SettingsError

from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.clients.rustapp.exceptions import (
    RustAppError,
    RustAppHTTPError,
    RustAppResponseError,
    RustAppTransportError,
)
from app.clients.rustapp.players import fetch_players_page
from app.clients.rustapp.retry_after import parse_retry_after
from app.core.config import Settings, get_settings
from app.schemas.rustapp.players import MAX_PLAYERS_PAGE_SIZE, RustAppPlayersPage

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 120.0
RETRY_FALLBACK_SECONDS = 300.0


async def scan_players(client: RustAppClient, *, project_id: int) -> None:
    page: RustAppPlayersPage = await fetch_players_page(client, page=0, limit=MAX_PLAYERS_PAGE_SIZE)

    if any(player.project_id != project_id for player in page.results):
        raise RustAppResponseError("RustApp вернул игроков из другого проекта")

    unique_players = {player.steam_id for player in page.results}

    logger.info(
        "Проект #%d  |  Страница [%d]  |  Игроков:  %d",
        project_id,
        page.page,
        len(unique_players),
    )

    if len(page.results) == page.limit:
        logger.info(
            "Страница полностью заполнена. Возможно, в RustApp есть еще страницы с игроками."
        )


def retry_delay(retry_after: str | None) -> float:
    seconds = parse_retry_after(retry_after)
    if seconds is None:
        return RETRY_FALLBACK_SECONDS

    return max(POLL_INTERVAL_SECONDS, seconds)


async def poll_players(client: RustAppClient, *, project_id: int) -> None:
    while True:
        delay = POLL_INTERVAL_SECONDS
        try:
            await scan_players(client, project_id=project_id)
        except RustAppHTTPError as exc:
            if exc.status_code != 429 and not 500 <= exc.status_code < 600:
                raise
            if exc.status_code == 429 or exc.retry_after is not None:
                delay = retry_delay(exc.retry_after)
            logger.warning("RustApp вернул HTTP %d. Пауза %.1f c.", exc.status_code, delay)
        except RustAppTransportError as exc:
            logger.warning("%s. Пауза %.1f c.", exc, delay)

        await asyncio.sleep(delay=delay)


async def run_worker(*, continuous: bool = False) -> int:
    try:
        settings: Settings = get_settings()
    except (SettingsError, ValidationError):
        logger.error("Не удалось загрузить настройки RustApp.")
        return 1

    try:
        async with create_rustapp_http_client(settings) as http:
            client = RustAppClient(http)
            if continuous:
                await poll_players(client, project_id=settings.rustapp_project_id)
            else:
                await scan_players(client, project_id=settings.rustapp_project_id)
    except RustAppError as exc:
        logger.error("Не удалось получить список игроков: %s", exc)
        return 1
    return 0


def main() -> int:
    parser = ArgumentParser(description="Опрос списка игроков RustApp")
    parser.add_argument("--loop", action="store_true", help="Повторять запрос каждые 120 секунд")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        return asyncio.run(run_worker(continuous=args.loop))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
