"""Keep historical research samples and cache project playtime per UTC day."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("moderation_decisions", sa.Column("eligibility", postgresql.JSONB()))
    op.add_column("rustapp_observations", sa.Column("request_params", postgresql.JSONB()))
    op.create_table(
        "rustapp_playtime_days",
        sa.Column("project_id", sa.Integer(), primary_key=True),
        sa.Column("steam_id", sa.Text(), primary_key=True),
        sa.Column("day", sa.BigInteger(), primary_key=True),
        sa.Column("played_ms", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "player_playtime",
        sa.Column("project_id", sa.Integer(), primary_key=True),
        sa.Column("steam_id", sa.Text(), primary_key=True),
        sa.Column("observed_ms", sa.BigInteger()),
        sa.Column("calculated_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("next_calculation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("first_join_at", sa.BigInteger()),
        sa.Column("first_join_server_id", sa.Integer()),
        sa.Column("project_ms", sa.BigInteger()),
        sa.Column("project_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("project_day_end", sa.BigInteger()),
        sa.Column("stats_after", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("stats_checked_at", sa.BigInteger()),
    )
    op.create_table(
        "research_cases",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("steam_id", sa.Text(), nullable=False),
        sa.Column("cohort", sa.Text(), nullable=False),
        sa.Column("episode", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("selected_at", sa.BigInteger(), nullable=False),
        sa.Column("hours_source", sa.Text()),
        sa.Column("qualifying_ms", sa.BigInteger()),
        sa.Column("cutoff_at", sa.BigInteger(), nullable=False),
        sa.Column("entry_at", sa.BigInteger()),
        sa.Column("label", sa.Text(), nullable=False),
        sa.UniqueConstraint("project_id", "server_id", "steam_id", "cohort", "episode", "version"),
    )
    op.create_index("ix_research_subject", "research_cases", ["project_id", "steam_id"])
    op.create_table(
        "research_case_sources",
        sa.Column("case_id", sa.BigInteger(), sa.ForeignKey("research_cases.id"), primary_key=True),
        sa.Column("source", sa.Text(), primary_key=True),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("next_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("attempted_at", sa.BigInteger()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("origin", sa.Text()),
        sa.Column("observed_at", sa.BigInteger()),
        sa.Column("covered_from", sa.BigInteger()),
        sa.Column("covered_to", sa.BigInteger()),
        sa.Column("complete", sa.Boolean()),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("features", postgresql.JSONB()),
    )
    op.create_index("ix_research_due", "research_case_sources", ["status", "next_at"])
    op.create_table(
        "rustapp_request_gate",
        sa.Column("account_key", sa.Text(), primary_key=True),
        sa.Column("research_denied", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("blocked_until", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "research_starts",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
            server_default="{}",
        ),
    )
    # A short first visit may end before the research worker's next tick.
    op.execute("""
        CREATE FUNCTION research_remember_entry() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO player_playtime(project_id, first_join_server_id, steam_id, first_join_at)
            VALUES (NEW.project_id, NEW.server_id, NEW.steam_id, NEW.joined_at)
            ON CONFLICT (project_id, steam_id) DO UPDATE SET
                first_join_at = excluded.first_join_at,
                first_join_server_id = excluded.first_join_server_id
            WHERE player_playtime.first_join_at IS NULL
               OR excluded.first_join_at < player_playtime.first_join_at;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER research_remember_entry AFTER INSERT OR UPDATE OF
            server_id, joined_at, first_active_at, active_since ON rustapp_player_presence
        FOR EACH ROW WHEN (NEW.server_id IS NOT NULL AND NEW.joined_at = NEW.first_active_at
                           AND NEW.active_since = NEW.first_active_at)
        EXECUTE FUNCTION research_remember_entry()
    """)
    op.execute("""
        INSERT INTO player_playtime(project_id, first_join_server_id, steam_id, first_join_at)
        SELECT project_id, server_id, steam_id, joined_at FROM rustapp_player_presence
        WHERE server_id IS NOT NULL AND joined_at = first_active_at
          AND active_since = first_active_at
    """)
    op.execute("""
        CREATE VIEW research_case_status AS
        SELECT c.*, CASE
            WHEN count(s.source) = 6 AND bool_and(s.status = 'ready') THEN 'ready'
            WHEN bool_or(s.status = 'unavailable') THEN 'partial'
            ELSE 'collecting' END AS status
        FROM research_cases c LEFT JOIN research_case_sources s ON s.case_id = c.id
        GROUP BY c.id
    """)
    op.execute("""
        CREATE VIEW player_research_status AS
        SELECT subjects.project_id, subjects.steam_id,
               coalesce(bool_or(c.status = 'ready'), false) AS trained
        FROM (
            SELECT project_id, steam_id FROM players
            UNION SELECT project_id, steam_id FROM research_cases WHERE version = 1
        ) subjects LEFT JOIN research_case_status c
          ON (c.project_id, c.steam_id) = (subjects.project_id, subjects.steam_id) AND c.version = 1
        GROUP BY subjects.project_id, subjects.steam_id
    """)
    op.execute("""
        CREATE VIEW research_dataset AS
        SELECT c.id AS case_id, c.project_id, c.server_id, c.steam_id, c.cohort, c.label,
               c.version, c.cutoff_at, c.entry_at, s.source, s.params, s.observed_at,
               s.covered_from, s.covered_to, s.complete, s.features
        FROM research_case_status c JOIN research_case_sources s ON s.case_id = c.id
        WHERE c.version = 1 AND c.status = 'ready' AND c.label IN ('control', 'confirmed')
          AND s.status = 'ready' AND s.features IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP VIEW research_dataset")
    op.execute("DROP VIEW player_research_status")
    op.execute("DROP VIEW research_case_status")
    op.execute("DROP TRIGGER research_remember_entry ON rustapp_player_presence")
    op.execute("DROP FUNCTION research_remember_entry()")
    op.drop_table("rustapp_request_gate")
    op.drop_table("research_case_sources")
    op.drop_table("research_cases")
    op.drop_table("player_playtime")
    op.drop_table("rustapp_playtime_days")
    op.drop_column("rustapp_observations", "request_params")
    op.drop_column("moderation_decisions", "eligibility")
