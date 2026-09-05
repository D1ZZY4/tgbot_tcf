# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Promotion request queue - manages promotion request queue for staff applications."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tcbot.database.documents import PromotionRequestDoc
from tcbot.database.mongos import col, db_call, make_short_id
from tcbot.utils.time_and_date import utc_now

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection

# ─────────────────────── Collection Helpers ─────────────────────── #
# * Internal collection access and ID generation utilities


def _requests() -> AsyncIOMotorCollection:
    return col("promotion_requests")


# ──────────────────────────── Mutations ─────────────────────────── #
# * Functions that create or modify promotion request records
# * Manages the queue's state for pending and resolved requests


async def enqueue(
    user_id: int,
    username: str | None,
    first_name: str,
    promoted_by: int,
) -> str:
    """Add a new promotion request to the queue."""
    request_id = make_short_id()
    await db_call(
        _requests().insert_one(
            {
                "request_id": request_id,
                "target_id": user_id,
                "username": username,
                "first_name": first_name,
                "promoted_by": promoted_by,
                "status": "pending",
                "requested_date": utc_now(),
                "resolved_date": None,
                "resolved_by": None,
            }
        )
    )
    return request_id


# ───────────────────────────── Queries ──────────────────────────── #
# * Functions to retrieve promotion request data from the database
# * Includes lookups by ID, user, and counts of pending requests


async def get_request_by_id(request_id: str) -> PromotionRequestDoc | None:
    """Get a promotion request by its unique request ID."""
    return await db_call(_requests().find_one({"request_id": request_id}))


async def get_request(user_id: int) -> PromotionRequestDoc | None:
    """Get the pending request for a specific user."""
    return await db_call(
        _requests().find_one({"target_id": user_id, "status": "pending"})
    )


async def all_pending() -> list[PromotionRequestDoc]:
    """Get all currently pending promotion requests, oldest first."""
    return await db_call(
        _requests()
        .find(
            {"status": "pending"},
            {
                "_id": 0,
                "request_id": 1,
                "target_id": 1,
                "username": 1,
                "first_name": 1,
                "requested_date": 1,
            },
            sort=[("requested_date", 1)],
        )
        .to_list(200)
    )


async def resolve(request_id: str, status: str, resolved_by: int) -> bool:
    """Mark a pending promotion request as resolved.

    The ``pending`` filter makes the claim atomic: concurrent decisions on
    the same request resolve exactly once, and late taps get ``False``.
    """
    result = await db_call(
        _requests().update_one(
            {"request_id": request_id, "status": "pending"},
            {
                "$set": {
                    "status": status,
                    "resolved_date": utc_now(),
                    "resolved_by": resolved_by,
                }
            },
        )
    )
    return result.modified_count > 0
