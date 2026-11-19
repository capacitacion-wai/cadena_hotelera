"""Async engine for the project's PostgreSQL — STRICTLY READ-ONLY.

The schema is pinned and ``default_transaction_read_only`` is set
at protocol level so even a stray ``INSERT`` raises ``ReadOnlySQL``.
"""

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import DATABASE_URL, DB_SCHEMA

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    connect_args={
        "server_settings": {
            "search_path": DB_SCHEMA,
            "default_transaction_read_only": "on",
        }
    },
)

async_session = async_sessionmaker(engine, expire_on_commit=False)

# Alias used by routers that prefer a "project read-only" name to make
# the read-only contract explicit at the call site.
project_db_ro = async_session


async def get_session():
    async with async_session() as s:
        yield s
