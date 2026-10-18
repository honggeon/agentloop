"""Phase 5 optional Redis delivery hint (``queue_mode=redis``).

The database is always the source of truth for ImportJobs: a job is created
as ``queued`` in the database and workers claim it with a transaction. The
Redis list is only a low-latency wakeup hint on top of that claim. This has
two honest consequences:

- a Redis outage never loses a job (the worker's database poll picks it up),
  but enqueueing must not *pretend* delivery happened: the API marks the
  job ``queue_degraded`` and returns a structured ``queue_unavailable``
  error (retryable);
- delivery is at-least-once. Exactly-once effects come from the
  transactional claim plus idempotent merge, not from Redis.

The Redis URL (and any credentials in it) never enters logs, audit events,
metrics or error responses.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from backend.app.config import Settings, get_settings

QUEUE_KEY = "auditrail:import_jobs"

_LOCK = threading.RLock()
_CLIENTS: dict[str, Any] = {}


class QueueUnavailableError(RuntimeError):
    """The configured broker is unreachable; the job stays ``queued`` and is
    explicitly marked degraded — never reported as delivered."""


def queue_mode(settings: Optional[Settings] = None) -> str:
    current = settings or get_settings()
    return str(getattr(current, "queue_mode", "inline") or "inline")


def _client(settings: Settings) -> Any:
    url = str(getattr(settings, "redis_url", "") or "").strip()
    if not url:
        raise QueueUnavailableError("queue_mode=redis requires AUDITRAIL_REDIS_URL")
    with _LOCK:
        client = _CLIENTS.get(url)
        if client is not None:
            return client
    try:
        import redis  # optional production dependency
    except ImportError as exc:
        raise QueueUnavailableError("redis queue mode requires the optional 'redis' dependency") from exc
    try:
        client = redis.Redis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0)
    except Exception as exc:
        raise QueueUnavailableError("cannot initialize redis client: %s" % type(exc).__name__) from exc
    with _LOCK:
        _CLIENTS[url] = client
    return client


def enqueue_hint(import_id: str, settings: Optional[Settings] = None) -> bool:
    """Push a delivery hint. Returns True when the hint landed.

    Raises :class:`QueueUnavailableError` in redis mode when the broker is
    down; in non-redis modes this is a no-op returning False (the caller
    does not need a hint).
    """
    current = settings or get_settings()
    if queue_mode(current) != "redis":
        return False
    try:
        _client(current).lpush(QUEUE_KEY, str(import_id))
    except QueueUnavailableError:
        raise
    except Exception as exc:
        raise QueueUnavailableError("redis enqueue failed: %s" % type(exc).__name__) from exc
    return True


def dequeue_hint(settings: Settings, *, timeout_seconds: int = 1) -> Optional[str]:
    """Best-effort hint read for the worker loop; ``None`` on timeout or
    any broker problem (the database poll is the correctness path)."""
    if queue_mode(settings) != "redis":
        return None
    try:
        result = _client(settings).brpop(QUEUE_KEY, timeout=max(int(timeout_seconds), 1))
    except Exception:
        return None
    if not result:
        return None
    try:
        _key, value = result
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
    except Exception:
        return None


def queue_ping(settings: Optional[Settings] = None) -> bool:
    """Broker liveness for health checks; True when no broker is configured
    (nothing to fail) — the mode is reported separately by the caller."""
    current = settings or get_settings()
    if queue_mode(current) != "redis":
        return True
    try:
        return bool(_client(current).ping())
    except Exception:
        return False


def reset_cached_clients() -> None:
    with _LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            pass
