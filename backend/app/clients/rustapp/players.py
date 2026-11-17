from pydantic import ValidationError

from app.clients.rustapp.client import create_rustapp_http_client, RustAppClient
from app.clients.rustapp.exceptions import RustAppResponseError
from app.schemas.rustapp.players import RustAppPlayer, RustAppPlayersPage

async def fetch_players_page(
    client: RustAppClient,
    *,
    page: int,
    limit: int,
) -> RustAppPlayersPage:
    response = await client.get(
        "/players/browse-full",
        params={
            "page": page,
            "limit": limit,
            "offline": False,
            "only_vpn": False,
            "only_report_ignore": False,
            "only_with_comment": False,
            "sort": "created_at"
        },
    )
    
    try:
        return RustAppPlayersPage.model_validate_json(response.content)
    except ValidationError:
        raise RustAppResponseError("Некорректный ответ со списком игроков RustApp") from None