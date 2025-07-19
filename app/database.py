from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from app.config import settings
from typing import AsyncGenerator

engine = create_async_engine(
    settings.DATABASE_URL,
    future=True,
    echo=False,
    pool_size=20,
    max_overflow=10
)

async_session = sessionmaker(
    engine, 
    expire_on_commit=False,
    class_=AsyncSession
)

Base = declarative_base()

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session