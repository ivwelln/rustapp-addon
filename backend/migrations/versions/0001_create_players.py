"""create players

Revision ID: 0001
Revises:
Create Date: 2026-11-20 15:34:34.703575

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "players",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("steam_id", sa.Text(), nullable=False),
        sa.Column(
            "last_client_kind", sa.Text(), server_default=sa.text("'unknown'"), nullable=False
        ),
        sa.Column("client_kind_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_whitelisted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint(
            "last_client_kind IN ('licensed', 'pirated', 'unknown')",
            name=op.f("ck_players_last_client_kind"),
        ),
        sa.PrimaryKeyConstraint("project_id", "steam_id", name=op.f("pk_players")),
    )


def downgrade() -> None:
    op.drop_table("players")
