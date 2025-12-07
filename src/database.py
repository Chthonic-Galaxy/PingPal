# src/database.py
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
    func,
    text,
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    metrics: Mapped[list["Metric"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


class Metric(Base):
    __tablename__ = "metrics"

    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    site_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sites.id", ondelete="CASCADE"),
        primary_key=True,
    )

    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    site: Mapped["Site"] = relationship(back_populates="metrics")


@dataclass(frozen=True)
class DBSettings:
    database_url: str
    echo_sql: bool = False


def _read_password_from_file(path: str) -> str | None:
    try:
        p = Path(path)
        if not p.exists():
            return None
        value = p.read_text(encoding="utf-8").strip()
        return value or None
    except Exception:
        return None


def build_db_settings() -> DBSettings:
    direct = os.getenv("DATABASE_URL")
    if direct:
        return DBSettings(database_url=direct, echo_sql=os.getenv("SQL_ECHO") == "1")

    user = os.getenv("PINGPAL_DB_USER", "pingpal")
    db = os.getenv("PINGPAL_DB_NAME", "pingpal")
    # Prefer IPv4 loopback by default; avoids edge cases with localhost/IPv6.
    host = os.getenv("PINGPAL_DB_HOST", "127.0.0.1")
    port = int(os.getenv("PINGPAL_DB_PORT", "5432"))

    password = os.getenv("PINGPAL_DB_PASSWORD")
    if not password:
        password = _read_password_from_file("/run/secrets/pg_password")
    if not password:
        password = _read_password_from_file("./secrets/pg_password.txt")
    if not password:
        password = "pingpal"

    url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"
    return DBSettings(database_url=url, echo_sql=os.getenv("SQL_ECHO") == "1")


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
    Handle transient DB unavailability (e.g. TimescaleDB tune restart during init).
    Tries a lightweight SELECT 1 with backoff.
    """
    delay_s = 0.1
    for attempt in range(1, 60 + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1;"))
            return
        except (OperationalError, ConnectionRefusedError, OSError):
            # keep retrying
            await asyncio.sleep(delay_s)
            delay_s = min(delay_s * 1.5, 2.0)

    # One last try; if it fails, let the real exception bubble with full context
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1;"))


async def init_db(engine: AsyncEngine) -> None:
    """
    MVP-style "migration":
      - enable timescaledb extension
      - create tables
      - convert metrics into a hypertable
    """
    await wait_for_db(engine)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb;"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("SELECT create_hypertable('metrics', 'time', if_not_exists => TRUE);")
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        yield session
