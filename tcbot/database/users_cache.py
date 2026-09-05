# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Member profile cache helpers.

This module handles all member_cache collection operations for Telegram user profiles.
Do not mix with users_roles.py which handles tc_owners, tc_admins, and tc_roles.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from tcbot.database.cache import CACHE_MISS, user_mention_cache
from tcbot.database.documents import UserDoc
from tcbot.database.mongos import col, db_call
from tcbot.utils.time_and_date import utc_now

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection

# ─────────────────────── Collection Helpers ─────────────────────── #
# * Internal collection access utilities for the member_cache database


def _members() -> AsyncIOMotorCollection:
    return col("member_cache")


# ────────── Member cache mutations ─────────


async def upsert_user(
    user_id: int,
    username: str | None,
    first_name: str,
    last_name: str | None = None,
) -> None:
    """Update or insert a user's profile information into the cache.

    ``username`` and ``last_name`` are treated as "unknown, preserve existing":
    when a caller passes ``None`` for either field, the previous stored value
    is left intact. This lets moderator-side flows (ban, kick, promote, /check)
    refresh the displayed name without wiping the username the user may have
    set later. Pass an explicit empty string only if you intend to clear the
    field.
    """
    now = utc_now()
    update: dict[str, object] = {
        "user_id": user_id,
        "first_name": first_name,
        "last_updated": now,
    }
    if username is not None:
        update["username"] = username
    if last_name is not None:
        update["last_name"] = last_name
    await db_call(
        _members().update_one(
            {"user_id": user_id},
            {
                "$set": update,
                "$setOnInsert": {
                    "commit_date": now,
                },
            },
            upsert=True,
        )
    )
    # * Invalidate mention cache so the next read reflects the updated profile.
    user_mention_cache.invalidate(user_id)


async def upsert_user_if_changed(
    user_id: int,
    username: str | None,
    first_name: str,
    last_name: str | None = None,
) -> bool:
    """Write user profile to DB only when identity data has changed since last cache entry.

    Checks the L1 in-memory mention cache first.  When the cached
    (first_name, username, last_name) triple matches the incoming data, the
    DB write is skipped entirely and False is returned.  This eliminates
    the MongoDB round-trip for the vast majority of updates (where identity
    data has not changed) and makes the hot-path member-cache handler
    nearly free.  Legacy two-element cache entries count as changed.

    Returns True when a DB write was performed, False when skipped.
    """
    cached = user_mention_cache.get(user_id)
    if cached is not CACHE_MISS:
        # * Compare the full triple: last_name-only changes must also write.
        data: list[str | None] = cached  # type: ignore[assignment]
        current = _cached_triple(data)
        if current == (first_name, username, last_name):
            return False
    await upsert_user(user_id, username, first_name, last_name)
    return True


# * L1 mention-cache entries are [first_name, username, last_name] triples.
# * Readers only use indexes 0 and 1; index 2 exists so change detection
# * notices last_name-only updates. The not-found sentinel is all-None.
_NOT_FOUND_SENTINEL: list[str | None] = [None, None, None]


def _cached_triple(data: list[str | None]) -> tuple[str | None, str | None, str | None]:
    """Split a cache entry into (first_name, username, last_name).

    Legacy L2 pairs stored before last_name tracking have length 2; the
    missing element reads as None, which correctly counts as changed.
    """
    return (
        data[0] if len(data) > 0 else None,
        data[1] if len(data) > 1 else None,
        data[2] if len(data) > 2 else None,
    )


def has_recent_identity_attempt(user_id: int) -> bool:
    """Return True when L1 holds any mention entry (data or sentinel) for the user.

    Identity resolvers use this to skip a repeat Telegram lookup while a
    previous attempt is still cached; entries expire with the L1 TTL.
    """
    return user_mention_cache.get(user_id) is not CACHE_MISS


def remember_identity(
    user_id: int,
    first_name: str | None,
    username: str | None,
    last_name: str | None,
) -> None:
    """Mirror a resolved identity into L1 without touching the database.

    Used after a Telegram-side resolve: ``upsert_user`` invalidates rather
    than populates, so without this the next read would miss L1. A fully
    empty identity stores the not-found sentinel.
    """
    if first_name is None:
        user_mention_cache.put(user_id, list(_NOT_FOUND_SENTINEL))
    else:
        user_mention_cache.put(user_id, [first_name, username, last_name])


# ───────── Member cache queries ─────────


async def get_user(user_id: int) -> UserDoc | None:
    """Get the full cached profile for a specific user."""
    return await db_call(_members().find_one({"user_id": user_id}))


async def get_user_mention_data(user_id: int) -> tuple[str, str | None]:
    """Return (first_name, username) for mention formatting (L1->L2->DB cached).

    Uses ``user_mention_cache`` (Redis-backed TwoLevelCache) to avoid MongoDB
    round-trips on repeated lookups.  Cache is invalidated on every ``upsert_user``.

    Cache sentinel: ``[None, None, None]`` is stored when the user has no document in
    ``member_cache``.  The consumer converts ``None`` → ``str(user_id)`` so the
    returned tuple always contains a non-empty string as the first element.
    """

    async def _fetch() -> list[str | None]:
        doc = await db_call(
            _members().find_one(
                {"user_id": user_id}, {"first_name": 1, "username": 1, "last_name": 1}
            )
        )
        if doc:
            return [
                doc.get("first_name") or str(user_id),
                doc.get("username"),
                doc.get("last_name"),
            ]
        # * Sentinel: user has no member_cache document.  Stored as all-None
        # * so get_first_name() can distinguish "real name" from "no record" and
        # * return the caller's fallback instead of a raw numeric ID string.
        return list(_NOT_FOUND_SENTINEL)

    data = await user_mention_cache.get_or_fetch(user_id, _fetch)
    # * data[0] is None when the user has no member_cache document (sentinel).
    name = cast("str", data[0]) if data[0] is not None else str(user_id)
    return (name, data[1])


async def get_mention_data_batch(
    user_ids: list[int],
) -> dict[int, tuple[str, str | None]]:
    """Fetch (first_name, username) for multiple users, checking cache for each ID first.

    Cache-aware: IDs found in L1 are returned immediately; only uncached IDs trigger
    a batch MongoDB query.  Newly fetched data is populated into the mention cache.
    """
    if not user_ids:
        return {}

    result: dict[int, tuple[str, str | None]] = {}
    missing: list[int] = []

    # * Check L1 in-memory cache for each user_id before hitting MongoDB.
    for uid in user_ids:
        cached = user_mention_cache.get(uid)
        if cached is not CACHE_MISS:
            data = cast("list[str | None]", cached)
            # * data[0] may be None (not-found sentinel); fall back to str(uid).
            fname = cast("str", data[0]) if data[0] is not None else str(uid)
            result[uid] = (
                fname,
                cast("str | None", data[1] if len(data) > 1 else None),
            )
        else:
            missing.append(uid)

    if not missing:
        return result

    # * Batch-fetch only uncached users from MongoDB in a single round-trip.
    docs = await db_call(
        _members()
        .find(
            {"user_id": {"$in": missing}},
            {"user_id": 1, "first_name": 1, "username": 1, "last_name": 1},
        )
        .to_list(None)
    )
    for doc in docs:
        uid = doc["user_id"]
        fname = doc.get("first_name") or str(uid)
        uname = doc.get("username")
        # * Populate L1 (and fire-and-forget L2 Redis write) for next lookup.
        user_mention_cache.put(uid, [fname, uname, doc.get("last_name")])
        result[uid] = (fname, uname)

    # * Fill fallback for users not found in DB either and cache the sentinel so
    # * subsequent calls (get_user_mention_data, get_first_name, this function)
    # * skip the MongoDB round-trip on the next lookup.
    for uid in missing:
        if uid not in result:
            user_mention_cache.put(uid, list(_NOT_FOUND_SENTINEL))
            result[uid] = (str(uid), None)

    return result


async def get_first_names_batch(user_ids: list[int]) -> dict[int, str]:
    """Fetch first names for multiple users in a single query.

    Optimized batch query that replaces multiple individual get_first_name()
    calls with a single database roundtrip.
    """
    if not user_ids:
        return {}
    docs = await db_call(
        _members()
        .find({"user_id": {"$in": user_ids}}, {"user_id": 1, "first_name": 1})
        .to_list(None)
    )
    result = {
        doc["user_id"]: doc.get("first_name") or str(doc["user_id"]) for doc in docs
    }
    # Fill in missing users with defaults
    for uid in user_ids:
        if uid not in result:
            result[uid] = str(uid)
    return result


async def get_first_name(user_id: int, fallback: str = "") -> str:
    """Return cached first_name or caller's fallback (L1 → L2 Redis → DB cached).

    Routes through ``user_mention_cache.get_or_fetch`` so all three layers are
    checked in order and both L1 and L2 are populated on a miss -- exactly the
    same path as ``get_user_mention_data``.  Calling this function never causes
    a redundant MongoDB round-trip for a user already fetched by either helper.

    When the user has no document in ``member_cache``, ``_fetch`` stores the
    sentinel ``[None, None, None]`` in the cache.  The sentinel is distinguished from a
    real name so the caller's ``fallback`` is returned instead of a raw numeric ID
    string (e.g. ``"Admin"`` instead of ``"123456789"``).
    """

    async def _fetch() -> list[str | None]:
        doc = await db_call(
            _members().find_one(
                {"user_id": user_id}, {"first_name": 1, "username": 1, "last_name": 1}
            )
        )
        if doc:
            return [
                doc.get("first_name") or str(user_id),
                doc.get("username"),
                doc.get("last_name"),
            ]
        # * Sentinel: user has no member_cache document.  All-None is used
        # * (not [str(user_id), None, None]) so the not-found case is unambiguous --
        # * a real first_name is never None, but str(user_id) could coincide
        # * with an actual numeric display name and would suppress fallback.
        return list(_NOT_FOUND_SENTINEL)

    data = await user_mention_cache.get_or_fetch(user_id, _fetch)
    # * data[0] is None when the sentinel is in cache (user not in member_cache DB).
    # * Use caller's fallback in that case; otherwise return the real name.
    return cast("str", data[0]) if data[0] is not None else fallback


async def total_users() -> int:
    """Get the total number of unique users in the cache."""
    return await db_call(_members().estimated_document_count())


_PAGE_LIMIT = 200

# * Allowed sort keys for all_users(). Unvalidated strings would force an
# * unindexed COLLSCAN plus an in-memory sort, then silently truncate at
# * _PAGE_LIMIT. Keep the set aligned with UserDoc fields and existing indexes.
_ALLOWED_USER_SORTS: frozenset[str] = frozenset(
    {"user_id", "username", "first_name", "last_name", "commit_date", "last_updated"}
)


async def all_users(*, sort_by: str = "first_name") -> list[UserDoc]:
    """Return cached users capped at ``_PAGE_LIMIT``, sorted by ``sort_by`` (default: first name).

    Used by the ``/tcstats`` Users drill-down. The cap prevents unbounded
    scans on large caches; pagination in the caller handles the rest.
    """
    if sort_by not in _ALLOWED_USER_SORTS:
        sort_by = "first_name"
    sort_dir = 1 if sort_by != "last_updated" else -1
    return await db_call(
        _members()
        .find(
            {},
            {
                "_id": 0,
                "user_id": 1,
                "username": 1,
                "first_name": 1,
                "last_name": 1,
                "commit_date": 1,
                "last_updated": 1,
            },
        )
        .sort(sort_by, sort_dir)
        .limit(_PAGE_LIMIT)
        .to_list(length=None)
    )


async def search_by_name(needle: str, limit: int = 5) -> list[UserDoc]:
    """Return up to ``limit`` cached users whose name or username contains ``needle``.

    Runs a server-side case-insensitive regex query with a result cap so only
    matching documents (and only the fields needed for target resolution) travel
    over the wire, regardless of cache size. This replaces the old pattern of
    loading all users into Python and scanning linearly.
    """
    if not needle:
        return []
    # * Anchored so "dan" matches a name that starts with "dan" (e.g. "daniel"),
    # * not one that merely contains "dan" mid-string (e.g. "randy").
    pattern = {"$regex": f"^{re.escape(needle)}", "$options": "i"}
    return await db_call(
        _members()
        .find(
            {"$or": [{"first_name": pattern}, {"username": pattern}]},
            {"user_id": 1, "first_name": 1, "username": 1, "_id": 0},
        )
        .limit(limit)
        .to_list(length=limit)
    )
