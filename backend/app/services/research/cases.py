from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rustapp.moderation import is_player
from app.crud.moderation import now_ms
from app.models import (
    Decision,
    Player,
    PlayerPlaytime,
    PlaytimeDay,
    ReferenceBan,
    ResearchCase,
    ResearchSource,
)
from app.services.moderation.kill_feed import PAGE_LIMIT
from app.services.research.playtime import CONTROL_MS, DAY

VERSION = 1
COMBAT_WINDOW = 30 * 60_000
ELIGIBLE = ("confirmed", "control")


def source_params(cutoff: int, entry: int | None) -> dict[str, dict]:
    return {
        "details": {},
        "stats": {"time_from": cutoff - 30 * DAY, "time_to": cutoff - 1},
        "kill-feed": {
            "time_from": max(cutoff - COMBAT_WINDOW, min(entry or 0, cutoff - 1)),
            "time_to": cutoff - 1,
            "page": 0,
            "limit": PAGE_LIMIT,
        },
        "checks": {"page": 0, "limit": 1},
        "reports": {"page": 0, "limit": 20, "variant": "received"},
        "ip-history": {"page": 0, "limit": 20},
    }


async def add_case(
    db: AsyncSession,
    project: int,
    server: int,
    sid: str,
    *,
    cohort: str,
    episode: int,
    cutoff: int,
    entry: int | None,
    label: str,
    hours_source: str | None = None,
    qualifying_ms: int | None = None,
    selected_at: int | None = None,
) -> None:
    if not is_player(sid) or not 0 < cutoff or (entry is not None and entry > cutoff):
        raise ValueError("Некорректная граница исследовательского среза")
    case_id = await db.scalar(
        insert(ResearchCase)
        .values(
            project_id=project,
            server_id=server,
            steam_id=sid,
            cohort=cohort,
            episode=episode,
            cutoff_at=cutoff,
            selected_at=now_ms() if selected_at is None else selected_at,
            hours_source=hours_source,
            qualifying_ms=qualifying_ms,
            entry_at=entry,
            label=label,
            version=VERSION,
        )
        .on_conflict_do_nothing()
        .returning(ResearchCase.id)
    )
    if case_id is not None:
        await db.execute(
            insert(ResearchSource).values(
                [
                    dict(case_id=case_id, source=source, params=params)
                    for source, params in source_params(cutoff, entry).items()
                ]
            )
        )


def ban_label(payload: dict, decision: Decision | None) -> str:
    if (
        str(payload.get("reason", "")).strip().casefold() != "чит"
        or payload.get("computed_is_active") is not True
    ):
        return "excluded"
    marked = str(payload.get("comment") or "").startswith("rustapp-addon:")
    if decision is None:
        return "awaiting_review" if marked else "confirmed"
    if decision.verdict == "confirmed_cheating":
        return "confirmed"
    if decision.verdict == "false_positive":
        return "excluded"
    return "awaiting_review"


async def sync_cases(db: AsyncSession, project: int, server: int, now: int) -> None:
    # Read the existing ban synchronizer's records. An edited reason or verdict also
    # updates an already collected case, without replacing the historical responses.
    bans = (
        await db.execute(
            select(ReferenceBan, ResearchCase, PlayerPlaytime.first_join_at)
            .outerjoin(
                ResearchCase,
                (ResearchCase.project_id == ReferenceBan.project_id)
                & (ResearchCase.steam_id == ReferenceBan.steam_id)
                & (ResearchCase.server_id == server)
                & (ResearchCase.cohort == "ban")
                & (ResearchCase.episode == ReferenceBan.ban_id)
                & (ResearchCase.version == VERSION),
            )
            .outerjoin(
                PlayerPlaytime,
                (PlayerPlaytime.project_id == ReferenceBan.project_id)
                & (PlayerPlaytime.steam_id == ReferenceBan.steam_id)
                & (PlayerPlaytime.first_join_server_id == server),
            )
            .where(
                ReferenceBan.project_id == project,
                ResearchCase.id.is_not(None)
                | (
                    (ReferenceBan.server_id == server)
                    & ReferenceBan.pirated
                    & (func.lower(func.btrim(ReferenceBan.payload["reason"].astext)) == "чит")
                ),
            )
        )
    ).all()
    subjects = {ban.steam_id for ban, _, _ in bans}
    decisions = await db.scalars(
        select(Decision).where(Decision.project_id == project, Decision.steam_id.in_(subjects))
    )
    by_ban, by_marker = {}, {}
    for decision in decisions:
        by_ban[(decision.steam_id, decision.external_id)] = decision
        marker = f"rustapp-addon:{decision.id}:{decision.primary_pattern}"
        by_marker[(decision.steam_id, marker)] = decision
    for ban, case, first_join in bans:
        marker = str(ban.payload.get("comment") or "").partition(" | ")[0]
        decision = by_ban.get((ban.steam_id, ban.ban_id)) or by_marker.get((ban.steam_id, marker))
        label = ban_label(ban.payload, decision)
        if case is not None:
            if case.label != label:
                case.label = label
        elif label != "excluded":
            entry = first_join if first_join and first_join <= ban.at else None
            await add_case(
                db,
                project,
                server,
                ban.steam_id,
                cohort="ban",
                episode=ban.ban_id,
                cutoff=ban.at,
                entry=entry,
                label=label,
            )
    await db.flush()

    def has_ban(sid):
        panel = (
            select(ReferenceBan.ban_id)
            .where(
                ReferenceBan.project_id == project,
                ReferenceBan.steam_id == sid,
                func.lower(func.btrim(ReferenceBan.payload["reason"].astext)) == "чит",
                ReferenceBan.payload["computed_is_active"].as_boolean().is_(True),
            )
            .exists()
        )
        other = ResearchCase.__table__.alias("prior_ban")
        archived = (
            select(other.c.id)
            .where(
                other.c.project_id == project,
                other.c.steam_id == sid,
                other.c.cohort == "ban",
                other.c.label.in_(("confirmed", "awaiting_review")),
            )
            .exists()
        )
        return panel | archived

    await db.execute(
        update(ResearchCase)
        .where(
            ResearchCase.project_id == project,
            ResearchCase.cohort == "control",
            ResearchCase.label.in_(("control", "awaiting_playtime")),
            has_ban(ResearchCase.steam_id),
        )
        .values(label="excluded")
    )
    verified_project = (
        select(func.sum(PlaytimeDay.played_ms))
        .where(
            PlaytimeDay.project_id == project,
            PlaytimeDay.steam_id == PlayerPlaytime.steam_id,
            PlaytimeDay.day >= now - 365 * DAY,
            PlaytimeDay.day + DAY <= now,
        )
        .correlate(PlayerPlaytime)
        .scalar_subquery()
    )
    recent_annual = (
        PlayerPlaytime.annual_checked_at.between(now - DAY, now)
        & (PlayerPlaytime.annual_ms > CONTROL_MS)
        & (PlayerPlaytime.annual_checked_at >= func.coalesce(ResearchCase.cutoff_at, 0))
    )
    controls = await db.execute(
        select(PlayerPlaytime, ResearchCase, verified_project, recent_annual)
        .select_from(PlayerPlaytime)
        .join(
            Player,
            (Player.project_id == PlayerPlaytime.project_id)
            & (Player.steam_id == PlayerPlaytime.steam_id),
        )
        .outerjoin(
            ResearchCase,
            (ResearchCase.project_id == project)
            & (ResearchCase.server_id == server)
            & (ResearchCase.steam_id == PlayerPlaytime.steam_id)
            & (ResearchCase.cohort == "control")
            & (ResearchCase.version == VERSION),
        )
        .where(
            PlayerPlaytime.project_id == project,
            Player.last_client_kind == "pirated",
            recent_annual
            | ((PlayerPlaytime.project_ms > CONTROL_MS) & (verified_project > CONTROL_MS)),
            ResearchCase.id.is_(None) | (ResearchCase.label == "awaiting_playtime"),
            ~has_ban(PlayerPlaytime.steam_id),
        )
        .limit(64)
    )
    for player, previous_case, project_ms, use_annual in controls:
        selected_at = player.annual_checked_at if use_annual else now
        qualifying_ms = player.annual_ms if use_annual else int(project_ms)
        if previous_case is not None:
            previous_case.hours_source = "rustapp"
            previous_case.qualifying_ms = qualifying_ms
            previous_case.selected_at = selected_at
            previous_case.label = "control"
            continue
        entry = player.first_join_at if player.first_join_server_id == server else None
        if entry is not None and entry > selected_at:
            entry = None
        cutoff = min(selected_at, entry + COMBAT_WINDOW) if entry else selected_at
        await add_case(
            db,
            project,
            server,
            player.steam_id,
            cohort="control",
            episode=0,
            cutoff=cutoff,
            entry=entry,
            label="control",
            hours_source="rustapp",
            qualifying_ms=qualifying_ms,
            selected_at=selected_at,
        )
