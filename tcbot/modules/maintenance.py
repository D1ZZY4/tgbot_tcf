# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""leaveall and cleanup maintenance commands for managing the connected-group list."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from telegram.ext import ContextTypes, MessageHandler

from tcbot import cfg
from tcbot import database as db
from tcbot.database.documents import GroupDoc
from tcbot.modules.helper import decorators, parse_logmsg, replies
from tcbot.modules.helper.formatter import bold, code
from tcbot.modules.helper.parse_editmsg import safe_reply
from tcbot.utils.dispatch import fan_out
from tcbot.utils.prefixes import build_prefixed_filters

if TYPE_CHECKING:
    from telegram import Bot, Update

log = logging.getLogger(__name__)

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_LONG_S: int = 60
_RL_PERIOD_BULK_S: int = 300
_RL_CLEANUP_LIMIT: int = 3
_RL_LEAVEALL_LIMIT: int = 1

_MEMBERSHIP_CHECK_TIMEOUT = 3.0


# ────────────────────── Module & Help Message ───────────────────── #

__module_name__ = "Maintenance"
__help_text__ = (
    "Maintenance commands for managing connected groups: clean up inaccessible ones "
    "or leave all in an emergency."
)

__help_sections__: list[tuple[str, str]] = [
    (
        replies.SEC_COMMANDS,
        f"{code('/leaveall')} (aliases: {code('/exitall')}, {code('/tcleave')})\n"
        f"{code('/cleanup')} (aliases: {code('/tcclean')}, {code('/tcc')})",
    ),
    replies.who_section(
        f"{bold('/leaveall')}: {replies.PERM_FOUNDER_ONLY}\n"
        f"{bold('/cleanup')}: {replies.PERM_STAFF_ONLY}"
    ),
    replies.where_section(replies.CONTEXT_EXEC_OR_GROUP),
    (
        "/leaveall",
        "Makes the bot leave every connected group simultaneously, marks them all as "
        "disconnected in the database, and posts a log entry for each group. "
        f"This is irreversible - each group must be manually reconnected with "
        f"{code('/tcconnect')}. Use only in emergencies.",
    ),
    (
        "/cleanup",
        "Scans all groups in the database and attempts to verify the bot still has access. "
        "Any group where the bot was kicked, removed, or can no longer reach is marked as "
        "disconnected and removed from the active list. "
        "Run this periodically to keep the group list accurate.",
    ),
    (
        replies.SEC_EXAMPLES,
        f"{code('/cleanup')}: remove stale or inaccessible groups.\n"
        f"{code('/leaveall')}: emergency withdrawal from all connected groups.",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ──────────────────────── Helper Functions ──────────────────────── #


# * Primary groups (main_group, exec_group) are managed separately from the
# * federated_groups collection. They are always-required destinations for
# * primary-group enforcement (ban / unban / mute / warn fan-out) and for the
# * federation log channel. They must not be torn down by leaveall / cleanup
# * / disconnect / rmtc paths. Guarded by cfg.is_primary_group (single
# * source of truth in tcbot/__init__.py) at every call site below.


@dataclass(frozen=True)
class _LeaveResult:
    """Result of a single ``_leave_one`` operation, one field per side-effect.

    The structured fields let ``cmd_leaveall`` count success and failure
    per side-effect instead of inspecting a raw ``gather`` tuple. The DB
    deactivation is the authoritative state write; a successful
    ``leave_chat`` with a failed ``deactivate_group`` would leave a
    "ghost" group in the database (still active but the bot has left),
    so the leave counts as failed when either side-effect fails.
    """

    chat_id: int
    left: bool
    deactivated: bool
    log_sent: bool

    @property
    def ok(self) -> bool:
        """A leave is considered successful only when BOTH Telegram and DB agree."""
        return self.left and self.deactivated


async def _leave_one(
    bot: Bot,
    grp: GroupDoc,
    lc: int,
    lt: int | None,
    admin_id: int,
    admin_name: str,
) -> _LeaveResult:
    """Leave one group, deactivate it in DB, and post a disconnection log.

    The three operations fire in parallel; the result is wrapped in a
    structured :class:`_LeaveResult` so the caller can count each
    side-effect independently. ``deactivate_group`` is the authoritative
    state write -- a "ghost" group (bot left, DB still active) is
    counted as failed.
    """
    chat_id = grp.get("chat_id")
    title = grp.get("title", "Unknown")
    if chat_id is None:
        # * Defensive: active_groups rows always carry chat_id, but a
        # * malformed row must count as a failed leave, not raise inside
        # * fan_out (which would surface as a bare BaseException entry).
        log.warning("leaveall: skipping group record without chat_id: %s", title)
        return _LeaveResult(chat_id=0, left=False, deactivated=False, log_sent=False)
    leave_result, deactivate_result, log_result = await asyncio.gather(
        bot.leave_chat(chat_id),
        db.groups_db.deactivate_group(chat_id),
        bot.send_message(
            lc,
            parse_logmsg.group_disconnected_log(
                chat_id,
                title,
                admin_id,
                admin_name,
            ),
            parse_mode="HTML",
            message_thread_id=lt,
        ),
        return_exceptions=True,
    )
    if isinstance(leave_result, BaseException):
        log.debug("leave_chat failed for chat %d: %s", chat_id, leave_result)
    if isinstance(deactivate_result, BaseException):
        log.warning(
            "deactivate_group failed for chat %d during leaveall: %s",
            chat_id,
            deactivate_result,
        )
    if isinstance(log_result, BaseException):
        log.warning(
            "group_disconnected log send failed for %d: %s", chat_id, log_result
        )
    return _LeaveResult(
        chat_id=chat_id,
        left=not isinstance(leave_result, BaseException),
        deactivated=not isinstance(deactivate_result, BaseException),
        log_sent=not isinstance(log_result, BaseException),
    )


async def _should_remove(bot: Bot, grp: GroupDoc) -> bool:
    """Return True if the bot has left or been kicked from the group.

    Primary groups are never "removable" -- they are managed separately
    and must never be deactivated by cleanup. If ``_should_remove``
    is called for a primary group, it short-circuits to ``False`` so
    ``cmd_cleanup`` skips it.
    """
    chat_id = grp.get("chat_id")
    if chat_id is None:
        return True
    if cfg.is_primary_group(chat_id):
        return False
    try:
        member = await asyncio.wait_for(
            bot.get_chat_member(chat_id, bot.id),
            timeout=_MEMBERSHIP_CHECK_TIMEOUT,
        )
        return member.status in ("left", "kicked")
    except Exception as exc:
        # ! CRITICAL: fail closed. A transient Telegram/DB error must not look
        # ! like "bot has left" or cleanup mass-deactivates healthy groups.
        log.warning(
            "Could not verify membership for %d, keeping group: %s", chat_id, exc
        )
        return False


# ────────────────── Command Leave All </leaveall> ───────────────── #


@decorators.ratelimiter(limit=_RL_LEAVEALL_LIMIT, period=_RL_PERIOD_BULK_S)
@decorators.owner_only
@decorators.log_execution
async def cmd_leaveall(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Leave all active connected groups and deactivate their DB records.

    Fetches active groups (excluding the primary groups, which are
    managed separately), fans out individual leave-and-deactivate
    coroutines concurrently (``_leave_one``), then edits the status
    message with the final success/failure counts. A leave counts as
    successful only when both ``leave_chat`` and ``deactivate_group``
    succeed; a partial success (one succeeded, the other failed) is
    counted as a failure so the operator can investigate the ghost.
    """
    admin = update.effective_user
    if admin is None:
        return
    try:
        all_groups = await db.groups_db.active_groups()
    except Exception:
        log.exception("active_groups failed during leaveall")
        status_msg = update.effective_message
        if status_msg is not None:
            await safe_reply(
                status_msg,
                replies.ERR_GROUPS_LOAD_FAILED,
                log_label="leaveall groups-failed",
            )
        return
    # * Never tear down the primary groups. They are not in
    # * ``federated_groups`` today, but the explicit guard is defence in
    # * depth: if a future change ever adds them to the collection, or if
    # * ``cfg.main_group`` is misconfigured, this prevents a global leaveall
    # * from cutting the bot out of its required enforcement destinations.
    groups = [g for g in all_groups if not cfg.is_primary_group(g.get("chat_id"))]
    if not groups:
        status_msg = update.effective_message
        if status_msg is not None:
            await safe_reply(
                status_msg,
                replies.ERR_NO_CONNECTED_GROUPS,
                log_label="leaveall no-groups",
            )
        return

    status_msg = update.effective_message
    status = None
    if status_msg is not None:
        # * Send a fresh status message and keep it: the final counts edit
        # * this bot-owned message. Editing the user's command message would
        # * always fail (bots cannot edit messages sent by others).
        try:
            status = await status_msg.reply_text(
                f"Leaving {len(groups)} groups...",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.debug("leaveall status reply failed: %s", exc)
    lc, lt = cfg.logs

    # * Semaphore-bounded to respect Telegram rate limits on large federations.
    all_results = await fan_out(
        [_leave_one(ctx.bot, g, lc, lt, admin.id, admin.first_name) for g in groups]
    )

    # * ``fan_out`` returns ``T | BaseException`` so a transport error that
    # * escapes before the gather starts still shows up as an entry.
    # * ``_leave_one`` itself never raises (a malformed row yields a failed
    # * result), but filter defensively before unpacking structured fields,
    # * then count each side-effect independently.
    ok_results = [r for r in all_results if isinstance(r, _LeaveResult)]

    # * Count success per side-effect. A leave is "fully successful" only
    # * when BOTH ``leave_chat`` and ``deactivate_group`` succeed. A
    # * partial state (one succeeded, the other failed) is a ghost and
    # * counts as a failure so the operator can reconcile manually.
    left_ok = sum(1 for r in ok_results if r.ok)
    partial = sum(1 for r in ok_results if (r.left or r.deactivated) and not r.ok)
    log_failed = sum(1 for r in ok_results if not r.log_sent)
    failed = len(groups) - left_ok

    if status is not None:
        detail = ""
        if partial:
            detail += f" ({partial} partial: bot left but DB deactivation failed)"
        if log_failed:
            detail += f" ({log_failed} log posts failed)"
        try:
            await status.edit_text(
                f"Left {code(str(left_ok))} groups. Failed: {code(str(failed))}.{detail}",
                parse_mode="HTML",
            )
        except Exception:
            log.exception("Leaveall status edit failed")


# ─────────────────── Command CleanUp </cleanup> ─────────────────── #


@decorators.ratelimiter(limit=_RL_CLEANUP_LIMIT, period=_RL_PERIOD_LONG_S)
@decorators.staff_only
@decorators.log_execution
async def cmd_cleanup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Prune inaccessible groups from the active federation list.

    Checks all groups concurrently via ``_should_remove`` (which excludes
    primary groups), deactivates the identified stale records in parallel,
    then replies with the count of removed groups.
    """
    reply_msg = update.effective_message
    if reply_msg is None:
        return
    try:
        groups = await db.groups_db.active_groups()
    except Exception:
        log.exception("active_groups failed during cleanup")
        await safe_reply(
            reply_msg,
            replies.ERR_GROUPS_LOAD_FAILED,
            log_label="cleanup groups-failed",
        )
        return

    # * Semaphore-bounded to respect Telegram rate limits on large federations.
    checks = await fan_out([_should_remove(ctx.bot, g) for g in groups])

    to_remove = [g for g, remove in zip(groups, checks, strict=False) if remove is True]

    if to_remove:
        # * Pure database writes: plain gather, not fan_out. fan_out wraps
        # * the Telegram circuit breaker, which must not gate DB work.
        deact_results = await asyncio.gather(
            *[db.groups_db.deactivate_group(g.get("chat_id", 0)) for g in to_remove],
            return_exceptions=True,
        )
        deactivated = sum(1 for r in deact_results if r is True)
        for grp, result in zip(to_remove, deact_results, strict=False):
            if isinstance(result, BaseException):
                log.error(
                    "cleanup deactivate failed for chat=%s: %s",
                    grp.get("chat_id", 0),
                    result,
                )
            elif result is not True:
                log.warning(
                    "cleanup deactivate no-op for chat=%s (record already gone?)",
                    grp.get("chat_id", 0),
                )
    else:
        deactivated = 0

    await safe_reply(
        reply_msg,
        f"Cleaned up {code(str(deactivated))} inaccessible group(s).",
        log_label="cleanup",
    )


# ──────────────────────────── Handlers ──────────────────────────── #

_LEAVEALL_CMDS = (
    build_prefixed_filters("leaveall")
    | build_prefixed_filters("exitall")
    | build_prefixed_filters("tcleave")
)
_CLEANUP_CMDS = (
    build_prefixed_filters("cleanup")
    | build_prefixed_filters("tcclean")
    | build_prefixed_filters("tcc")
)


__handlers__ = [
    MessageHandler(_LEAVEALL_CMDS, cmd_leaveall),
    MessageHandler(_CLEANUP_CMDS, cmd_cleanup),
]
