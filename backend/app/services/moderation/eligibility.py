import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rustapp.client import RustAppClient
from app.clients.rustapp.exceptions import RustAppHTTPError, RustAppResponseError
from app.clients.rustapp.moderation import read
from app.crud.moderation import archive, now_ms
from app.crud.players import whitelist_for_playtime
from app.models import Decision, PlaytimeDay
from app.services.moderation.playtime import WHITELIST_PLAYTIME_MS, YEAR_MS
from app.services.research.playtime import DAY, HOUR, cache_days, daily_playtime

logger = logging.getLogger(__name__)


async def cached_project_ms(db: AsyncSession, project: int, sid: str, now: int) -> int:
    # A partial cache can prove that the player is protected; it cannot clear them for a ban.
    return int(
        await db.scalar(
            select(func.sum(PlaytimeDay.played_ms)).where(
                PlaytimeDay.project_id == project,
                PlaytimeDay.steam_id == sid,
                PlaytimeDay.day >= now - YEAR_MS,
                PlaytimeDay.day + DAY <= now,
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


async def verify_ban_hours(db: AsyncSession, client: RustAppClient, decision: Decision) -> bool:
    project, sid, end = decision.project_id, decision.steam_id, now_ms()
    params = {"time_from": end - YEAR_MS + 1, "time_to": end}
    # Always get a fresh upper bound including today immediately before authorizing a ban.
    try:
        raw = await read(client, f"/players/{sid}/stats", **params)
        duration = annual_ms(raw, sid, params["time_from"], end)
    except RustAppHTTPError as exc:
        if exc.status_code not in (400, 404, 422):
            raise
        decision.eligibility = {
            "checked_at": end,
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
        decision.eligibility = {"checked_at": end, "outcome": "incomplete_stats", **params}
        decision.state = "cancelled"
        logger.warning("Бан отменён: годовые часы не подтверждены | SteamID=%s", sid)
        return False
    await archive(db, project, sid, "research-stats-window", {"params": params, "response": raw})
    await cache_days(db, project, sid, raw, params, now_ms())
    protected = duration >= WHITELIST_PLAYTIME_MS
    decision.eligibility = {
        "checked_at": end,
        "pirated": True,
        "project_ms": duration,
        "outcome": "protected" if protected else "eligible",
        **params,
    }
    if protected:
        await whitelist_for_playtime(db, project, sid)
        decision.state = "cancelled"
        logger.info(
            "Бан отменён: белый список по часам RustApp | SteamID=%s часы=%.2f",
            sid,
            duration / HOUR,
        )
    return not protected
