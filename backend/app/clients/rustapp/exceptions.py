class RustAppError(Exception):
    """Ошибка обращения к RustApp."""


class RustAppTransportError(RustAppError):
    """Не удалось получить ответ от RustApp."""


class RustAppTimeoutError(RustAppTransportError):
    """Истекло время ожидания при обращении к RustApp."""


class RustAppHTTPError(RustAppError):
    """RustApp вернул ответ с неуспешным HTTP-статусом."""

    def __init__(self, status_code: int, *, retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"RustApp вернул HTTP {status_code}")


class RustAppResponseError(RustAppError):
    """RustApp вернул ответ неожиданного формата."""


class PlayerNotOnline(RustAppError):
    """Персональный запрос отменён, пока не подтверждён статус online."""
