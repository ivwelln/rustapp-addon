from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Text, false, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (
        CheckConstraint(
            "last_client_kind IN ('licensed', 'pirated', 'unknown')",
            name="last_client_kind",
        ),
    )

    project_id: Mapped[int] = mapped_column(primary_key=True)
    steam_id: Mapped[str] = mapped_column(Text, primary_key=True)
    last_client_kind: Mapped[str] = mapped_column(Text, server_default=text("'unknown'"))
    client_kind_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_whitelisted: Mapped[bool] = mapped_column(server_default=false())
