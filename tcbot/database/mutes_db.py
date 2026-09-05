# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Mute log helpers - tracks all mute events in groups."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tcbot.database.documents import ActiveMuteDoc, MuteDoc
from tcbot.database.mongos import col, db_call
from tcbot.database.types import ChatId, UserId
from tcbot.utils.time_and_date import utc_now

if TYPE_CHECKING:
    from datetime import datetime

    from motor.motor_asyncio import AsyncIOMotorCollection

# ─────────────────────── Collection Helpers ─────────────────────── #
# * Internal collection access utilities for the mutes database


def _mutes() -> AsyncIOMotorCollection:
    return col("mutes")


def _active_mutes() -> AsyncIOMotorCollection:
    return col("active_mutes")


# ──────────────────────────── Mutations ─────────────────────────── #
# * Functions that create or modify mute log records
# * Primarily used for audit logging of moderation actions


async def log_mute(
    user_id: int,
    chat_id: int,
    reason: str,
    admin_id: int,
    *,
    duration_secs: int | None = None,
) -> None:
    """Log a mute event to the database for audit purposes.

    ``duration_secs`` is ``None`` for a permanent mute, or the total number of
    seconds for a timed mute. Storing it allows mute-history views to show
    how long a restriction was intended to last.
    """
    doc: MuteDoc = {
        "user_id": UserId(user_id),
        "chat_id": ChatId(chat_id),
        "reason": reason,
        "admin_id": UserId(admin_id),
        "timestamp": utc_now(),
    }
    if duration_secs is not None:
        doc["duration_secs"] = duration_secs
    await db_call(_mutes().insert_one(doc))


# ──────────────────────── Active mute store ─────────────────────── #
# * One document per muted user in `active_mutes`.
# * set_active_mute is called by _execute_mute after fan_out succeeds.
# * clear_active_mute is called by execute_unmute.
# * get_active_mute / active_mute_docs filter out expired timed mutes at
#   query time so no background cleanup job is needed.


async def set_active_mute(user_id: int, *, until: datetime | None = None) -> None:
    """Upsert the active mute entry for a user.

    Pass ``until=None`` for a permanent mute.  For timed mutes, pass the
    UTC datetime when the restriction expires (matches the ``until_date``
    value passed to ``restrict_chat_member``).
    """
    await db_call(
        _active_mutes().update_one(
            {"user_id": UserId(user_id)},
            {
                "$set": {
                    "user_id": UserId(user_id),
                    "until_date": until,
                    "timestamp": utc_now(),
                }
            },
            upsert=True,
        )
    )


async def clear_active_mute(user_id: int) -> None:
    """Remove the active mute entry when a user is unmuted."""
    await db_call(_active_mutes().delete_one({"user_id": UserId(user_id)}))


async def get_active_mute(user_id: int) -> ActiveMuteDoc | None:
    """Return the active mute for a user, or None if none exists or has expired."""
    now = utc_now()
    return await db_call(
        _active_mutes().find_one(
            {
                "user_id": UserId(user_id),
                "$or": [{"until_date": None}, {"until_date": {"$gt": now}}],
            }
        )
    )


async def active_mute_docs() -> list[ActiveMuteDoc]:
    """Return all currently active mute records (used for group-connect replay).

    Expired timed mutes (``until_date <= now``) are excluded at query time.
    """
    now = utc_now()
    return await db_call(
        _active_mutes()
        .find(
            {"$or": [{"until_date": None}, {"until_date": {"$gt": now}}]},
            {"user_id": 1, "until_date": 1, "_id": 0},
        )
        .to_list(None)
    )


# ─────────────────────── Per-user history ───────────────────────── #


async def user_mutes(user_id: int) -> list[MuteDoc]:
    """Return every mute record for a user, newest first."""
    return await db_call(
        _mutes()
        .find({"user_id": user_id}, {"_id": 0}, sort=[("timestamp", -1)])
        .to_list(None)
    )


async def user_mute_count(user_id: int) -> int:
    """Count every mute ever logged against the user."""
    return await db_call(_mutes().count_documents({"user_id": user_id}))
