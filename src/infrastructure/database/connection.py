from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import settings

engine = create_async_engine(
    url=settings.database.url,
    echo=settings.database.echo,
    pool_size=settings.database.pool_size,
    pool_pre_ping=settings.database.pool_pre_ping,
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)
