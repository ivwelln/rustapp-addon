"""Index recent requests for the monitoring page."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(op.f("ix_rustapp_requests_requested_at"), "rustapp_requests", ["requested_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_rustapp_requests_requested_at"), table_name="rustapp_requests")
