from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_engine_from_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Base, build_db_settings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    return build_db_settings().database_url


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    retries = int(os.getenv("ALEMBIC_DB_RETRIES", "60"))
    base_delay = float(os.getenv("ALEMBIC_DB_RETRY_BASE_DELAY", "0.2"))
    max_delay = float(os.getenv("ALEMBIC_DB_RETRY_MAX_DELAY", "2.0"))

    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            async with connectable.connect() as connection:
                await connection.execute(text("SELECT 1;"))
                await connection.run_sync(do_run_migrations)

            last_exc = None
            break
        except (OperationalError, ConnectionRefusedError, OSError) as e:
            last_exc = e
            delay = min(base_delay * (1.5 ** (attempt - 1)), max_delay)
            await asyncio.sleep(delay)

    if last_exc is not None:
        async with connectable.connect() as connection:
            await connection.execute(text("SELECT 1;"))
            await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
