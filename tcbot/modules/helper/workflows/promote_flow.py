# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Centralised promotion logic: role assignment and Admin promotion request flow."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pymongo.errors import DuplicateKeyError

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper import keyboards, parse_logmsg
from tcbot.modules.helper.formatter import esc, user_ref

if TYPE_CHECKING:
    from telegram import Bot

log = logging.getLogger(__name__)

# ──────────────── User-facing reply constants ──────────────────── #

_MSG_REQUEST_SUBMITTED = (
    "Submitted - the Founder has been notified and will review it shortly."
)
_ERR_TARGET_IS_FOUNDER = "That's the Founder - can't assign a role over them."
_ERR_NO_ASSIGN_PERMS = "You don't have permission to assign this role."

# * Tokenised CLI aliases the /tcpromote command accepts.
ROLE_ALIASES: dict[str, str] = {
    "admin": "admin",
    "developer": "developer",
    "dev": "developer",
    "tester": "tester",
    "test": "tester",
}


# ────────────────────────── Promote class ───────────────────────── #


class Promote:
    """All federation-promotion logic.

    * ``execute(...)`` runs the full role-assignment flow used by /tcpromote
      (both the inline-role and inline-button entry points).
    * ``request_admin(...)`` enqueues a promotion request and notifies the Founder.
    * ``available_roles_for(executor_role)`` lists what roles the executor can assign.
    """

    @staticmethod
    def available_roles_for(executor_role: str) -> list[str]:
        """Return the roles an executor with the given role is allowed to assign."""
        if executor_role == "founder":
            return ["admin", "developer", "tester"]
        if executor_role == "admin":
            return ["developer", "tester"]
        return []

    @staticmethod
    async def _assign_admin(
        bot: Bot,
        admin_id: int,
        admin_fname: str,
        target_id: int,
        target_fname: str,
        current_role: str | None,
    ) -> tuple[bool, str]:
        """Founder-only path: directly add the target to tc_admins and log it."""
        # * Write the primary record first; if this fails the target is never promoted,
        # * leaving a consistent state (no partial write).
        try:
            await db.users_roles.add_admin(target_id, admin_id)
        except Exception:
            log.exception("_assign_admin: add_admin failed for target=%d", target_id)
            return False, "Failed to save the promotion. Please try again."
        # * Secondary cleanup: purge any old tc_roles entry and update the user cache.
        # * These are non-critical; a failure leaves the user correctly promoted so we
        # * log a warning instead of aborting.
        cleanup: list = [db.users_cache.upsert_user(target_id, None, target_fname)]
        if current_role in ("developer", "tester"):
            cleanup.append(db.users_roles.remove_role(target_id))
        for _r in await asyncio.gather(*cleanup, return_exceptions=True):
            if isinstance(_r, BaseException):
                log.warning(
                    "_assign_admin cleanup step failed for target=%d: %s", target_id, _r
                )
        lc, lt = cfg.logs
        log_text = parse_logmsg.promoted(
            target_id, target_fname, "admin", admin_id, admin_fname
        )
        for result in await asyncio.gather(
            bot.send_message(lc, log_text, parse_mode="HTML", message_thread_id=lt),
            bot.send_message(
                target_id,
                f"You've been promoted to Admin in {cfg.community_name} - welcome to the staff team.",
            ),
            return_exceptions=True,
        ):
            if isinstance(result, BaseException):
                log.warning(
                    "_assign_admin log/DM send failed for target=%d: %s",
                    target_id,
                    result,
                )
        return True, (
            f"Done. {user_ref(target_id, target_fname)} "
            f"is now a {esc(cfg.community_name)} Admin."
        )

    @staticmethod
    async def _assign_subrole(
        bot: Bot,
        admin_id: int,
        admin_fname: str,
        target_id: int,
        target_fname: str,
        current_role: str | None,
        role: str,
    ) -> tuple[bool, str]:
        """Founder/Admin path for Developer/Tester role assignment."""
        if current_role == "admin":
            label = db.users_roles.ROLE_LABEL.get(role, role)
            return (
                False,
                f"That user is already an Admin. Demote them first before assigning {esc(label)}.",
            )
        # * set_role uses update_one(upsert=True) so it atomically replaces an existing
        # * developer/tester entry without a prior remove_role call.  This eliminates
        # * the partial-state window where the target holds no role between delete and insert.
        try:
            await db.users_roles.set_role(target_id, role, admin_id)
        except Exception:
            log.exception(
                "_assign_subrole: set_role failed for target=%d role=%s",
                target_id,
                role,
            )
            return False, "Failed to save the role assignment. Please try again."
        # * Cache upsert is non-critical; log but do not abort.
        try:
            await db.users_cache.upsert_user(target_id, None, target_fname)
        except Exception as exc:
            log.warning(
                "_assign_subrole: upsert_user failed for target=%d: %s", target_id, exc
            )
        role_label = db.users_roles.ROLE_LABEL.get(role, role)
        lc, lt = cfg.logs
        log_text = parse_logmsg.promoted(
            target_id, target_fname, role, admin_id, admin_fname
        )
        for result in await asyncio.gather(
            bot.send_message(lc, log_text, parse_mode="HTML", message_thread_id=lt),
            bot.send_message(
                target_id,
                f"You've been assigned the {role_label} role in {cfg.community_name} - welcome to the team.",
            ),
            return_exceptions=True,
        ):
            if isinstance(result, BaseException):
                log.warning(
                    "_assign_subrole log/DM send failed for target=%d: %s",
                    target_id,
                    result,
                )
        return (
            True,
            f"Done. {user_ref(target_id, target_fname)} "
            f"is now a {esc(cfg.community_name)} {esc(role_label)}.",
        )

    @classmethod
    async def request_admin(
        cls,
        bot: Bot,
        admin_id: int,
        target_id: int,
        target_fname: str,
        target_username: str | None = None,
    ) -> tuple[bool, str]:
        """Enqueue an Admin promotion request and notify the Founder (DM, then fallback to log)."""
        existing = await db.queues_db.get_request(target_id)
        if existing:
            return False, (
                f"There's already a pending promotion request for "
                f"{user_ref(target_id, target_fname)}."
            )
        request_id, owner_id = await asyncio.gather(
            db.queues_db.enqueue(target_id, target_username, target_fname, admin_id),
            db.users_roles.get_owner_id(),
            return_exceptions=True,
        )
        if isinstance(request_id, DuplicateKeyError):
            # * Lost the insert race: another promote queued first under the
            # * pending-unique index. Report the existing request, not an error.
            log.info(
                "Promotion request race for target=%d; using existing entry",
                target_id,
            )
            return False, (
                f"There's already a pending promotion request for "
                f"{user_ref(target_id, target_fname)}."
            )
        if isinstance(request_id, BaseException):
            log.error("Failed to enqueue promotion request: %s", request_id)
            return False, "Failed to queue the promotion request. Please try again."
        if isinstance(owner_id, BaseException):
            log.warning("Failed to fetch owner id for promo notify: %s", owner_id)
            owner_id = None
        req_text = parse_logmsg.promote_request_log(
            target_id, target_fname, target_username, request_id
        )
        lc, lt = cfg.logs
        notified = False
        if owner_id:
            try:
                await bot.send_message(
                    owner_id,
                    req_text,
                    parse_mode="HTML",
                    reply_markup=keyboards.promo_decision_kb(request_id),
                )
                notified = True
            except Exception as exc:
                log.warning("Owner DM failed, falling back to log channel: %s", exc)
        if not notified:
            try:
                await bot.send_message(
                    lc,
                    req_text,
                    parse_mode="HTML",
                    message_thread_id=lt,
                    reply_markup=keyboards.promo_decision_kb(request_id),
                )
            except Exception:
                log.exception("Promo request notify failed")
        return (True, _MSG_REQUEST_SUBMITTED)

    @classmethod
    async def execute(
        cls,
        bot: Bot,
        admin_id: int,
        admin_fname: str,
        executor_role: str,
        target_id: int,
        target_fname: str,
        current_role: str | None,
        role: str,
    ) -> tuple[bool, str]:
        """Execute a role assignment. Returns (success, reply_text).

        * Founder can directly assign Admin / Developer / Tester.
        * Admin can directly assign Developer / Tester.
        * Admin requesting Admin promotion creates a queue entry for the Founder.
        """
        if current_role == "founder":
            return False, _ERR_TARGET_IS_FOUNDER

        if db.users_roles.role_rank(current_role) >= db.users_roles.role_rank(role):
            label = db.users_roles.ROLE_LABEL.get(
                current_role or "", current_role or ""
            )
            return False, f"That user already holds the {esc(label)} role or higher."

        if role == "admin":
            if executor_role == "founder":
                return await cls._assign_admin(
                    bot, admin_id, admin_fname, target_id, target_fname, current_role
                )
            # * Admin promoting to Admin → request for Founder to approve.
            return await cls.request_admin(bot, admin_id, target_id, target_fname)

        if executor_role not in ("founder", "admin"):
            return False, _ERR_NO_ASSIGN_PERMS

        return await cls._assign_subrole(
            bot,
            admin_id,
            admin_fname,
            target_id,
            target_fname,
            current_role,
            role,
        )
