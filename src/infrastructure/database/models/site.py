from uuid import UUID, uuid4

from sqlalchemy import ARRAY, DateTime, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func, text

from src.infrastructure.database.models.base import Base
from src.infrastructure.database.models.metric import Metric


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    interval: Mapped[int] = mapped_column(nullable=False, default=60)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    regions: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{\"global\"}'")
    )
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    metrics: Mapped[list["Metric"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
