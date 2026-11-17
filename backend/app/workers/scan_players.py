import asyncio
import logging

from pydantic import ValidationError
from pydantic_settings import SettingsError

from app.core.config import get_settings, Settings
from app.clients.rustapp.client import create_rustapp_http_client, RustAppClient
from app.clients.rustapp.exceptions import RustAppError, RustAppResponseError
from app.clients.rustapp.players import fetch_players_page
from app.schemas.rustapp.players import RustAppPlayer, RustAppPlayersPage

logger = logging.getLogger(__name__)

async def scan_once() -> int:
    try:
        settings: Settings = get_settings()
    except (SettingsError, ValidationError):
        logger.error("Не удалось загрузить настройки RustApp.")
        return 1
    
    try:
        async with create_rustapp_http_client(settings) as http:
            page: RustAppPlayersPage = await fetch_players_page(RustAppClient(http), page=0, limit=200)
            
        if any(player.project_id != settings.rustapp_project_id for player in page.results):
            raise RustAppResponseError("RustApp вернул игроков из другого проекта")
    except RustAppError as exc:
        logger.error("Не удалось получить список игроков: %s", exc)
        return 1
    
    unique_players = {player.steam_id for player in page.results}
    logger.info(
        "Проект #%d  |  Страница [%d]  |  Игроков:  %d",
        settings.rustapp_project_id,
        page.page,
        len(unique_players)
    )
    
    if len(page.results) == page.limit:
        logger.info("Страница полностью заполнена. Возможно, в RustApp есть еще страницы с игроками.")
        
    return 0

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(asyncio.run(scan_once()))