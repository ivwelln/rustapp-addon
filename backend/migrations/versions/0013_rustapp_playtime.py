"""Use RustApp statistics for project playtime."""

import sqlalchemy as sa
from alembic import op

from app.clients.rustapp.exceptions import RustAppResponseError
from app.services.moderation.eligibility import annual_ms
from app.services.moderation.playtime import YEAR_MS

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collection_jobs", sa.Column("playtime_due_at", sa.BigInteger()))
    op.add_column("player_playtime", sa.Column("annual_ms", sa.BigInteger()))
    op.add_column("player_playtime", sa.Column("annual_checked_at", sa.BigInteger()))
    db = op.get_bind()
    snapshots = db.execute(
        sa.text("""
        SELECT project_id, steam_id, responses->'stats'->'params' AS params,
               responses->'stats'->'payload' AS payload FROM rustapp_player_data
        WHERE responses->'stats'->'params' IS NOT NULL
        UNION ALL
        SELECT project_id, steam_id, responses->'research-stats-window'->'payload'->'params',
               responses->'research-stats-window'->'payload'->'response' FROM rustapp_player_data
        WHERE responses ? 'research-stats-window'
    """).execution_options(yield_per=64)
    )
    for project, sid, params, payload in snapshots:
        if not isinstance(params, dict) or not isinstance(payload, dict):
            continue
        start, end = params.get("time_from"), params.get("time_to")
        if type(start) is not int or type(end) is not int or end - start + 1 != YEAR_MS:
            continue
        try:
            duration = annual_ms(payload, sid, start, end)
        except (RustAppResponseError, ValueError):
            continue
        db.execute(
            sa.text("""
                INSERT INTO player_playtime(project_id, steam_id, annual_ms, annual_checked_at)
                VALUES (:project, :sid, :duration, :end)
                ON CONFLICT (project_id, steam_id) DO UPDATE SET
                    annual_ms = excluded.annual_ms, annual_checked_at = excluded.annual_checked_at
                WHERE player_playtime.annual_checked_at IS NULL
                   OR player_playtime.annual_checked_at < excluded.annual_checked_at
            """),
            {"project": project, "sid": sid, "duration": duration, "end": end},
        )
    # Retain historical evidence and the existing whitelist during the transition.
    op.execute("""
        UPDATE research_cases SET label = 'awaiting_playtime'
        WHERE cohort = 'control' AND label = 'control' AND hours_source = 'observations'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE research_cases SET label = 'control'
        WHERE cohort = 'control' AND label = 'awaiting_playtime' AND hours_source = 'observations'
    """)
    op.drop_column("player_playtime", "annual_checked_at")
    op.drop_column("player_playtime", "annual_ms")
    op.drop_column("collection_jobs", "playtime_due_at")
