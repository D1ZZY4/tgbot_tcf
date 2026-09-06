# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Warnings collection helpers - manages user warning records in groups."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pymongo import ReturnDocument

from tcbot.database.documents import WarnCountDoc, WarnDoc
from tcbot.database.mongos import col, db_call
from tcbot.utils.time_and_date import utc_now

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection

log = logging.getLogger(__name__)

# ─────────────────────── Collection Helpers ─────────────────────── #
# * Internal collection access utilities for the warns database


def _warns() -> AsyncIOMotorCollection:
    return col("warns")


def _warn_counts() -> AsyncIOMotorCollection:
    return col("warn_counts")


def _warn_key(user_id: int, chat_id: int) -> dict[str, int]:
    return {"user_id": user_id, "chat_id": chat_id}


async def _sync_warn_count(user_id: int, chat_id: int) -> int:
    """Read the counter doc, or backfill it from warn history when missing.

    Uses an atomic ``find_one_and_update`` upsert so concurrent callers cannot
    double-backfill the same missing counter document.
    """
    doc: WarnCountDoc | None = await db_call(
        _warn_counts().find_one(
            _warn_key(user_id, chat_id),
            {"_id": 0, "count": 1},
        )
    )
    if doc is not None:
        return int(doc.get("count", 0))

    count = await db_call(_warns().count_documents(_warn_key(user_id, chat_id)))
    if count > 0:
        # * Atomic upsert: only one concurrent caller wins the insert; others
        # * get the newly inserted doc back and re-read its count.
        updated = await db_call(
            _warn_counts().find_one_and_update(
                _warn_key(user_id, chat_id),
                {
                    "$setOnInsert": {
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "count": count,
                        "updated_at": utc_now(),
                    },
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
                projection={"_id": 0, "count": 1},
            )
        )
        if updated is not None:
            return int(updated.get("count", count))
    return count


async def _store_warn_count(user_id: int, chat_id: int, count: int) -> None:
    """Persist the counter doc for a user/chat pair."""
    if count <= 0:
        await db_call(_warn_counts().delete_one(_warn_key(user_id, chat_id)))
        return
    await db_call(
        _warn_counts().update_one(
            _warn_key(user_id, chat_id),
            {
                "$set": {
                    "count": count,
                    "updated_at": utc_now(),
                },
                "$setOnInsert": {
                    "user_id": user_id,
                    "chat_id": chat_id,
                },
            },
            upsert=True,
        )
    )


# ──────────────────────────── Mutations ─────────────────────────── #
# * Functions that modify warning records in the database
# * Includes adding, removing, and clearing warnings
# ! CRITICAL: These functions modify per-chat warning counts


async def add_warn(user_id: int, reason: str, admin_id: int, chat_id: int) -> int:
    """Add a new warning to a user in a specific chat."""
    c = _warns()
    inserted = await db_call(
        c.insert_one(
            {
                "user_id": user_id,
                "reason": reason,
                "admin_id": admin_id,
                "chat_id": chat_id,
                "timestamp": utc_now(),
            }
        )
    )
    try:
        counter = await db_call(
            _warn_counts().find_one_and_update(
                _warn_key(user_id, chat_id),
                {
                    "$inc": {"count": 1},
                    "$set": {"updated_at": utc_now()},
                    "$setOnInsert": {
                        # * On insert the missing ``count`` field is treated
                        # * as zero by MongoDB; ``$inc`` then sets it to 1.
                        # * Do NOT add ``count: 0`` here -- it would conflict
                        # * with the ``$inc`` modifier and raise
                        # * ``OperationFailure: ConflictingUpdateOperators``
                        # * (MongoDB error code 40) on every first warn.
                        "user_id": user_id,
                        "chat_id": chat_id,
                    },
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
                projection={"_id": 0, "count": 1},
            )
        )
    except Exception:
        log.exception("add_warn counter update failed; rolling back warn insert")
        try:
            await db_call(c.delete_one({"_id": inserted.inserted_id}))
        except Exception as rollback_exc:
            log.warning(
                "add_warn rollback failed for inserted_id=%s: %s",
                inserted.inserted_id,
                rollback_exc,
            )
        raise
    if counter is None:
        return await _sync_warn_count(user_id, chat_id)
    return int(counter.get("count", 0))


# ─────────────────────── Queries & Retrieval ────────────────────── #
# * Functions to fetch warning data from the database
# * Includes counting, listing, and retrieving user warnings


async def warn_count(user_id: int, chat_id: int) -> int:
    """Get the current number of warnings for a user in a specific chat."""
    return await _sync_warn_count(user_id, chat_id)


async def clear_warns(user_id: int, chat_id: int) -> int:
    """Remove ALL warnings for a user in a specific chat."""
    warn_del, _cnt_del = await asyncio.gather(
        db_call(_warns().delete_many(_warn_key(user_id, chat_id))),
        db_call(_warn_counts().delete_one(_warn_key(user_id, chat_id))),
        return_exceptions=True,
    )
    if isinstance(_cnt_del, BaseException):
        log.warning(
            "clear_warns counter delete failed for user=%d chat=%d: %s",
            user_id,
            chat_id,
            _cnt_del,
        )
    return warn_del.deleted_count if not isinstance(warn_del, BaseException) else 0


async def clear_all_warns(user_id: int) -> int:
    """Remove ALL warnings for a user across every federation group.

    Used on federation auto-ban to ensure the user starts with a clean warn
    slate in every group after a potential unban, preventing immediate re-ban
    from stale per-group counts accumulated before the federation ban.
    """
    warn_del, _cnt_del = await asyncio.gather(
        db_call(_warns().delete_many({"user_id": user_id})),
        db_call(_warn_counts().delete_many({"user_id": user_id})),
        return_exceptions=True,
    )
    if isinstance(_cnt_del, BaseException):
        log.warning(
            "clear_all_warns counter delete failed for user=%d: %s",
            user_id,
            _cnt_del,
        )
    return warn_del.deleted_count if not isinstance(warn_del, BaseException) else 0


async def get_warns(user_id: int, chat_id: int) -> list[WarnDoc]:
    """Return all warn documents for a user in a chat, oldest first."""
    return await db_call(
        _warns()
        .find(
            {"user_id": user_id, "chat_id": chat_id},
            {
                "_id": 0,
                "user_id": 1,
                "reason": 1,
                "admin_id": 1,
                "chat_id": 1,
                "timestamp": 1,
            },
            sort=[("timestamp", 1)],
        )
        .to_list(length=None)
    )


async def remove_last_warn(user_id: int, chat_id: int) -> bool:
    """Delete the most recent warn document. Returns True if one was removed."""
    doc = await db_call(
        _warns().find_one(
            _warn_key(user_id, chat_id),
            {"_id": 1},
            sort=[("timestamp", -1), ("_id", -1)],
        )
    )
    if not doc:
        return False

    # Delete warn and update counter in parallel
    del_res, counter = await asyncio.gather(
        db_call(_warns().delete_one({"_id": doc["_id"]})),
        db_call(
            _warn_counts().find_one_and_update(
                {
                    **_warn_key(user_id, chat_id),
                    "count": {"$gt": 0},
                },
                {"$inc": {"count": -1}, "$set": {"updated_at": utc_now()}},
                return_document=ReturnDocument.AFTER,
                projection={"_id": 0, "count": 1},
            )
        ),
        return_exceptions=True,
    )

    if isinstance(del_res, BaseException):
        log.warning(
            "remove_last_warn delete failed for user=%d chat=%d: %s",
            user_id,
            chat_id,
            del_res,
        )
        count = await db_call(_warns().count_documents(_warn_key(user_id, chat_id)))
        await _store_warn_count(user_id, chat_id, count)
        return False
    if del_res.deleted_count == 0:
        count = await db_call(_warns().count_documents(_warn_key(user_id, chat_id)))
        await _store_warn_count(user_id, chat_id, count)
        return False

    if isinstance(counter, BaseException) or counter is None:
        count = await db_call(_warns().count_documents(_warn_key(user_id, chat_id)))
        await _store_warn_count(user_id, chat_id, count)
    return True


# ─────────────────────── Per-user history ───────────────────────── #


async def user_total_warns(user_id: int) -> int:
    """Total number of warning rows recorded against the user (all groups)."""
    return await db_call(_warns().count_documents({"user_id": user_id}))


async def user_warn_groups(user_id: int) -> list[tuple[int, int]]:
    """Return [(chat_id, count), ...] for every group where the user has warns, newest first."""
    docs = await db_call(
        _warn_counts()
        .find(
            {"user_id": user_id, "count": {"$gt": 0}},
            {"_id": 0, "chat_id": 1, "count": 1},
            sort=[("updated_at", -1)],
        )
        .to_list(length=None)
    )
    return [(int(d["chat_id"]), int(d["count"])) for d in docs]


async def migrate_records(old_chat_id: int, new_chat_id: int) -> bool:
    """Repoint every warn record and counter from ``old_chat_id`` to ``new_chat_id``.

    Called when a basic group migrates to a supergroup. Both ``warns`` (audit
    history) and ``warn_counts`` (the per-group counter documents that gate
    the auto-ban threshold) are keyed by ``chat_id``, so without this the
    supergroup would silently start with a clean slate and lose all warning
    history from the legacy chat. Returns ``True`` if any record was updated.
    """
    results = await asyncio.gather(
        db_call(
            _warns().update_many(
                {"chat_id": old_chat_id},
                {"$set": {"chat_id": new_chat_id}},
            )
        ),
        db_call(
            _warn_counts().update_many(
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
                "warns_db.migrate_records (%d -> %d) DB call failed: %s",
                old_chat_id,
                new_chat_id,
                r,
            )
        elif r.matched_count > 0:
            matched_any = True
    return matched_any


async def federation_warn_count(user_id: int) -> int:
    """Total active warn count for a user across all federation chats.

    Sums ``count`` across all ``warn_counts`` documents for the user via a
    server-side ``$group`` aggregation, avoiding a Python-side sum over a
    potentially large result set.  Returns 0 when the user has no active
    warnings.
    """
    pipeline = [
        {"$match": {"user_id": user_id, "count": {"$gt": 0}}},
        {"$group": {"_id": None, "total": {"$sum": "$count"}}},
    ]
    result = await db_call(_warn_counts().aggregate(pipeline).to_list(length=1))
    return int(result[0]["total"]) if result else 0
