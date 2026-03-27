from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import settings

engine = create_async_engine(
    url=settings.database.url,
    echo=settings.database.echo,
    pool_size=settings.database.pool_size,
    pool_pre_ping=settings.database.pool_pre_ping,
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    async with async_session_factory.begin() as session:
        yield session


async def get_sandbox_db_session() -> AsyncGenerator[AsyncSession]:
    async with async_session_factory() as session:
        yield session
        await session.rollback()
