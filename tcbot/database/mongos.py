# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""MongoDB connection manager - single client shared across the entire application."""

from __future__ import annotations

import asyncio
import logging
import secrets
import string
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)

from tcbot import cfg
from tcbot.utils.circuit_breaker import mongodb as _mongo_cb

if TYPE_CHECKING:
    from collections.abc import Awaitable

_T = TypeVar("_T")

_RESOLV_CONF = "/etc/resolv.conf"


# ──────────────────────────── DNS Patch ─────────────────────────── #
# * Fixes DNS resolution issues on platforms without standard resolv.conf
# * Required for Termux/Android and other restricted environments
# * Enables mongodb+srv:// SRV records to work correctly


def _patch_dns_if_needed() -> None:
    """Write a fallback nameserver config when /etc/resolv.conf is absent."""
    if not Path(_RESOLV_CONF).exists():
        try:
            import dns.resolver  # noqa: PLC0415 (optional dependency; lazy import avoids ImportError when dnspython is absent)

            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = ["8.8.8.8", "8.8.4.4"]
            dns.resolver.default_resolver = resolver
        except Exception as exc:
            logging.getLogger(__name__).debug("DNS patch skipped: %s", exc)


log = logging.getLogger(__name__)

_db: AsyncIOMotorDatabase | None = None

_ID_ALPHABET: str = string.ascii_lowercase + string.digits

# ──────────────── MongoDB Connection Pool Parameters ────────────── #
_MONGO_SERVER_SELECTION_MS: int = 10_000
_MONGO_CONNECT_TIMEOUT_MS: int = 10_000
_MONGO_SOCKET_TIMEOUT_MS: int = 45_000
_MONGO_MAX_POOL_SIZE: int = 20
_MONGO_MIN_POOL_SIZE: int = 2
_MONGO_MAX_IDLE_MS: int = 60_000
_MONGO_HEARTBEAT_MS: int = 30_000

# ──────────────────────── Index TTL Constants ───────────────────── #
# * MongoDB TTL index for member_cache: auto-expire docs after 90 days.
# * 90 days * 86400 s/day = 7776000 s; split into named parts so the
# * intent is obvious and adjustable without hunting for a raw literal.
_MEMBER_CACHE_TTL_DAYS: int = 90
_MEMBER_CACHE_EXPIRE_S: int = _MEMBER_CACHE_TTL_DAYS * 86_400


# ────────────────────────── ID Generator ────────────────────────── #
# * Creates unique, URL-safe IDs for database records
# * Uses cryptographically secure random number generation


def make_short_id(length: int = 10) -> str:
    """Generate a random URL-safe lowercase alphanumeric ID."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


# ──────────────────────── Client Accessors ──────────────────────── #
# * Safe accessors to get the database instance
# * Prevents accidental use before connection is established


def db() -> AsyncIOMotorDatabase:
    """Get the main MongoDB database instance."""
    if _db is None:
        raise RuntimeError("DB not initialised; call connect() first.")
    return _db


# ─────────────────────────── Connection ─────────────────────────── #
# * Establishes the main MongoDB connection pool
# * Configures all connection parameters for optimal performance
# ! CRITICAL: Must be called before any database operations


async def connect() -> None:
    """Establish MongoDB connection and initialize the global _db instance."""
    global _db
    _patch_dns_if_needed()
    client = AsyncIOMotorClient(
        cfg.mongodb_uri,
        serverSelectionTimeoutMS=_MONGO_SERVER_SELECTION_MS,
        connectTimeoutMS=_MONGO_CONNECT_TIMEOUT_MS,
        socketTimeoutMS=_MONGO_SOCKET_TIMEOUT_MS,
        maxPoolSize=_MONGO_MAX_POOL_SIZE,
        minPoolSize=_MONGO_MIN_POOL_SIZE,
        maxIdleTimeMS=_MONGO_MAX_IDLE_MS,
        heartbeatFrequencyMS=_MONGO_HEARTBEAT_MS,
        compressors=["zlib"],
        retryWrites=True,
        retryReads=True,
    )
    await _mongo_cb.call(client.admin.command("ping"))
    _db = client[cfg.db_name]
    log.info("MongoDB connected → %s", cfg.db_name)


# ─────────────────────────── Index Setup ────────────────────────── #
# * Creates all required database indexes in parallel
# * Improves query performance and enforces data uniqueness
# * Safe to call multiple times - MongoDB ignores existing indexes


async def ensure_indexes() -> None:
    """Create all critical collection indexes in parallel. No-op if they already exist.

    Raises the first index failure after logging every failure, so the
    fail-fast checks in ``__main__._post_init`` and ``serverless`` startup
    actually fire. Serving traffic without uniqueness indexes (bans
    ``ban_id``, ``warn_counts`` per-user counter, one-pending-request) risks
    duplicate moderation records, which is worse than a loud startup crash
    that the runner watchdog restarts from.
    """
    results = await asyncio.gather(
        # * Serves get_active_ban(): filter on banned_user_id + is_active, sort on
        # * timestamp + ban_id. Compound index replaces the separate prefix index below.
        col("bans").create_index(
            [("banned_user_id", 1), ("is_active", 1), ("timestamp", -1), ("ban_id", -1)]
        ),
        col("bans").create_index([("ban_id", 1)], unique=True),
        col("tc_owners").create_index([("user_id", 1)], unique=True),
        # * Serves user_appeal_count() which filters on banned_user_id + appeal_log_msg_id presence
        col("bans").create_index(
            [("banned_user_id", 1), ("appeal_log_msg_id", 1)], sparse=True
        ),
        # * Serves active_bans()/active_ban_count() which filter on is_active only
        col("bans").create_index([("is_active", 1), ("timestamp", -1), ("ban_id", -1)]),
        # * Serves /check history: every ban (active+inactive) for a user, newest first
        col("bans").create_index(
            [("banned_user_id", 1), ("timestamp", -1), ("ban_id", -1)]
        ),
        col("tc_admins").create_index([("user_id", 1)], unique=True),
        col("tc_roles").create_index([("user_id", 1)], unique=True),
        # * Serves users_roles.all_by_role() which filters by role only
        col("tc_roles").create_index([("role", 1)]),
        col("federated_groups").create_index([("chat_id", 1), ("is_active", 1)]),
        col("federated_groups").create_index([("chat_id", 1)], unique=True),
        # * pending_joins keyed by chat_id with upsert (one pending per chat)
        col("pending_joins").create_index([("chat_id", 1)], unique=True),
        col("member_cache").create_index([("user_id", 1)], unique=True),
        # * Covered-query index: serves get_first_names_batch and get_mention_data_batch
        # * $in on user_id with {first_name,username} projection; all fields in index
        col("member_cache").create_index(
            [("user_id", 1), ("first_name", 1), ("username", 1)]
        ),
        # * Serves batch username lookups and search operations
        col("member_cache").create_index([("username", 1)]),
        # * Serves name search operations (case-insensitive search by first_name)
        col("member_cache").create_index([("first_name", 1)]),
        col("warns").create_index([("user_id", 1), ("chat_id", 1), ("timestamp", -1)]),
        # * Serves /check history: every warning for a user across groups
        col("warns").create_index([("user_id", 1), ("timestamp", -1)]),
        # * Serves get_warns() oldest-first sort (timestamp ASC)
        col("warns").create_index([("user_id", 1), ("chat_id", 1), ("timestamp", 1)]),
        # * Serves warn expiry: delete_many({"timestamp": {"$lt": cutoff}}) COLLSCAN without this
        col("warns").create_index([("timestamp", 1)]),
        col("warn_counts").create_index([("user_id", 1), ("chat_id", 1)], unique=True),
        # * Serves warn expiry: delete_many({"updated_at": {"$lt": cutoff}}) COLLSCAN without this
        col("warn_counts").create_index([("updated_at", 1)]),
        # * Serves user_warn_groups() and federation_warn_count() filter + sort
        col("warn_counts").create_index(
            [("user_id", 1), ("count", 1), ("updated_at", -1)]
        ),
        # * Per-user kick / mute history for /check
        col("kicks").create_index([("user_id", 1), ("timestamp", -1)]),
        col("mutes").create_index([("user_id", 1), ("timestamp", -1)]),
        col("promotion_requests").create_index([("request_id", 1)], unique=True),
        col("promotion_requests").create_index([("target_id", 1), ("status", 1)]),
        # * One pending request per user: concurrent promotes for the same
        # * target collapse into a single queue entry instead of duplicates.
        col("promotion_requests").create_index(
            [("target_id", 1)],
            unique=True,
            partialFilterExpression={"status": "pending"},
        ),
        # * Serves queues_db.all_pending() which filters on status and sorts
        # * by requested_date; compound avoids an in-memory sort.
        col("promotion_requests").create_index([("status", 1), ("requested_date", 1)]),
        col("kicks").create_index([("chat_id", 1)]),
        col("mutes").create_index([("chat_id", 1)]),
        col("federated_groups").create_index([("is_active", 1)]),
        # * active_mutes: one document per muted user (upserted by set_active_mute)
        col("active_mutes").create_index([("user_id", 1)], unique=True),
        # * Serves active_mute_docs() bulk-fetch and get_active_mute() filtered by expiry
        col("active_mutes").create_index([("until_date", 1)]),
        # * Compound for get_active_mute() $or filter on specific user
        col("active_mutes").create_index([("user_id", 1), ("until_date", 1)]),
        # * TTL: MongoDB auto-deletes expired timed mutes (until_date <= now).
        # * Permanent mutes (until_date None/missing) are never TTL-expired,
        # * matching the query-time filter in get_active_mute/active_mute_docs,
        # * so this only prunes rows those queries already ignore.
        col("active_mutes").create_index([("until_date", 1)], expireAfterSeconds=0),
        # * TTL index: MongoDB auto-expires member_cache docs older than _MEMBER_CACHE_TTL_DAYS.
        # * Replaces the APScheduler weekly cleanup job, shrinking the scheduler surface.
        col("member_cache").create_index(
            [("last_updated", 1)], expireAfterSeconds=_MEMBER_CACHE_EXPIRE_S
        ),
        return_exceptions=True,
    )
    failed = [r for r in results if isinstance(r, BaseException)]
    if failed:
        for exc in failed:
            log.error("Index creation failed: %s", exc)
        # * Re-raise (preserving cancellation) instead of limping on without
        # * indexes: the startup fail-fast checks depend on this raising.
        raise failed[0]
    log.info(
        "MongoDB indexes ensured (%d/%d succeeded).",
        len(results) - len(failed),
        len(results),
    )


# ─────────────────────── Collection Shortcut ────────────────────── #
# * Convenience function to get a collection by name
# * Wraps the db() accessor for cleaner code


def col(name: str) -> AsyncIOMotorCollection:
    """Get a MongoDB collection by name."""
    return db()[name]


def is_connected() -> bool:
    """Return True when a MongoDB connection has been established via connect()."""
    return _db is not None


# ─────────────────── Circuit-Breaker Wrapper ────────────────────── #
# * Optional convenience for database helpers that want to route a
# * single Motor coroutine through the mongodb circuit breaker.
# * Raises CircuitOpenError when the circuit is OPEN (MongoDB is
# * considered unreachable) so callers can fast-fail without waiting
# * for a socket timeout.


async def db_call(coro: Awaitable[_T]) -> _T:
    """Execute a Motor coroutine through the MongoDB circuit breaker.

    Use this inside database helper modules for operations where a full
    socket-timeout wait is undesirable when the cluster is unreachable.

    Raises:
        CircuitOpenError: Circuit is OPEN; the call was rejected without
            touching MongoDB.
        Any exception the coroutine raises (also recorded as a failure;
            the exception propagates to the caller unchanged).

    """
    return await _mongo_cb.call(coro)
