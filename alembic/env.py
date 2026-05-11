from logging.config import fileConfig
from sqlalchemy.ext.asyncio import create_async_engine, AsyncConnection
from alembic import context
import os
import asyncio
from dotenv import load_dotenv
from app.models.user import User
from app.models.interview import InterviewResult
from app.models.resume import ResumeHistory
from app.database import Base

# For Windows event loop policy
import sys
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Load environment variables
load_dotenv()

config = context.config

# Setup logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL environment variable not set")
    
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations():
    """Async migration function using SQLAlchemy 2.0 async API"""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL environment variable not set")
    
    # Create async engine
    connectable = create_async_engine(url)
    
    async with connectable.connect() as connection:
        await connection.run_sync(
            lambda sync_conn: context.configure(
                connection=sync_conn, 
                target_metadata=target_metadata
            )
        )
        
        async with connection.begin():
            await connection.run_sync(lambda sync_conn: context.run_migrations())

if context.is_offline_mode():
    run_migrations_offline()
else:
    # Run async migrations in event loop
    asyncio.run(run_async_migrations())