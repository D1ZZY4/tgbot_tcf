# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Kick executor + conversation factory."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper import keyboards, parse_logmsg, replies
from tcbot.modules.helper.parse_link import message_link
from tcbot.modules.helper.workflows.demote_flow import Demote
from tcbot.modules.helper.workflows.proof_flow import BuildProof, upload_proof
from tcbot.modules.helper.workflows.reason_flow import BuildReason, build_modaction_conv
from tcbot.utils.formatter import esc, mention, user_ref
from tcbot.utils.time_and_date import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable

    from telegram import Message, Update
    from telegram.ext import ContextTypes
    from telegram.ext.filters import BaseFilter

log = logging.getLogger(__name__)

# ──────────────── User-facing reply constants ──────────────────── #

_MSG_REJOIN_ALLOWED = "They can rejoin via invite link."

# * Per-action BuildReason and BuildProof instances; imported by kicking.py
reason = BuildReason("kick")
proof = BuildProof("kick")


# ────────────────────────── Kick executor ───────────────────────── #


async def execute_kick(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    target_id: int,
    target_name: str,
    reason_text: str,
    proof_msgs: list[Message] | None = None,
) -> None:
    """Kick (ban then immediately unban) a user from the current group."""
    msg = update.effective_message
    effective_user = update.effective_user
    effective_chat = update.effective_chat
    if effective_user is None or effective_chat is None or msg is None:
        log.warning("Kick executor called without user, chat, or message")
        return
    chat_id = effective_chat.id
    admin_id = effective_user.id
    admin_fname = effective_user.first_name

    # * Upload proof concurrently with enforcement: the ban below must not wait
    # * for the proof-channel round trip. The task is awaited after the ban
    # * lands and before the keyboards are built; on ban failure it is
    # * cancelled so no orphan upload outlives the executor. An upload failure
    # * still degrades to no proof link, exactly as before.
    proof_task: asyncio.Task[int | None] | None = None
    pc, pt = cfg.proofs
    if proof_msgs:
        caption = parse_logmsg.proof_caption_new(
            target_id, admin_id, admin_fname, utc_now()
        )
        proof_task = asyncio.create_task(
            upload_proof(ctx.bot, proof_msgs, caption, pc, pt)
        )

    try:
        await ctx.bot.ban_chat_member(chat_id, target_id)
        proof_link: str | None = None
        if proof_task is not None:
            try:
                proof_msg_id = await proof_task
            except asyncio.CancelledError:
                proof_task.cancel()
                raise
            except Exception:
                log.warning("Kick proof upload skipped for target=%d", target_id)
                proof_msg_id = None
            if proof_msg_id:
                proof_link = message_link(pc, proof_msg_id, pt)
        proof_kb = keyboards.action_proof_kb(target_id, proof_link)
        chat_title = effective_chat.title or str(chat_id)
        lc, lt = cfg.logs
        log_text = parse_logmsg.kick_log(
            target_id,
            target_name,
            admin_id,
            admin_fname,
            reason_text,
            chat_id,
            chat_title,
        )
        # * Three independent side-effects run in parallel: the unban that
        # * converts the ban into a "kick" (user can rejoin), the DB kick
        # * log, and the federation log-channel post. The user-facing reply
        # * runs *after* the unban completes so we can append a warning if
        # * the unban failed -- otherwise the admin would see "kicked" while
        # * the user is still banned.
        # * The entry auto-demote ran before the proof-collection window, so
        # * re-demote here to close the TOCTOU gap before the unban fan-out.
        # * Best-effort: ban_chat_member above already ran, so a lookup
        # * failure must not skip the unban and turn this kick into a ban.
        await Demote.redemote_before_fanout(
            ctx.bot,
            target_id,
            target_name,
            admin_id,
            admin_fname,
            trigger="kick",
        )
        unban_result, log_kick_result, log_send_result = await asyncio.gather(
            ctx.bot.unban_chat_member(chat_id, target_id, only_if_banned=True),
            db.kicks_db.log_kick(target_id, chat_id, reason_text, admin_id),
            ctx.bot.send_message(
                lc,
                log_text,
                parse_mode="HTML",
                message_thread_id=lt,
                reply_markup=proof_kb,
            ),
            return_exceptions=True,
        )
        if isinstance(unban_result, BaseException):
            log.warning(
                "unban_chat_member failed after kick for target=%d: %s",
                target_id,
                unban_result,
            )
        if isinstance(log_kick_result, BaseException):
            log.error(
                "log_kick DB write failed for target=%d: %s", target_id, log_kick_result
            )
        if isinstance(log_send_result, BaseException):
            log.error("Kick log send failed: %s", log_send_result)
        unban_warning = (
            " WARNING: the post-kick unban step failed; the user is "
            "still banned in this chat and cannot rejoin. Demote them "
            "manually if needed and unban from the chat member list."
            if isinstance(unban_result, BaseException)
            else ""
        )
        try:
            await msg.reply_text(
                f"{user_ref(target_id, target_name)} has been kicked.\n"
                f"Reason: {esc(reason_text)}\n"
                f"{_MSG_REJOIN_ALLOWED}{unban_warning}",
                parse_mode="HTML",
                reply_markup=proof_kb,
            )
        except Exception as exc:
            log.debug("Kick reply_text failed: %s", exc)
    except Exception:
        # * Ban failed while the proof upload may still be in flight: cancel
        # * it so no orphan upload outlives this executor.
        if proof_task is not None and not proof_task.done():
            proof_task.cancel()
            await asyncio.gather(proof_task, return_exceptions=True)
        log.exception("Kick failed for %s in %s", target_id, chat_id)
        try:
            await msg.reply_text(
                f"Couldn't kick {mention(target_id, target_name)}. "
                "Please check bot permissions and retry.",
                parse_mode="HTML",
            )
        except Exception as reply_exc:
            log.debug("Kick error reply failed: %s", reply_exc)


# ──────────────────────── Executor adapter ──────────────────────── #


async def _exec_kick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Pop kick data from user_data and call execute_kick."""
    if ctx.user_data is None:
        log.warning("_exec_kick called without user_data")
        return
    target_id = ctx.user_data.pop("kick_target_id", 0)
    target_name = ctx.user_data.pop("kick_target_name", "")
    reason_text = ctx.user_data.pop("kick_reason", replies.NO_REASON)
    proof_msgs = ctx.user_data.pop("kick_proof_msgs", None)
    ctx.user_data.pop("kick_extra_info", None)
    await execute_kick(
        update,
        ctx,
        target_id,
        target_name,
        reason_text,
        proof_msgs=proof_msgs,
    )


# ─────────────────── ConversationHandler factory ────────────────── #


def kick_conversation(entry_fn: Callable[..., Any], entry_filter: BaseFilter) -> object:
    """Return the kick ConversationHandler via the central reason_flow factory."""
    return build_modaction_conv(reason, proof, entry_fn, _exec_kick, entry_filter)
