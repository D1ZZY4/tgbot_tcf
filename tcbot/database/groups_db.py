# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Federated groups and pending joins collection helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from tcbot.database.cache import (
    _ALL_GROUPS_KEY,
    active_groups_cache,
    connected_cache,
)
from tcbot.database.documents import GroupDoc, PendingGroupDoc
from tcbot.database.mongos import col, db_call
from tcbot.utils.time_and_date import utc_now

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection

log = logging.getLogger(__name__)

# ─────────────────────── Collection Helpers ─────────────────────── #
# * Internal collection access utilities for groups database


def _groups() -> AsyncIOMotorCollection:
    return col("federated_groups")


def _pending() -> AsyncIOMotorCollection:
    return col("pending_joins")


# ──────────────────── Group Queries & Mutations ─────────────────── #
# * Functions to manage active connected groups in the federation
# * Includes caching to minimize database roundtrips
# ! CRITICAL: Cache invalidation is crucial here - always clear caches on changes


async def get_group(chat_id: int) -> GroupDoc | None:
    """Get the full group record for a specific chat ID."""
    return await db_call(_groups().find_one({"chat_id": chat_id}))


async def get_group_titles(chat_ids: list[int]) -> dict[int, str]:
    """Return {chat_id: title} for the given chat_ids; missing groups are absent."""
    if not chat_ids:
        return {}
    docs = await db_call(
        _groups()
        .find({"chat_id": {"$in": chat_ids}}, {"_id": 0, "chat_id": 1, "title": 1})
        .to_list(length=None)
    )
    return {int(d["chat_id"]): d.get("title") or str(d["chat_id"]) for d in docs}


async def refresh_group_title(chat_id: int, title: str) -> bool:
    """Update the stored title when live Telegram reports a different one.

    Returns True when a change was written (and the groups cache
    invalidated), False when already current or the group is unknown.
    """
    doc = await db_call(
        _groups().find_one({"chat_id": chat_id}, {"_id": 0, "title": 1})
    )
    if doc is None or doc.get("title") == title:
        return False
    await db_call(
        _groups().update_one({"chat_id": chat_id}, {"$set": {"title": title}})
    )
    active_groups_cache.invalidate(_ALL_GROUPS_KEY)
    return True


async def is_connected(chat_id: int) -> bool:
    """Check if a group is currently active and connected to the federation (L1->L2->DB cached)."""

    async def _fetch() -> bool:
        return (
            await db_call(
                _groups().find_one({"chat_id": chat_id, "is_active": True}, {"_id": 1})
            )
            is not None
        )

    return cast("bool", await connected_cache.get_or_fetch(chat_id, _fetch))


async def add_group(chat_id: int, title: str, added_by: int) -> None:
    """Add or update a group in the federated_groups collection."""
    await db_call(
        _groups().update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "chat_id": chat_id,
                    "title": title,
                    "added_by": added_by,
                    "added_date": utc_now(),
                    "is_active": True,
                }
            },
            upsert=True,
        )
    )
    connected_cache.put(chat_id, True)  # noqa: FBT003
    active_groups_cache.invalidate(_ALL_GROUPS_KEY)


async def deactivate_group(chat_id: int) -> bool:
    """Mark a group as inactive (disconnect from federation)."""
    r = await db_call(
        _groups().update_one({"chat_id": chat_id}, {"$set": {"is_active": False}})
    )
    if r.matched_count > 0:
        connected_cache.put(chat_id, False)  # noqa: FBT003
    active_groups_cache.invalidate(_ALL_GROUPS_KEY)
    return r.matched_count > 0


async def active_groups() -> list[GroupDoc]:
    """Get all currently active and connected groups (L1->L2->DB cached)."""

    async def _fetch() -> list[GroupDoc]:
        return await db_call(
            _groups().find({"is_active": True}, {"_id": 0}).to_list(None)
        )

    return cast(
        "list[GroupDoc]",
        await active_groups_cache.get_or_fetch(_ALL_GROUPS_KEY, _fetch),
    )


async def active_group_count() -> int:
    """Count the number of currently active connected groups."""
    return await db_call(_groups().count_documents({"is_active": True}))


async def migrate_group(old_chat_id: int, new_chat_id: int) -> bool:
    """Update all group records from ``old_chat_id`` to ``new_chat_id`` after migration.

    Called when a basic group migrates to a supergroup. Updates both the
    ``federated_groups`` and ``pending_joins`` collections and invalidates
    the relevant cache entries. Returns ``True`` if any record was updated.
    """
    results = await asyncio.gather(
        db_call(
            _groups().update_one(
                {"chat_id": old_chat_id},
                {"$set": {"chat_id": new_chat_id}},
            )
        ),
        db_call(
            _pending().update_one(
                {"chat_id": old_chat_id},
                {"$set": {"chat_id": new_chat_id}},
            )
        ),
        return_exceptions=True,
    )
    matched_any = False
    for r in results:
        if isinstance(r, BaseException):
            log.error(
                "migrate_group (%d -> %d) DB call failed: %s",
                old_chat_id,
                new_chat_id,
                r,
            )
        elif r.matched_count > 0:
            matched_any = True
    if matched_any:
        connected_cache.put(old_chat_id, False)  # noqa: FBT003
        connected_cache.put(new_chat_id, True)  # noqa: FBT003
        active_groups_cache.invalidate(_ALL_GROUPS_KEY)
    return matched_any


# ──────────────────── Pending Joins Management ──────────────────── #
# * Functions to manage groups waiting to be approved into the federation
# * Tracks join requests with all necessary metadata


async def add_pending(chat_id: int, title: str, owner_id: int, message_id: int) -> None:
    """Add or update a pending join request from a group."""
    await db_call(
        _pending().update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "chat_id": chat_id,
                    "title": title,
                    "owner_id": owner_id,
                    "message_id": message_id,
                    "added_date": utc_now(),
                }
            },
            upsert=True,
        )
    )


async def get_pending(chat_id: int) -> PendingGroupDoc | None:
    """Get a pending join request by chat ID."""
    return await db_call(_pending().find_one({"chat_id": chat_id}))


async def remove_pending(chat_id: int) -> None:
    """Remove a pending join request after it's approved or rejected."""
    await db_call(_pending().delete_one({"chat_id": chat_id}))
