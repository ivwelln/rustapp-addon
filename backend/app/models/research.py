from sqlalchemy import BigInteger, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PlaytimeDay(Base):
    __tablename__ = "rustapp_playtime_days"

    project_id: Mapped[int] = mapped_column(primary_key=True)
    steam_id: Mapped[str] = mapped_column(Text, primary_key=True)
    day: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    played_ms: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[int] = mapped_column(BigInteger)


class PlayerPlaytime(Base):
    __tablename__ = "player_playtime"

    project_id: Mapped[int] = mapped_column(primary_key=True)
    steam_id: Mapped[str] = mapped_column(Text, primary_key=True)
    annual_ms: Mapped[int | None] = mapped_column(BigInteger)
    annual_checked_at: Mapped[int | None] = mapped_column(BigInteger)
    calculated_at: Mapped[int] = mapped_column(BigInteger, server_default="0")
    next_calculation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    first_join_at: Mapped[int | None] = mapped_column(BigInteger)
    first_join_server_id: Mapped[int | None]
    project_ms: Mapped[int | None] = mapped_column(BigInteger)
    project_days: Mapped[int] = mapped_column(server_default="0")
    project_day_end: Mapped[int | None] = mapped_column(BigInteger)
    stats_after: Mapped[int] = mapped_column(BigInteger, server_default="0")
    stats_checked_at: Mapped[int | None] = mapped_column(BigInteger)


class ResearchCase(Base):
    __tablename__ = "research_cases"
    __table_args__ = (
        UniqueConstraint("project_id", "server_id", "steam_id", "cohort", "episode", "version"),
        Index("ix_research_subject", "project_id", "steam_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int]
    server_id: Mapped[int]
    steam_id: Mapped[str] = mapped_column(Text)
    cohort: Mapped[str] = mapped_column(Text)
    episode: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int]
    selected_at: Mapped[int] = mapped_column(BigInteger)
    hours_source: Mapped[str | None] = mapped_column(Text)
    qualifying_ms: Mapped[int | None] = mapped_column(BigInteger)
    cutoff_at: Mapped[int] = mapped_column(BigInteger)
    entry_at: Mapped[int | None] = mapped_column(BigInteger)
    label: Mapped[str] = mapped_column(Text)


class ResearchSource(Base):
    __tablename__ = "research_case_sources"
    __table_args__ = (Index("ix_research_due", "status", "next_at"),)

    case_id: Mapped[int] = mapped_column(ForeignKey("research_cases.id"), primary_key=True)
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    params: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    next_at: Mapped[int] = mapped_column(BigInteger, server_default="0")
    attempted_at: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(server_default="0")
    lease: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[int | None] = mapped_column(BigInteger)
    covered_from: Mapped[int | None] = mapped_column(BigInteger)
    covered_to: Mapped[int | None] = mapped_column(BigInteger)
    complete: Mapped[bool | None]
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    features: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))


class RustAppGate(Base):
    __tablename__ = "rustapp_request_gate"

    account_key: Mapped[str] = mapped_column(Text, primary_key=True)
    blocked_until: Mapped[int] = mapped_column(BigInteger, server_default="0")
    research_denied: Mapped[bool] = mapped_column(server_default="false")
    research_starts: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), server_default=text("'{}'")
    )
