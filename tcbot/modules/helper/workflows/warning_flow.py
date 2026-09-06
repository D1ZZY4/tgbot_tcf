# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Warning executor + conversation factory."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper import keyboards, parse_logmsg
from tcbot.modules.helper.formatter import esc, user_ref
from tcbot.modules.helper.parse_link import message_link
from tcbot.modules.helper.workflows.demote_flow import Demote
from tcbot.modules.helper.workflows.proof_flow import BuildProof, upload_proof
from tcbot.modules.helper.workflows.reason_flow import BuildReason, build_modaction_conv
from tcbot.utils.dispatch import (
    count_transient_errors,
    fan_out,
    is_benign_telegram_error,
)
from tcbot.utils.time_and_date import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable

    from telegram.ext import ContextTypes
    from telegram.ext.filters import BaseFilter

from telegram import Bot, InlineKeyboardMarkup, Message, Update

log = logging.getLogger(__name__)

# * Per-action BuildReason and BuildProof instances; imported by warnings.py
# * skip_allowed=False because warn requires a reason; Skip is not offered
reason = BuildReason("warn", skip_allowed=False)
proof = BuildProof("warn")


# ──────────────────────────── Executors ─────────────────────────── #


async def execute_warn(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    target_id: int,
    target_name: str,
    reason_text: str,
    proof_desc: str | None = None,
    proof_msgs: list | None = None,
) -> None:
    """Issue a warning and auto-ban the target if a warn threshold is reached.

    Two thresholds can trigger an automatic federation ban:

    1. **Per-group threshold** (``cfg.warn_limit``): fires when a user's warn count
       in the *current* group reaches exactly the configured limit.  Uses ``==`` so
       that concurrent warns at limit+1 don't double-fire.

    2. **Federation-wide threshold** (``cfg.fed_warn_limit``, default 0 = off):
       fires when the user's total warns *across all groups* reach or exceed the
       configured value, even if no single group has hit its per-group limit.
       This closes the evasion path of spreading thin warns across many groups.

    In both cases a non-staff user is banned from all active federation
    groups. A target holding a federation role is demoted first and then
    exempted from the auto-ban (staff are never auto-banned via warnings);
    the warn itself is still recorded. When ``cfg.fed_warn_limit`` is 0
    only the per-group threshold applies (backward-compatible default).
    """
    msg = update.effective_message
    if msg is None:
        return
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id
    chat_title = chat.title or str(chat_id)
    admin = update.effective_user
    if admin is None:
        return
    admin_id = admin.id
    admin_fname = admin.first_name
    lc, lt = cfg.logs

    proof_link: str | None = None
    if proof_msgs:
        try:
            pc, pt = cfg.proofs
            caption = parse_logmsg.proof_caption_new(
                target_id, admin_id, admin_fname, utc_now()
            )
            warn_proof_id = await upload_proof(ctx.bot, proof_msgs, caption, pc, pt)
            if warn_proof_id:
                proof_link = message_link(pc, warn_proof_id, pt)
        except Exception:
            log.warning("Warn proof upload skipped for target=%d", target_id)

    proof_kb = keyboards.action_proof_kb(target_id, proof_link)

    warn_limit = cfg.warn_limit
    count = await db.warns_db.add_warn(target_id, reason_text, admin_id, chat_id)
    log_text = parse_logmsg.warn_log(
        target_id,
        target_name,
        admin_id,
        admin_fname,
        reason_text,
        count,
        warn_limit,
        chat_id,
        chat_title,
    )

    # ── Determine whether to auto-ban and record the trigger ────────
    # * "per_group": per-chat warn_limit reached for this group.
    # * "fed_global": federation-wide FED_WARN_LIMIT reached across all groups.
    # * None: below both thresholds; issue a plain warning only.
    #
    # * Per-group uses == (not >=) so that only the exact hit triggers the ban;
    # * a concurrent second warn returns warn_limit+1 and is silently skipped,
    # * preventing a double-ban race condition.
    # * Federation-wide uses >= because the aggregation is a separate DB read
    # * and has no atomicity guarantee across chat boundaries; >= ensures no
    # * trigger is missed, and the already_banned guard below prevents double bans.
    auto_ban_trigger: str | None = None
    fed_count: int = 0

    if count == warn_limit:
        auto_ban_trigger = "per_group"
    else:
        fed_limit = cfg.fed_warn_limit
        if fed_limit > 0:
            fed_count = await db.warns_db.federation_warn_count(target_id)
            if fed_count >= fed_limit:
                auto_ban_trigger = "fed_global"

    if auto_ban_trigger is not None:
        await _execute_warn_auto_ban(
            ctx.bot,
            msg,
            target_id,
            target_name,
            admin_id,
            admin_fname,
            reason_text,
            count,
            warn_limit,
            fed_count,
            auto_ban_trigger,
            proof_kb,
            chat_id,
            lc,
            lt,
            log_text,
        )
    else:
        # * federation log + reply in parallel
        results2 = await asyncio.gather(
            ctx.bot.send_message(
                lc,
                log_text,
                parse_mode="HTML",
                message_thread_id=lt,
                reply_markup=proof_kb,
            ),
            msg.reply_text(
                f"{user_ref(target_id, target_name)} has been warned "
                f"({count}/{warn_limit}) - {esc(reason_text)}",
                parse_mode="HTML",
                reply_markup=proof_kb,
            ),
            return_exceptions=True,
        )
        if isinstance(results2[0], BaseException):
            log.error("Warn log send failed: %s", results2[0])


async def execute_unwarn(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    target_id: int,
    target_name: str,
) -> None:
    """Remove one warning from the target in the current group.

    Checks the current warn count; replies and returns early if the target has
    none. Otherwise decrements by one, logs the action, and sends the log and
    reply concurrently.
    """
    msg = update.effective_message
    if msg is None:
        return
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id

    count = await db.warns_db.warn_count(target_id, chat_id)
    if count == 0:
        try:
            await msg.reply_text(
                f"{user_ref(target_id, target_name)} has no warnings in this group.",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.debug("execute_unwarn no-warns reply failed: %s", exc)
        return

    new_count = max(count - 1, 0)
    chat_title = chat.title or str(chat_id)
    admin = update.effective_user
    if admin is None:
        return
    lc, lt = cfg.logs
    log_text = parse_logmsg.unwarn_log(
        target_id,
        target_name,
        admin.id,
        admin.first_name,
        new_count,
        cfg.warn_limit,
        chat_id,
        chat_title,
    )
    # * Remove first, then report what actually happened. Logging the
    # * computed new_count before the delete lands lies on concurrent
    # * unwarns: a second racing unwarn finds nothing to delete but the
    # * reply was already sent claiming success.
    try:
        removed = await db.warns_db.remove_last_warn(target_id, chat_id)
    except Exception:
        log.exception(
            "remove_last_warn DB write failed for target=%d chat=%d",
            target_id,
            chat_id,
        )
        removed = False
    if not removed:
        try:
            await msg.reply_text(
                f"{user_ref(target_id, target_name)} has no warnings in this group.",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.debug("execute_unwarn empty reply failed: %s", exc)
        return
    # * Re-read the count after the delete so concurrent unwarns report
    # * the true remaining total instead of a stale computed value.
    try:
        new_count = await db.warns_db.warn_count(target_id, chat_id)
    except Exception:
        log.exception(
            "warn_count re-read failed for target=%d chat=%d",
            target_id,
            chat_id,
        )
        new_count = max(count - 1, 0)
    log_text = parse_logmsg.unwarn_log(
        target_id,
        target_name,
        admin.id,
        admin.first_name,
        new_count,
        cfg.warn_limit,
        chat_id,
        chat_title,
    )
    results = await asyncio.gather(
        ctx.bot.send_message(lc, log_text, parse_mode="HTML", message_thread_id=lt),
        msg.reply_text(
            f"One warning removed from {user_ref(target_id, target_name)}. "
            f"They're now at {new_count}/{cfg.warn_limit}.",
            parse_mode="HTML",
        ),
        return_exceptions=True,
    )
    if isinstance(results[0], BaseException):
        log.error("Unwarn log send failed: %s", results[0])


async def execute_warnlist(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    target_id: int,
    target_name: str,
) -> None:
    """Reply with the numbered list of active warnings for the target in this group.

    Fetches all warnings from ``db.warns_db.get_warns`` and replies with a
    formatted list. Replies early if no warnings exist.
    """
    msg = update.effective_message
    if msg is None:
        return
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id

    warns = await db.warns_db.get_warns(target_id, chat_id)
    count = len(warns)

    if count == 0:
        try:
            await msg.reply_text(
                f"{user_ref(target_id, target_name)} has no warnings in this group.",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.debug("execute_warnlist no-warns reply failed: %s", exc)
        return

    lines = [
        f"{user_ref(target_id, target_name)} has {count}/{cfg.warn_limit} warnings:\n"
    ]
    for i, w in enumerate(warns, 1):
        lines.append(f"  {i}. {esc(w.get('reason', 'No reason'))}")

    try:
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        log.debug("execute_warnlist reply failed: %s", exc)


async def execute_resetwarns(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    target_id: int,
    target_name: str,
) -> None:
    """Clear all active warnings for the target in the current group.

    Calls ``db.warns_db.clear_warns`` and replies with the number of removed
    warnings. Replies early if the target has no warnings to clear.
    Logs the action to the mod log channel on success.
    """
    msg = update.effective_message
    if msg is None:
        return
    admin = update.effective_user
    if admin is None:
        return
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id
    chat_title = chat.title or str(chat_id)
    lc, lt = cfg.logs

    removed = await db.warns_db.clear_warns(target_id, chat_id)
    if removed == 0:
        try:
            await msg.reply_text(
                f"{user_ref(target_id, target_name)} has no warnings to clear.",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.debug("execute_resetwarns no-warns reply failed: %s", exc)
        return

    log_text = parse_logmsg.resetwarns_log(
        target_id,
        target_name,
        admin.id,
        admin.first_name,
        removed,
        chat_id,
        chat_title,
    )
    results = await asyncio.gather(
        ctx.bot.send_message(lc, log_text, parse_mode="HTML", message_thread_id=lt),
        msg.reply_text(
            f"All {removed} warning(s) cleared for {user_ref(target_id, target_name)}. Clean slate.",
            parse_mode="HTML",
        ),
        return_exceptions=True,
    )
    if isinstance(results[0], BaseException):
        log.error(
            "Reset-warns log send failed for target=%d: %s", target_id, results[0]
        )


# ──────────────────────── Warn auto-ban helper ───────────────────── #
# * Extracted from execute_warn to keep the main flow under 200 lines.


async def _execute_warn_auto_ban(
    bot: Bot,
    msg: Message,
    target_id: int,
    target_name: str,
    admin_id: int,
    admin_fname: str,
    reason_text: str,
    count: int,
    warn_limit: int,
    fed_count: int,
    auto_ban_trigger: str,
    proof_kb: InlineKeyboardMarkup | None,
    chat_id: int,
    lc: int,
    lt: int | None,
    log_text: str,
) -> None:
    """Handle warn-threshold auto-ban: staff demotion, DB record, fan-out, reply."""
    # * The role lookup is guarded: the warn above is already recorded and the
    # * per-group trigger fires on exact equality, so letting a transient
    # * lookup failure propagate would both skip this threshold's auto-ban and
    # * wedge the retry (count is now limit+1, which never re-fires). Entry
    # * authorization already fail-closed; proceed as non-staff with a loud log.
    try:
        target_role = await db.users_roles.get_effective_role(target_id)
    except Exception:
        log.exception(
            "Warn auto-ban role lookup failed for target=%d; proceeding as non-staff",
            target_id,
        )
        target_role = None
    if target_role:
        demoted = True
        try:
            await Demote.execute(
                bot,
                target_id,
                target_name,
                target_role,
                admin_id,
                admin_fname,
                trigger="ban",
            )
        except Exception:
            demoted = False
            log.exception("Auto-demote on warn limit failed for role=%s", target_role)
        # * Only tell the admin the user was demoted when the demotion actually
        # * succeeded. If it failed (DB outage, race, etc.) the user still
        # * holds the role and the message must reflect that, otherwise the
        # * admin believes the role is gone and may forget to retry.
        exemption_text = (
            f"{user_ref(target_id, target_name)} is a {target_role} and was "
            "demoted, but staff are not auto-banned via warnings."
            if demoted
            else (
                f"{user_ref(target_id, target_name)} is a {target_role}, but "
                "the auto-demote step failed; staff are not auto-banned via "
                "warnings. See logs and retry manually if needed."
            )
        )
        try:
            await msg.reply_text(
                exemption_text,
                parse_mode="HTML",
                reply_markup=proof_kb,
            )
        except Exception as exc:
            log.debug("Warn auto-ban staff exemption reply failed: %s", exc)
        return

    groups_result, existing_ban, log_result = await asyncio.gather(
        db.groups_db.active_groups(),
        db.bans_db.get_active_ban(target_id),
        bot.send_message(
            lc,
            log_text,
            parse_mode="HTML",
            message_thread_id=lt,
            reply_markup=proof_kb,
        ),
        return_exceptions=True,
    )
    if isinstance(log_result, BaseException):
        log.error("Warn-auto-ban log send failed: %s", log_result)
    log_msg_id: int = (
        log_result.message_id if not isinstance(log_result, BaseException) else 0
    )
    groups: list = groups_result if not isinstance(groups_result, BaseException) else []
    already_banned = (
        not isinstance(existing_ban, BaseException) and existing_ban is not None
    )

    _all_group_ids: set[int] = {grp["chat_id"] for grp in groups}
    for _extra in [chat_id] + [cid for cid in (cfg.main_group, cfg.exec_group) if cid]:
        if _extra not in _all_group_ids:
            groups = [*groups, {"chat_id": _extra}]
            _all_group_ids.add(_extra)

    if not already_banned:
        try:
            await db.bans_db.create_ban(target_id, reason_text, admin_id, 0, log_msg_id)
        except Exception:
            # * Fail closed like execute_unban: enforcing chats without a DB
            # * record would leave an un-appealable, un-unbannable split
            # * brain (get_active_ban returns None and appeal submit
            # * revalidates the DB). No group is touched below, the
            # * threshold warn above stays recorded, and the admin retries
            # * via /tcban once the database recovers.
            log.exception(
                "Failed to create federation ban record on warn limit for user %d",
                target_id,
            )
            try:
                await msg.reply_text(
                    f"{user_ref(target_id, target_name)} reached the warning "
                    "threshold but the federation ban record could not be "
                    "written to the database, so no groups were touched. "
                    "Check the logs and ban them manually with /tcban once "
                    "the database recovers.",
                    parse_mode="HTML",
                    reply_markup=proof_kb,
                )
            except Exception as exc:
                log.debug("Warn auto-ban DB-fail reply failed: %s", exc)
            return

    ban_results = await fan_out(
        [bot.ban_chat_member(grp["chat_id"], target_id) for grp in groups]
    )

    total_groups = len(groups)
    failed_groups = [
        (grp, r)
        for grp, r in zip(groups, ban_results, strict=False)
        if isinstance(r, BaseException)
    ]
    transient_groups = [
        (grp, r) for grp, r in failed_groups if not is_benign_telegram_error(r)
    ]
    failed = count_transient_errors(ban_results)
    applied = total_groups - failed
    any_ban_ok = applied > 0

    for grp, exc in failed_groups:
        log.warning(
            "Warn auto-ban enforcement failed for user=%d in group=%s (%d): %s",
            target_id,
            grp.get("title", ""),
            grp["chat_id"],
            exc,
        )
    log.info(
        "Warn auto-ban enforced (%s): target=%d applied=%d/%d",
        auto_ban_trigger,
        target_id,
        applied,
        total_groups,
    )

    if total_groups == 0:
        applied_line = " No connected groups configured."
    elif failed == total_groups:
        sample = ", ".join(
            grp.get("title") or str(grp["chat_id"]) for grp, _ in transient_groups[:5]
        )
        applied_line = (
            f" WARNING: ban not enforced in any group ({total_groups}/{total_groups} failed)."
            f" Check bot admin rights in: {esc(sample)}"
            + (" ..." if len(transient_groups) > 5 else "")
        )
    elif failed > 0:
        sample = ", ".join(
            grp.get("title") or str(grp["chat_id"]) for grp, _ in transient_groups[:3]
        )
        applied_line = (
            f" Applied to {applied}/{total_groups} groups"
            f" ({failed} failed: {esc(sample)}"
            + (" ..." if len(transient_groups) > 3 else ")")
        )
    else:
        applied_line = f" Applied to {total_groups}/{total_groups} groups."

    if auto_ban_trigger == "per_group":
        ban_notice = (
            f"{user_ref(target_id, target_name)} "
            f"hit {warn_limit} warnings "
            f"and has been federation-banned."
        )
        ban_fail_notice = (
            f"{user_ref(target_id, target_name)} "
            f"hit {warn_limit} warnings "
            f"but federation-ban failed - please ban them manually."
        )
    else:
        ban_notice = (
            f"{user_ref(target_id, target_name)} "
            f"hit {fed_count}/{cfg.fed_warn_limit} warnings across the federation "
            f"and has been federation-banned."
        )
        ban_fail_notice = (
            f"{user_ref(target_id, target_name)} "
            f"hit {fed_count}/{cfg.fed_warn_limit} federation-wide warnings "
            f"but federation-ban failed - please ban them manually."
        )

    if any_ban_ok:
        clear_result, reply_result = await asyncio.gather(
            db.warns_db.clear_all_warns(target_id),
            msg.reply_text(
                f"{ban_notice}{applied_line}",
                parse_mode="HTML",
                reply_markup=proof_kb,
            ),
            return_exceptions=True,
        )
        if isinstance(clear_result, BaseException):
            log.error(
                "Warn clear after auto-ban failed for target=%d chat=%d: %s",
                target_id,
                chat_id,
                clear_result,
            )
        if isinstance(reply_result, BaseException):
            log.debug("Auto-ban notification reply failed: %s", reply_result)
    else:
        try:
            await msg.reply_text(
                f"{ban_fail_notice}{applied_line}",
                parse_mode="HTML",
                reply_markup=proof_kb,
            )
        except Exception as exc:
            log.debug("Auto-ban failure notice reply failed: %s", exc)


# ──────────────────────── Executor adapter ──────────────────────── #


async def _exec_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Pop warn data from user_data and call execute_warn."""
    assert ctx.user_data is not None
    target_id = ctx.user_data.pop("warn_target_id", 0)
    target_name = ctx.user_data.pop("warn_target_name", "")
    reason_text = ctx.user_data.pop("warn_reason", "")
    proof_desc = ctx.user_data.pop("warn_proof_desc", None)
    proof_msgs = ctx.user_data.pop("warn_proof_msgs", None)
    ctx.user_data.pop("warn_extra_info", None)
    await execute_warn(
        update,
        ctx,
        target_id,
        target_name,
        reason_text,
        proof_desc=proof_desc,
        proof_msgs=proof_msgs,
    )


# ─────────────────── ConversationHandler factory ────────────────── #


def warn_conversation(
    entry_fn: Callable[..., Any],
    entry_filter: BaseFilter,
    *,
    escape_filter: BaseFilter | None = None,
) -> object:
    """Return the warn ConversationHandler via the central reason_flow factory."""
    return build_modaction_conv(
        reason,
        proof,
        entry_fn,
        _exec_warn,
        entry_filter,
        escape_filter=escape_filter,
    )
