from collections.abc import Mapping

import httpx2

from app.clients.rustapp.exceptions import (
    RustAppHTTPError,
    RustAppTimeoutError,
    RustAppTransportError,
)
from app.core.config import Settings

RUSTAPP_BASE_URL = httpx2.URL("https://court.rustapp.io")
RUSTAPP_CORE_URL = httpx2.URL("https://core.rustapp.io")


def _origin(url: httpx2.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port


_RUSTAPP_ORIGINS = {_origin(RUSTAPP_BASE_URL), _origin(RUSTAPP_CORE_URL)}


def create_rustapp_http_client(
    settings: Settings,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    base_url: httpx2.URL = RUSTAPP_BASE_URL,
) -> httpx2.AsyncClient:
    if base_url not in (RUSTAPP_BASE_URL, RUSTAPP_CORE_URL):
        raise ValueError("Недопустимый адрес RustApp")
    return httpx2.AsyncClient(
        base_url=base_url,
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
        self._expected_origin = _origin(http.base_url)
        if self._expected_origin not in _RUSTAPP_ORIGINS or http.base_url.userinfo:
            raise ValueError("Недопустимый адрес RustApp")
        self._http = http

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> httpx2.Response:
        request = self._http.build_request("GET", path, params=params)

        if _origin(request.url) != self._expected_origin or request.url.userinfo:
            raise ValueError("Запрос выходит за пределы адреса клента RustApp")

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
