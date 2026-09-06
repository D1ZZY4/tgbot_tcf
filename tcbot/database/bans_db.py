# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Bans collection helpers - manages all ban-related database operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from tcbot.database.documents import BanDoc
from tcbot.database.mongos import col, db_call, make_short_id
from tcbot.utils.time_and_date import utc_now

if TYPE_CHECKING:
    from datetime import datetime

    from motor.motor_asyncio import AsyncIOMotorCollection

# ─────────────────────── Collection Helpers ─────────────────────── #
# * Internal collection access and ID generation utilities
# * These are only used within this module for database interactions


def _bans() -> AsyncIOMotorCollection:
    return col("bans")


def make_ban_id() -> str:
    """Generate a unique short ID for a new ban record."""
    return make_short_id()


# ──────────────────────────── Retrieval ─────────────────────────── #
# * Functions to fetch ban data from the database
# * Includes both active ban queries and full ban record lookups


async def get_active_ban(user_id: int) -> BanDoc | None:
    """Get the currently active ban for a specific user."""
    return await db_call(
        _bans().find_one(
            {"banned_user_id": user_id, "is_active": True},
            sort=[("timestamp", -1), ("ban_id", -1)],
        )
    )


async def get_ban(ban_id: str) -> BanDoc | None:
    """Get any ban record by its unique ban_id."""
    return await db_call(_bans().find_one({"ban_id": ban_id}))


# ──────────────────────────── Mutations ─────────────────────────── #
# * Functions that modify ban records in the database
# * Includes creation, updates, deactivation, and metadata changes
# ! CRITICAL: These functions modify persistent data - always validate inputs


async def create_ban(
    target_id: int,
    reason: str,
    admin_id: int,
    proof_msg_id: int,
    log_msg_id: int,
    ban_id: str | None = None,
    *,
    until_date: datetime | None = None,
    duration_str: str | None = None,
) -> BanDoc:
    """Create a new ban record in the database."""
    auto_id = ban_id is None
    if auto_id:
        ban_id = make_ban_id()
    doc = {
        "ban_id": ban_id,
        "banned_user_id": target_id,
        "reason": reason,
        "admin_user_id": admin_id,
        "proof_message_id": proof_msg_id,
        "log_message_id": log_msg_id,
        "previous_proof_message_id": None,
        "previous_log_message_id": None,
        "timestamp": utc_now(),
        "updated_timestamp": None,
        "until_date": until_date,
        "duration_str": duration_str,
        "is_active": True,
        "update_count": 0,
        "review_message_id": None,
        "review_timestamp": None,
    }
    try:
        await db_call(_bans().insert_one(doc))
    except DuplicateKeyError:
        # * 10-char random ID collision (vanishingly rare but possible under
        # * concurrent bans): retry once with a fresh ID instead of failing
        # * the moderation action. Only safe for auto-generated IDs: callers
        # * that pass an explicit ban_id reuse it for the appeal link and log
        # * patch, so a silent swap would orphan those references. A second
        # * collision (or any explicit-ID collision) propagates to the caller.
        if not auto_id:
            raise
        doc["ban_id"] = make_ban_id()
        await db_call(_bans().insert_one(doc))
    return cast("BanDoc", doc)


async def update_ban(
    ban_id: str,
    reason: str,
    admin_id: int,
    new_proof_id: int,
    new_log_id: int = 0,
    old_proof_id: int = 0,
    old_log_id: int = 0,
    *,
    until_date: datetime | None = None,
    duration_str: str | None = None,
) -> BanDoc | None:
    """Update an existing ban record with new information."""
    return await db_call(
        _bans().find_one_and_update(
            {"ban_id": ban_id},
            {
                "$set": {
                    "reason": reason,
                    "admin_user_id": admin_id,
                    "proof_message_id": new_proof_id,
                    "log_message_id": new_log_id,
                    "previous_proof_message_id": old_proof_id,
                    "previous_log_message_id": old_log_id,
                    "updated_timestamp": utc_now(),
                    "until_date": until_date,
                    "duration_str": duration_str,
                },
                "$inc": {"update_count": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
    )


async def set_log_message_id(ban_id: str, log_msg_id: int) -> None:
    """Update only the log message ID for an existing ban."""
    await db_call(
        _bans().update_one(
            {"ban_id": ban_id},
            {"$set": {"log_message_id": log_msg_id}},
        )
    )


async def deactivate_ban(ban_id: str) -> bool:
    """Mark a ban as inactive (user is unbanned). Returns True if the ban was active."""
    r = await db_call(
        _bans().update_one({"ban_id": ban_id}, {"$set": {"is_active": False}})
    )
    return r.modified_count > 0


async def deactivate_all_active_bans(user_id: int) -> int:
    """Deactivate every active ban for a user. Returns the number of bans deactivated.

    Use this in unban and appeal-approve flows to ensure all active bans are
    cleared regardless of how many were created (guarding against duplicate
    active bans that may exist from earlier race conditions or re-ban paths).
    """
    r = await db_call(
        _bans().update_many(
            {"banned_user_id": user_id, "is_active": True},
            {"$set": {"is_active": False}},
        )
    )
    return r.modified_count


async def deactivate_extra_active_bans(user_id: int, keep_ban_id: str) -> int:
    """Deactivate all active bans for a user except the one with keep_ban_id.

    Called during the ban flow when an existing active ban is being updated
    to suppress any stale duplicate active bans while preserving the canonical
    record that is being reused. Returns the number of extras deactivated.
    """
    r = await db_call(
        _bans().update_many(
            {
                "banned_user_id": user_id,
                "is_active": True,
                "ban_id": {"$ne": keep_ban_id},
            },
            {"$set": {"is_active": False}},
        )
    )
    return r.modified_count


async def set_review(ban_id: str, msg_id: int) -> None:
    """Attach a review message ID to a ban record."""
    await db_call(
        _bans().update_one(
            {"ban_id": ban_id},
            {"$set": {"review_message_id": msg_id, "review_timestamp": utc_now()}},
        )
    )


async def clear_review(ban_id: str) -> None:
    """Clear review_message_id from a ban, allowing a new appeal to be submitted.

    Called after an appeal is rejected so the banned user is not permanently
    locked out from submitting a second appeal.
    """
    await db_call(
        _bans().update_one(
            {"ban_id": ban_id},
            {"$set": {"review_message_id": None, "review_timestamp": None}},
        )
    )


async def set_rejected_by(ban_id: str, admin_id: int, admin_name: str) -> None:
    """Persist the rejector identity and rejection timestamp on a ban document.

    Called on appeal reject so the audit trail is preserved even if the log
    message is later deleted or the log channel is unavailable.
    """
    await db_call(
        _bans().update_one(
            {"ban_id": ban_id},
            {
                "$set": {
                    "rejected_by_id": admin_id,
                    "rejected_by_name": admin_name,
                    "rejected_at": utc_now(),
                }
            },
        )
    )


async def set_appeal_log_msg(
    ban_id: str,
    msg_id: int,
    submitted_at: datetime | None = None,
    appeal_link: str = "",
) -> None:
    """Attach appeal-related metadata to a ban record."""
    await db_call(
        _bans().update_one(
            {"ban_id": ban_id},
            {
                "$set": {
                    "appeal_log_msg_id": msg_id,
                    "appeal_submitted_at": submitted_at or utc_now(),
                    "appeal_link": appeal_link,
                }
            },
        )
    )


# ─────────────────────────── Statistics ─────────────────────────── #
# * Aggregation and counting functions for ban statistics
# * Optimized queries for performance-critical operations


async def active_ban_count() -> int:
    """Count the total number of currently active bans."""
    return await db_call(_bans().count_documents({"is_active": True}))


async def active_bans() -> list[BanDoc]:
    """Get all active ban records in the database."""
    return await db_call(
        _bans()
        .find(
            {"is_active": True},
            {"_id": 0},
            sort=[("timestamp", -1), ("ban_id", -1)],
        )
        .to_list(None)
    )


async def active_ban_user_ids() -> list[int]:
    """Return only the user IDs of all active bans (projection-only, fastest path)."""
    docs = await db_call(
        _bans()
        .find(
            {"is_active": True},
            {"_id": 0, "banned_user_id": 1},
            sort=[("timestamp", -1), ("ban_id", -1)],
        )
        .to_list(None)
    )
    return [doc["banned_user_id"] for doc in docs]


# ─────────────────────── Per-user history ───────────────────────── #


async def user_bans(user_id: int) -> list[BanDoc]:
    """Return every ban (active + inactive) for a user, newest first."""
    return await db_call(
        _bans()
        .find(
            {"banned_user_id": user_id},
            {"_id": 0},
            sort=[("timestamp", -1), ("ban_id", -1)],
        )
        .to_list(None)
    )


async def user_ban_count(user_id: int) -> int:
    """Count every ban ever issued against the user."""
    return await db_call(_bans().count_documents({"banned_user_id": user_id}))


async def user_appeal_count(user_id: int) -> int:
    """Count bans on this user that ever had an appeal submitted."""
    return await db_call(
        _bans().count_documents(
            {
                "banned_user_id": user_id,
                "appeal_log_msg_id": {"$ne": None, "$exists": True},
            }
        )
    )
