"""Store the live interval for regular player checks."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "moderation_control",
        sa.Column("poll_seconds", sa.Integer(), server_default="150", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("moderation_control", "poll_seconds")
