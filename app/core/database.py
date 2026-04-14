from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

try:
    import sqlite_vec
except ImportError:  # pragma: no cover - optional runtime dependency
    sqlite_vec = None  # type: ignore[assignment]

logger = get_logger("app.database")
_SQLITE_VEC_MISSING_LOGGED = False


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False, "timeout": 30} if settings.database_is_sqlite else {}
engine_kwargs = {"connect_args": connect_args}
if not settings.database_is_sqlite:
    engine_kwargs["pool_pre_ping"] = True
    engine_kwargs["pool_recycle"] = 1800

engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
    module_name = dbapi_connection.__class__.__module__
    if module_name.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

        sqlite_vec_lock_active = bool(
            getattr(settings, "sqlite_vec_lock_active", None)
            if getattr(settings, "sqlite_vec_lock_active", None) is not None
            else (getattr(settings, "database_is_sqlite", False) and getattr(settings, "sqlite_vec_enabled", False))
        )
        if not sqlite_vec_lock_active:
            return

        if connection_record.info.get("sqlite_vec_attempted"):
            return
        connection_record.info["sqlite_vec_attempted"] = True

        global _SQLITE_VEC_MISSING_LOGGED
        if sqlite_vec is None:
            if not _SQLITE_VEC_MISSING_LOGGED:
                logger.warning(
                    "sqlite-vec package is not installed. Vector index table will not be available for SQLite."
                )
                _SQLITE_VEC_MISSING_LOGGED = True
            return

        try:
            dbapi_connection.enable_load_extension(True)
        except Exception:
            pass

        try:
            sqlite_vec.load(dbapi_connection)
            connection_record.info["sqlite_vec_loaded"] = True
        except Exception as exc:  # pragma: no cover - defensive runtime logging
            logger.warning("Failed to load sqlite-vec extension on SQLite connection: %s", exc)
            connection_record.info["sqlite_vec_loaded"] = False
        finally:
            try:
                dbapi_connection.enable_load_extension(False)
            except Exception:
                pass


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
