from collections.abc import Mapping

import httpx2

from app.clients.rustapp.exceptions import (
    RustAppHTTPError,
    RustAppTimeoutError,
    RustAppTransportError,
)
from app.core.config import Settings

RUSTAPP_BASE_URL = httpx2.URL("https://court.rustapp.io")


def create_rustapp_http_client(
    settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url=RUSTAPP_BASE_URL,
        headers={
            "Authorization": f"Bearer {settings.rustapp_access_token.get_secret_value()}",
            "project-id": str(settings.rustapp_project_id),
            "Accept": "application/json",
        },
        timeout=httpx2.Timeout(settings.rustapp_timeout),
        limits=httpx2.Limits(max_connections=5, max_keepalive_connections=5),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


class RustAppClient:
    def __init__(self, http: httpx2.AsyncClient) -> None:
        self._http = http

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> httpx2.Response:
        request = self._http.build_request("GET", path, params=params)

        if (
            request.url.scheme != RUSTAPP_BASE_URL.scheme
            or request.url.host != RUSTAPP_BASE_URL.host
            or request.url.port != RUSTAPP_BASE_URL.port
            or request.url.userinfo
        ):
            raise ValueError("Клиент допускает запросы только к https://court.rustapp.io")

        try:
            response = await self._http.send(request, follow_redirects=False)
        except httpx2.TimeoutException:
            raise RustAppTimeoutError("Истекло время ожидания ответа от RustApp") from None
        except httpx2.RequestError:
            raise RustAppTransportError("Не удалось выполнить запрос к RustApp") from None

        if not response.is_success:
            raise RustAppHTTPError(
                response.status_code,
                retry_after=response.headers.get("Retry-after"),
            )

        return response
