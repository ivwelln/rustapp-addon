import logging

import httpx2
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)


class PermitResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    allowed: bool


class BanGuard:
    def __init__(self, http: httpx2.AsyncClient, project_id: int) -> None:
        self._http = http
        self._project_url = f"/projects/{project_id}"
        self._unavailable = False

    async def available(self) -> bool:
        return await self._ask("GET", "capacity")

    async def claim(self, decision_id: int, pattern: str, ban_ip: str) -> bool:
        return await self._ask(
            "POST",
            "claims",
            json={"decision_id": decision_id, "pattern": pattern, "ban_ip": ban_ip},
        )

    async def _ask(self, method: str, action: str, *, json: dict | None = None) -> bool:
        try:
            response = await self._http.request(method, f"{self._project_url}/{action}", json=json)
            response.raise_for_status()
            permit = PermitResponse.model_validate_json(response.content)
        except (httpx2.HTTPError, ValidationError):
            if not self._unavailable:
                logger.error(
                    "Сервис аварийного лимита недоступен. Отправка автобанов приостановлена"
                )
            self._unavailable = True
            return False
        if self._unavailable:
            logger.info("Связь с сервисом аварийного лимита восстановлена")
        self._unavailable = False
        return permit.allowed
