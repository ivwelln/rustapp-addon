import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.crud.moderation import now_ms
from app.crud.moderation_control import autoban_enabled, set_autoban
from app.models import (
    CollectionJob,
    CombatDeath,
    CombatKill,
    Decision,
    ModerationControl,
    Player,
    PlayerPresence,
    RustAppRequest,
)
from app.services.dashboard.decisions import decision_details, decision_summary
from app.services.dashboard.roster import STEAM_AVATAR_HOSTS, online_players

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parents[2] / "static" / "dashboard"
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "img-src 'self' " + " ".join(f"https://{host}" for host in STEAM_AVATAR_HOSTS)
    ),
    "X-Content-Type-Options": "nosniff",
}


async def local_dashboard(request: Request) -> None:
    if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Панель доступна локально")


router = APIRouter(dependencies=[Depends(local_dashboard)])


@router.get("/ready", include_in_schema=False)
async def dashboard_ready(request: Request) -> dict[str, str]:
    sessions = getattr(request.app.state, "dashboard_sessions", None)
    if sessions is None:
        raise HTTPException(status_code=503, detail="PostgreSQL не настроена", headers=HEADERS)
    try:
        async with sessions() as db:
            await db.execute(select(CombatDeath.event_id).limit(0))
            await db.execute(select(ModerationControl.project_id).limit(0))
    except (OSError, SQLAlchemyError):
        raise HTTPException(
            status_code=503, detail="PostgreSQL или миграции пока недоступны", headers=HEADERS
        ) from None
    return {"status": "ready"}


@router.get("/", include_in_schema=False)
async def page() -> FileResponse:
    return FileResponse(ASSETS / "index.html", headers=HEADERS)


@router.get("/dashboard-assets/{name}", include_in_schema=False)
async def asset(name: str) -> FileResponse:
    if name not in {
        "dashboard.css",
        "dashboard.js",
        "rustapp-logo.js",
        "logo-shaders.js",
        "rustapp-mark.png",
        "rustapp-mark.sdf",
        "rustapp.ico",
        "manrope-latin.woff2",
        "manrope-cyrillic.woff2",
    }:
        raise HTTPException(status_code=404)
    return FileResponse(ASSETS / name, headers=HEADERS)


class ModeratorVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    verdict: Literal["confirmed_cheating", "false_positive", "inconclusive"]
    note: str = Field(default="", max_length=2000)


class AutobanSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


def verify_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(status_code=403, detail="Запрос с другого сайта")


@router.put("/api/dashboard/autoban", include_in_schema=False)
async def update_autoban(request: Request, response: Response, body: AutobanSetting) -> dict:
    verify_same_origin(request)
    response.headers.update(HEADERS)
    sessions = request.app.state.dashboard_sessions
    if sessions is None:
        raise HTTPException(status_code=503, detail="PostgreSQL не настроена", headers=HEADERS)
    project = request.app.state.dashboard_project
    try:
        async with sessions.begin() as db:
            await set_autoban(db, project, body.enabled)
    except (OSError, SQLAlchemyError):
        raise HTTPException(
            status_code=503, detail="Не удалось сохранить настройку автобанов", headers=HEADERS
        ) from None
    logger.info(
        "Автобаны %s через панель | проект=%d", "включены" if body.enabled else "выключены", project
    )
    return body.model_dump()


@router.get("/api/dashboard/decisions/{decision_id}", include_in_schema=False)
async def read_decision(request: Request, response: Response, decision_id: int) -> dict:
    response.headers.update(HEADERS)
    return await access_decision(request, decision_id)


@router.put("/api/dashboard/decisions/{decision_id}", include_in_schema=False)
async def review_decision(
    request: Request, response: Response, decision_id: int, body: ModeratorVerdict
) -> dict:
    verify_same_origin(request)
    response.headers.update(HEADERS)
    return await access_decision(request, decision_id, body)


async def access_decision(
    request: Request, decision_id: int, body: ModeratorVerdict | None = None
) -> dict:
    sessions = request.app.state.dashboard_sessions
    if sessions is None:
        raise HTTPException(status_code=503, detail="PostgreSQL не настроена", headers=HEADERS)
    try:
        async with sessions.begin() as db:
            query = select(Decision).where(
                Decision.id == decision_id,
                Decision.project_id == request.app.state.dashboard_project,
            )
            decision = await db.scalar(query if body is None else query.with_for_update())
            if decision is None:
                raise HTTPException(status_code=404, detail="Решение не найдено", headers=HEADERS)
            if body is not None and (body.verdict, body.note) != (
                decision.verdict,
                decision.verdict_note,
            ):
                decision.verdict = body.verdict
                decision.verdict_note = body.note
                decision.verdict_at = now_ms()
            return decision_details(decision)
    except (OSError, SQLAlchemyError):
        raise HTTPException(
            status_code=503, detail="PostgreSQL или миграции пока недоступны", headers=HEADERS
        ) from None


@router.get("/api/dashboard", include_in_schema=False)
async def dashboard(request: Request, response: Response) -> dict:
    response.headers.update(HEADERS)
    return await read_snapshot(request)


@router.get("/api/dashboard/events", include_in_schema=False)
async def dashboard_events(request: Request) -> StreamingResponse:
    if request.app.state.dashboard_sessions is None:
        raise HTTPException(status_code=503, detail="PostgreSQL не настроена", headers=HEADERS)
    return StreamingResponse(
        stream_snapshots(request),
        media_type="text/event-stream",
        headers={**HEADERS, "X-Accel-Buffering": "no"},
    )


async def stream_snapshots(request: Request) -> AsyncIterator[str]:
    updates = request.app.state.dashboard_updates
    while True:
        changed = updates.changed
        try:
            data = await read_snapshot(request)
        except HTTPException as exc:
            yield f"event: unavailable\ndata: {json.dumps(str(exc.detail), ensure_ascii=False)}\n\n"
        else:
            serialized = json.dumps(
                jsonable_encoder(data), ensure_ascii=False, separators=(",", ":")
            )
            yield f"event: snapshot\ndata: {serialized}\n\n"
        with suppress(TimeoutError):
            await asyncio.wait_for(changed.wait(), timeout=15)
            await asyncio.sleep(0.3)


async def read_snapshot(request: Request) -> dict:
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
            CollectionJob.due_at,
            PlayerPresence.status,
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
    waiting = scheduled & (jobs.c.status == "online")
    bucket = func.date_trunc("minute", RustAppRequest.requested_at)
    try:
        async with sessions() as db:
            counts = (
                await db.execute(
                    select(
                        func.count().filter(waiting),
                        func.count().filter(waiting & (jobs.c.due_at <= now_ms)),
                        func.count().filter(scheduled & (jobs.c.status == "afk")),
                    ).select_from(jobs)
                )
            ).one()
            player_counts = (
                await db.execute(
                    select(func.count(), func.count().filter(Player.is_whitelisted))
                    .select_from(Player)
                    .where(Player.project_id == project)
                )
            ).one()
            kill_count = await db.scalar(
                select(func.count()).select_from(CombatKill).where(CombatKill.project_id == project)
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
            roster = await online_players(db, project, now_ms)
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
            enabled = await autoban_enabled(db, project)
    except (OSError, SQLAlchemyError):
        raise HTTPException(
            status_code=503, detail="PostgreSQL или миграции пока недоступны", headers=HEADERS
        ) from None

    return {
        "project": project,
        "updated_at": now.isoformat(),
        "queue": counts[0],
        "ready": counts[1],
        "afk": counts[2],
        "trusted": player_counts[1],
        "storage": {"players": player_counts[0], "kills": kill_count},
        "requests_hour": request_count,
        "request_buckets": [
            timeline.get(first_minute + timedelta(minutes=index), 0) for index in range(60)
        ],
        "autoban_enabled": enabled,
        "players": roster,
        "decisions": [decision_summary(decision) for decision in decisions],
    }
