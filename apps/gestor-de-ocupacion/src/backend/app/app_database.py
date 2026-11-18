import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import APP_DATABASE_URL

AppBase = declarative_base()

app_engine = create_async_engine(APP_DATABASE_URL, echo=False, pool_pre_ping=True)
app_session = async_sessionmaker(app_engine, expire_on_commit=False)


@event.listens_for(app_engine.sync_engine, "connect")
def _sqlite_pragmas(connection, _record):
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


_single_writer = asyncio.Lock()
_current_write: ContextVar = ContextVar("current_local_write", default=None)


@asynccontextmanager
async def app_write():
    current = _current_write.get()
    if current is not None:
        yield current
        return
    async with _single_writer:
        async with app_session() as session:
            token = _current_write.set(session)
            try:
                yield session
            finally:
                _current_write.reset(token)


async def get_app_session():
    async with app_session() as session:
        yield session


async def get_app_write_session():
    async with app_write() as session:
        yield session
