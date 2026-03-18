import asyncio

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import text


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
