# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Federation ban command entry point: validates permissions and starts the ban flow."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram.ext import ContextTypes, ConversationHandler

from tcbot.modules.helper import decorators, extraction, identity, replies
from tcbot.modules.helper.decorators import resolve_and_check
from tcbot.modules.helper.formatter import bold, code, mention
from tcbot.modules.helper.workflows.ban_flow import (
    WAITING_PROOF,
    ban_conversation,
    proof,
)
from tcbot.modules.helper.workflows.demote_flow import Demote
from tcbot.utils.prefixes import build_prefixed_filters, parse_cmd_args

if TYPE_CHECKING:
    from telegram import Update

log = logging.getLogger(__name__)

# ──────────────── User-facing reply constants ──────────────────── #

_ERR_REASON_REQUIRED = "A reason is required - /tcban <target> <reason>."

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 60
_RL_LIMIT: int = 3


# ────────────────────── Module & Help Message ───────────────────── #

__module_name__ = "Ban"
__help_text__ = (
    f"Issues a {bold('federation-wide ban')} on a user, applied across every connected "
    "group at once. Auto-demotes staff targets and stores proof with the ban record."
)

__help_sections__: list[tuple[str, str]] = [
    (
        replies.SEC_COMMANDS,
        f"{code('/tcban')} (alias: {code('/tcb')})",
    ),
    replies.who_section(replies.PERM_DEV_ABOVE),
    replies.where_section(replies.CONTEXT_EXEC_OR_GROUP),
    (
        replies.SEC_WHAT,
        f"Issues a {bold('federation-wide ban')} on the target, applied across all connected "
        "groups automatically. A reason is required - provide it directly after the target.\n\n"
        "After the command, the bot walks you through the proof step: send one or more "
        "photos or videos as evidence. Proof is required and is logged with the ban record "
        "to the federation log channel.\n\n"
        "If the user already has an active ban, the existing record is updated with the new "
        "reason and proof rather than creating a duplicate.\n"
        "If the target holds a federation role (Tester / Developer / Admin), that role is "
        "automatically removed and they are notified by DM before the ban is enforced.",
    ),
    replies.target_section(),
    (
        replies.SEC_EXAMPLES,
        f"{code('/tcban @username spamming in connected groups')}\n"
        f"{code('/tcban 123456789 scamming members')}\n"
        f"Or reply to a message and run {code('/tcb reason here')}.",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ────────────────────── Command Ban </tcban> ────────────────────── #


@decorators.ratelimiter(limit=_RL_LIMIT, period=_RL_PERIOD_S)
@decorators.mod_only
@decorators.log_execution
async def cmd_ban_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the federation ban flow.

    Resolves the target, validates the inline reason, runs identity and role
    checks in parallel, auto-demotes any federation role held by the target, then
    stores ban metadata in ``user_data`` and shows the proof prompt. Returns
    ``WAITING_PROOF`` or ``ConversationHandler.END`` on any failure.
    """
    msg = update.effective_message
    admin = update.effective_user
    if msg is None or admin is None or ctx.user_data is None:
        return ConversationHandler.END
    raw_args = parse_cmd_args(msg.text)

    # * Reply wins in extract_target, so every arg is reason text when the
    # * command replies to a user (shared helper mirrors priority 1,
    # * including the anonymous-admin sender_chat skip).
    has_explicit_target = (
        not extraction.has_reply_target(msg)
        and bool(raw_args)
        and (raw_args[0].lstrip("-").isdigit() or raw_args[0].startswith("@"))
    )
    target_id, target_fname = await extraction.extract_target(update, raw_args, ctx.bot)

    ban_reason = " ".join(raw_args[1:] if has_explicit_target else raw_args).strip()

    if not target_id:
        try:
            await msg.reply_text(replies.ERR_CANNOT_RESOLVE)
        except Exception as exc:
            log.debug("cmd_ban_start no-target reply failed: %s", exc)
        return ConversationHandler.END

    if not ban_reason:
        try:
            await msg.reply_text(_ERR_REASON_REQUIRED)
        except Exception as exc:
            log.debug("cmd_ban_start no-reason reply failed: %s", exc)
        return ConversationHandler.END

    # * Identity check + role lookup happen in parallel; both depend only on
    # * already-resolved IDs so there is no need to wait for them sequentially.
    # * return_exceptions=True prevents a DB failure from leaving the ConversationHandler open.
    ident, role_result = await asyncio.gather(
        identity.classify(ctx.bot, admin.id, target_id, target_fname),
        resolve_and_check(msg, admin.id, target_id, min_role="developer"),
        return_exceptions=True,
    )
    if isinstance(ident, asyncio.CancelledError):
        raise ident
    if isinstance(role_result, asyncio.CancelledError):
        raise role_result
    if isinstance(ident, BaseException):
        log.exception("identity.classify failed in cmd_ban_start: %s", ident)
        return ConversationHandler.END
    if isinstance(role_result, BaseException):
        log.exception("resolve_and_check failed in cmd_ban_start: %s", role_result)
        return ConversationHandler.END
    # * isinstance + early return above already narrows role_result to the
    # * success tuple; no assert needed (asserts vanish under python -O).
    executor_role, target_role = role_result
    # * Guard first: if resolve_and_check already replied and rejected (e.g. target
    # * outranks executor), skip the identity refusal to avoid sending two replies.
    if executor_role is None:
        return ConversationHandler.END

    refusal = identity.refuse_message("ban", ident)
    if refusal is not None:
        try:
            await msg.reply_text(refusal, parse_mode="HTML")
        except Exception as exc:
            log.debug("cmd_ban_start refusal reply failed: %s", exc)
        return ConversationHandler.END

    # * Auto-demote is required before the ban to preserve the
    # * role-vs-state invariant: a banned user must not still hold a
    # * federation role. The helper replies and signals abort when the
    # * demote fails, so the ban never proceeds on a role holder.
    if target_role and not await Demote.auto_demote_or_abort(
        msg,
        ctx.bot,
        target_id,
        target_fname or str(target_id),
        target_role,
        admin.id,
        admin.first_name,
        trigger="ban",
    ):
        return ConversationHandler.END

    ctx.user_data["ban_target_id"] = target_id
    ctx.user_data["ban_target_fname"] = target_fname or str(target_id)
    ctx.user_data["ban_reason"] = ban_reason
    ctx.user_data["ban_admin_id"] = admin.id
    ctx.user_data["ban_admin_fname"] = admin.first_name

    target_mention = mention(target_id, target_fname or str(target_id))
    try:
        prompt = await msg.reply_text(
            proof.noted_prompt("ban", ban_reason, target_mention),
            parse_mode="HTML",
            reply_markup=proof.keyboard(),
        )
        ctx.user_data["ban_prompt_msg_id"] = prompt.message_id
        ctx.user_data["ban_prompt_chat_id"] = msg.chat.id
    except Exception as exc:
        log.debug("cmd_ban_start proof-prompt reply failed: %s", exc)
        for key in (
            "ban_target_id",
            "ban_target_fname",
            "ban_reason",
            "ban_admin_id",
            "ban_admin_fname",
        ):
            ctx.user_data.pop(key, None)
        return ConversationHandler.END

    return WAITING_PROOF


# ──────────────────────────── Handlers ──────────────────────────── #

_BAN_CMDS = build_prefixed_filters("tcban") | build_prefixed_filters("tcb")

__handlers__ = [ban_conversation(cmd_ban_start, _BAN_CMDS)]
