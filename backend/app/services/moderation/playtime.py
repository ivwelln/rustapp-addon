from collections.abc import Iterable

from app.services.moderation.facts import MINUTE, Session

YEAR_MS = 365 * 24 * 60 * MINUTE
WHITELIST_PLAYTIME_MS = 3 * 60 * MINUTE


def played_ms(sessions: Iterable[Session], server_id: int, now: int) -> int:
    """Count non-overlapping play intervals in the trailing year."""
    ordered = sorted(sessions, key=lambda session: session.connected_at)
    covered_until = now - YEAR_MS
    total = 0
    for index, session in enumerate(ordered):
        if session.server_id != server_id or session.joined_at is None:
            continue
        following = ordered[index + 1].connected_at if index + 1 < len(ordered) else None
        # A stale open record followed by another connection has no reliable end time.
        if session.disconnected_at is None and following is not None:
            continue
        end = min(session.disconnected_at or now, following or now, now)
        start = max(session.joined_at, covered_until)
        if end > start:
            total += end - start
            covered_until = end
    return total
