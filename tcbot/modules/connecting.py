# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Group connect command handler: manages federation group onboarding."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram.ext import (
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
)

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper import decorators, replies
from tcbot.modules.helper.formatter import bold, code, esc
from tcbot.modules.helper.workflows.connected_flow import connection
from tcbot.modules.maintenance import _is_primary_group
from tcbot.utils.prefixes import build_prefixed_filters
from tcbot.utils.time_and_date import TELEGRAM_LOOKUP_TIMEOUT

if TYPE_CHECKING:
    from telegram import Update

log = logging.getLogger(__name__)

# ──────────────── User-facing reply constants ──────────────────── #

_ERR_ADMIN_REQUIRED = "Only group admins can request to connect."
_ERR_PENDING_REQUEST = "A connect request for this group is already pending."

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 60
_RL_LIMIT: int = 3


# ────────────────────── Module & Help Message ───────────────────── #

_CNAME = esc(cfg.community_name)

__module_name__ = "Connect"
__help_text__ = (
    f"Connects your group to the {_CNAME} federation so federation bans, "
    f"mutes, and broadcasts are applied automatically."
)

__help_sections__: list[tuple[str, str]] = [
    (
        replies.SEC_COMMANDS,
        f"{code('/tcconnect')} (alias: {code('/tccon')})",
    ),
    replies.who_section("Group admins and creators only (checked per-group)."),
    replies.where_section(f"Inside the group you want to connect to {_CNAME}."),
    (
        replies.SEC_WHAT,
        f"Connects your group to the {_CNAME} federation. Once connected:\n"
        f"- Federation bans are automatically enforced: currently banned users are removed, "
        f"and newly banned users are kicked on ban.\n"
        f"- Federation mutes are applied when issued.\n"
        f"- Broadcast messages from TC Staff are forwarded to your group.",
    ),
    (
        "Required permissions",
        f"Before running the command, make the bot a group admin with these three "
        f"permissions: {bold('Delete Messages')}, {bold('Ban Users')}, and {bold('Invite Users via Link')}.",
    ),
    (
        "Notes",
        "If a connect request is already pending for your group, a second request will be "
        "rejected; wait for TC Staff to process the existing one.\n\n"
        "When the bot is first added to a group, it automatically prompts the group owner "
        "to connect, so you can also just add the bot and follow that prompt.",
    ),
    (
        "Example",
        f"Make the bot a group admin, then run {code('/tcconnect')} inside the group.",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ───────────── Command to Connect a Group </tcconnect> ──────────── #


@decorators.ratelimiter(limit=_RL_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def cmd_tcconnect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Request to connect the current group to the federation.

    Group-only command. Checks admin status, existing connection, and pending
    requests in parallel (Telegram lookup is bounded to avoid stalls). On
    success, creates a pending request and notifies the main group for founder
    approval.
    """
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if chat is None or user is None or msg is None:
        return

    if chat.type == "private":
        try:
            await msg.reply_text(replies.ERR_GROUP_ONLY)
        except Exception as exc:
            log.debug("cmd_tctc group-only reply failed: %s", exc)
        return

    # * All four calls are independent; fire them in one round-trip.
    # * bot_member is fetched speculatively alongside the user/DB reads so no
    # * extra Telegram round-trip is needed after the early-exit checks.
    member, is_connected, pending, bot_member = await asyncio.gather(
        asyncio.wait_for(
            ctx.bot.get_chat_member(chat.id, user.id), timeout=TELEGRAM_LOOKUP_TIMEOUT
        ),
        db.groups_db.is_connected(chat.id),
        db.groups_db.get_pending(chat.id),
        asyncio.wait_for(
            ctx.bot.get_chat_member(chat.id, ctx.bot.id),
            timeout=TELEGRAM_LOOKUP_TIMEOUT,
        ),
        return_exceptions=True,
    )
    if isinstance(member, BaseException):
        log.debug("get_chat_member failed for %d/%d: %s", chat.id, user.id, member)
        try:
            await msg.reply_text(replies.ERR_ROLE_VERIFY)
        except Exception as exc:
            log.debug("cmd_tctc role-verify reply failed: %s", exc)
        return

    if member.status not in ("administrator", "creator"):
        try:
            await msg.reply_text(_ERR_ADMIN_REQUIRED)
        except Exception as exc:
            log.debug("cmd_tctc admin-required reply failed: %s", exc)
        return

    # * Primary groups are required enforcement destinations, not federation
    # * members; connecting them would pollute federated_groups with an ID
    # * that fan-out paths unconditionally include.
    if _is_primary_group(chat.id):
        try:
            await msg.reply_text(
                "This is a primary group of the federation (main or exec). "
                "Primary groups are not connected via /tcconnect."
            )
        except Exception as exc:
            log.debug("cmd_tctc primary-group reply failed: %s", exc)
        return

    if isinstance(is_connected, BaseException):
        is_connected = False
    if isinstance(pending, BaseException):
        pending = None

    if is_connected:
        try:
            await msg.reply_text(connection.already_connected_message())
        except Exception as exc:
            log.debug("cmd_tctc already-connected reply failed: %s", exc)
        return

    if pending:
        try:
            await msg.reply_text(_ERR_PENDING_REQUEST)
        except Exception as exc:
            log.debug("cmd_tctc pending-request reply failed: %s", exc)
        return

    if isinstance(bot_member, BaseException):
        log.debug("Could not verify bot permissions for %d: %s", chat.id, bot_member)
        try:
            await msg.reply_text(replies.ERR_ROLE_VERIFY)
        except Exception as exc:
            log.debug("cmd_tctc perms-verify reply failed: %s", exc)
        return

    if not connection.check_perms(bot_member):
        try:
            await msg.reply_text(connection.perms_required_message())
        except Exception as exc:
            log.debug("cmd_tctc perms-required reply failed: %s", exc)
        return

    # * Run complete_join first so that we only send a "connected" confirmation
    # * when the DB write (add_group) actually succeeded.  Sending the reply in
    # * parallel was an optimistic pattern that silently swallowed add_group
    # * failures and left the user with a false confirmation.
    try:
        await connection.complete_join(
            chat.id, chat.title or "", user.id, user.first_name, ctx.bot
        )
    except Exception:
        log.exception("complete_join failed for chat %d", chat.id)
        try:
            await msg.reply_text(
                "Failed to connect the group due to a server error. Please try again."
            )
        except Exception as reply_exc:
            log.debug("connect failure reply failed: %s", reply_exc)
        return
    try:
        await msg.reply_text(connection.connected_message())
    except Exception as exc:
        log.debug("connected reply failed for chat %d: %s", chat.id, exc)


# ──────────────────────────── Handlers ──────────────────────────── #

_CONNECT_CMDS = build_prefixed_filters("tcconnect") | build_prefixed_filters("tccon")

__handlers__ = [
    ChatMemberHandler(connection.on_bot_added, ChatMemberHandler.MY_CHAT_MEMBER),
    MessageHandler(_CONNECT_CMDS, cmd_tcconnect),
    CallbackQueryHandler(
        connection.on_join_decision,
        pattern=rf"^({connection.join_callback}|{connection.cancel_callback})$",
    ),
]
