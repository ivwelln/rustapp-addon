from app.services.moderation.facts import MINUTE, Session
from app.services.moderation.playtime import YEAR_MS, played_ms


def test_playtime_uses_joined_at_and_clips_to_server_and_year():
    now = 2 * YEAR_MS
    year_start = now - YEAR_MS

    def session(connected_at, joined_at, disconnected_at, server_id=163):
        return Session(
            project_id=1,
            steam_id="player",
            server_id=server_id,
            connected_at=connected_at,
            joined_at=joined_at,
            disconnected_at=disconnected_at,
        )

    records = [
        session(year_start - 3 * MINUTE, year_start - 2 * MINUTE, year_start + MINUTE),
        session(now - 500 * MINUTE, now - 490 * MINUTE, now - 300 * MINUTE, server_id=164),
        session(now - 250 * MINUTE, None, now - 240 * MINUTE),
        session(now - 220 * MINUTE, now - 219 * MINUTE, None),
        session(now - 120 * MINUTE, now - 110 * MINUTE, now - 30 * MINUTE),
        session(now - 60 * MINUTE, now - 50 * MINUTE, None),
    ]
    assert played_ms(records, 163, now) == (1 + 50 + 50) * MINUTE
    assert played_ms(records, server_id=None, now=now) == (1 + 190 + 50 + 50) * MINUTE
