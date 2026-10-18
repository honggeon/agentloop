"""Phase 5 database engine: a small, explicit DB-API abstraction.

Two backends share one interface:

- ``sqlite:///path`` — the embedded default (stdlib ``sqlite3``). WAL mode,
  ``busy_timeout`` and ``BEGIN IMMEDIATE`` give real cross-process
  transactional semantics on a single host; it is the backend every
  automated test exercises (not a fake).
- ``postgresql://...`` — production backend via the optional ``psycopg``
  dependency. TLS is configured through the conninfo (``sslmode=...``).

All SQL in the codebase is written with ``%s`` placeholders (psycopg style);
the SQLite dialect translates them to ``?``. Datetimes are UTC ISO-8601
strings and JSON payloads are serialized text, so both dialects share one
schema. The database holds platform metadata and indexes only — run
evidence stays in ``runs/<run_id>/`` files, and large payloads (trajectory,
prompts, raw external payloads) never enter the database.

This module is core logic: no ``backend`` imports, no network of its own
(the PostgreSQL connection is opened by the caller's configuration), no
model credentials.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

DIALECT_SQLITE = "sqlite"
DIALECT_POSTGRES = "postgresql"

SUPPORTED_DIALECTS = (DIALECT_SQLITE, DIALECT_POSTGRES)


class StorageUnavailableError(RuntimeError):
    """The configured database is unreachable or failed an operation.

    Carries no connection string, password or absolute path — the message is
    safe to surface in structured API errors and health checks.
    """


class MigrationError(RuntimeError):
    """A schema migration failed; it is never recorded as applied."""


def parse_database_url(url: str) -> Tuple[str, str]:
    """Split a database URL into (dialect, conninfo). Raises ``ValueError``."""
    text = str(url or "").strip()
    if not text:
        raise ValueError("database url is required")
    if text.startswith("sqlite:///"):
        path = text[len("sqlite:///") :]
        if not path:
            raise ValueError("sqlite database url must include a file path")
        return DIALECT_SQLITE, path
    if text.startswith("sqlite://"):
        # sqlite://relative/path (two slashes) is accepted for convenience.
        path = text[len("sqlite://") :]
        if not path:
            raise ValueError("sqlite database url must include a file path")
        return DIALECT_SQLITE, path
    if text.startswith(("postgresql://", "postgres://")):
        return DIALECT_POSTGRES, text
    raise ValueError("unsupported database url scheme (expected sqlite:/// or postgresql://)")


def _translate(sql: str, dialect: str) -> str:
    if dialect == DIALECT_SQLITE:
        return sql.replace("%s", "?")
    return sql


class Database:
    """One logical database handle. Thread-safe within a process; workers in
    other processes open their own instance against the same URL."""

    def __init__(self, url: str) -> None:
        self._dialect, self._conninfo = parse_database_url(url)
        self._lock = threading.RLock()
        self._conn: Any = None

    @property
    def dialect(self) -> str:
        return self._dialect

    # ------------------------------------------------------------------
    # connection lifecycle
    # ------------------------------------------------------------------

    def _connect(self) -> Any:
        if self._dialect == DIALECT_SQLITE:
            path = Path(self._conninfo).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(path),
                timeout=30.0,
                isolation_level=None,  # autocommit; explicit transactions below
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn
        try:
            import psycopg  # optional production dependency
        except ImportError as exc:  # pragma: no cover - exercised without psycopg
            raise StorageUnavailableError(
                "postgresql storage requires the optional 'psycopg' dependency"
            ) from exc
        try:
            conn = psycopg.connect(self._conninfo, autocommit=True)
        except Exception as exc:
            raise StorageUnavailableError("cannot connect to postgresql: %s" % type(exc).__name__) from exc
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        return conn

    def _connection(self) -> Any:
        with self._lock:
            if self._conn is None:
                self._conn = self._connect()
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _reset(self) -> None:
        """Drop a broken connection so the next call reconnects."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> int:
        """Run a single statement (autocommit); returns affected row count."""
        with self._lock:
            try:
                cursor = self._connection().execute(_translate(sql, self._dialect), params)
                return cursor.rowcount
            except StorageUnavailableError:
                raise
            except Exception as exc:
                if not isinstance(exc, (sqlite3.Error,)):
                    self._reset()
                raise self._wrap(exc) from exc

    def query(self, sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                cursor = self._connection().execute(_translate(sql, self._dialect), params)
                rows = cursor.fetchall()
                return [self._row_dict(row) for row in rows]
            except StorageUnavailableError:
                raise
            except Exception as exc:
                if not isinstance(exc, (sqlite3.Error,)):
                    self._reset()
                raise self._wrap(exc) from exc

    def query_one(self, sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _row_dict(row: Any) -> Dict[str, Any]:
        if isinstance(row, dict):
            return dict(row)
        return {key: row[key] for key in row.keys()}

    def _wrap(self, exc: Exception) -> Exception:
        # Integrity/constraint errors pass through unwrapped so callers can
        # react to duplicates; everything else is an availability failure.
        if isinstance(exc, sqlite3.IntegrityError):
            return exc
        module = type(exc).__module__
        if "psycopg" in module and "errors" in module:
            return exc  # psycopg integrity/operational errors keep their type
        return StorageUnavailableError("database operation failed: %s" % type(exc).__name__)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[Any]:
        """Explicit transactional block.

        SQLite uses ``BEGIN IMMEDIATE`` so a concurrent writer in another
        process fails fast instead of silently upgrading later; PostgreSQL
        uses a regular ``BEGIN``. Statements inside the block still go
        through :meth:`execute`/:meth:`query` (same connection).
        """
        with self._lock:
            conn = self._connection()
            begin = "BEGIN IMMEDIATE" if (self._dialect == DIALECT_SQLITE and immediate) else "BEGIN"
            try:
                conn.execute(begin)
            except Exception as exc:
                raise self._wrap(exc) from exc
            try:
                yield conn
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    self._reset()
                raise
            else:
                try:
                    conn.execute("COMMIT")
                except Exception as exc:
                    self._reset()
                    raise self._wrap(exc) from exc

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------

    def ping(self, *, timeout_seconds: float = 2.0) -> bool:
        """Cheap liveness probe. Never raises, never leaks connection info."""
        try:
            if self._dialect == DIALECT_SQLITE:
                with self._lock:
                    self._connection().execute("SELECT 1").fetchone()
                return True
            with self._lock:
                self._connection().execute("SELECT 1").fetchone()
            return True
        except Exception:
            self._reset()
            return False


# ---------------------------------------------------------------------------
# JSON / timestamp helpers shared by repository modules
# ---------------------------------------------------------------------------


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads_json(text: Any, default: Any = None) -> Any:
    if text is None:
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(str(text))
    except (ValueError, TypeError):
        return default
