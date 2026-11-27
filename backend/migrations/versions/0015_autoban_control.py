"""Store the live autoban switch per project."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "moderation_control",
        sa.Column("project_id", sa.Integer(), primary_key=True),
        sa.Column("autoban_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("""
        CREATE TRIGGER notify_dashboard AFTER INSERT OR UPDATE OR DELETE ON moderation_control
        FOR EACH STATEMENT EXECUTE FUNCTION rustapp_notify_dashboard()
    """)


def downgrade() -> None:
    op.drop_table("moderation_control")
