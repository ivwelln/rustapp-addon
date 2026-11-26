"""Track feed coverage, listener liveness and moderation review evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collection_jobs", sa.Column("kill_feed_covered_from", sa.BigInteger()))
    op.add_column("collection_jobs", sa.Column("kill_feed_covered_through", sa.BigInteger()))
    for column in ("joined_at", "reference_ban_id", "verdict_at"):
        op.add_column("moderation_decisions", sa.Column(column, sa.BigInteger()))
    for column in ("verdict", "verdict_note"):
        op.add_column("moderation_decisions", sa.Column(column, sa.Text()))
    op.create_table(
        "rustapp_presence_heartbeat",
        sa.Column("project_id", sa.Integer(), primary_key=True),
        sa.Column("confirmed_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rustapp_presence_heartbeat")
    for column in ("joined_at", "reference_ban_id", "verdict_at", "verdict", "verdict_note"):
        op.drop_column("moderation_decisions", column)
    op.drop_column("collection_jobs", "kill_feed_covered_through")
    op.drop_column("collection_jobs", "kill_feed_covered_from")
