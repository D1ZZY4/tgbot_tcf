# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Centralised demotion logic: manual via /tcdemote and auto-demote on ban/kick."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper import parse_logmsg
from tcbot.modules.helper.formatter import bold, esc, mention

if TYPE_CHECKING:
    from telegram import Bot, Message

log = logging.getLogger(__name__)


# ────────────────────────── Demote class ────────────────────────── #


class Demote:
    """All federation-demotion logic.

    * ``execute(trigger=None)`` runs the manual /tcdemote path.
    * ``execute(trigger="ban")`` / ``"kick"`` / ``"mute"`` runs the auto-demote
      path used by the ban, kick, and mute flows before the actual action.
    """

    @staticmethod
    async def remove_role(target_id: int, target_role: str) -> bool:
        """Remove the user's role from the correct collection."""
        if target_role == "admin":
            return await db.users_roles.remove_admin(target_id)
        return await db.users_roles.remove_role(target_id)

    @classmethod
    async def execute(
        cls,
        bot: Bot,
        target_id: int,
        target_fname: str,
        target_role: str,
        executor_id: int,
        executor_fname: str,
        *,
        trigger: str | None = None,
    ) -> bool:
        """Remove the role, post a federation log, and DM the target.

        Returns True if the role was actually removed.
        """
        removed = await cls.remove_role(target_id, target_role)
        if not removed:
            return False

        lc, lt = cfg.logs
        log_text = parse_logmsg.demoted(
            target_id,
            target_fname,
            target_role,
            executor_id,
            executor_fname,
            trigger=trigger,
        )

        role_label = db.users_roles.ROLE_LABEL.get(
            target_role, target_role.capitalize()
        )
        if trigger is None:
            user_msg = (
                f"Your {bold(role_label)} role in {esc(cfg.community_name)} has been removed by "
                f"{esc(executor_fname)}."
            )
        else:
            if trigger == "ban":
                verb = "banned"
            elif trigger == "mute":
                verb = "muted"
            else:
                verb = "kicked"
            user_msg = (
                f"Your {bold(role_label)} role in {esc(cfg.community_name)} has been removed - "
                f"you were {verb} from the federation."
            )

        for result in await asyncio.gather(
            bot.send_message(lc, log_text, parse_mode="HTML", message_thread_id=lt),
            bot.send_message(target_id, user_msg, parse_mode="HTML"),
            return_exceptions=True,
        ):
            if isinstance(result, BaseException):
                # * Error-level so a silent audit/DM gap ships to LOG_ERRORS
                # * like other moderation log-send failures; the role itself
                # * was already removed, so this never blocks the caller.
                log.error(
                    "Demote log/DM send failed for target=%d: %s", target_id, result
                )
        return True

    @classmethod
    async def auto_demote_or_abort(
        cls,
        msg: Message,
        bot: Bot,
        target_id: int,
        target_display: str,
        target_role: str,
        executor_id: int,
        executor_fname: str,
        *,
        trigger: str,
    ) -> bool:
        """Auto-demote a role-holding target, or reply and signal abort.

        Shared by the ban/kick/mute entry handlers, whose demote-fail blocks
        were byte-identical apart from the action noun. Runs
        :meth:`execute` and returns ``True`` when the caller may proceed.
        When the demote raises, logs the failure, tells the executor to
        demote manually, and returns ``False`` so the caller ends the
        conversation without enforcing — a banned/muted/kicked user must
        never keep a federation role.
        """
        try:
            await cls.execute(
                bot,
                target_id,
                target_display,
                target_role,
                executor_id,
                executor_fname,
                trigger=trigger,
            )
        except Exception:
            log.exception(
                "Auto-demote before %s failed for target=%d role=%s",
                trigger,
                target_id,
                target_role,
            )
            try:
                await msg.reply_text(
                    f"{mention(target_id, target_display)} "
                    f"holds a federation role ({target_role}) and the auto-demote "
                    f"step failed, so the {trigger} cannot proceed safely. Demote "
                    f"them manually with /tcdemote and retry the {trigger}.",
                    parse_mode="HTML",
                )
            except Exception as exc:
                log.debug("auto-demote-fail reply failed: %s", exc)
            return False
        return True
