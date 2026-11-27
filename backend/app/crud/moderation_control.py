from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CollectionJob, ModerationControl

DEFAULT_POLL_SECONDS = 150


async def autoban_enabled(db: AsyncSession, project: int, *, lock: bool = False) -> bool:
    query = select(ModerationControl.autoban_enabled).where(ModerationControl.project_id == project)
    if not lock:
        return await db.scalar(query) is not False
    # Serialize dispatch with a settings change, including the first write for the project.
    query = query.with_for_update()
    enabled = await db.scalar(query)
    if enabled is None:
        await db.execute(
            insert(ModerationControl).values(project_id=project).on_conflict_do_nothing()
        )
        enabled = await db.scalar(query)
    return enabled is True


async def set_autoban(db: AsyncSession, project: int, enabled: bool) -> None:
    statement = insert(ModerationControl).values(project_id=project, autoban_enabled=enabled)
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[ModerationControl.project_id],
            set_={"autoban_enabled": statement.excluded.autoban_enabled},
        )
    )


async def player_poll_seconds(db: AsyncSession, project: int) -> int:
    interval = await db.scalar(
        select(ModerationControl.poll_seconds).where(ModerationControl.project_id == project)
    )
    return DEFAULT_POLL_SECONDS if interval is None else interval


async def set_player_poll_seconds(db: AsyncSession, project: int, seconds: int) -> None:
    await db.execute(insert(ModerationControl).values(project_id=project).on_conflict_do_nothing())
    control = await db.get(ModerationControl, project, with_for_update=True)
    previous = control.poll_seconds
    if previous == seconds:
        return
    control.poll_seconds = seconds
    # Match regular repeats only. Entry, report and retry deadlines keep their own schedules.
    await db.execute(
        update(CollectionJob)
        .where(
            CollectionJob.project_id == project,
            ~CollectionJob.report_pending,
            CollectionJob.requested_at <= CollectionJob.collected_at,
            CollectionJob.due_at == CollectionJob.collected_at + previous * 1000,
        )
        .values(due_at=CollectionJob.collected_at + seconds * 1000)
    )
