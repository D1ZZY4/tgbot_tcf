# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Group kick command entry point: validates permissions and delegates to kicking_flow."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram.ext import ContextTypes, ConversationHandler

from tcbot.modules.helper import decorators, extraction, identity, replies
from tcbot.modules.helper.decorators import resolve_and_check
from tcbot.modules.helper.formatter import bold, code, mention
from tcbot.modules.helper.workflows.demote_flow import Demote
from tcbot.modules.helper.workflows.kicking_flow import kick_conversation, proof, reason
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

__module_name__ = "Kick"
__help_text__ = (
    f"Removes a user from the {bold('current group only')}. Federation roles are auto-removed "
    "if the target is staff."
)

__help_sections__: list[tuple[str, str]] = [
    (
        replies.SEC_COMMANDS,
        f"{code('/tckick')} (alias: {code('/tck')})",
    ),
    replies.who_section(replies.PERM_TESTER_ABOVE),
    replies.where_section(replies.WHERE_CONNECTED_GROUP),
    (
        replies.SEC_WHAT,
        f"Removes a user from the {bold('current group only')}. This is not a federation-wide "
        "action; the user can rejoin via an invite link unless they are separately "
        "federation-banned.\n\n"
        "If the target holds a federation role (Tester / Developer / Admin), that role is "
        "automatically removed and they are notified by DM. A log entry is posted to the "
        "federation logs channel.",
    ),
    (
        "Flow",
        f"1. Run {code('/tckick')} with the target (and optional inline reason).\n"
        f"2. If no reason was given, the bot asks: reply with text or tap {bold('Skip')}.\n"
        f"3. The bot asks for proof: send a photo/video or tap {bold('Skip')}.",
    ),
    replies.target_section(),
    (
        replies.SEC_EXAMPLES,
        f"{code('/tckick @username being disruptive')}: reason inline\n"
        f"{code('/tck 123456789')}: bot will ask for reason\n"
        f"Or reply to a message and run {code('/tck')}.",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ───────────────────── Command Kick </tckick> ───────────────────── #


@decorators.ratelimiter(limit=_RL_LIMIT, period=_RL_PERIOD_S)
@decorators.basic_mod_only
@decorators.log_execution
async def cmd_kick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the kick flow.

    Resolves the target, runs identity and role checks in parallel, auto-demotes
    any federation role, then either executes an immediate kick (with inline
    reason) or opens the reason/proof conversation. Returns
    ``ConversationHandler.END`` on validation failure.
    """
    msg = update.effective_message
    admin = update.effective_user
    if msg is None or admin is None or ctx.user_data is None:
        return ConversationHandler.END

    args = parse_cmd_args(msg.text)
    # * Reply wins in extract_target, so every arg is reason text when the
    # * command replies to a user. Checking the shape of args[0] alone
    # * would drop a leading numeric/@ token that belongs to the reason
    # * (e.g. reply + "/tck 12345 spamming" lost "12345").
    has_explicit_target = (
        not extraction.has_reply_target(msg)
        and bool(args)
        and (args[0].lstrip("-").isdigit() or args[0].startswith("@"))
    )
    target_id, target_name = await extraction.extract_target(update, args, ctx.bot)

    inline_reason = parse_inline_reason(args, has_explicit_target=has_explicit_target)

    if not target_id:
        try:
            await msg.reply_text(replies.ERR_CANNOT_RESOLVE)
        except Exception as exc:
            log.debug("cmd_kick no-target reply failed: %s", exc)
        return ConversationHandler.END

    # * return_exceptions=True prevents a DB failure from leaving the ConversationHandler open.
    ident, role_result = await asyncio.gather(
        identity.classify(ctx.bot, admin.id, target_id, target_name),
        resolve_and_check(msg, admin.id, target_id, min_role="tester"),
        return_exceptions=True,
    )
    if isinstance(ident, asyncio.CancelledError):
        raise ident
    if isinstance(role_result, asyncio.CancelledError):
        raise role_result
    if isinstance(ident, BaseException):
        log.exception("identity.classify failed in cmd_kick: %s", ident)
        return ConversationHandler.END
    if isinstance(role_result, BaseException):
        log.exception("resolve_and_check failed in cmd_kick: %s", role_result)
        return ConversationHandler.END
    # * isinstance + early return above already narrows role_result to the
    # * success tuple; no assert needed (asserts vanish under python -O).
    executor_role, target_role = role_result
    # * Guard first: if resolve_and_check already replied and rejected (e.g. target
    # * outranks executor), skip the identity refusal to avoid sending two replies.
    if executor_role is None:
        return ConversationHandler.END

    refusal = identity.refuse_message("kick", ident)
    if refusal is not None:
        try:
            await msg.reply_text(refusal, parse_mode="HTML")
        except Exception as exc:
            log.debug("cmd_kick refusal reply failed: %s", exc)
        return ConversationHandler.END

    # * Auto-demote must succeed before the kick to preserve the
    # * role-vs-state invariant. The helper replies and signals abort
    # * when the demote fails, so the kick never proceeds on a role holder.
    if target_role and not await Demote.auto_demote_or_abort(
        msg,
        ctx.bot,
        target_id,
        target_name or str(target_id),
        target_role,
        admin.id,
        admin.first_name,
        trigger="kick",
    ):
        return ConversationHandler.END

    ctx.user_data.update(
        {
            "kick_target_id": target_id,
            "kick_target_name": target_name or str(target_id),
            "kick_proof_desc": None,
        }
    )

    target_mention = mention(target_id, target_name or str(target_id))

    _KICK_KEYS = ("kick_target_id", "kick_target_name", "kick_proof_desc")

    if inline_reason:
        ctx.user_data["kick_reason"] = inline_reason
        try:
            await msg.reply_text(
                proof.noted_prompt("kick", inline_reason, target_mention),
                parse_mode="HTML",
                reply_markup=proof.keyboard(),
            )
        except Exception as exc:
            log.debug("cmd_kick proof-prompt reply failed: %s", exc)
            for key in (*_KICK_KEYS, "kick_reason"):
                ctx.user_data.pop(key, None)
            return ConversationHandler.END
        return WAITING_PROOF

    try:
        await msg.reply_text(
            reason.prompt(target_mention, "kick"),
            parse_mode="HTML",
            reply_markup=reason.keyboard(),
        )
    except Exception as exc:
        log.debug("cmd_kick reason-prompt reply failed: %s", exc)
        for key in _KICK_KEYS:
            ctx.user_data.pop(key, None)
        return ConversationHandler.END
    return WAITING_REASON


# ──────────────────────────── Handlers ──────────────────────────── #

_KICK_CMDS = build_prefixed_filters("tckick") | build_prefixed_filters("tck")

__handlers__ = [kick_conversation(cmd_kick, _KICK_CMDS)]
