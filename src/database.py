from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    ARRAY,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func, text

from src.config import settings


class Base(DeclarativeBase):
    pass


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    interval: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    regions: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default=text("'{\"global\"}'"))
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    metrics: Mapped[list["Metric"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


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
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    site: Mapped["Site"] = relationship(back_populates="metrics")


@dataclass(frozen=True)
class DBSettings:
    database_url: str
    echo_sql: bool = False


def build_db_settings() -> DBSettings:
    return DBSettings(database_url=settings.db_url, echo_sql=settings.db_echo)


def create_engine(settings: DBSettings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=settings.echo_sql,
        pool_pre_ping=True,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def wait_for_db(engine: AsyncEngine) -> None:
    """
    DB readiness wait with retry/backoff.
    Useful both on host runs and in containers.
    """
    delay_s = 0.1
    for _ in range(60):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1;"))
            return
        except (OperationalError, ConnectionRefusedError, OSError):
            await asyncio.sleep(delay_s)
            delay_s = min(delay_s * 1.5, 2.0)

    # final attempt (raises real error)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1;"))


async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        yield session
