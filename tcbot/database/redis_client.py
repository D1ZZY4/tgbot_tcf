# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Async Redis client - optional distributed cache and session store.

If ``REDIS_URL`` is not set the module remains inert: :func:`client` returns
``None`` and all callers must degrade gracefully to in-process caching.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

# * hiredis is optional at import time: only required when REDIS_URL is set.
# * This allows the bot to start with in-memory caching only when Redis is
# * not configured, matching the TwoLevelCache graceful-degradation contract.

_hiredis_available = True
_HIREDIS_VERSION: str = "unknown"

try:
    import hiredis as _hiredis_mod

    _HIREDIS_VERSION = getattr(_hiredis_mod, "__version__", "unknown")
    del _hiredis_mod
except ImportError:
    _hiredis_available = False

log = logging.getLogger(__name__)

# ────────────────────── Module-level state ──────────────────────── #
# * Single shared async client; None until connect() succeeds.

_client: aioredis.Redis | None = None

# ─────────────── Connection pool / socket parameters ────────────── #

_SOCKET_CONNECT_TIMEOUT_S: float = 5.0
_SOCKET_TIMEOUT_S: float = 10.0
_MAX_CONNECTIONS: int = 20
_HEALTH_CHECK_INTERVAL_S: int = 30


# ──────────────────────── Public API ────────────────────────────── #


async def connect(url: str) -> None:
    """Create the async Redis client from *url* and verify connectivity with PING.

    Stores the client in the module-level ``_client`` singleton.
    Raises :class:`redis.asyncio.RedisError` on connection failure so the
    caller can decide whether to abort or continue without Redis.

    * hiredis is verified lazily here instead of at import time so that the
    * bot can start without Redis (or without hiredis) when REDIS_URL is
    * unset. This keeps the "Redis is optional" contract intact.
    """
    if not _hiredis_available:
        raise RuntimeError(
            "hiredis C extension is required when REDIS_URL is set. "
            "Install with: pip install 'redis[hiredis]'"
        )
    global _client
    pool = aioredis.ConnectionPool.from_url(
        url,
        decode_responses=True,
        max_connections=_MAX_CONNECTIONS,
        socket_connect_timeout=_SOCKET_CONNECT_TIMEOUT_S,
        socket_timeout=_SOCKET_TIMEOUT_S,
        health_check_interval=_HEALTH_CHECK_INTERVAL_S,
    )
    c = aioredis.Redis(connection_pool=pool)
    await c.ping()
    _client = c
    log.info("Redis connected (hiredis %s).", _HIREDIS_VERSION)


async def close() -> None:
    """Close the Redis client and release all pooled connections."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        log.info("Redis disconnected.")


def client() -> aioredis.Redis | None:
    """Return the active Redis client, or ``None`` when Redis is not configured."""
    return _client
