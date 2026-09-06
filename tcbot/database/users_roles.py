# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Federation staff role helpers: owners, admins, developer/tester roles, and effective-role resolution.

This module handles all tc_owners, tc_admins, and tc_roles collection operations.
Do not mix with users_cache.py which handles member_cache collection.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from pymongo.errors import DuplicateKeyError

from tcbot.database.cache import (
    _OWNER_KEY,
    effective_role_cache,
    owner_id_cache,
)
from tcbot.database.documents import AdminDoc, RoleRefDoc
from tcbot.database.mongos import col, db_call
from tcbot.utils.time_and_date import utc_now

log = logging.getLogger(__name__)

# ────────────────────── Role hierarchy tables ───────────────────── #
# * Numeric ranks compare executor vs target; labels render to humans.

VALID_ROLES: frozenset[str] = frozenset({"developer", "tester"})

ROLE_RANK: dict[str, int] = {
    "founder": 4,
    "admin": 3,
    "developer": 2,
    "tester": 1,
}

ROLE_LABEL: dict[str, str] = {
    "founder": "Founder",
    "admin": "Admin",
    "developer": "Developer",
    "tester": "Tester",
}


def role_rank(role: str | None) -> int:
    """Convert a role string to its numeric rank value for comparison."""
    return ROLE_RANK.get(role or "", 0)


# ─────────────────────── Owner CRUD ─────────────────────── #


async def get_owner_id() -> int | None:
    """Return the current owner's user ID (L1->L2->DB cached)."""

    async def _fetch() -> int | None:
        doc: AdminDoc | None = await db_call(
            col("tc_owners").find_one({}, {"_id": 0, "user_id": 1})
        )
        return doc["user_id"] if doc else None

    return cast("int | None", await owner_id_cache.get_or_fetch(_OWNER_KEY, _fetch))


async def is_owner(user_id: int) -> bool:
    """Return True if user_id belongs to the bot owner."""
    return (
        await db_call(col("tc_owners").find_one({"user_id": user_id}, {"_id": 1}))
        is not None
    )


async def ensure_initial_owner(initial_id: int) -> None:
    """Create the first owner entry if the owners collection is empty."""
    if await db_call(col("tc_owners").estimated_document_count()) > 0:
        return
    try:
        await db_call(col("tc_owners").insert_one({"user_id": initial_id}))
        owner_id_cache.put(_OWNER_KEY, initial_id)
    except DuplicateKeyError:
        # * Another instance won the startup race; drop the cache so the next read refreshes.
        owner_id_cache.invalidate(_OWNER_KEY)


async def set_owner(user_id: int) -> None:
    """Replace the current owner with user_id; clears the role cache."""
    # * Insert the new owner first so a mid-flight crash leaves the new owner present,
    # * not zero owners. The unique index on user_id makes the upsert idempotent.
    await db_call(
        col("tc_owners").update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"user_id": user_id}},
            upsert=True,
        )
    )
    await db_call(col("tc_owners").delete_many({"user_id": {"$ne": user_id}}))
    owner_id_cache.put(_OWNER_KEY, user_id)
    # * Full two-layer invalidation: old owner's ID is unknown, so we cannot
    # * call invalidate(old_id).  clear_all() sweeps L1 + all Redis keys with
    # * the "role" prefix so no process reads a stale "founder" role after the
    # * transfer completes.
    await effective_role_cache.clear_all()


# ────────────────────────── Admin CRUD ─────────────────────────── #


async def is_admin(user_id: int) -> bool:
    """Return True if user_id is a registered admin."""
    return (
        await db_call(col("tc_admins").find_one({"user_id": user_id}, {"_id": 1}))
        is not None
    )


async def is_staff(user_id: int) -> bool:
    """Return True if user_id is owner or admin."""
    owner, admin = await asyncio.gather(
        is_owner(user_id), is_admin(user_id), return_exceptions=True
    )
    # * Cancellation is not a lookup result: re-raise instead of coercing to
    # * False, matching can_act_on/resolve_and_check semantics.
    if isinstance(owner, asyncio.CancelledError):
        raise owner
    if isinstance(admin, asyncio.CancelledError):
        raise admin
    if isinstance(owner, BaseException):
        log.warning("is_staff owner check failed for %d: %s", user_id, owner)
        owner = False
    if isinstance(admin, BaseException):
        log.warning("is_staff admin check failed for %d: %s", user_id, admin)
        admin = False
    return owner or admin


async def add_admin(user_id: int, promoted_by: int) -> None:
    """Add a new admin via upsert; no-op if already present."""
    await db_call(
        col("tc_admins").update_one(
            {"user_id": user_id},
            {
                "$setOnInsert": {
                    "user_id": user_id,
                    "promoted_by": promoted_by,
                    "promoted_date": utc_now(),
                }
            },
            upsert=True,
        )
    )
    effective_role_cache.invalidate(user_id)


async def remove_admin(user_id: int) -> bool:
    """Remove an admin; return True if the entry was deleted."""
    r = await db_call(col("tc_admins").delete_one({"user_id": user_id}))
    effective_role_cache.invalidate(user_id)
    return r.deleted_count > 0


async def all_admins() -> list[AdminDoc]:
    """Return all admin documents."""
    return await db_call(
        col("tc_admins").find({}, {"_id": 0, "user_id": 1}).to_list(None)
    )


async def admin_count() -> int:
    """Return the total count of registered admins."""
    return await db_call(col("tc_admins").estimated_document_count())


# ───────────────── Custom role CRUD (dev / tester) ─────────────── #


async def set_role(user_id: int, role: str, assigned_by: int) -> None:
    """Assign a custom role (developer/tester) to a user."""
    await db_call(
        col("tc_roles").update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "role": role,
                    "assigned_by": assigned_by,
                    "assigned_at": utc_now(),
                }
            },
            upsert=True,
        )
    )
    effective_role_cache.invalidate(user_id)


async def remove_role(user_id: int) -> bool:
    """Remove a user's custom role from the database."""
    r = await db_call(col("tc_roles").delete_one({"user_id": user_id}))
    effective_role_cache.invalidate(user_id)
    return r.deleted_count > 0


async def get_role(user_id: int) -> str | None:
    """Get a user's custom role from the database only."""
    doc = await db_call(col("tc_roles").find_one({"user_id": user_id}, {"role": 1}))
    return doc["role"] if doc else None


async def all_by_role(role: str) -> list[RoleRefDoc]:
    """Get all users with a specific custom role."""
    return await db_call(
        col("tc_roles").find({"role": role}, {"_id": 0, "user_id": 1}).to_list(None)
    )


# ──────────────────── Effective role resolution ────────────────── #


async def can_act_on(executor_id: int, target_id: int) -> bool:
    """Check if an executor can perform moderation actions on a target."""
    executor_role, target_role = await asyncio.gather(
        get_effective_role(executor_id),
        get_effective_role(target_id),
        return_exceptions=True,
    )
    if isinstance(executor_role, asyncio.CancelledError):
        raise executor_role
    if isinstance(target_role, asyncio.CancelledError):
        raise target_role
    if isinstance(executor_role, BaseException):
        log.warning(
            "can_act_on executor role failed for %d: %s", executor_id, executor_role
        )
        return False
    if isinstance(target_role, BaseException):
        log.warning("can_act_on target role failed for %d: %s", target_id, target_role)
        return False
    return role_rank(executor_role) > role_rank(target_role)


async def get_effective_role(user_id: int) -> str | None:
    """Resolve a user's full effective role including owner/admin status (L1->L2->DB cached)."""

    async def _fetch() -> str | None:
        # * Propagate DB errors so get_or_fetch does not cache a degraded role result.
        # * A false "founder" or "admin" cached for a real error would silently bypass
        # * authorization checks for every subsequent call until TTL expiry.
        owner, admin, role = await asyncio.gather(
            is_owner(user_id),
            is_admin(user_id),
            get_role(user_id),
            return_exceptions=True,
        )
        if isinstance(owner, BaseException):
            raise owner
        if isinstance(admin, BaseException):
            raise admin
        if isinstance(role, BaseException):
            raise role
        return "founder" if owner else "admin" if admin else role

    return cast("str | None", await effective_role_cache.get_or_fetch(user_id, _fetch))


async def role_meta(user_id: int) -> tuple[str | None, int | None, Any]:
    """Return ``(role, assigned_by, assigned_at)``; owner has no metadata."""
    role = await get_effective_role(user_id)
    if role is None:
        return None, None, None
    if role == "founder":
        # * tc_owners stores only user_id; no metadata to surface.
        return role, None, None
    if role == "admin":
        doc = await db_call(
            col("tc_admins").find_one(
                {"user_id": user_id},
                {"_id": 0, "promoted_by": 1, "promoted_date": 1},
            )
        )
        if doc:
            return role, doc.get("promoted_by"), doc.get("promoted_date")
        return role, None, None
    doc = await db_call(
        col("tc_roles").find_one(
            {"user_id": user_id},
            {"_id": 0, "assigned_by": 1, "assigned_at": 1},
        )
    )
    if doc:
        return role, doc.get("assigned_by"), doc.get("assigned_at")
    return role, None, None
