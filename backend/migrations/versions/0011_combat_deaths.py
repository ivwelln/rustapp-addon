"""Index deaths from the archived combat feed for the dashboard."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "combat_deaths",
        sa.Column("project_id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.BigInteger(), primary_key=True),
        sa.Column("steam_id", sa.Text(), nullable=False),
        sa.Column("at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_death_dashboard", "combat_deaths", ["project_id", "steam_id", "at"])
    op.execute(
        """
        CREATE FUNCTION rustapp_feed_deaths(project integer, feed jsonb)
        RETURNS TABLE(event_id bigint, steam_id text, at bigint)
        LANGUAGE sql IMMUTABLE PARALLEL SAFE
        AS $$
            SELECT CASE WHEN event->>'id' ~ '^[0-9]{1,18}$' THEN (event->>'id')::bigint END,
                   event #>> '{target,steam_id}',
                   CASE WHEN event->>'created_at' ~ '^[0-9]{1,18}$'
                        THEN (event->>'created_at')::bigint END
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(feed->'results') = 'array'
                     THEN feed->'results' ELSE '[]'::jsonb END
            ) AS events(event)
            WHERE event->>'project_id' = project::text
              AND event->>'id' ~ '^[0-9]{1,18}$'
              AND event->>'created_at' ~ '^[0-9]{1,18}$'
              AND event #>> '{target,steam_id}' ~ '^7656[0-9]{13}$'
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION rustapp_record_feed_deaths() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO combat_deaths (project_id, event_id, steam_id, at)
            SELECT NEW.project_id, death.event_id, death.steam_id, death.at
            FROM rustapp_feed_deaths(NEW.project_id, NEW.payload) death
            ORDER BY death.event_id
            ON CONFLICT (project_id, event_id) DO NOTHING;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER record_feed_deaths AFTER INSERT ON rustapp_observations
        FOR EACH ROW WHEN (NEW.source = 'kill-feed')
        EXECUTE FUNCTION rustapp_record_feed_deaths()
        """
    )
    op.execute(
        """
        WITH cutoff AS (
            SELECT (extract(epoch FROM current_timestamp) * 1000)::bigint - 86400000 AS start_at
        )
        INSERT INTO combat_deaths (project_id, event_id, steam_id, at)
        SELECT observation.project_id, death.event_id, death.steam_id, death.at
        FROM rustapp_observations observation
        CROSS JOIN cutoff
        CROSS JOIN LATERAL rustapp_feed_deaths(observation.project_id, observation.payload) death
        WHERE observation.source = 'kill-feed'
          AND observation.observed_at >= cutoff.start_at AND death.at >= cutoff.start_at
        ON CONFLICT (project_id, event_id) DO NOTHING
        """
    )
    op.execute(
        """
        CREATE TRIGGER notify_dashboard AFTER INSERT OR UPDATE OR DELETE ON combat_deaths
        FOR EACH STATEMENT EXECUTE FUNCTION rustapp_notify_dashboard()
        """
    )
    op.execute(
        """
        INSERT INTO combat_deaths (project_id, event_id, steam_id, at)
        SELECT project_id, event_id, facts->>'victim', at FROM combat_kills
        WHERE at >= (extract(epoch FROM current_timestamp) * 1000)::bigint - 86400000
          AND facts->>'victim' ~ '^7656[0-9]{13}$'
        ON CONFLICT (project_id, event_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER record_feed_deaths ON rustapp_observations")
    op.execute("DROP FUNCTION rustapp_record_feed_deaths()")
    op.execute("DROP FUNCTION rustapp_feed_deaths(integer, jsonb)")
    op.drop_table("combat_deaths")
