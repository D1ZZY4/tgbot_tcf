# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Caching layer: in-process TTL cache (L1) + optional Redis (L2).

Architecture
------------
``TTLCache[T]``
    Pure in-memory cache.  All reads/writes are synchronous and sub-microsecond.
    Instances are used standalone for caches that do not need Redis.

``TwoLevelCache[T]``
    Wraps ``TTLCache[T]`` and adds an optional Redis L2 layer (via
    ``tcbot.database.redis_client``).  Public methods are drop-in compatible
    with ``TTLCache[T]``.

    *  ``get`` / ``put`` / ``invalidate`` / ``clear`` stay synchronous, operating
       on the in-memory layer.  ``put`` and ``invalidate`` also enqueue ordered
       Redis writes/deletes to keep L2 eventually consistent.
    *  ``get_or_fetch`` is the primary hot-path: L1 → L2 → DB fetch, populating
       both layers on a miss.

Redis is optional.  When ``REDIS_URL`` is not set (or Redis is unreachable),
``TwoLevelCache`` degrades transparently to pure in-memory behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import cachetools as _cachetools
from bson import ObjectId

import tcbot.database.redis_client as _redis_mod
from tcbot.database.documents import GroupDoc

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


# ──────────────────────── JSON Encoder ───────────────────────── #
# * Tagged values preserve MongoDB scalar types across the Redis JSON boundary.
_MONGO_TYPE_KEY: str = "__tcbot_type__"
_MONGO_DATETIME_TYPE: str = "datetime"
_MONGO_OBJECT_ID_TYPE: str = "objectid"


class _MongoJSONEncoder(json.JSONEncoder):
    """Extend the standard encoder to handle types returned by Motor queries.

    ``datetime`` and ``ObjectId`` values receive explicit type tags so a Redis
    cache hit has the same runtime types as the original MongoDB document.
    Unknown MongoDB scalar types still fall back to strings rather than making
    a cache write fail.
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return {
                _MONGO_TYPE_KEY: _MONGO_DATETIME_TYPE,
                "value": o.isoformat(),
            }
        if isinstance(o, ObjectId):
            return {
                _MONGO_TYPE_KEY: _MONGO_OBJECT_ID_TYPE,
                "value": str(o),
            }
        try:
            return str(o)
        except Exception:
            return super().default(o)


def _mongo_object_hook(value: dict[str, Any]) -> Any:
    """Restore tagged MongoDB scalar values while tolerating legacy cache data."""
    value_type = value.get(_MONGO_TYPE_KEY)
    raw_value = value.get("value")
    if value_type == _MONGO_DATETIME_TYPE and isinstance(raw_value, str):
        try:
            return datetime.fromisoformat(raw_value)
        except ValueError:
            return value
    if value_type == _MONGO_OBJECT_ID_TYPE and isinstance(raw_value, str):
        try:
            return ObjectId(raw_value)
        except Exception:
            return value
    return value


# * Strong references to in-flight Redis background tasks; prevents GC before completion.
# * Mirrors the pattern used in __main__._asyncio_report_tasks and ban_flow._album_tasks.
_redis_bg_tasks: set[asyncio.Task[None]] = set()
# * Redis namespaces must be ordered across cache instances sharing a prefix.
# * Scope by event loop because asyncio tasks cannot be awaited across loops.
_redis_tails: dict[tuple[str, asyncio.AbstractEventLoop], asyncio.Task[None]] = {}

# * Public sentinel; compare using ``is CACHE_MISS`` to detect a cache miss.
# * Distinct from None because None is a valid cache value (e.g. user has no role).
CACHE_MISS: object = object()


# ───────────────────────── TTL Cache Class ──────────────────────── #
# * Core single-process in-memory implementation with TTL expiration.
# * Designed for asyncio applications - no locks needed (single-threaded event loop).


class TTLCache[T]:
    """Single-process in-memory TTL cache backed by cachetools.TTLCache.

    Uses cachetools for automatic TTL expiry AND LRU eviction when *maxsize* is
    reached, preventing unbounded memory growth that would occur with a plain dict.
    """

    __slots__ = ("_locks", "_store")

    def __init__(self, ttl: float, maxsize: int = 512) -> None:
        """Initialise the cache with a time-to-live in seconds and a maximum size."""
        self._store: _cachetools.TTLCache = _cachetools.TTLCache(
            maxsize=maxsize, ttl=ttl
        )
        self._locks: dict[Any, asyncio.Lock] = {}

    def get(self, key: Any) -> T | object:
        """Return the cached value, or CACHE_MISS if absent or expired."""
        try:
            return self._store[key]
        except KeyError:
            return CACHE_MISS

    def put(self, key: Any, val: T) -> None:
        """Store *val* under *key*; evicts LRU entry when maxsize is reached."""
        self._store[key] = val

    def invalidate(self, key: Any) -> None:
        """Remove *key* from the cache (no-op if absent or already expired)."""
        with contextlib.suppress(KeyError):
            del self._store[key]
        # * Drop the per-key lock so high-cardinality callers (e.g. member
        # * profile lookups) don't accumulate a stale lock for every user
        # * ever fetched. The lock object is only re-created on the next miss.
        self._locks.pop(key, None)

    def clear(self) -> None:
        """Remove all entries immediately."""
        self._store.clear()
        # * Locks from in-flight fetches remain in use; leave them. The next
        # * miss for any key will reuse the existing lock. Subsequent misses
        # * after the in-flight fetch completes will repopulate _locks with
        # * fresh entries, and stale ones for absent keys get cleared by
        # * invalidate(). A full lock purge is unnecessary here.
        # * If a true memory purge is required, restart the process.

    async def get_or_fetch(
        self,
        key: Any,
        fetch: Callable[[], Awaitable[T]],
    ) -> T:
        """Return cached value, or call *fetch()*, cache the result, and return it.

        A per-key ``asyncio.Lock`` serialises concurrent misses for the same key
        so that only one ``fetch()`` runs; the winner populates the cache and all
        waiters read the same result.  Different keys remain fully parallel.
        """
        val = self.get(key)
        if val is not CACHE_MISS:
            return cast("T", val)

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            val = self.get(key)
            if val is not CACHE_MISS:
                return cast("T", val)
            val = await fetch()
            self.put(key, val)
            return val


# ────────────────────── Two-Level Cache Class ───────────────────── #
# * Wraps TTLCache (L1) and adds Redis (L2) for distributed caching.
# * Drop-in compatible interface with TTLCache.


class TwoLevelCache[T]:
    """Two-level cache: in-memory L1 (fast) + Redis L2 (distributed, optional).

    When Redis is unavailable the cache degrades to pure in-memory behaviour
    identical to ``TTLCache``.  No configuration changes are required at call
    sites.
    """

    __slots__ = ("_locks", "_mem", "_redis_prefix", "_redis_ttl")

    def __init__(
        self,
        memory_ttl: float,
        redis_ttl: float,
        redis_prefix: str,
        maxsize: int = 512,
    ) -> None:
        """Initialise with separate TTLs for each layer, a Redis key prefix, and maxsize."""
        self._mem: TTLCache[T] = TTLCache(ttl=memory_ttl, maxsize=maxsize)
        self._redis_ttl: int = max(1, int(redis_ttl))
        self._redis_prefix: str = redis_prefix
        # * Per-key lock to serialise concurrent fetches for the same key
        # * across L1 + L2 + DB. Mirrors TTLCache.get_or_fetch semantics.
        # * Dropped per-fetch in get_or_fetch()'s finally block (plus in
        # * invalidate()) to prevent unbounded growth for high-cardinality
        # * callers (e.g. member profile lookups). clear() intentionally
        # * leaves locks alone: in-flight fetches may still hold them.
        self._locks: dict[Any, asyncio.Lock] = {}

    # ── Sync operations (in-memory layer only) ── #

    def get(self, key: Any) -> T | object:
        """Return the in-memory cached value, or CACHE_MISS."""
        return self._mem.get(key)

    def put(self, key: Any, val: T) -> None:
        """Store in memory and enqueue an ordered Redis write."""
        self._mem.put(key, val)
        self._redis_put_background(key, val)

    def invalidate(self, key: Any) -> None:
        """Remove from memory and enqueue an ordered Redis delete."""
        self._mem.invalidate(key)
        # * Drop the per-key lock so high-cardinality callers (e.g. member
        # * profile lookups) don't accumulate a stale lock for every key
        # * ever fetched.
        self._locks.pop(key, None)
        self._redis_del_background(key)

    def clear(self) -> None:
        """Clear the in-memory layer (does not flush Redis keys)."""
        self._mem.clear()

    async def clear_all(self) -> None:
        """Clear the in-memory layer AND delete all matching keys from Redis.

        Use this when you need a full two-layer invalidation and the set of
        affected keys is not known in advance (e.g. after an ownership transfer
        where the previous owner's ID is unavailable).  Unlike ``clear()``,
        this method is async and removes Redis keys via SCAN + UNLINK in
        batches of 100, avoiding the O(N) blocking behaviour of ``KEYS``.

        Unlike ``invalidate(key)``, which removes one known key from both layers,
        this sweeps every key matching ``tcbot:<prefix>:v2:*``.  Do not call it in
        hot paths; it is designed for rare, high-impact invalidations only.
        """
        self._mem.clear()
        rc = _redis_client()
        if rc is None:
            return
        pattern = f"tcbot:{self._redis_prefix}:v2:*"

        async def _clear_redis() -> None:
            cursor: int = 0
            try:
                while True:
                    cursor, keys = await rc.scan(cursor, match=pattern, count=100)
                    if keys:
                        await rc.unlink(*keys)
                    if cursor == 0:
                        break
            except Exception as exc:
                log.debug(
                    "Redis clear_all failed for prefix %s: %s",
                    self._redis_prefix,
                    exc,
                )

        task = self._enqueue_redis_mutation(_clear_redis)
        if task is not None:
            await asyncio.shield(task)

    # ── Async hot-path ── #

    async def get_or_fetch(
        self,
        key: Any,
        fetch: Callable[[], Awaitable[T]],
    ) -> T:
        """L1 → L2 → DB fetch with population of both layers on a miss.

        Layers checked in order:
        1. In-memory (sub-microsecond, no I/O).
        2. Redis (single round-trip, returns cached value from another process
           or previous bot run).
        3. ``fetch()`` coroutine (DB query); result is written to both layers.

        A per-key ``asyncio.Lock`` serialises concurrent misses for the same
        key so that only one ``fetch()`` runs; the winner populates the cache
        and all waiters read the same result. Different keys remain fully
        parallel. Mirrors ``TTLCache.get_or_fetch`` semantics.
        """
        # Fast path: in-memory hit, no lock needed.
        val = self._mem.get(key)
        if val is not CACHE_MISS:
            return cast("T", val)

        # * Slow path: take a per-key lock so concurrent misses for the same
        # * key do not all run the DB fetch. Re-check L1 inside the lock to
        # * catch a winner that just populated it.
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                val = self._mem.get(key)
                if val is not CACHE_MISS:
                    return cast("T", val)

                # L2: Redis
                rc = _redis_client()
                if rc is not None:
                    rkey = self._rkey(key)
                    try:
                        raw = await rc.get(rkey)
                        if raw is not None:
                            loaded: T = json.loads(raw, object_hook=_mongo_object_hook)
                            self._mem.put(key, loaded)
                            return loaded
                    except Exception as exc:
                        log.debug("Redis get failed for %s: %s", rkey, exc)

                # L3: DB fetch
                val = await fetch()
                self._mem.put(key, val)
                if rc is not None:
                    rkey = self._rkey(key)
                    payload = json.dumps(val, cls=_MongoJSONEncoder)
                    task = self._enqueue_redis_mutation(
                        lambda: self._redis_set(rc, rkey, payload)
                    )
                    if task is not None:
                        await asyncio.shield(task)

                return cast("T", val)
        finally:
            # * Drop the per-key lock so a high-cardinality caller does not
            # * accumulate a stale lock for every key ever fetched. The next
            # * miss will create a fresh lock; the value itself is in
            # * ``self._mem`` already and is served by the fast path.
            self._locks.pop(key, None)

    # ── Internal helpers ── #

    def _rkey(self, key: Any) -> str:
        return f"tcbot:{self._redis_prefix}:v2:{key}"

    def _redis_put_background(self, key: Any, val: T) -> None:
        """Fire-and-forget Redis write without blocking the caller."""
        rc = _redis_client()
        if rc is None:
            return
        rkey = self._rkey(key)
        payload = json.dumps(val, cls=_MongoJSONEncoder)
        self._enqueue_redis_mutation(lambda: self._redis_set(rc, rkey, payload))

    def _redis_del_background(self, key: Any) -> None:
        """Fire-and-forget Redis key deletion without blocking the caller."""
        rc = _redis_client()
        if rc is None:
            return
        rkey = self._rkey(key)
        self._enqueue_redis_mutation(lambda: self._redis_delete(rc, rkey))

    async def _redis_set(self, rc: Any, rkey: str, payload: str) -> None:
        """Write one Redis value and keep failures observable but non-fatal."""
        try:
            await rc.set(rkey, payload, ex=self._redis_ttl)
        except Exception as exc:
            log.debug("Redis set failed for %s: %s", rkey, exc)

    async def _redis_delete(self, rc: Any, rkey: str) -> None:
        """Delete one Redis value and keep failures observable but non-fatal."""
        try:
            await rc.delete(rkey)
        except Exception as exc:
            log.debug("Redis delete failed for %s: %s", rkey, exc)

    def _enqueue_redis_mutation(
        self, operation: Callable[[], Awaitable[None]]
    ) -> asyncio.Task[None] | None:
        """Run Redis mutations FIFO for this Redis prefix and event loop.

        ``put()``, ``invalidate()``, and ``clear_all()`` are called from
        different synchronous and asynchronous paths.  Chaining each operation
        to the previous task prevents a slower Redis write from completing
        after a newer delete or prefix-wide clear, including when separate
        cache objects share the same Redis namespace.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        tail_key = (self._redis_prefix, loop)
        previous = _redis_tails.get(tail_key)

        async def _run() -> None:
            if previous is not None:
                try:
                    await previous
                except BaseException as exc:
                    log.debug(
                        "Previous Redis mutation failed for prefix %s: %s",
                        self._redis_prefix,
                        exc,
                    )
            await operation()

        task = loop.create_task(_run(), name=f"tcbot.redis.{self._redis_prefix}")
        _redis_tails[tail_key] = task
        _redis_bg_tasks.add(task)
        task.add_done_callback(_redis_bg_tasks.discard)
        task.add_done_callback(_log_redis_task_error)
        task.add_done_callback(lambda completed: _clear_redis_tail(tail_key, completed))
        return task


def _clear_redis_tail(
    tail_key: tuple[str, asyncio.AbstractEventLoop],
    task: asyncio.Task[None],
) -> None:
    """Release a namespace tail when no newer mutation follows it."""
    if _redis_tails.get(tail_key) is task:
        _redis_tails.pop(tail_key, None)


def _redis_client() -> Any:
    """Return the active Redis client instance, or None when Redis is not configured."""
    return _redis_mod.client()


def _log_redis_task_error(task: asyncio.Task) -> None:  # type: ignore[type-arg]
    """Done-callback: log Redis background task errors without raising."""
    if not task.cancelled() and task.exception() is not None:
        log.debug("Redis background task failed: %s", task.exception())


# ───────────────────────── Cache TTL Constants ──────────────────────── #
# * Named TTL constants kept together so tuning is one-place-one-change.
# * Unit: seconds (float).

# Per-user effective-role: short enough to pick up role changes quickly.
_ROLE_CACHE_TTL_S: float = 60.0
_ROLE_REDIS_TTL_S: float = 90.0  # Redis TTL slightly longer than in-memory

# ─────────────────────── Cache Maxsize Constants ─────────────────── #
# * Maximum in-memory entry counts per cache instance.
# * Sized to hold peak concurrent users/chats without unbounded growth.
_ROLE_CACHE_MAXSIZE: int = 2048  # roles: one entry per active user
_USER_MENTION_CACHE_MAXSIZE: int = (
    4096  # mention data: larger pool for check/stats lookups
)
_CONNECTED_CACHE_MAXSIZE: int = 512  # connection status: one entry per connected chat

# Per-chat connection status: medium window; connection changes are infrequent.
_CONNECTION_CACHE_TTL_S: float = 120.0
_CONNECTION_REDIS_TTL_S: float = 180.0

# Full active-groups list: short window; group add/remove is rare but must propagate.
_GROUPS_LIST_CACHE_TTL_S: float = 30.0
_GROUPS_LIST_REDIS_TTL_S: float = 45.0

# Owner ID: long window; ownership transfers are very rare.
_OWNER_CACHE_TTL_S: float = 300.0
_OWNER_REDIS_TTL_S: float = 360.0


# ───────────────────── Shared Cache Singletons ──────────────────── #
# * Global TwoLevelCache instances: L1 in-memory + L2 Redis (when available).
# * Each has separate TTLs tuned to its usage pattern and Redis prefix.
# * All are populated and invalidated by specific database modules.

# Per-user effective-role cache (str | None per user_id)
# Populated by users_roles.get_effective_role; invalidated on every role write
effective_role_cache: TwoLevelCache[str | None] = TwoLevelCache(
    memory_ttl=_ROLE_CACHE_TTL_S,
    redis_ttl=_ROLE_REDIS_TTL_S,
    redis_prefix="role",
    maxsize=_ROLE_CACHE_MAXSIZE,
)

# Per-chat connection cache (bool per chat_id)
# Populated by groups_db.is_connected; invalidated on add/deactivate
connected_cache: TwoLevelCache[bool] = TwoLevelCache(
    memory_ttl=_CONNECTION_CACHE_TTL_S,
    redis_ttl=_CONNECTION_REDIS_TTL_S,
    redis_prefix="conn",
    maxsize=_CONNECTED_CACHE_MAXSIZE,
)

# Whole-list active-groups cache (list[dict], single entry keyed by _ALL_GROUPS_KEY)
# Populated by groups_db.active_groups; invalidated on add/deactivate
active_groups_cache: TwoLevelCache[list[GroupDoc]] = TwoLevelCache(
    memory_ttl=_GROUPS_LIST_CACHE_TTL_S,
    redis_ttl=_GROUPS_LIST_REDIS_TTL_S,
    redis_prefix="groups",
    maxsize=4,
)
_ALL_GROUPS_KEY: str = "__all__"

# Owner-ID cache (single int entry - ownership transfers are very rare)
# Populated by users_roles.get_owner_id; invalidated on set_owner / ensure_initial_owner
owner_id_cache: TwoLevelCache[int | None] = TwoLevelCache(
    memory_ttl=_OWNER_CACHE_TTL_S,
    redis_ttl=_OWNER_REDIS_TTL_S,
    redis_prefix="owner",
    maxsize=4,
)
_OWNER_KEY: str = "__owner__"

# Per-user mention data cache (list [first_name, username] per user_id)
# Populated by users_cache.get_user_mention_data; invalidated on upsert_user
# JSON round-trip: tuple stored as list, caller casts back to tuple on read.
_USER_MENTION_CACHE_TTL_S: float = 300.0
_USER_MENTION_REDIS_TTL_S: float = 600.0

user_mention_cache: TwoLevelCache[list[str | None]] = TwoLevelCache(
    memory_ttl=_USER_MENTION_CACHE_TTL_S,
    redis_ttl=_USER_MENTION_REDIS_TTL_S,
    redis_prefix="umention",
    maxsize=_USER_MENTION_CACHE_MAXSIZE,
)
