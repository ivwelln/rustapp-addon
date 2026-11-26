"""add player data and whitelist

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-26 00:44:14.021265

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rustapp_player_data",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("steam_id", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("responses", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("project_id", "steam_id", name=op.f("pk_rustapp_player_data")),
    )
    op.create_table(
        "rustapp_player_ips",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("steam_id", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_at", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint(
            "project_id", "steam_id", "address", name=op.f("pk_rustapp_player_ips")
        ),
    )
    op.create_table(
        "rustapp_player_reports",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("steam_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("project_id", "report_id", name=op.f("pk_rustapp_player_reports")),
    )
    op.create_index(
        "ix_player_reports_target",
        "rustapp_player_reports",
        ["project_id", "steam_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "rustapp_player_sessions",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("steam_id", sa.Text(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("connected_at", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint(
            "project_id",
            "steam_id",
            "server_id",
            "connected_at",
            name=op.f("pk_rustapp_player_sessions"),
        ),
    )
    op.add_column(
        "combat_kills", sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column("players", sa.Column("whitelist_reason", sa.Text(), nullable=True))

    op.add_column("collection_jobs", sa.Column("session_history_covered_at", sa.BigInteger()))
    op.execute("""
        UPDATE players
        SET is_whitelisted = TRUE, whitelist_reason = 'license'
        WHERE last_client_kind = 'licensed' AND NOT is_whitelisted
    """)
    op.execute("""
        INSERT INTO rustapp_player_data (project_id, steam_id, updated_at, responses)
        SELECT project_id, steam_id, max(observed_at),
               jsonb_object_agg(source,
                   jsonb_build_object('observed_at', observed_at, 'payload', payload))
        FROM (
            SELECT DISTINCT ON (project_id, steam_id, source)
                   project_id, steam_id, source, observed_at, payload
            FROM rustapp_observations
            WHERE source IN ('details', 'session-history', 'stats', 'checks',
                             'reports', 'kill-feed', 'browse-full', 'ip-history')
            ORDER BY project_id, steam_id, source, observed_at DESC, id DESC
        ) AS latest
        GROUP BY project_id, steam_id
    """)
    op.execute("""
        INSERT INTO rustapp_player_sessions (project_id, steam_id, server_id, connected_at, payload)
        SELECT DISTINCT ON (observation.project_id, observation.steam_id,
            (entry->>'server_id')::integer, (entry->>'connected_at')::bigint)
            observation.project_id, observation.steam_id,
            (entry->>'server_id')::integer, (entry->>'connected_at')::bigint, entry
        FROM rustapp_observations AS observation
        CROSS JOIN LATERAL jsonb_array_elements(observation.payload->'results') AS entry
        WHERE observation.source = 'session-history'
          AND entry->>'project_id' = observation.project_id::text
          AND entry->>'steam_id' = observation.steam_id
          AND entry->>'server_id' IS NOT NULL AND entry->>'connected_at' IS NOT NULL
        ORDER BY observation.project_id, observation.steam_id,
                 (entry->>'server_id')::integer, (entry->>'connected_at')::bigint,
                 observation.observed_at DESC, observation.id DESC
    """)
    op.execute("""
        INSERT INTO rustapp_player_ips
            (project_id, steam_id, address, first_seen_at, last_seen_at, payload)
        SELECT project_id, steam_id, payload->>'ip', min(connected_at), max(connected_at), '{}'
        FROM rustapp_player_sessions
        WHERE coalesce(payload->>'ip', '') <> ''
        GROUP BY project_id, steam_id, payload->>'ip'
    """)
    op.execute("""
        INSERT INTO rustapp_player_reports (project_id, report_id, steam_id, created_at, payload)
        SELECT DISTINCT ON (observation.project_id, (entry->>'id')::bigint)
               observation.project_id, (entry->>'id')::bigint, observation.steam_id,
               (entry->>'created_at')::bigint, entry
        FROM rustapp_observations AS observation
        CROSS JOIN LATERAL jsonb_array_elements(observation.payload->'results') AS entry
        WHERE observation.source = 'reports'
          AND entry->>'target_steam_id' = observation.steam_id
          AND entry->>'id' IS NOT NULL AND entry->>'created_at' IS NOT NULL
        ORDER BY observation.project_id, (entry->>'id')::bigint,
                 observation.observed_at DESC, observation.id DESC
    """)
    op.execute("""
        UPDATE combat_kills AS kill SET payload = events.entry
        FROM (
            SELECT DISTINCT ON (observation.project_id, (entry->>'id')::bigint)
                   observation.project_id, (entry->>'id')::bigint AS event_id, entry
            FROM rustapp_observations AS observation
            CROSS JOIN LATERAL jsonb_array_elements(observation.payload->'results') AS entry
            WHERE observation.source = 'kill-feed'
              AND entry->>'project_id' = observation.project_id::text
              AND entry->'initiator'->>'steam_id' = observation.steam_id
              AND entry->>'id' IS NOT NULL
            ORDER BY observation.project_id, (entry->>'id')::bigint,
                     observation.observed_at DESC, observation.id DESC
        ) AS events
        WHERE kill.project_id = events.project_id AND kill.event_id = events.event_id
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("collection_jobs", "session_history_covered_at")
    op.drop_column("players", "whitelist_reason")
    op.drop_column("combat_kills", "payload")
    op.drop_table("rustapp_player_sessions")
    op.drop_index("ix_player_reports_target", table_name="rustapp_player_reports")
    op.drop_table("rustapp_player_reports")
    op.drop_table("rustapp_player_ips")
    op.drop_table("rustapp_player_data")
