import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from pydantic import Field, ValidationError

from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.health import router as health_router
from app.core.config import DatabaseSettings
from app.db.session import open_database_sessions
from app.services.dashboard.updates import DashboardUpdates


class DashboardSettings(DatabaseSettings):
    rustapp_project_id: int = Field(default=9867, gt=0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.dashboard_sessions = None
    try:
        settings = DashboardSettings()
    except ValidationError:
        yield
    else:
        app.state.dashboard_project = settings.rustapp_project_id
        async with open_database_sessions(settings) as sessions:
            app.state.dashboard_sessions = sessions
            app.state.dashboard_updates = DashboardUpdates()
            listener = asyncio.create_task(app.state.dashboard_updates.listen(settings))
            try:
                yield
            finally:
                listener.cancel()
                with suppress(asyncio.CancelledError):
                    await listener


app = FastAPI(title="RustApp Addon", lifespan=lifespan)

app.include_router(health_router)
app.include_router(dashboard_router)
