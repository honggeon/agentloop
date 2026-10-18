"""Phase 5 platform metadata database.

Public surface:

- :class:`~auditrail.db.engine.Database` — dialect-agnostic DB handle
  (``sqlite:///`` embedded, ``postgresql://`` production);
- :mod:`auditrail.db.migrations` — explicit, versioned schema migrations;
- :mod:`auditrail.db.job_store` — cross-process ImportJob state store with
  transactional claim, heartbeat and fencing tokens.

The database holds platform metadata and indexes only; ``runs/<run_id>/``
files remain the authoritative run-evidence contract.
"""

from .engine import (
    DIALECT_POSTGRES,
    DIALECT_SQLITE,
    Database,
    MigrationError,
    StorageUnavailableError,
    dumps_json,
    loads_json,
    parse_database_url,
)
from .migrations import apply_migrations, current_version, migration_status, pending_migrations

__all__ = [
    "DIALECT_POSTGRES",
    "DIALECT_SQLITE",
    "Database",
    "MigrationError",
    "StorageUnavailableError",
    "dumps_json",
    "loads_json",
    "parse_database_url",
    "apply_migrations",
    "current_version",
    "migration_status",
    "pending_migrations",
]
