import asyncio
import logging

from pydantic import ValidationError
from pydantic_settings import SettingsError

from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.clients.rustapp.exceptions import RustAppError, RustAppResponseError
from app.clients.rustapp.players import fetch_players_page
from app.core.config import Settings, get_settings
from app.schemas.rustapp.players import MAX_PLAYERS_PAGE_SIZE, RustAppPlayersPage

logger = logging.getLogger(__name__)


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


async def run_worker() -> int:
    try:
        settings: Settings = get_settings()
    except (SettingsError, ValidationError):
        logger.error("Не удалось загрузить настройки RustApp.")
        return 1

    try:
        async with create_rustapp_http_client(settings) as http:
            client = RustAppClient(http)
            await scan_players(client, project_id=settings.rustapp_project_id)
    except RustAppError as exc:
        logger.error("Не удалось получить список игроков: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(asyncio.run(run_worker()))
