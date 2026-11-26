from collections.abc import Mapping

from app.schemas.rustapp.realtime import PlayerStatus


class PlayerSessionTracker:
    def __init__(self) -> None:
        self._statuses: dict[str, PlayerStatus] = {}

    def apply(self, statuses: Mapping[str, PlayerStatus]) -> tuple[list[str], list[str]]:
        joined: list[str] = []
        discovered: list[str] = []

        for steam_id, status in statuses.items():
            previous = self._statuses.get(steam_id)
            self._statuses[steam_id] = status
            if status == "online":
                if previous in ("joining", "offline"):
                    joined.append(steam_id)
                elif previous is None:
                    discovered.append(steam_id)

        return joined, discovered

    def reset(self) -> None:
        self._statuses.clear()

    def seed_missing(self, statuses: Mapping[str, PlayerStatus]) -> None:
        for steam_id, status in statuses.items():
            self._statuses.setdefault(steam_id, status)
