from pydantic import ValidationError

from app.clients.rustapp.client import RustAppClient
from app.clients.rustapp.exceptions import RustAppResponseError
from app.schemas.rustapp.realtime import RustAppHandshake


async def fetch_centrifugo_token(client: RustAppClient) -> str:
    response = await client.get("/auth/handshake")
    try:
        handshake = RustAppHandshake.model_validate_json(response.content)
    except ValidationError:
        raise RustAppResponseError("RustApp не вернул токен Centrifugo") from None

    return handshake.centrifugo_token.get_secret_value()
