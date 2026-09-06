# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Admin management handlers: promote, demote, transfer ownership, and manage requests."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper import (
    decorators,
    extraction,
    identity,
    keyboards,
    parse_logmsg,
    replies,
)
from tcbot.modules.helper.decorators import resolve_and_check
from tcbot.modules.helper.formatter import bold, code, esc, mention, user_ref
from tcbot.modules.helper.identity import ANONYMOUS_BOT_ID
from tcbot.modules.helper.workflows.demote_flow import Demote
from tcbot.modules.helper.workflows.promote_flow import ROLE_ALIASES, Promote
from tcbot.utils import error_reporter
from tcbot.utils.prefixes import build_prefixed_filters, parse_cmd_args

if TYPE_CHECKING:
    from telegram import Update

log = logging.getLogger(__name__)

# ──────────────── User-facing reply constants ──────────────────── #

_ERR_NO_ASSIGN_PERMS = "You don't have permission to assign any roles."
_MSG_PROMOTE_CANCELLED = "Promotion cancelled. No changes were made."
_ERR_NO_REMOVABLE_ROLE = "That user doesn't hold a role that can be removed."
_ERR_FOUNDER_DEMOTE_ONLY = "Only the Founder can demote an Admin."
_ERR_NO_LONGER_REMOVABLE = "That user no longer holds a removable role."
_ERR_ROLE_CLEAR_FAILED = "Couldn't remove the role - it may have already been cleared."
_ERR_ROLE_LOOKUP_FAILED = (
    "I couldn't verify federation roles right now. Please try again in a moment."
)
_MSG_CANCELLED = "Cancelled. No changes were made."
_MSG_NO_PENDING = "No pending promotion requests."
_ERR_REQUEST_NOT_FOUND = "Request not found or already resolved."
_ERR_CLASSIFY_FAILED = "Classification check failed - please try again."

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 30
_RL_PERIOD_LONG_S: int = 60
_RL_PERIOD_BULK_S: int = 300
_RL_CMD_LIMIT: int = 10
_RL_QUERY_LIMIT: int = 5
_RL_BULK_LIMIT: int = 3


# ────────────────────── Module & Help Message ───────────────────── #

__module_name__ = "Admin"
__help_text__ = (
    "Promote and demote staff, transfer ownership, and manage promotion requests "
    "across the federation."
)

__help_sections__: list[tuple[str, str]] = [
    (
        replies.SEC_COMMANDS,
        f"{code('/tcpromote')} (alias: {code('/tcp')})\n"
        f"{code('/tcdemote')} (alias: {code('/tcd')})\n"
        f"{code('/transferowner')} (alias: {code('/tfowner')})\n"
        f"{code('/tcpromoterequests')} (alias: {code('/tcreqs')})\n"
        f"{code('/tcpromotelist')} (alias: {code('/tcplist')})",
    ),
    replies.who_section(
        f"{bold('/tcpromote')}, {bold('/tcdemote')}, {bold('/tcpromotelist')}: Founder and Admin.\n"
        f"{bold('/transferowner')}: {replies.PERM_FOUNDER_ONLY}\n"
        f"{bold('/tcpromoterequests')}: anyone (creates a self-request to the Founder)."
    ),
    replies.where_section(replies.CONTEXT_BOT_OR_GROUP),
    (
        "Role Hierarchy",
        "Founder (rank 4) > Admin (rank 3) > Developer (rank 2) > Tester (rank 1)\n\n"
        "You cannot promote a user to a rank equal to or above your own. "
        "Admins promoting someone to Admin queues a request for the Founder.",
    ),
    replies.target_section(),
    (
        "/tcpromote",
        "Assigns a role to a user. Omit the role argument to get an inline button menu.\n\n"
        f"{bold('Usage:')} {code('/tcpromote <target> [admin|developer|tester]')}\n"
        "- Founder can promote to any role directly.\n"
        "- Admin can promote to Developer or Tester directly; promoting to Admin "
        "sends a pending request to the Founder for approval.",
    ),
    (
        "/tcdemote",
        "Removes a user's role. A confirmation button is shown before the action executes.\n\n"
        f"{bold('Usage:')} {code('/tcdemote <target>')}\n"
        "- Founder can demote any role.\n"
        "- Admin can demote Developer or Tester only.\n"
        "- When a user with a role is banned or kicked, their role is automatically removed "
        "and they are notified by DM.",
    ),
    (
        "/transferowner",
        "Transfers federation ownership to another user. The current Founder steps down "
        "to Admin. Founder only.\n\n"
        f"{bold('Usage:')} {code('/transferowner <target>')}",
    ),
    (
        replies.SEC_EXAMPLES,
        f"{code('/tcpromote @username developer')}\n"
        f"{code('/tcpromote 123456789')} - shows role selection menu\n"
        f"{code('/tcdemote @username')}\n"
        f"{code('/transferowner @newowner')}\n"
        f"{code('/tcpromoterequests')} - request promotion to Admin\n"
        f"{code('/tcplist')} - list pending promotion requests",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ────────────────── Command Promote </tcpromote> ────────────────── #


@decorators.ratelimiter(limit=_RL_QUERY_LIMIT, period=_RL_PERIOD_LONG_S)
@decorators.staff_only
@decorators.log_execution
async def cmd_promote(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Assign a federation role to a user (admin / developer / tester).

    Fetches the executor role and resolves the target in parallel. Then fetches
    identity classification and the target's current role in a second parallel
    gather, since both are independent reads. When a role name is given inline,
    executes the promotion immediately via ``Promote.execute``. When no role is
    given, shows a role-selection keyboard. Identity and rank checks prevent
    self-promotion or promoting above one's own rank.
    """
    admin = update.effective_user
    msg = update.effective_message
    if admin is None or msg is None:
        return
    args = parse_cmd_args(msg.text)

    has_explicit_target = bool(args) and (
        args[0].lstrip("-").isdigit() or args[0].startswith("@")
    )
    # * Executor role (founder/admin guaranteed by @staff_only) + target run in parallel
    _exec_r, _target_r = await asyncio.gather(
        db.users_roles.get_effective_role(admin.id),
        extraction.extract_target(update, args, ctx.bot),
        return_exceptions=True,
    )
    if isinstance(_exec_r, asyncio.CancelledError):
        raise _exec_r
    if isinstance(_target_r, asyncio.CancelledError):
        raise _target_r
    executor_role = None if isinstance(_exec_r, BaseException) else _exec_r
    if executor_role is None:
        return
    if isinstance(_target_r, BaseException):
        log.error("extract_target failed during promote: %s", _target_r)
        try:
            await msg.reply_text(replies.ERR_CANNOT_RESOLVE)
        except Exception as exc:
            log.debug("cmd_promote no-target reply failed: %s", exc)
        return
    target_id, target_fname = _target_r
    remaining_args = args[1:] if has_explicit_target else args
    role_arg = remaining_args[0].lower() if remaining_args else ""

    if not target_id:
        try:
            await msg.reply_text(replies.ERR_CANNOT_RESOLVE)
        except Exception as exc:
            log.debug("cmd_promote no-target-id reply failed: %s", exc)
        return

    # * identity classify and current-role fetch are independent reads; run in parallel.
    ident, current_role = await asyncio.gather(
        identity.classify(ctx.bot, admin.id, target_id, target_fname),
        db.users_roles.get_effective_role(target_id),
        return_exceptions=True,
    )
    if isinstance(ident, BaseException):
        if isinstance(ident, asyncio.CancelledError):
            raise ident
        log.error(
            "identity.classify failed during promote for target=%d: %s",
            target_id,
            ident,
        )
        try:
            await msg.reply_text(_ERR_CLASSIFY_FAILED)
        except Exception as exc:
            log.debug("cmd_promote classify-failed reply failed: %s", exc)
        return
    if isinstance(current_role, asyncio.CancelledError):
        raise current_role
    if isinstance(current_role, BaseException):
        log.error(
            "target role lookup failed during promote for target=%d: %s",
            target_id,
            current_role,
        )
        try:
            await msg.reply_text(_ERR_ROLE_LOOKUP_FAILED)
        except Exception as exc:
            log.debug("cmd_promote role-lookup-failed reply failed: %s", exc)
        return
    refusal = identity.refuse_message("promote", ident)
    if refusal is not None:
        try:
            await msg.reply_text(refusal, parse_mode="HTML")
        except Exception as exc:
            log.debug("cmd_promote refusal reply failed: %s", exc)
        return

    role = ROLE_ALIASES.get(role_arg)

    if role:
        _, text = await Promote.execute(
            ctx.bot,
            admin.id,
            admin.first_name,
            executor_role,
            target_id,
            target_fname or str(target_id),
            current_role,
            role,
        )
        try:
            await msg.reply_text(text, parse_mode="HTML")
        except Exception as exc:
            log.debug("cmd_promote result reply failed: %s", exc)
        return

    # * No role arg - show selection buttons
    available = Promote.available_roles_for(executor_role)
    if not available:
        try:
            await msg.reply_text(_ERR_NO_ASSIGN_PERMS)
        except Exception as exc:
            log.debug("cmd_promote no-perms reply failed: %s", exc)
        return
    try:
        await msg.reply_text(
            f"Choose a role to assign to {mention(target_id, target_fname or str(target_id), ident.username)}:",
            parse_mode="HTML",
            reply_markup=keyboards.promote_role_kb(target_id, available),
        )
    except Exception as exc:
        log.debug("cmd_promote role-picker reply failed: %s", exc)


# ──────────────────────── Callback Handlers ─────────────────────── #


@decorators.ratelimiter(limit=_RL_CMD_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_promote_role_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the role-selection inline button from /tcpromote.

    Verifies the executor still holds staff rank; rejects with alert if expired.
    Fetches the target's name and current role in parallel, then delegates to
    ``Promote.execute`` and edits the prompt to the result.
    """
    q = update.callback_query
    if q is None or q.data is None:
        return
    admin = update.effective_user
    if admin is None:
        await q.answer()
        return
    parts = q.data.split(":", 2)
    if len(parts) != 3:
        await q.answer()
        return
    _, role, target_id_str = parts
    try:
        target_id = int(target_id_str)
    except ValueError:
        await q.answer()
        return

    # * Gather role check + q.answer() in parallel so the spinner disappears
    # * immediately, regardless of DB latency.
    executor_role, _ = await asyncio.gather(
        db.users_roles.get_effective_role(admin.id),
        q.answer(),
        return_exceptions=True,
    )
    if isinstance(executor_role, asyncio.CancelledError):
        raise executor_role
    if isinstance(executor_role, BaseException) or executor_role not in (
        "founder",
        "admin",
    ):
        try:
            await q.edit_message_text(replies.ERR_PERM_EXPIRED, reply_markup=None)
        except Exception as exc:
            log.debug("admins promote perm-expired edit failed: %s", exc)
        return

    if role not in ("admin", "developer", "tester"):
        try:
            await q.edit_message_text(replies.ERR_UNKNOWN_ROLE, reply_markup=None)
        except Exception as exc:
            log.debug("admins promote unknown-role edit failed: %s", exc)
        return
    target_fname, current_role = await asyncio.gather(
        db.users_cache.get_first_name(target_id, str(target_id)),
        db.users_roles.get_effective_role(target_id),
        return_exceptions=True,
    )
    if isinstance(current_role, asyncio.CancelledError):
        raise current_role
    if isinstance(target_fname, BaseException):
        target_fname = str(target_id)
    if isinstance(current_role, BaseException):
        log.error(
            "target role lookup failed during promote callback for target=%d: %s",
            target_id,
            current_role,
        )
        try:
            await q.edit_message_text(_ERR_ROLE_LOOKUP_FAILED, reply_markup=None)
        except Exception as exc:
            log.debug("on_promote_role_btn role-lookup-failed edit failed: %s", exc)
        return

    _, text = await Promote.execute(
        ctx.bot,
        admin.id,
        admin.first_name,
        executor_role,
        target_id,
        target_fname,
        current_role,
        role,
    )
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=None)
    except Exception as exc:
        log.debug("on_promote_role_btn result edit failed: %s", exc)


@decorators.ratelimiter(limit=_RL_CMD_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_promote_role_cancel(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """Acknowledge the cancel button and replace the role-selection prompt with a cancellation notice."""
    q = update.callback_query
    if q is None:
        return
    await asyncio.gather(
        q.answer(),
        q.edit_message_text(_MSG_PROMOTE_CANCELLED, reply_markup=None),
        return_exceptions=True,
    )


# ─────────────────── Command Demote </tcdemote> ─────────────────── #


@decorators.ratelimiter(limit=_RL_QUERY_LIMIT, period=_RL_PERIOD_LONG_S)
@decorators.staff_only
@decorators.log_execution
async def cmd_demote(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a user's federation role.

    Fetches the executor role and resolves the target in parallel. Then fetches
    identity classification and the target's current role in a second parallel
    gather, since both are independent reads. Shows a confirmation keyboard for
    the target's current role. Identity guards prevent demoting the Founder or
    self without the correct flow.
    """
    admin = update.effective_user
    msg = update.effective_message
    if admin is None or msg is None:
        return
    args = parse_cmd_args(msg.text)

    # * Executor role (founder/admin guaranteed by @staff_only) + target run in parallel
    _exec_r, _target_r = await asyncio.gather(
        db.users_roles.get_effective_role(admin.id),
        extraction.extract_target(update, args, ctx.bot),
        return_exceptions=True,
    )
    if isinstance(_exec_r, asyncio.CancelledError):
        raise _exec_r
    if isinstance(_target_r, asyncio.CancelledError):
        raise _target_r
    executor_role = None if isinstance(_exec_r, BaseException) else _exec_r
    if executor_role is None:
        return
    # staff_only guarantees a valid role; assert narrows str | None → str for pyright
    assert executor_role is not None
    if isinstance(_target_r, BaseException):
        log.error("extract_target failed during demote: %s", _target_r)
        try:
            await msg.reply_text(replies.ERR_CANNOT_RESOLVE)
        except Exception as exc:
            log.debug("cmd_demote no-target reply failed: %s", exc)
        return
    target_id, target_fname = _target_r

    if not target_id:
        try:
            await msg.reply_text(replies.ERR_CANNOT_RESOLVE)
        except Exception as exc:
            log.debug("cmd_demote no-target-id reply failed: %s", exc)
        return

    # * identity classify and target-role fetch are independent reads; run in parallel.
    ident, target_role = await asyncio.gather(
        identity.classify(ctx.bot, admin.id, target_id, target_fname),
        db.users_roles.get_effective_role(target_id),
        return_exceptions=True,
    )
    if isinstance(ident, BaseException):
        if isinstance(ident, asyncio.CancelledError):
            raise ident
        log.error(
            "identity.classify failed during demote for target=%d: %s", target_id, ident
        )
        try:
            await msg.reply_text(_ERR_CLASSIFY_FAILED)
        except Exception as exc:
            log.debug("cmd_demote classify-failed reply failed: %s", exc)
        return
    if isinstance(target_role, asyncio.CancelledError):
        raise target_role
    if isinstance(target_role, BaseException):
        log.error(
            "target role lookup failed during demote for target=%d: %s",
            target_id,
            target_role,
        )
        try:
            await msg.reply_text(_ERR_ROLE_LOOKUP_FAILED)
        except Exception as exc:
            log.debug("cmd_demote role-lookup-failed reply failed: %s", exc)
        return
    refusal = identity.refuse_message("demote", ident)
    if refusal is not None:
        try:
            await msg.reply_text(refusal, parse_mode="HTML")
        except Exception as exc:
            log.debug("cmd_demote refusal reply failed: %s", exc)
        return

    if not target_role:
        try:
            await msg.reply_text(_ERR_NO_REMOVABLE_ROLE)
        except Exception as exc:
            log.debug("cmd_demote no-role reply failed: %s", exc)
        return

    if target_role == "admin" and executor_role != "founder":
        try:
            await msg.reply_text(_ERR_FOUNDER_DEMOTE_ONLY)
        except Exception as exc:
            log.debug("cmd_demote founder-only reply failed: %s", exc)
        return

    role_label = db.users_roles.ROLE_LABEL.get(target_role, target_role)
    try:
        await msg.reply_text(
            f"{mention(target_id, target_fname or str(target_id), ident.username)} is currently a "
            f"{bold(role_label)}.\nConfirm to remove their role.",
            parse_mode="HTML",
            reply_markup=keyboards.demote_confirm_kb(target_id),
        )
    except Exception as exc:
        log.debug("cmd_demote confirm-prompt reply failed: %s", exc)


# ──────────────────────── Callback Handlers ─────────────────────── #


@decorators.ratelimiter(limit=_RL_CMD_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_demote_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm the demote action from the inline keyboard.

    Re-validates the executor's rank; rejects with alert if insufficient.
    Answers the query, fetches the target's current role and mention data in
    parallel, then executes ``Demote.execute`` and edits the prompt to the result.
    """
    q = update.callback_query
    if q is None or q.data is None:
        return
    admin = update.effective_user
    if admin is None:
        await q.answer()
        return
    try:
        target_id = int(q.data.split(":", 1)[1])
    except ValueError:
        await q.answer()
        return
    except IndexError:
        await q.answer()
        return
    # * Gather role check + q.answer() in parallel so the spinner disappears
    # * immediately, regardless of DB latency.
    executor_role, _ = await asyncio.gather(
        db.users_roles.get_effective_role(admin.id),
        q.answer(),
        return_exceptions=True,
    )
    if isinstance(executor_role, asyncio.CancelledError):
        raise executor_role
    if isinstance(executor_role, BaseException) or executor_role not in (
        "founder",
        "admin",
    ):
        try:
            await q.edit_message_text(replies.ERR_PERM_EXPIRED, reply_markup=None)
        except Exception as exc:
            log.debug("on_demote_confirm perm-expired edit failed: %s", exc)
        return
    # Narrow executor_role to str after passing the "founder"/"admin" check
    assert executor_role is not None

    target_role, mention_data = await asyncio.gather(
        db.users_roles.get_effective_role(target_id),
        db.users_cache.get_user_mention_data(target_id),
        return_exceptions=True,
    )
    if isinstance(target_role, asyncio.CancelledError):
        raise target_role
    if isinstance(target_role, BaseException):
        log.error(
            "target role lookup failed during demote callback for target=%d: %s",
            target_id,
            target_role,
        )
        try:
            await q.edit_message_text(_ERR_ROLE_LOOKUP_FAILED, reply_markup=None)
        except Exception as exc:
            log.debug("on_demote_confirm role-lookup-failed edit failed: %s", exc)
        return
    if isinstance(mention_data, BaseException):
        target_fname, target_uname = str(target_id), None
    else:
        target_fname, target_uname = mention_data

    if not target_role or target_role == "founder":
        try:
            await q.edit_message_text(_ERR_NO_LONGER_REMOVABLE, reply_markup=None)
        except Exception as exc:
            log.debug("on_demote_confirm no-longer-removable edit failed: %s", exc)
        return

    if target_role == "admin" and executor_role != "founder":
        try:
            await q.edit_message_text(_ERR_FOUNDER_DEMOTE_ONLY, reply_markup=None)
        except Exception as exc:
            log.debug("on_demote_confirm founder-only edit failed: %s", exc)
        return

    try:
        removed = await Demote.execute(
            ctx.bot,
            target_id,
            target_fname,
            target_role,
            admin.id,
            admin.first_name,
            trigger=None,
        )
    except Exception:
        log.exception(
            "Demote.execute raised for target=%d in on_demote_confirm", target_id
        )
        removed = False
    if not removed:
        try:
            await q.edit_message_text(_ERR_ROLE_CLEAR_FAILED, reply_markup=None)
        except Exception as exc:
            log.debug("on_demote_confirm role-clear-failed edit failed: %s", exc)
        return

    role_label = db.users_roles.ROLE_LABEL.get(target_role, target_role)
    try:
        await q.edit_message_text(
            f"Done. {user_ref(target_id, target_fname, target_uname)} "
            f"has been removed from {esc(role_label)}.",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception as exc:
        log.debug("on_demote_confirm success edit failed: %s", exc)


@decorators.ratelimiter(limit=_RL_CMD_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_demote_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Acknowledge the cancel button and collapse the demotion confirmation prompt."""
    q = update.callback_query
    if q is None:
        return
    await asyncio.gather(
        q.answer(),
        q.edit_message_text(_MSG_CANCELLED, reply_markup=None),
        return_exceptions=True,
    )


# ───────────── Command Transfer Owner </transferowner> ──────────── #


@decorators.ratelimiter(limit=_RL_BULK_LIMIT, period=_RL_PERIOD_BULK_S)
@decorators.owner_only
@decorators.log_execution
async def cmd_transfer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Transfer federation ownership to another user.

    Resolves the new owner target, runs identity checks, then sequentially
    demotes the current owner to Admin (``add_admin``) and promotes the target
    to Founder (``set_owner``). The two DB writes are intentionally sequential
    because ``set_owner`` does a ``delete_many`` that must see the owner record
    before it is replaced. Logs and confirmation reply run in parallel afterward.
    """
    current_owner = update.effective_user
    msg = update.effective_message
    if current_owner is None or msg is None:
        return

    args = parse_cmd_args(msg.text)
    try:
        target_id, target_fname = await extraction.extract_target(update, args, ctx.bot)
    except Exception:
        log.exception("extract_target failed during transfer")
        try:
            await msg.reply_text(replies.ERR_ROLE_VERIFY)
        except Exception as reply_exc:
            log.debug("cmd_transfer extract-failed reply failed: %s", reply_exc)
        return
    if not target_id:
        try:
            await msg.reply_text(replies.ERR_CANNOT_RESOLVE)
        except Exception as exc:
            log.debug("cmd_transfer no-target reply failed: %s", exc)
        return

    try:
        ident = await identity.classify(
            ctx.bot, current_owner.id, target_id, target_fname
        )
    except Exception:
        log.exception("identity.classify failed during transfer")
        try:
            await msg.reply_text(replies.ERR_ROLE_VERIFY)
        except Exception as reply_exc:
            log.debug("cmd_transfer classify-failed reply failed: %s", reply_exc)
        return
    refusal = identity.refuse_message("transfer", ident)
    if refusal is not None:
        try:
            await msg.reply_text(refusal, parse_mode="HTML")
        except Exception as exc:
            log.debug("cmd_transfer refusal reply failed: %s", exc)
        return

    target_uname = ident.username

    # * Resolve and rank-check the target. Without this, a Developer could
    # * transfer ownership to a Founder, an Admin could be promoted past the
    # * rank they should be eligible for, or the new owner could keep a
    # * residual Developer/Tester role from before the transfer (which would
    # * leave the federation with a user who is simultaneously Founder and
    # * Developer -- a state the rest of the codebase does not handle).
    role_check = await resolve_and_check(
        msg, current_owner.id, target_id, min_role="developer"
    )
    if role_check == (None, None):
        # * resolve_and_check already replied and rejected.
        return
    _executor_role, target_role = role_check
    if target_role and target_role != "founder":
        # * Clear any non-Founder role the target currently holds so the
        # * post-transfer state has exactly one Founder and zero Developer
        # * /Tester records pointing at the new owner. Use ``Demote.remove_role``
        # * (not ``db.users_roles.remove_role`` directly) so an Admin target
        # * is removed from ``tc_admins`` rather than ``tc_roles``: the bare
        # * ``remove_role`` helper only touches ``tc_roles`` and would leave
        # * a stale Admin record on the new owner.
        try:
            await Demote.remove_role(target_id, target_role)
            log.info(
                "cmd_transfer cleared pre-existing role %s from new owner %d",
                target_role,
                target_id,
            )
        except Exception as exc:
            log.warning(
                "cmd_transfer remove_role failed for new owner %d: %s",
                target_id,
                exc,
            )

    # * set_owner first: it is atomic (upsert + delete_many). If it fails, the
    # * old founder is untouched. add_admin is non-fatal and runs second so a
    # * transient failure there does not leave the federation ownerless.
    try:
        await db.users_roles.set_owner(target_id)
    except Exception:
        log.exception("cmd_transfer set_owner failed for target %d", target_id)
        try:
            await msg.reply_text(
                "Couldn't transfer ownership due to a server error. "
                "No changes were made; please try again."
            )
        except Exception as exc:
            log.debug("cmd_transfer set-owner-failed reply failed: %s", exc)
        return
    # * Refresh the in-process error_reporter owner so subsequent infra
    # * errors go to the new owner via DM instead of the old one.
    error_reporter.set_owner(target_id)
    # * Non-fatal but must not be silent: set_owner already committed, so a
    # * failed add_admin would leave the old Founder role-less behind a
    # * success reply. Flag it and say so in the reply instead.
    prev_owner_admin_ok = True
    try:
        await db.users_roles.add_admin(current_owner.id, current_owner.id)
    except Exception:
        prev_owner_admin_ok = False
        log.exception("cmd_transfer add_admin failed after set_owner")
    lc, lt = cfg.logs
    log_text = parse_logmsg.ownership_transferred(
        target_id,
        target_fname or str(target_id),
        current_owner.id,
        current_owner.first_name or "unknown",
    )
    transfer_note = (
        f"Done. Ownership has been transferred to "
        f"{user_ref(target_id, target_fname or str(target_id), target_uname)}."
    )
    if not prev_owner_admin_ok:
        transfer_note += (
            f" WARNING: {user_ref(current_owner.id, current_owner.first_name or 'unknown')} "
            "could not be kept as Admin due to a server error; grant the role "
            "manually with /tcpromote if needed."
        )
    # * log and reply in parallel
    transfer_log_r, transfer_reply_r = await asyncio.gather(
        ctx.bot.send_message(lc, log_text, parse_mode="HTML", message_thread_id=lt),
        msg.reply_text(
            transfer_note,
            parse_mode="HTML",
        ),
        return_exceptions=True,
    )
    if isinstance(transfer_log_r, BaseException):
        log.error("Ownership transfer log send failed: %s", transfer_log_r)
    if isinstance(transfer_reply_r, BaseException):
        log.debug("Ownership transfer reply failed: %s", transfer_reply_r)


# ───────── Command Promotion Requests </tcpromoterequests> ──────── #


@decorators.ratelimiter(limit=_RL_BULK_LIMIT, period=_RL_PERIOD_BULK_S)
@decorators.log_execution
async def cmd_promote_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Submit a promotion request to the Founder.

    Checks the caller's existing role and any pending request in parallel.
    Rejects if the user already holds a federation role (no request needed) or
    already has an open request, then calls ``Promote.request_admin`` to create
    a new queue entry and notify the Founder.

    Note: identity.classify is intentionally not used here because the caller is
    the subject of the request (executor_id == target_id), which would always
    produce an ``Identity("self")`` refusal - incorrect for a self-submission flow.
    """
    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return

    # * Reject anonymous admins (the GroupAnonymousBot placeholder, id
    # * 1087968824). Without this, a real admin posting "as the group" could
    # * submit a promotion request attributed to the pseudo-user, which the
    # * Founder would then DM-respond to without ever knowing who really
    # * sent it. Real humans send the command from their own account.
    if user.id == ANONYMOUS_BOT_ID:
        try:
            await msg.reply_text(
                "This command must be sent from your personal account, not as "
                "the group. Anonymous-admin commands are not accepted here.",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.debug("cmd_promote_request anon-admin reply failed: %s", exc)
        return

    existing_role, existing = await asyncio.gather(
        db.users_roles.get_effective_role(user.id),
        db.queues_db.get_request(user.id),
        return_exceptions=True,
    )
    # * Fail closed like cmd_promote/cmd_demote: a role-lookup outage must not
    # * green-light a request from an already-staff user or duplicate the queue.
    for _lookup in (existing_role, existing):
        if isinstance(_lookup, asyncio.CancelledError):
            raise _lookup
    if isinstance(existing_role, BaseException) or isinstance(existing, BaseException):
        log.warning(
            "cmd_promote_request lookup failed for user=%d: role=%s request=%s",
            user.id,
            existing_role,
            existing,
        )
        try:
            await msg.reply_text(_ERR_ROLE_LOOKUP_FAILED)
        except Exception as exc:
            log.debug("cmd_promote_request lookup-fail reply failed: %s", exc)
        return
    if existing_role:
        label = db.users_roles.ROLE_LABEL.get(existing_role, existing_role or "unknown")
        label = label.capitalize()
        try:
            await msg.reply_text(f"You're already a {label} - no request needed.")
        except Exception as exc:
            log.debug("cmd_promote_request already-role reply failed: %s", exc)
        return

    if existing:
        try:
            await msg.reply_text(
                f"You already have a pending request (ID: {code(existing.get('request_id', 'unknown'))}).",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.debug("cmd_promote_request existing-request reply failed: %s", exc)
        return
    _, reply = await Promote.request_admin(
        ctx.bot, user.id, user.id, user.first_name or "unknown", user.username or ""
    )
    try:
        await msg.reply_text(reply, parse_mode="HTML")
    except Exception as exc:
        log.debug("cmd_promote_request result reply failed: %s", exc)


# ──────── Command Promotion Requests List </tcpromotelist> ──────── #


@decorators.ratelimiter(limit=_RL_QUERY_LIMIT, period=_RL_PERIOD_LONG_S)
@decorators.staff_only
@decorators.log_execution
async def cmd_promote_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with a formatted list of all pending promotion requests."""
    msg = update.effective_message
    if msg is None:
        return
    try:
        pending = await db.queues_db.all_pending()
    except Exception:
        log.exception("all_pending failed during promote_list")
        try:
            await msg.reply_text(_ERR_ROLE_LOOKUP_FAILED)
        except Exception as exc:
            log.debug("cmd_promote_list db-error reply failed: %s", exc)
        return
    if not pending:
        try:
            await msg.reply_text(_MSG_NO_PENDING)
        except Exception as exc:
            log.debug("cmd_promote_list no-pending reply failed: %s", exc)
        return
    lines = [f"{bold(f'Pending Promotion Requests ({len(pending)})')}\n"]
    for req in pending:
        target_id = req.get("target_id", 0)
        target_fname = req.get("first_name", "unknown")
        uname_val = req.get("username")
        uname = f"@{uname_val}" if uname_val else "no username"
        lines.append(
            f"- {mention(target_id, target_fname, uname_val)} "
            f"{code(str(target_id))} | {esc(uname)} | ID: {code(req.get('request_id', 'unknown'))}"
        )
    try:
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        log.debug("cmd_promote_list result reply failed: %s", exc)


# ──────────────────────── Callback Handlers ─────────────────────── #


@decorators.ratelimiter(limit=_RL_CMD_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_promo_decision(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle approve / reject decisions on promotion request cards.

    Owner-only callback. Answers the query and fetches the request record in
    parallel. On approve, executes ``Promote.execute`` and updates the card. On
    reject, marks the request resolved and edits the card to show the rejection.
    """
    q = update.callback_query
    if q is None or q.data is None:
        return
    admin = update.effective_user
    if admin is None:
        await q.answer()
        return
    try:
        action, request_id = q.data.split(":", 1)
    except ValueError:
        await q.answer()
        return
    # * Pre-fetch request record alongside ownership check and q.answer();
    # * request_id is known from q.data so the DB call can fire speculatively.
    is_owner, req_result, _ = await asyncio.gather(
        db.users_roles.is_owner(admin.id),
        db.queues_db.get_request_by_id(request_id),
        q.answer(),
        return_exceptions=True,
    )
    if isinstance(is_owner, BaseException) or not is_owner:
        try:
            await q.edit_message_text(replies.PERM_FOUNDER_ONLY)
        except Exception as exc:
            log.debug("on_promo_decision perm-denied edit failed: %s", exc)
        return
    if isinstance(req_result, BaseException):
        log.error("get_request_by_id failed for %s: %s", request_id, req_result)
        try:
            await q.edit_message_text(_ERR_REQUEST_NOT_FOUND)
        except Exception as exc:
            log.debug("on_promo_decision db-error edit failed: %s", exc)
        return
    if not req_result:
        try:
            await q.edit_message_text(_ERR_REQUEST_NOT_FOUND)
        except Exception as exc:
            log.debug("on_promo_decision not-found edit failed: %s", exc)
        return
    req = req_result
    # * A concurrent tap may have resolved the request after our fetch.
    # * resolve() below is atomic (pending-only filter), but refuse early
    # * so a stale card never shows a second success message.
    if req.get("status") != "pending":
        try:
            await q.edit_message_text(_ERR_REQUEST_NOT_FOUND, reply_markup=None)
        except Exception as exc:
            log.debug("on_promo_decision resolved edit failed: %s", exc)
        return
    target_id = req.get("target_id", 0)
    target_fname = req.get("first_name", str(target_id))
    lc, lt = cfg.logs

    if action == "promo_approve":
        # * Sequential, not parallel: add_admin first so any failure leaves the
        # * request pending and retryable. The old parallel form could resolve
        # * "approved" while add_admin failed, orphaning the user (role never
        # * granted, retry rejected as already-resolved). add_admin is an
        # * idempotent upsert, so a retry after a resolve failure still
        # * converges; the atomic resolve still decides a concurrent race.
        try:
            await db.users_roles.add_admin(target_id, admin.id)
        except Exception:
            log.exception(
                "add_admin failed for %d (request %s)",
                target_id,
                request_id,
            )
            try:
                await q.edit_message_text(
                    "Couldn't approve the request due to a server error. "
                    "Please try again.",
                    reply_markup=None,
                )
            except Exception as exc:
                log.debug("promo_approve db-fail edit failed: %s", exc)
            return
        try:
            db_resolve_r = await db.queues_db.resolve(request_id, "approved", admin.id)
        except Exception:
            log.exception("resolve(approved) failed for request %s", request_id)
            try:
                await q.edit_message_text(
                    "Couldn't approve the request due to a server error. "
                    "Please try again.",
                    reply_markup=None,
                )
            except Exception as exc:
                log.debug("promo_approve db-fail edit failed: %s", exc)
            return
        if db_resolve_r is False:
            try:
                await q.edit_message_text(
                    "Couldn't approve the request due to a server error. "
                    "Please try again.",
                    reply_markup=None,
                )
            except Exception as exc:
                log.debug("promo_approve db-fail edit failed: %s", exc)
            return
        log_text = parse_logmsg.promote_approved_log(
            target_id,
            target_fname,
            admin.id,
            admin.first_name,
            request_id,
        )
        # * notify target, send log, and edit review message all in parallel
        existing_text = ""
        if q.message is not None:
            # text_html is unavailable on MaybeInaccessibleMessage; use a safe fallback
            existing_text = getattr(q.message, "text_html", "") or ""
        notify_results = await asyncio.gather(
            q.edit_message_text(
                existing_text + f"\n\n- Approved by {esc(admin.first_name)}",
                parse_mode="HTML",
                reply_markup=None,
            ),
            ctx.bot.send_message(
                target_id,
                f"Your promotion request has been approved - welcome to the {esc(cfg.community_name)} staff team, Admin.",
            ),
            ctx.bot.send_message(lc, log_text, parse_mode="HTML", message_thread_id=lt),
            return_exceptions=True,
        )
        for i, r in enumerate(notify_results):
            if isinstance(r, BaseException):
                log.debug("promo_approve notify[%d] failed: %s", i, r)

    elif action == "promo_reject":
        # * Resolve first and atomically: only the winner may mutate the UI.
        # * A lost race (or write failure) shows an error instead of a
        # * "Rejected" card over a still-pending request.
        try:
            resolved = await db.queues_db.resolve(request_id, "rejected", admin.id)
        except Exception:
            log.exception("resolve(rejected) failed for request %s", request_id)
            resolved = False
        if not resolved:
            try:
                await q.edit_message_text(
                    "Couldn't reject the request due to a server error. "
                    "Please try again.",
                    reply_markup=None,
                )
            except Exception as exc:
                log.debug("promo_reject db-fail edit failed: %s", exc)
            return
        log_text = parse_logmsg.promote_rejected_log(
            target_id,
            target_fname,
            admin.id,
            admin.first_name,
            request_id,
        )
        # * Notify + send log + edit review message in parallel; all are
        # * best-effort now that the queue state is final.
        existing_text = ""
        if q.message is not None:
            existing_text = getattr(q.message, "text_html", "") or ""
        reject_results = await asyncio.gather(
            q.edit_message_text(
                existing_text + f"\n\n- Rejected by {esc(admin.first_name)}",
                parse_mode="HTML",
                reply_markup=None,
            ),
            ctx.bot.send_message(
                target_id,
                "Your request was reviewed but wasn't approved this time. You're free to apply again later.",
            ),
            ctx.bot.send_message(lc, log_text, parse_mode="HTML", message_thread_id=lt),
            return_exceptions=True,
        )
        for i, r in enumerate(reject_results):
            if isinstance(r, BaseException):
                log.debug("promo_reject notify[%d] failed: %s", i, r)


# ──────────────────────────── Handlers ──────────────────────────── #

_PMT_CMDS = build_prefixed_filters("tcpromote") | build_prefixed_filters("tcp")
_DMT_CMDS = build_prefixed_filters("tcdemote") | build_prefixed_filters("tcd")
_TF_CMDS = build_prefixed_filters("transferowner") | build_prefixed_filters("tfowner")
_PMTREQ_CMDS = build_prefixed_filters("tcpromoterequests") | build_prefixed_filters(
    "tcreqs"
)
_PMTLIST_CMDS = build_prefixed_filters("tcpromotelist") | build_prefixed_filters(
    "tcplist"
)

__handlers__ = [
    MessageHandler(_PMT_CMDS, cmd_promote),
    MessageHandler(_DMT_CMDS, cmd_demote),
    MessageHandler(_TF_CMDS, cmd_transfer),
    MessageHandler(_PMTREQ_CMDS, cmd_promote_request),
    MessageHandler(_PMTLIST_CMDS, cmd_promote_list),
    CallbackQueryHandler(on_promo_decision, pattern=r"^(promo_approve|promo_reject):"),
    CallbackQueryHandler(on_promote_role_btn, pattern=r"^promo_role:[a-z]+:\d+$"),
    CallbackQueryHandler(on_promote_role_cancel, pattern=r"^promo_role_cancel:\d+$"),
    CallbackQueryHandler(on_demote_confirm, pattern=r"^demote_confirm:\d+$"),
    CallbackQueryHandler(on_demote_cancel, pattern=r"^demote_cancel:\d+$"),
]
