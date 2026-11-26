from sqlalchemy import BigInteger, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CollectionJob(Base):
    __tablename__ = "collection_jobs"
    project_id: Mapped[int] = mapped_column(primary_key=True)
    steam_id: Mapped[str] = mapped_column(Text, primary_key=True)
    requested_at: Mapped[int] = mapped_column(BigInteger)
    collected_at: Mapped[int | None] = mapped_column(BigInteger)
    due_at: Mapped[int | None] = mapped_column(BigInteger, index=True)


class Observation(Base):
    __tablename__ = "rustapp_observations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int]
    steam_id: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (Index("ix_observation_lookup", "project_id", "steam_id", "source", "id"),)


class CombatKill(Base):
    __tablename__ = "combat_kills"
    project_id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    server_id: Mapped[int]
    steam_id: Mapped[str] = mapped_column(Text)
    at: Mapped[int] = mapped_column(BigInteger)
    facts: Mapped[dict] = mapped_column(JSONB)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        Index("ix_kill_window", "project_id", "steam_id", "server_id", "at"),
        Index("ix_kill_dashboard", "project_id", "steam_id", "at"),
    )


class ReferenceBan(Base):
    __tablename__ = "rustapp_bans"
    project_id: Mapped[int] = mapped_column(primary_key=True)
    ban_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    steam_id: Mapped[str] = mapped_column(Text)
    at: Mapped[int] = mapped_column(BigInteger)
    signed_at: Mapped[int | None] = mapped_column(BigInteger)
    server_id: Mapped[int | None]
    pirated: Mapped[bool]
    eligible: Mapped[bool]
    payload: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        Index("ix_ban_registration", "project_id", "signed_at"),
        Index("ix_ban_time", "project_id", "at"),
    )


class Decision(Base):
    __tablename__ = "moderation_decisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int]
    steam_id: Mapped[str] = mapped_column(Text)
    server_id: Mapped[int]
    connected_at: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    primary_pattern: Mapped[str] = mapped_column(Text)
    matches: Mapped[dict] = mapped_column(JSONB)
    ban_ip: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (
        Index(
            "uq_decision_session",
            "project_id",
            "steam_id",
            "server_id",
            "connected_at",
            unique=True,
        ),
    )
