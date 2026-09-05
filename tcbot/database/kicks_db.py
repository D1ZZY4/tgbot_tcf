# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Kick log helpers - tracks all kick events in groups."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tcbot.database.documents import KickDoc
from tcbot.database.mongos import col, db_call
from tcbot.database.types import ChatId, UserId
from tcbot.utils.time_and_date import utc_now

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection

# ─────────────────────── Collection Helpers ─────────────────────── #
# * Internal collection access utilities for the kicks database


def _kicks() -> AsyncIOMotorCollection:
    return col("kicks")


# ──────────────────────────── Mutations ─────────────────────────── #
# * Functions that create or modify kick log records
# * Used exclusively for audit logging of moderation actions


async def log_kick(user_id: int, chat_id: int, reason: str, admin_id: int) -> None:
    """Log a kick event to the database for audit purposes."""
    doc: KickDoc = {
        "user_id": UserId(user_id),
        "chat_id": ChatId(chat_id),
        "reason": reason,
        "admin_id": UserId(admin_id),
        "timestamp": utc_now(),
    }
    await db_call(_kicks().insert_one(doc))


# ─────────────────────── Per-user history ───────────────────────── #


async def user_kicks(user_id: int) -> list[KickDoc]:
    """Return every kick record for a user, newest first."""
    return await db_call(
        _kicks()
        .find({"user_id": user_id}, {"_id": 0}, sort=[("timestamp", -1)])
        .to_list(None)
    )


async def user_kick_count(user_id: int) -> int:
    """Count every kick ever logged against the user."""
    return await db_call(_kicks().count_documents({"user_id": user_id}))
