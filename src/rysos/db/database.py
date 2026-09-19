"""Database session management and repository helpers."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator
from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
try:
    from sqlalchemy.ext.asyncio import async_sessionmaker
except ImportError:
    from sqlalchemy.orm import sessionmaker as async_sessionmaker
from sqlalchemy.pool import NullPool
from rysos.config import settings
from rysos.db.models import Base

# Ensure local data directory exists
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)

# NullPool means every get_db_session() opens a fresh sqlite3 connection. Without WAL,
# one writer blocks all others under the default journal mode, and the pysqlite default
# busy timeout (5s) is routinely too short once a transaction spans a slow Drive-mount
# file write (apply_pending_triagem does exactly that) — readers/writers then hit
# "database is locked", which callers log and swallow, so a Triagem note can sit
# unresolved indefinitely with no visible error. WAL lets readers and the single writer
# proceed concurrently; the longer busy_timeout absorbs the rest.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    poolclass=NullPool,
    connect_args={"timeout": 30},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def _add_pending_segments_column(sync_conn) -> None:
    """create_all() never alters an existing table, so a DB from before this column
    existed needs it added by hand — otherwise every draft-session query on the live
    service starts failing with 'no such column' the moment this code deploys."""
    existing = {c["name"] for c in inspect(sync_conn).get_columns("telegram_draft_sessions")}
    if "pending_segments_json" not in existing:
        sync_conn.execute(text("ALTER TABLE telegram_draft_sessions ADD COLUMN pending_segments_json TEXT"))


async def init_db() -> None:
    """Initialize database tables."""
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_pending_segments_column)


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional async database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
