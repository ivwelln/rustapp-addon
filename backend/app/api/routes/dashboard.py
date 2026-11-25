from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.ban_guard.quota import BAN_LIMIT, slots_used
from app.models import CollectionJob, Decision, Player, PlayerPresence, RustAppRequest
from app.services.moderation.evaluate import describe

ASSETS = Path(__file__).resolve().parents[2] / "static" / "dashboard"
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
}


async def local_dashboard(request: Request) -> None:
    if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Панель доступна локально")


router = APIRouter(dependencies=[Depends(local_dashboard)])


@router.get("/", include_in_schema=False)
async def page() -> FileResponse:
    return FileResponse(ASSETS / "index.html", headers=HEADERS)


@router.get("/dashboard-assets/{name}", include_in_schema=False)
async def asset(name: str) -> FileResponse:
    if name not in {"dashboard.css", "dashboard.js"}:
        raise HTTPException(status_code=404)
    return FileResponse(ASSETS / name, headers=HEADERS)


def reason(decision: Decision) -> str:
    if decision.primary_pattern == "P01":
        return "Совпадение даты регистрации Steam с ранее заблокированным аккаунтом"
    match = decision.matches.get(decision.primary_pattern)
    if not match:
        return ""
    try:
        return describe(decision.primary_pattern, match)
    except (KeyError, ValueError, TypeError):
        return ""


@router.get("/api/dashboard", include_in_schema=False)
async def dashboard(request: Request, response: Response) -> dict:
    response.headers.update(HEADERS)
    sessions = request.app.state.dashboard_sessions
    if sessions is None:
        raise HTTPException(status_code=503, detail="PostgreSQL не настроена", headers=HEADERS)
    project = request.app.state.dashboard_project
    now = datetime.now(UTC)
    current_minute = now.replace(second=0, microsecond=0)
    first_minute = current_minute - timedelta(minutes=59)
    now_ms = int(now.timestamp() * 1000)

    jobs = (
        select(
            CollectionJob.steam_id,
            CollectionJob.collected_at,
            CollectionJob.due_at,
            PlayerPresence.is_afk,
            Player.is_whitelisted,
        )
        .outerjoin(
            PlayerPresence,
            (PlayerPresence.project_id == CollectionJob.project_id)
            & (PlayerPresence.steam_id == CollectionJob.steam_id),
        )
        .outerjoin(
            Player,
            (Player.project_id == CollectionJob.project_id)
            & (Player.steam_id == CollectionJob.steam_id),
        )
        .where(CollectionJob.project_id == project)
        .subquery()
    )
    scheduled = jobs.c.due_at.is_not(None) & jobs.c.is_whitelisted.is_not(True)
    waiting = scheduled & jobs.c.is_afk.is_not(True)
    bucket = func.date_trunc("minute", RustAppRequest.requested_at)
    try:
        async with sessions() as db:
            counts = (
                await db.execute(
                    select(
                        func.count().filter(waiting),
                        func.count().filter(waiting & (jobs.c.due_at <= now_ms)),
                        func.count().filter(scheduled & jobs.c.is_afk.is_(True)),
                    ).select_from(jobs)
                )
            ).one()
            trusted = await db.scalar(
                select(func.count())
                .select_from(Player)
                .where(
                    Player.project_id == project,
                    Player.is_whitelisted,
                )
            )
            timeline = dict(
                (
                    await db.execute(
                        select(bucket, func.count())
                        .where(
                            RustAppRequest.requested_at >= first_minute,
                        )
                        .group_by(bucket)
                    )
                ).all()
            )
            request_count = await db.scalar(
                select(func.count())
                .select_from(RustAppRequest)
                .where(
                    RustAppRequest.requested_at >= now - timedelta(hours=1),
                )
            )
            activity = (
                await db.execute(
                    select(jobs)
                    .where(scheduled | jobs.c.collected_at.is_not(None))
                    .order_by(
                        scheduled.desc(),
                        jobs.c.due_at.asc().nulls_last(),
                        jobs.c.collected_at.desc().nulls_last(),
                        jobs.c.steam_id,
                    )
                    .limit(200)
                )
            ).all()
            decisions = (
                await db.scalars(
                    select(Decision)
                    .where(
                        Decision.project_id == project,
                    )
                    .order_by(Decision.id.desc())
                    .limit(40)
                )
            ).all()
            used = await slots_used(db, project)
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503, detail="PostgreSQL или миграции пока недоступны", headers=HEADERS
        ) from None

    return {
        "project": project,
        "updated_at": now.isoformat(),
        "queue": counts[0],
        "ready": counts[1],
        "afk": counts[2],
        "trusted": trusted,
        "requests_hour": request_count,
        "request_buckets": [
            timeline.get(first_minute + timedelta(minutes=index), 0) for index in range(60)
        ],
        "guard": {"used": used, "limit": BAN_LIMIT},
        "players": [
            {
                "steam_id": player.steam_id,
                "collected_at": player.collected_at,
                "due_at": player.due_at,
                "status": (
                    "trusted"
                    if player.is_whitelisted
                    else "afk"
                    if player.is_afk and player.due_at is not None
                    else "waiting"
                    if player.due_at is not None
                    else "stopped"
                ),
            }
            for player in activity
        ],
        "decisions": [
            {
                "id": decision.id,
                "steam_id": decision.steam_id,
                "state": decision.state,
                "pattern": decision.primary_pattern,
                "reason": reason(decision),
                "ban_id": decision.external_id,
                "at": decision.matches.get(decision.primary_pattern, {}).get("at"),
            }
            for decision in decisions
        ],
    }
