from sqlalchemy import BigInteger, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CombatKill, Player, PlayerData, PlayerPresence

DAY_MS = 24 * 60 * 60 * 1000


async def online_players(db: AsyncSession, project: int, now_ms: int) -> list[dict]:
    recent = now_ms - DAY_MS
    present = PlayerPresence.status.in_(("online", "afk"))
    kills = (
        select(CombatKill.steam_id, func.count().label("count"))
        .join(
            PlayerPresence,
            (PlayerPresence.project_id == CombatKill.project_id)
            & (PlayerPresence.steam_id == CombatKill.steam_id),
        )
        .where(CombatKill.project_id == project, CombatKill.at.between(recent, now_ms), present)
        .group_by(CombatKill.steam_id)
        .subquery()
    )

    overview = PlayerData.responses["browse-full"]
    details = PlayerData.responses["details"]
    latest_overview = func.coalesce(overview["observed_at"].astext.cast(BigInteger), 0)
    latest_details = func.coalesce(details["observed_at"].astext.cast(BigInteger), 0)
    browse_payload = overview["payload"]
    details_payload = details["payload"]["player"]

    def profile_field(*keys: str):
        browse_field, details_field = browse_payload, details_payload
        for key in keys:
            browse_field = browse_field[key]
            details_field = details_field[key]
        browse_text = func.nullif(browse_field.astext, "")
        details_text = func.nullif(details_field.astext, "")
        return case(
            (latest_details >= latest_overview, func.coalesce(details_text, browse_text)),
            else_=func.coalesce(browse_text, details_text),
        )

    name = profile_field("steam_name")
    trusted = func.coalesce(Player.is_whitelisted, False)
    last_feed_at = PlayerData.responses["kill-feed"]["observed_at"].astext.cast(BigInteger)
    rows = (
        await db.execute(
            select(
                PlayerPresence.steam_id,
                PlayerPresence.status,
                trusted.label("trusted"),
                name.label("name"),
                profile_field("ip_details", "provider").label("provider"),
                profile_field("ip_details", "country_code").label("country_code"),
                profile_field("ip_details", "country_name").label("country_name"),
                case(
                    (kills.c.count.is_not(None), kills.c.count),
                    (last_feed_at >= recent, 0),
                ).label("kills"),
            )
            .select_from(PlayerPresence)
            .outerjoin(
                Player,
                (Player.project_id == PlayerPresence.project_id)
                & (Player.steam_id == PlayerPresence.steam_id),
            )
            .outerjoin(
                PlayerData,
                (PlayerData.project_id == PlayerPresence.project_id)
                & (PlayerData.steam_id == PlayerPresence.steam_id),
            )
            .outerjoin(kills, kills.c.steam_id == PlayerPresence.steam_id)
            .where(PlayerPresence.project_id == project, present)
            .order_by(trusted, func.lower(name).asc().nulls_last(), PlayerPresence.steam_id)
        )
    ).mappings()
    return [dict(row) for row in rows]
