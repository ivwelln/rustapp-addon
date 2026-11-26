"""Store live presence and notify monitoring clients on committed changes."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

WATCHED_TABLES = (
    "rustapp_player_presence",
    "rustapp_player_data",
    "players",
    "collection_jobs",
    "combat_kills",
    "moderation_decisions",
    "rustapp_requests",
)


def upgrade() -> None:
    op.add_column("rustapp_player_presence", sa.Column("status", sa.Text()))
    op.create_index("ix_kill_dashboard", "combat_kills", ["project_id", "steam_id", "at"])
    op.execute(
        """
        CREATE FUNCTION rustapp_notify_dashboard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('rustapp_dashboard', '');
            RETURN NULL;
        END;
        $$
        """
    )
    for table in WATCHED_TABLES:
        op.execute(
            f"CREATE TRIGGER notify_dashboard AFTER INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION rustapp_notify_dashboard()"
        )


def downgrade() -> None:
    for table in WATCHED_TABLES:
        op.execute(f"DROP TRIGGER notify_dashboard ON {table}")
    op.execute("DROP FUNCTION rustapp_notify_dashboard()")
    op.drop_index("ix_kill_dashboard", table_name="combat_kills")
    op.drop_column("rustapp_player_presence", "status")
