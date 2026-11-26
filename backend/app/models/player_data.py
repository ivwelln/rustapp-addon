from sqlalchemy import BigInteger, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PlayerData(Base):
    __tablename__ = "rustapp_player_data"

    project_id: Mapped[int] = mapped_column(primary_key=True)
    steam_id: Mapped[str] = mapped_column(Text, primary_key=True)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    responses: Mapped[dict] = mapped_column(JSONB)


class PlayerSession(Base):
    __tablename__ = "rustapp_player_sessions"

    project_id: Mapped[int] = mapped_column(primary_key=True)
    steam_id: Mapped[str] = mapped_column(Text, primary_key=True)
    server_id: Mapped[int] = mapped_column(primary_key=True)
    connected_at: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class PlayerIP(Base):
    __tablename__ = "rustapp_player_ips"

    project_id: Mapped[int] = mapped_column(primary_key=True)
    steam_id: Mapped[str] = mapped_column(Text, primary_key=True)
    address: Mapped[str] = mapped_column(Text, primary_key=True)
    first_seen_at: Mapped[int] = mapped_column(BigInteger)
    last_seen_at: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSONB)


class PlayerReport(Base):
    __tablename__ = "rustapp_player_reports"
    __table_args__ = (Index("ix_player_reports_target", "project_id", "steam_id", "created_at"),)

    project_id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    steam_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSONB)


class PlayerPresence(Base):
    __tablename__ = "rustapp_player_presence"

    project_id: Mapped[int] = mapped_column(primary_key=True)
    steam_id: Mapped[str] = mapped_column(Text, primary_key=True)
    is_afk: Mapped[bool]
    status: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[int] = mapped_column(BigInteger)
    active_since: Mapped[int | None] = mapped_column(BigInteger)
    first_active_at: Mapped[int | None] = mapped_column(BigInteger)
    connecting_at: Mapped[int | None] = mapped_column(BigInteger)
    joined_at: Mapped[int | None] = mapped_column(BigInteger)
    server_id: Mapped[int | None]
    server_since: Mapped[int | None] = mapped_column(BigInteger)
    server_verified_at: Mapped[int | None] = mapped_column(BigInteger)
    is_pirated: Mapped[bool | None]

    @property
    def session_key(self) -> int | None:
        return (self.connecting_at or self.active_since) if self.active_since is not None else None


class ObservedPlay(Base):
    __tablename__ = "rustapp_observed_play"

    project_id: Mapped[int] = mapped_column(primary_key=True)
    steam_id: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    server_id: Mapped[int]
    ended_at: Mapped[int] = mapped_column(BigInteger)
    is_pirated: Mapped[bool | None]
