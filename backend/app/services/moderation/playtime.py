from dataclasses import dataclass

from app.models.player_data import PlayerPresence
from app.services.moderation.facts import MINUTE

YEAR_MS = 365 * 24 * 60 * MINUTE
WHITELIST_PLAYTIME_MS = 3 * 60 * MINUTE


@dataclass(frozen=True)
class LivePlaytime:
    started_at: int
    duration: int
    observed_at: int
    server_id: int

    def within_year(self, now: int) -> int:
        return max(0, self.started_at + self.duration - max(self.started_at, now - YEAR_MS))

    def matches(self, presence: PlayerPresence) -> bool:
        return (
            presence.status in ("online", "afk")
            and presence.active_since is not None
            and presence.active_since <= self.observed_at
            and presence.server_id in (None, self.server_id)
            and (presence.joined_at is None or self.started_at + MINUTE >= presence.joined_at)
        )


def live_playtime(details: dict, observed_at: int) -> LivePlaytime | None:
    player, entity = details.get("player"), details.get("entity")
    if not isinstance(player, dict) or not isinstance(entity, dict):
        return None
    if player.get("status") not in ("online", "afk"):
        return None
    started_at, seconds, server = (
        entity.get("connected_at"),
        entity.get("seconds_connected"),
        entity.get("server_id"),
    )
    if (
        type(started_at) is not int
        or not 0 < started_at <= observed_at
        or type(seconds) is not int
        or not 0 <= seconds * 1000 <= observed_at - started_at + 5000
        or type(server) is not int
        or server != player.get("server_id")
    ):
        return None
    return LivePlaytime(
        started_at, min(seconds * 1000, observed_at - started_at), observed_at, server
    )
