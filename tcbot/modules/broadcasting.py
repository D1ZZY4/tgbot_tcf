# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Broadcast command handler: sends a message to all connected groups."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram.error import BadRequest
from telegram.ext import ContextTypes, MessageHandler

from tcbot import cfg
from tcbot import database as db
from tcbot.database.documents import GroupDoc
from tcbot.modules.helper import decorators, parse_logmsg, replies
from tcbot.modules.helper.formatter import code, esc
from tcbot.modules.helper.parse_editmsg import safe_reply
from tcbot.utils.dispatch import count_errors, fan_out
from tcbot.utils.prefixes import build_prefixed_filters, parse_cmd_args

if TYPE_CHECKING:
    from telegram import Update

log = logging.getLogger(__name__)

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 60
_RL_LIMIT: int = 3


# ────────────────────── Module & Help Message ───────────────────── #

_CNAME = esc(cfg.community_name)

__module_name__ = "Broadcast"
__help_text__ = f"Sends a message to every group currently connected to {_CNAME}."

__help_sections__: list[tuple[str, str]] = [
    (
        replies.SEC_COMMANDS,
        f"{code('/tcbroadcast')} (alias: {code('/bc')})",
    ),
    replies.who_section(replies.PERM_STAFF_ONLY),
    replies.where_section(replies.CONTEXT_EXEC_OR_GROUP),
    (
        replies.SEC_WHAT,
        f"Sends a message to every group currently connected to {_CNAME}.\n\n"
        f"You can compose the message in two ways:\n"
        f"- Type the message directly after the command (HTML formatting is supported).\n"
        f"- Reply to an existing message with {code('/bc')} to forward that message "
        f"to all groups.\n\n"
        f"When the broadcast is complete, the bot shows a summary of how many groups "
        f"received the message and how many deliveries failed, and posts a log entry "
        f"to the federation logs channel.",
    ),
    (
        replies.SEC_EXAMPLES,
        f"{code('/tcbroadcast Reminder: please review the community rules.')}\n"
        f"{code('/bc <b>Event tonight</b> (join us at 8 PM UTC).')}\n"
        f"Or reply to any message and run {code('/bc')} to forward it to all groups.",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ──────────────── Command Broadcast </tcbroadcast> ──────────────── #


@decorators.ratelimiter(limit=_RL_LIMIT, period=_RL_PERIOD_S)
@decorators.staff_only
@decorators.log_execution
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message or forward a replied-to message to all connected groups.

    Accepts an inline text argument or a reply-to-message. Fans out sends to
    every active group via ``fan_out`` with semaphore limiting. Logs the result
    and edits the status message in parallel.
    """
    msg = update.effective_message
    admin = update.effective_user
    assert msg is not None
    assert admin is not None

    args = parse_cmd_args(msg.text)
    broadcast_text: str | None = " ".join(args).strip() if args else None

    has_reply = bool(msg.reply_to_message)
    if not broadcast_text and not has_reply:
        await safe_reply(
            msg,
            "Please provide a message to broadcast, or reply to a message.",
            log_label="cmd_broadcast no-content",
        )
        return

    try:
        groups = await db.groups_db.active_groups()
    except Exception:
        log.exception("active_groups failed during broadcast")
        await safe_reply(
            msg,
            "Could not load the group list due to a server error. Please try again.",
            log_label="cmd_broadcast groups-failed",
        )
        return
    if not groups:
        await safe_reply(
            msg, replies.ERR_NO_CONNECTED_GROUPS, log_label="cmd_broadcast no-groups"
        )
        return

    status = None
    try:
        status = await msg.reply_text(f"Broadcasting to {len(groups)} group(s)...")
    except Exception as exc:
        log.debug("cmd_broadcast status reply failed: %s", exc)

    # * Build per-group send coroutines, then fan out with semaphore limiting.
    # * A malformed HTML tag in staff text raises BadRequest per group; fall
    # * back to plain text so one typo does not fail the whole broadcast.
    async def _send_one(grp: GroupDoc) -> None:
        chat_id = grp.get("chat_id")
        if chat_id is None:
            return
        if has_reply and msg.reply_to_message:
            await msg.reply_to_message.forward(chat_id)
        elif broadcast_text:
            try:
                await ctx.bot.send_message(chat_id, broadcast_text, parse_mode="HTML")
            except BadRequest:
                log.info(
                    "Broadcast HTML rejected in chat=%d; retrying as plain text",
                    chat_id,
                )
                await ctx.bot.send_message(chat_id, broadcast_text)

    results = await fan_out([_send_one(grp) for grp in groups])
    failed = count_errors(results)
    success = len(results) - failed

    for grp, r in zip(groups, results, strict=False):
        if isinstance(r, BaseException):
            log.warning("Broadcast failed for %d: %s", grp.get("chat_id", "?"), r)

    if broadcast_text:
        preview = broadcast_text
    elif msg.reply_to_message:
        preview = msg.reply_to_message.text or "media"
    else:
        preview = ""
    lc, lt = cfg.logs

    # * send log and update status message in parallel
    edit_coro = (
        status.edit_text(
            f"Broadcast sent to {code(str(success))} groups. Failed: {code(str(failed))}.",
            parse_mode="HTML",
        )
        if status is not None
        else asyncio.sleep(0)
    )
    edit_r, log_r = await asyncio.gather(
        edit_coro,
        ctx.bot.send_message(
            lc,
            parse_logmsg.broadcast_log(
                admin.id, admin.first_name, preview, success, failed
            ),
            parse_mode="HTML",
            message_thread_id=lt,
        ),
        return_exceptions=True,
    )
    if isinstance(edit_r, BaseException):
        log.debug("Broadcast status edit failed: %s", edit_r)
    if isinstance(log_r, BaseException):
        log.error("Broadcast log send failed: %s", log_r)


# ──────────────────────────── Handlers ──────────────────────────── #

_BROADCAST_CMDS = build_prefixed_filters("tcbroadcast") | build_prefixed_filters("bc")

__handlers__ = [MessageHandler(_BROADCAST_CMDS, cmd_broadcast)]
