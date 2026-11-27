from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModerationControl


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
