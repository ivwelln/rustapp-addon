"""Keep observed playtime without polling or storing RustApp session history."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TIME_COLUMNS = (
    "active_since",
    "first_active_at",
    "connecting_at",
    "joined_at",
    "server_since",
    "server_verified_at",
)


def upgrade() -> None:
    for column in TIME_COLUMNS:
        op.add_column("rustapp_player_presence", sa.Column(column, sa.BigInteger()))
    op.add_column("rustapp_player_presence", sa.Column("server_id", sa.Integer()))
    op.add_column("rustapp_player_presence", sa.Column("is_pirated", sa.Boolean()))
    op.create_table(
        "rustapp_observed_play",
        sa.Column("project_id", sa.Integer(), primary_key=True),
        sa.Column("steam_id", sa.Text(), primary_key=True),
        sa.Column("started_at", sa.BigInteger(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("ended_at", sa.BigInteger(), nullable=False),
        sa.Column("is_pirated", sa.Boolean()),
    )
    op.drop_column("collection_jobs", "session_history_covered_at")


def downgrade() -> None:
    op.add_column("collection_jobs", sa.Column("session_history_covered_at", sa.BigInteger()))
    op.drop_table("rustapp_observed_play")
    op.drop_column("rustapp_player_presence", "is_pirated")
    op.drop_column("rustapp_player_presence", "server_id")
    for column in reversed(TIME_COLUMNS):
        op.drop_column("rustapp_player_presence", column)
