"""Database session management."""
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings


def configure_sqlite(dbapi_conn, connection_record):
    """Configure SQLite with WAL mode, busy timeout, and foreign keys."""
    # Set isolation_level to None for manual transaction control
    dbapi_conn.isolation_level = None
    
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def begin_immediate(connection):
    """Start a transaction: BEGIN IMMEDIATE (write lock now) unless the session is a read-only one.

    Read-only sessions (execution option sqlite_deferred, see get_read_session_factory) use a plain deferred BEGIN, which
    in WAL mode never blocks on, and never blocks, the single writer. Every other session is IMMEDIATE so that
    read-then-write handlers (ticket ids, idempotency checks) cannot fail on a lock upgrade.
    """
    if connection.get_execution_options().get("sqlite_deferred"):
        connection.exec_driver_sql("BEGIN")
    else:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def create_db_engine(settings: Settings):
    """Create and configure database engine."""
    # SQLite-specific: check_same_thread=False for FastAPI threadpool
    connect_args = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # a fresh clone has no data/ folder (it is gitignored): create it so SQLite can create the file
        db_file = settings.database_url.split("///", 1)[-1]
        if db_file and db_file != ":memory:":
            Path(db_file).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    
    engine = create_engine(
        settings.database_url,
        connect_args=connect_args,
        echo=False
    )
    
    # Configure SQLite if using SQLite
    if settings.database_url.startswith("sqlite"):
        event.listen(engine, "connect", configure_sqlite)
        event.listen(engine, "begin", begin_immediate)
    
    return engine


def get_session_factory(engine):
    """Create session factory."""
    return sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)


def get_read_session_factory(engine):
    """Sessions for handlers that only read: deferred transactions (no write lock)."""
    return sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False,
                        bind=engine.execution_options(sqlite_deferred=True))


def get_db(session_factory) -> Session:
    """FastAPI dependency for database sessions."""
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
