# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Group mute and unmute command handlers: validates permissions and delegates to muting_flow."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram.ext import ContextTypes, ConversationHandler, MessageHandler

from tcbot import cfg
from tcbot.modules.helper import decorators, extraction, identity, replies
from tcbot.modules.helper.decorators import resolve_and_check
from tcbot.modules.helper.formatter import bold, code, mention
from tcbot.modules.helper.workflows.demote_flow import Demote
from tcbot.modules.helper.workflows.muting_flow import (
    _DURATION_RE,
    execute_unmute,
    fmt_duration,
    mute_conversation,
    parse_duration,
    proof,
    reason,
)
from tcbot.modules.helper.workflows.reason_flow import (
    WAITING_PROOF,
    WAITING_REASON,
    parse_inline_reason,
)
from tcbot.utils.prefixes import build_prefixed_filters, parse_cmd_args

if TYPE_CHECKING:
    from telegram import Update

log = logging.getLogger(__name__)

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 60
_RL_LIMIT: int = 5


# ────────────────────── Module & Help Message ───────────────────── #

__module_name__ = "Mute"
__help_text__ = (
    "Federation-wide mute and unmute: restricts a user from sending messages "
    f"across {bold('all connected groups')} at once."
)

__help_sections__: list[tuple[str, str]] = [
    (
        replies.SEC_COMMANDS,
        f"{code('/tcmute')} (alias: {code('/tcm')})\n"
        f"{code('/tcunmute')} (aliases: {code('/tcunm')}, {code('/tcum')})",
    ),
    replies.who_section(replies.PERM_TESTER_ABOVE),
    replies.where_section(replies.WHERE_CONNECTED_GROUP),
    (
        replies.SEC_WHAT,
        f"{bold('/tcmute')}: restricts a user from sending messages, media, stickers, and GIFs "
        f"across {bold('all connected groups')} simultaneously. After the command, the bot "
        "asks for a reason and optionally proof - both steps can be skipped. If the user "
        "is already muted, the existing restriction is replaced. A summary shows how many "
        "groups the mute was applied in.\n\n"
        f"{bold('/tcunmute')}: restores the user's full send permissions across all connected "
        "groups. A summary shows how many groups the unmute was applied in.",
    ),
    (
        "Time format",
        "Place the duration before the reason. Omit a duration to apply a permanent mute.\n\n"
        f"- {code('s')} Seconds: {code('30s')} = 30 seconds\n"
        f"- {code('m')} Minutes: {code('15m')} = 15 minutes\n"
        f"- {code('h')} Hours: {code('2h')} = 2 hours\n"
        f"- {code('d')} Days: {code('7d')} = 7 days\n"
        f"- {code('w')} Weeks: {code('2w')} = 2 weeks\n"
        f"- {code('mo')} Months: {code('3mo')} = 3 months\n"
        f"- {code('ye')} Years: {code('2ye')} = 2 years",
    ),
    replies.target_section(),
    (
        replies.SEC_EXAMPLES,
        f"{code('/tcmute @username 3d spamming')}: 3-day mute, reason inline\n"
        f"{code('/tcm @username 1w')}: 1-week mute, bot will ask for reason\n"
        f"{code('/tcm @username')}: permanent mute, bot walks you through it\n"
        f"{code('/tcunmute @username')}: lift mute immediately across all groups",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ───────────────────── Command Mute </tcmute> ───────────────────── #


@decorators.ratelimiter(limit=_RL_LIMIT, period=_RL_PERIOD_S)
@decorators.basic_mod_only
@decorators.log_execution
async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the mute flow.

    Resolves the target, parses an optional duration token from the inline
    arguments, runs identity and role checks in parallel, auto-demotes any
    federation role, then opens the reason/proof conversation (an inline
    reason skips the reason prompt and goes straight to proof collection).
    Returns ``ConversationHandler.END`` on validation failure.
    """
    msg = update.effective_message
    admin = update.effective_user
    assert msg is not None
    assert admin is not None
    assert ctx.user_data is not None

    raw_args = parse_cmd_args(msg.text)
    has_explicit_target = bool(raw_args) and (
        raw_args[0].lstrip("-").isdigit() or raw_args[0].startswith("@")
    )
    target_id, target_fname = await extraction.extract_target(update, raw_args, ctx.bot)

    remaining_args = list(raw_args[1:] if has_explicit_target else raw_args)

    if not target_id:
        try:
            await msg.reply_text(replies.ERR_CANNOT_RESOLVE)
        except Exception as exc:
            log.debug("cmd_mute no-target reply failed: %s", exc)
        return ConversationHandler.END

    # * return_exceptions=True prevents a DB failure from leaving the ConversationHandler open.
    ident, role_result = await asyncio.gather(
        identity.classify(ctx.bot, admin.id, target_id, target_fname),
        resolve_and_check(msg, admin.id, target_id, min_role="tester"),
        return_exceptions=True,
    )
    if isinstance(ident, BaseException):
        log.exception("identity.classify failed in cmd_mute: %s", ident)
        return ConversationHandler.END
    if isinstance(role_result, BaseException):
        log.exception("resolve_and_check failed in cmd_mute: %s", role_result)
        return ConversationHandler.END
    assert role_result is not None
    executor_role, target_role = role_result
    # * Guard first: if resolve_and_check already replied and rejected (e.g. target
    # * outranks executor), skip the identity refusal to avoid sending two replies.
    if executor_role is None:
        return ConversationHandler.END

    refusal = identity.refuse_message("mute", ident)
    if refusal is not None:
        try:
            await msg.reply_text(refusal, parse_mode="HTML")
        except Exception as exc:
            log.debug("cmd_mute refusal reply failed: %s", exc)
        return ConversationHandler.END

    # * Auto-demote must succeed before the federation-wide mute to
    # * preserve the role-vs-state invariant. The helper replies and
    # * signals abort when the demote fails, so the mute never proceeds
    # * on a role holder.
    if target_role and not await Demote.auto_demote_or_abort(
        msg,
        ctx.bot,
        target_id,
        target_fname or str(target_id),
        target_role,
        admin.id,
        admin.first_name,
        trigger="mute",
    ):
        return ConversationHandler.END

    duration = None
    if remaining_args and _DURATION_RE.match(remaining_args[0]):
        duration = parse_duration(remaining_args.pop(0))

    inline_reason = parse_inline_reason(remaining_args, has_explicit_target=False)
    target_mention = mention(target_id, target_fname or str(target_id))
    dur_str = fmt_duration(duration)
    extra_info = f"{code(str(target_id))}: {dur_str}"

    ctx.user_data.update(
        {
            "mute_target_id": target_id,
            "mute_target_fname": target_fname or str(target_id),
            "mute_duration": duration,
            "mute_admin_id": admin.id,
            "mute_admin_fname": admin.first_name,
            "mute_prompt_chat": msg.chat.id,
            "mute_reason": "",
            "mute_proof_desc": None,
            "mute_extra_info": extra_info,
        }
    )

    _MUTE_KEYS = (
        "mute_target_id",
        "mute_target_fname",
        "mute_duration",
        "mute_admin_id",
        "mute_admin_fname",
        "mute_prompt_chat",
        "mute_reason",
        "mute_proof_desc",
        "mute_extra_info",
    )

    if inline_reason:
        ctx.user_data["mute_reason"] = inline_reason
        try:
            prompt = await msg.reply_text(
                proof.noted_prompt(
                    "mute", inline_reason, target_mention, extra_info=extra_info
                ),
                parse_mode="HTML",
                reply_markup=proof.keyboard(),
            )
            ctx.user_data["mute_prompt_id"] = prompt.message_id
        except Exception as exc:
            log.debug("cmd_mute proof-prompt send failed: %s", exc)
            for key in _MUTE_KEYS:
                ctx.user_data.pop(key, None)
            return ConversationHandler.END
        return WAITING_PROOF

    try:
        prompt = await msg.reply_text(
            reason.prompt(target_mention, "mute", extra_info=extra_info),
            parse_mode="HTML",
            reply_markup=reason.keyboard(),
        )
        ctx.user_data["mute_prompt_id"] = prompt.message_id
    except Exception as exc:
        log.debug("cmd_mute reason-prompt send failed: %s", exc)
        for key in _MUTE_KEYS:
            ctx.user_data.pop(key, None)
        return ConversationHandler.END
    return WAITING_REASON


# ─────────────────── Command Unmute </tcunmute> ─────────────────── #


@decorators.ratelimiter(limit=_RL_LIMIT, period=_RL_PERIOD_S)
@decorators.basic_mod_only
@decorators.log_execution
async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove the chat restriction from the target and restore messaging rights.

    Resolves the target, runs identity and role checks in parallel (mirroring
    ``cmd_mute``), optionally emits a staff-action notice, then delegates to
    ``execute_unmute``.
    """
    msg = update.effective_message
    admin = update.effective_user
    assert msg is not None
    assert admin is not None
    args = parse_cmd_args(msg.text)
    target_id, target_name = await extraction.extract_target(update, args, ctx.bot)
    if not target_id:
        try:
            await msg.reply_text(replies.ERR_CANNOT_RESOLVE)
        except Exception as exc:
            log.debug("cmd_unmute no-target reply failed: %s", exc)
        return

    # * Run identity classification and rank check in parallel.
    # * return_exceptions=True prevents a DB error from leaving the command silently dead.
    ident, role_result = await asyncio.gather(
        identity.classify(ctx.bot, admin.id, target_id, target_name),
        resolve_and_check(msg, admin.id, target_id, min_role="tester"),
        return_exceptions=True,
    )
    if isinstance(ident, BaseException):
        log.exception("identity.classify failed in cmd_unmute: %s", ident)
        return
    if isinstance(role_result, BaseException):
        log.exception("resolve_and_check failed in cmd_unmute: %s", role_result)
        return
    assert role_result is not None
    executor_role, _ = role_result
    # * Guard first: if resolve_and_check already replied and rejected (e.g. target
    # * outranks executor), skip the identity refusal to avoid sending two replies.
    if executor_role is None:
        return

    refusal = identity.refuse_message("unmute", ident)
    if refusal is not None:
        try:
            await msg.reply_text(refusal, parse_mode="HTML")
        except Exception as exc:
            log.debug("cmd_unmute refusal reply failed: %s", exc)
        return

    notice = identity.staff_notice("unmute", ident, cfg.community_name)
    if notice is not None:
        try:
            await msg.reply_text(notice, parse_mode="HTML")
        except Exception as exc:
            log.debug("cmd_unmute notice reply failed: %s", exc)

    await execute_unmute(update, ctx, target_id, target_name or str(target_id))


# ──────────────────────────── Handlers ──────────────────────────── #

_MUTE_CMDS = build_prefixed_filters("tcmute") | build_prefixed_filters("tcm")
_UNMUTE_CMDS = (
    build_prefixed_filters("tcunmute")
    | build_prefixed_filters("tcunm")
    | build_prefixed_filters("tcum")
)

__handlers__ = [
    mute_conversation(cmd_mute, _MUTE_CMDS, escape_filter=_UNMUTE_CMDS),
    MessageHandler(_UNMUTE_CMDS, cmd_unmute),
]
