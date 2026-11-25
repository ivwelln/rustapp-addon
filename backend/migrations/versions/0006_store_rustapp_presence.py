"""Persist RustApp player presence for scheduled checks."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rustapp_player_presence",
        sa.Column("project_id", sa.Integer(), primary_key=True),
        sa.Column("steam_id", sa.Text(), primary_key=True),
        sa.Column("is_afk", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rustapp_player_presence")
