from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.infrastructure.database.models.base import Base


class Metric(Base):
    __tablename__ = "metrics"

    time: Mapped[object] = mapped_column(DateTime(timezone=True), primary_key=True)
    site_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sites.id", ondelete="CASCADE"),
        primary_key=True,
    )
    region: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="global", primary_key=True
    )
    status_code: Mapped[int] = mapped_column(nullable=False)
    latency_ms: Mapped[float] = mapped_column(nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    site: Mapped["Site"] = relationship(back_populates="metrics")  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
