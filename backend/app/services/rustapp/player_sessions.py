from collections.abc import Mapping

from app.schemas.rustapp.realtime import PlayerStatus


class PlayerSessionTraker:
    def __init__(self) -> None:
        self._statuses: dict[str, PlayerStatus] = {}

    def apply(self, statuses: Mapping[str, PlayerStatus]) -> list[str]:
        joined: list[str] = []

        for steamd_id, status in statuses.items():
            previous = self._statuses.get(steamd_id)
            self._statuses[steamd_id] = status
            if previous in ("joining", "offline") and status in ("afk", "online"):
                joined.append(steamd_id)

        return joined

    def reset(self) -> None:
        self._statuses.clear()
