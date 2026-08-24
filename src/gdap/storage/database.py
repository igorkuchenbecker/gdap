"""Metadata database engine and session lifecycle.

One `Database` instance per process. SQLite gets WAL + foreign keys + a busy timeout so that the
API, the CLI and a worker can share a file safely; PostgreSQL gets a real pool. The rest of the
platform only ever sees ``with db.session() as session``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdap.core.config import Settings
from gdap.core.errors import ConfigurationError, StorageError
from gdap.observability.logging import get_logger
from gdap.storage.models import Base

log = get_logger(__name__)


class Database:
    def __init__(self, url: str, *, echo: bool = False, settings: Settings | None = None) -> None:
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        self.is_memory = ":memory:" in url
        self._engine = self._build_engine(url, echo=echo, settings=settings)
        self._session_factory = sessionmaker(
            bind=self._engine, expire_on_commit=False, class_=Session
        )

    # ----------------------------------------------------------------- construction
    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        url = settings.database_url
        if url.startswith("sqlite:///") and not settings.database.url:
            Path(url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
        return cls(url, echo=settings.database.echo, settings=settings)

    def _build_engine(self, url: str, *, echo: bool, settings: Settings | None) -> Engine:
        kwargs: dict[str, Any] = {"echo": echo, "future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                kwargs["poolclass"] = StaticPool
        else:
            if settings is not None:
                kwargs["pool_size"] = settings.database.pool_size
                kwargs["max_overflow"] = settings.database.max_overflow
                kwargs["pool_pre_ping"] = settings.database.pool_pre_ping
        try:
            engine = create_engine(url, **kwargs)
        except Exception as exc:  # pragma: no cover - bad url
            raise ConfigurationError(f"invalid database url: {url}", cause=exc) from exc

        if url.startswith("sqlite"):

            @event.listens_for(engine, "connect")
            def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

        return engine

    # ----------------------------------------------------------------- lifecycle
    @property
    def engine(self) -> Engine:
        return self._engine

    def create_all(self) -> None:
        """Bootstrap schema. Production upgrades go through Alembic (see deployment/)."""
        Base.metadata.create_all(self._engine)
        log.debug("metadata_schema_ready", url=self._safe_url())

    def drop_all(self) -> None:
        Base.metadata.drop_all(self._engine)

    def dispose(self) -> None:
        self._engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Unit of work: commit on success, rollback on any exception."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def new_session(self) -> Session:
        """Caller-managed session (used by long-running job execution)."""
        return self._session_factory()

    # ----------------------------------------------------------------- diagnostics
    def ping(self) -> float:
        import time

        start = time.perf_counter()
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise StorageError("metadata database unreachable", cause=exc) from exc
        return (time.perf_counter() - start) * 1000

    def _safe_url(self) -> str:
        """URL with any credentials stripped — safe for logs."""
        if "@" in self.url:
            scheme, _, rest = self.url.partition("://")
            return f"{scheme}://***@{rest.split('@', 1)[1]}"
        return self.url
