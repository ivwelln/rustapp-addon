import re

from sqlalchemy import BigInteger, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CollectionJob,
    CombatDeath,
    CombatKill,
    Player,
    PlayerData,
    PlayerPresence,
    PlayerReport,
)

DAY_MS = 24 * 60 * 60 * 1000
STEAM_AVATAR_HOSTS = (
    "avatars.akamai.steamstatic.com",
    "avatars.cloudflare.steamstatic.com",
    "avatars.steamstatic.com",
)
STEAM_AVATAR_URL = re.compile(
    rf"https://(?:{'|'.join(map(re.escape, STEAM_AVATAR_HOSTS))})/"
    r"[0-9a-f]{40}(?:_(?:medium|full))?\.jpg"
)


async def online_players(db: AsyncSession, project: int, now_ms: int) -> list[dict]:
    recent = now_ms - DAY_MS
    present = PlayerPresence.status.in_(("online", "afk"))

    def combat_counts(model: type[CombatKill] | type[CombatDeath]):
        return (
            select(model.steam_id, func.count().label("count"))
            .join(
                PlayerPresence,
                (PlayerPresence.project_id == model.project_id)
                & (PlayerPresence.steam_id == model.steam_id),
            )
            .where(model.project_id == project, model.at.between(recent, now_ms), present)
            .group_by(model.steam_id)
            .subquery()
        )

    kills = combat_counts(CombatKill)
    deaths = combat_counts(CombatDeath)

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
    known_zero = case((last_feed_at.between(recent, now_ms), 0))
    kill_count = func.coalesce(kills.c.count, known_zero)
    death_count = func.coalesce(deaths.c.count, known_zero)
    pirated = func.coalesce(PlayerPresence.is_pirated, Player.last_client_kind == "pirated", False)
    priority = func.coalesce(~trusted & pirated & (kill_count > 5) & (death_count < 2), False)
    reports = PlayerData.responses["reports"]["payload"]
    stored_reports = (
        select(func.count())
        .select_from(PlayerReport)
        .where(
            PlayerReport.project_id == PlayerPresence.project_id,
            PlayerReport.steam_id == PlayerPresence.steam_id,
        )
        .correlate(PlayerPresence)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(
                PlayerPresence.steam_id,
                PlayerPresence.status,
                trusted.label("trusted"),
                priority.label("priority"),
                func.greatest(CollectionJob.collected_at, last_feed_at).label("checked_at"),
                name.label("name"),
                profile_field("steam_avatar").label("steam_avatar"),
                profile_field("steam_data", "avatar_medium_url").label("steam_avatar_medium"),
                reports["total"].label("report_total"),
                reports.has_key("results").label("reports_fetched"),
                stored_reports.label("stored_reports"),
                profile_field("ip_details", "provider").label("provider"),
                profile_field("ip_details", "country_code").label("country_code"),
                profile_field("ip_details", "country_name").label("country_name"),
                kill_count.label("kills"),
                death_count.label("deaths"),
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
            .outerjoin(
                CollectionJob,
                (CollectionJob.project_id == PlayerPresence.project_id)
                & (CollectionJob.steam_id == PlayerPresence.steam_id),
            )
            .outerjoin(kills, kills.c.steam_id == PlayerPresence.steam_id)
            .outerjoin(deaths, deaths.c.steam_id == PlayerPresence.steam_id)
            .where(PlayerPresence.project_id == project, present)
            .order_by(
                priority.desc(),
                trusted,
                func.lower(name).asc().nulls_last(),
                PlayerPresence.steam_id,
            )
        )
    ).mappings()
    players = []
    for row in rows:
        player = dict(row)
        avatars = (player.pop("steam_avatar"), player.pop("steam_avatar_medium"))
        player["avatar_url"] = next(
            (url for url in avatars if url and STEAM_AVATAR_URL.fullmatch(url)), None
        )
        total = player.pop("report_total")
        stored = player.pop("stored_reports")
        fetched = player.pop("reports_fetched")
        if type(total) is int and total >= 0:
            player["reports"] = total
        else:
            player["reports"] = stored if stored or fetched else None
        players.append(player)
    return players
