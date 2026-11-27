import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rustapp.client import RustAppClient
from app.clients.rustapp.exceptions import RustAppHTTPError, RustAppResponseError
from app.clients.rustapp.moderation import read
from app.crud.moderation import archive, now_ms
from app.crud.players import whitelist_for_playtime
from app.models import Decision, PlaytimeDay
from app.services.moderation.playtime import WHITELIST_PLAYTIME_MS, YEAR_MS, LivePlaytime
from app.services.research.playtime import DAY, HOUR, cache_days, daily_playtime, save_annual

logger = logging.getLogger(__name__)


async def cached_project_ms(
    db: AsyncSession, project: int, sid: str, now: int, *, before: int | None = None
) -> int:
    # A partial cache can prove that the player is protected; it cannot clear them for a ban.
    return int(
        await db.scalar(
            select(func.sum(PlaytimeDay.played_ms)).where(
                PlaytimeDay.project_id == project,
                PlaytimeDay.steam_id == sid,
                PlaytimeDay.day >= now - YEAR_MS,
                PlaytimeDay.day + DAY <= (now if before is None else before),
            )
        )
        or 0
    )


def annual_ms(payload: dict, sid: str, start: int, end: int) -> int:
    days = daily_playtime(payload, sid)
    expected = set(range(start // DAY * DAY, end // DAY * DAY + DAY, DAY))
    total = payload["playTime"].get("total")
    if (
        payload["kills"].get("steam_id") != sid
        or set(days) != expected
        or type(total) not in (float, int)
        or not 0 <= total <= 24 * len(expected)
        or abs(total * HOUR - sum(days.values())) > len(expected)
    ):
        raise RustAppResponseError("Не подтверждена полная годовая статистика игрока")
    return max(round(total * HOUR), sum(days.values()))


def hours_proof_is_current(proof: dict | None, now: int) -> bool:
    if not proof or proof.get("outcome") != "eligible" or proof.get("pirated") is not True:
        return False
    checked_at, duration = proof.get("checked_at"), proof.get("project_ms")
    if type(checked_at) is not int or type(duration) is not int or duration < 0:
        return False
    elapsed = now - checked_at
    return 0 <= elapsed <= 30_000 and duration + elapsed < WHITELIST_PLAYTIME_MS


async def verify_ban_hours(
    db: AsyncSession, client: RustAppClient, decision: Decision, live: LivePlaytime | None
) -> bool:
    project, sid, checked_at = decision.project_id, decision.steam_id, now_ms()
    live_ms = live.within_year(checked_at) if live else 0
    params = {
        "time_from": checked_at - YEAR_MS + 1,
        "time_to": live.started_at - 1 if live else checked_at,
    }
    if live_ms >= WHITELIST_PLAYTIME_MS:
        return await record_ban_hours(db, decision, live, live_ms, live_ms, params)
    # End before the active connection so later updates cannot count its time twice.
    try:
        raw = await read(client, f"/players/{sid}/stats", **params)
        duration = annual_ms(raw, sid, params["time_from"], params["time_to"])
    except RustAppHTTPError as exc:
        if exc.status_code not in (400, 404, 422):
            raise
        decision.eligibility = {
            "checked_at": checked_at,
            "outcome": "stats_unavailable",
            "http_status": exc.status_code,
            **params,
        }
        decision.state = "cancelled"
        logger.warning(
            "Бан отменён: годовая статистика недоступна | SteamID=%s HTTP=%s", sid, exc.status_code
        )
        return False
    except (RustAppResponseError, ValueError):
        decision.eligibility = {"checked_at": checked_at, "outcome": "incomplete_stats", **params}
        decision.state = "cancelled"
        logger.warning("Бан отменён: годовые часы не подтверждены | SteamID=%s", sid)
        return False
    await archive(db, project, sid, "research-stats-window", {"params": params, "response": raw})
    await cache_days(db, project, sid, raw, params, now_ms())
    await save_annual(
        db, project, sid, duration, checked_at, before=live.started_at if live else None
    )
    return await record_ban_hours(db, decision, live, duration + live_ms, live_ms, params)


async def record_ban_hours(
    db: AsyncSession,
    decision: Decision,
    live: LivePlaytime | None,
    total: int,
    live_ms: int,
    params: dict,
) -> bool:
    protected = total >= WHITELIST_PLAYTIME_MS
    decision.eligibility = {
        "checked_at": live.observed_at if live else now_ms(),
        "pirated": True,
        "project_ms": total,
        "active_session_ms": live_ms if live else None,
        "outcome": "protected" if protected else "eligible" if live else "session_unavailable",
        **params,
    }
    if protected:
        await whitelist_for_playtime(db, decision.project_id, decision.steam_id)
        logger.info(
            "Бан отменён: белый список по часам RustApp | SteamID=%s часы=%.2f",
            decision.steam_id,
            total / HOUR,
        )
    elif live is None:
        logger.info(
            "Бан отменён: RustApp не подтвердил длительность текущей сессии | SteamID=%s",
            decision.steam_id,
        )
    else:
        return True
    decision.state = "cancelled"
    return False
