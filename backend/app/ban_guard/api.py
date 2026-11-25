import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.routes.health import router as health_router
from app.ban_guard.quota import BAN_LIMIT, claim, slots_used
from app.core.config import DatabaseSettings
from app.db.session import open_database_sessions

logger = logging.getLogger(__name__)


class Claim(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    decision_id: int = Field(gt=0)
    pattern: str = Field(min_length=1)
    ban_ip: str = Field(min_length=1)


class Permit(BaseModel):
    allowed: bool
    used: int
    limit: int = BAN_LIMIT


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with open_database_sessions(DatabaseSettings()) as sessions:
        app.state.sessions = sessions
        yield


app = FastAPI(title="RustApp Ban Guard", lifespan=lifespan)
app.include_router(health_router)


@app.get("/projects/{project_id}/capacity")
async def capacity(
    request: Request, project_id: Annotated[int, Path(gt=0, le=2**31 - 1)]
) -> Permit:
    async with request.app.state.sessions() as db:
        used = await slots_used(db, project_id)
    return Permit(allowed=used < BAN_LIMIT, used=used)


@app.post("/projects/{project_id}/claims")
async def reserve(
    request: Request, candidate: Claim, project_id: Annotated[int, Path(gt=0, le=2**31 - 1)]
) -> Permit:
    async with request.app.state.sessions.begin() as db:
        allowed, used = await claim(
            db, project_id, candidate.decision_id, candidate.pattern, candidate.ban_ip
        )
    if allowed and used == BAN_LIMIT:
        logger.warning(
            "Аварийный лимит: занято %d из %d мест | проект=%s. Новые автобаны приостановлены",
            used,
            BAN_LIMIT,
            project_id,
        )
    return Permit(allowed=allowed, used=used)
