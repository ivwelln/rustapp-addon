"""Store the session address requested in a moderation ban."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("moderation_decisions", sa.Column("ban_ip", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("moderation_decisions", "ban_ip")
