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
    observed_at: Mapped[int] = mapped_column(BigInteger)
