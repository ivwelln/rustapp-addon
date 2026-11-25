from datetime import datetime

from sqlalchemy import BigInteger, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RustAppRequest(Base):
    __tablename__ = "rustapp_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
