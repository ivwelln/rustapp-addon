"""Track RustApp details and historical stats separately from active playtime."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

TIMESTAMPS = ("annual_before", "live_started_at", "live_ms", "live_checked_at", "live_session_key")


def upgrade() -> None:
    for name in TIMESTAMPS:
        op.add_column("player_playtime", sa.Column(name, sa.BigInteger()))
    op.add_column("player_playtime", sa.Column("live_server_id", sa.Integer()))
    op.add_column(
        "player_playtime",
        sa.Column("live_after", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.execute("UPDATE collection_jobs SET playtime_due_at = NULL WHERE due_at IS NOT NULL")


def downgrade() -> None:
    # Older readers expect the full annual response in these fields.
    op.execute(
        "UPDATE player_playtime SET annual_ms = NULL, annual_checked_at = NULL "
        "WHERE annual_before IS NOT NULL"
    )
    op.drop_column("player_playtime", "live_server_id")
    op.drop_column("player_playtime", "live_after")
    for name in reversed(TIMESTAMPS):
        op.drop_column("player_playtime", name)
