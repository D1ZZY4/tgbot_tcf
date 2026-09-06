# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Ban executor + proof collection conversation."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper import keyboards, parse_logmsg, replies
from tcbot.modules.helper.formatter import esc, user_ref
from tcbot.modules.helper.parse_link import appeal_deep_link, message_link
from tcbot.modules.helper.workflows.demote_flow import Demote
from tcbot.modules.helper.workflows.proof_flow import BuildProof, upload_proof
from tcbot.utils.dispatch import (
    count_transient_errors,
    fan_out,
    is_benign_telegram_error,
)
from tcbot.utils.prefixes import ALL_PREFIXES_CMD_FILTER
from tcbot.utils.time_and_date import to_utc, utc_now

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from telegram import Bot, Message
    from telegram.ext.filters import BaseFilter

from tcbot.database.documents import BanDoc

log = logging.getLogger(__name__)

# ──────────────── User-facing reply constants ──────────────────── #

_MSG_CANCELLED = "Cancelled. No ban was issued."
_MSG_TIMEOUT = "Timed out waiting for proof. No ban was issued."
_MSG_PROOF_EXPECTED = "Please send a photo or video as proof, or press Cancel."

_BAN_USER_DATA_KEYS = (
    "ban_target_id",
    "ban_target_fname",
    "ban_reason",
    "ban_admin_id",
    "ban_admin_fname",
    "ban_prompt_msg_id",
    "ban_prompt_chat_id",
    "ban_duration",
    "ban_executing",
)

WAITING_PROOF = 0

# * Per-action BuildProof instance; imported by banning.py
# * skip_allowed=False: ban proof is required; there is no Skip option
proof = BuildProof("ban", skip_allowed=False)

# * Module-level album accumulators (keyed by media_group_id)
_albums: dict[str, list[Message]] = {}
_album_meta: dict[str, dict[str, Any]] = {}

# * Weak references to user_data dicts for post-flush cleanup (keyed by media_group_id).
# * Stored as a reference (not a copy) so we can clear ban keys after execution.
_album_userdata: dict[str, dict[str, Any]] = {}

# * Strong references to in-flight album flush tasks (prevents GC)
_album_tasks: dict[str, asyncio.Task[None]] = {}


# ─────────────────── Album state helpers ────────────────────────── #


def _clear_ban_state(user_data: dict[str, Any] | None) -> None:
    """Remove all ban-related keys from ``user_data``.

    Safe to call when ``user_data`` is ``None`` (no-op).
    """
    if user_data is None:
        return
    for key in _BAN_USER_DATA_KEYS:
        user_data.pop(key, None)


def _cancel_proof_session(user_data: dict[str, Any] | None) -> None:
    """Cancel any in-flight album flush tasks and clear all ban state.

    Clears ban keys from ``user_data`` and cancels every album flush task
    whose ``user_data`` reference matches the given dict.  Called from
    both ``on_cancel_proof`` and ``on_proof_timeout`` so the cleanup path
    is defined exactly once.
    """
    _clear_ban_state(user_data)
    if user_data is None:
        return
    for mgid in [k for k, ud in _album_userdata.items() if ud is user_data]:
        if mgid in _album_meta:
            _album_meta[mgid]["_cancelled"] = True
        task = _album_tasks.pop(mgid, None)
        if task is not None:
            task.cancel()
        _albums.pop(mgid, None)
        _album_meta.pop(mgid, None)
        _album_userdata.pop(mgid, None)


# ────────────────────────── Ban executor ────────────────────────── #


async def _execute_ban(bot: Bot, msgs: list[Message], meta: dict[str, Any]) -> None:
    target_id: int = meta.get("ban_target_id") or 0
    target_fname: str = meta.get("ban_target_fname", str(target_id))
    reason: str = meta.get("ban_reason", replies.NO_REASON)
    admin_id: int = meta.get("ban_admin_id") or 0
    admin_fname: str = meta.get("ban_admin_fname", "Admin")
    prompt_msg_id: int = meta.get("ban_prompt_msg_id", 0)
    prompt_chat_id: int = meta.get("ban_prompt_chat_id", 0)
    ban_duration = meta.get("ban_duration")

    now = utc_now()
    # * ban_duration is reserved for future timed-ban support; Telegram enforcement
    # * via until_date is not yet wired up, so we do not compute until/dur_str here.
    _ = ban_duration
    proof_chat, proof_thread = cfg.proofs

    # * Pre-fetch active groups immediately so DB round-trip overlaps with the
    # * get_active_ban call and the proof-upload I/O that follows.
    _groups_task: asyncio.Task[list] = asyncio.create_task(db.groups_db.active_groups())

    existing = await db.bans_db.get_active_ban(target_id)
    is_update = existing is not None
    bot_username = bot.username or ""
    ban_id = str(existing.get("ban_id", "")) if is_update else db.bans_db.make_ban_id()

    if is_update:
        # * Suppress any stale duplicate active bans for this user, keeping only the
        # * canonical record (existing) that will be updated. This is a no-op when
        # * there are no duplicates. Duplicates can arise from race conditions or from
        # * a re-ban that failed to find the prior active record before creating a new
        # * one. Cleaning them here ensures a single active ban at all times.
        extras = await db.bans_db.deactivate_extra_active_bans(
            target_id, existing.get("ban_id", "")
        )
        if extras > 0:
            log.warning(
                "Suppressed %d duplicate active ban(s) for user %d before update",
                extras,
                target_id,
            )

    # * Start old-admin name fetch immediately - runs during proof upload I/O below
    _old_admin_fname_task = (
        asyncio.create_task(
            db.users_cache.get_first_name(
                existing.get("admin_user_id", admin_id), "Admin"
            )
        )
        if is_update
        else None
    )

    # * Build proof caption
    if is_update:
        prev_proof_msg_id = existing.get("proof_message_id")
        prev_proof_link = (
            message_link(proof_chat, prev_proof_msg_id, proof_thread)
            if prev_proof_msg_id
            else None
        )
        caption = parse_logmsg.proof_caption_update(
            target_id,
            admin_id,
            admin_fname,
            existing.get("timestamp", now),
            prev_proof_link,
        )
    else:
        prev_proof_link = None
        caption = parse_logmsg.proof_caption_new(target_id, admin_id, admin_fname, now)

    # * Upload proof to PROOF channel
    proof_msg_id = await upload_proof(bot, msgs, caption, proof_chat, proof_thread)
    proof_link = (
        message_link(proof_chat, proof_msg_id, proof_thread) if proof_msg_id else None
    )

    logs_chat, logs_thread = cfg.logs

    if is_update:
        log_msg_id = await _execute_ban_update(
            bot,
            existing,
            meta,
            proof_msg_id,
            proof_link,
            prev_proof_link,
            logs_chat,
            logs_thread,
        )
    else:
        # * Single canonical ban_id: generated once above and reused for the DB
        # * record, the PM appeal link, and set_log_message_id below.
        log_msg_id = await _execute_new_ban(
            bot, meta, proof_msg_id, proof_link, now, logs_chat, logs_thread, ban_id
        )

    # * log_msg_id returned from _execute_ban_update / _execute_new_ban

    # * set_log_message_id and pre-fetched active_groups in parallel.
    # * _groups_task was started at the top of this function and has been
    # * running concurrently through get_active_ban, upload_proof, and log send.
    if log_msg_id:
        set_log_result, groups = await asyncio.gather(
            db.bans_db.set_log_message_id(ban_id, log_msg_id),
            _groups_task,
            return_exceptions=True,
        )
        if isinstance(set_log_result, BaseException):
            log.error(
                "set_log_message_id failed for ban_id=%s: %s", ban_id, set_log_result
            )
        if isinstance(groups, BaseException):
            log.error("active_groups failed during ban of %d: %s", target_id, groups)
            groups = []
    else:
        try:
            groups = await _groups_task
        except Exception:
            log.exception("active_groups failed during ban of %d", target_id)
            groups = []

    # * Enforce across all connected groups + primary groups - semaphore-bounded
    # * Re-check the target's effective role immediately before the fan-out.
    # * The auto-demote in ``cmd_ban_start`` ran before the proof
    # * collection window; if the target was re-promoted by a concurrent
    # * command during that window, the role cache and the live DB would
    # * both report them as staff. Demote again here to preserve the
    # * role-vs-state invariant right up to the ban fan-out. Best-effort
    # * like the entry-point demote: a failure logs and the ban still
    # * proceeds because the user IS already banned in the DB; the chat
    # * enforcement is the side-effect.
    pre_fanout_role = await db.users_roles.get_effective_role(target_id)
    if pre_fanout_role:
        try:
            await Demote.execute(
                bot,
                target_id,
                target_fname,
                pre_fanout_role,
                admin_id,
                admin_fname,
                trigger="ban",
            )
            log.info(
                "_execute_ban: re-demoted target %d (role=%s) before fan-out to "
                "close the proof-collection TOCTOU window",
                target_id,
                pre_fanout_role,
            )
        except Exception:
            log.exception(
                "_execute_ban: re-demote before fan-out failed for target %d "
                "(role=%s); proceeding with ban anyway",
                target_id,
                pre_fanout_role,
            )
    _primary_ids = [cid for cid in (cfg.main_group, cfg.exec_group) if cid]
    _existing_ids = {grp["chat_id"] for grp in groups}
    for _pid in _primary_ids:
        if _pid not in _existing_ids:
            groups = [*groups, {"chat_id": _pid, "title": ""}]
    results = await fan_out(
        [bot.ban_chat_member(grp["chat_id"], target_id) for grp in groups]
    )
    # * Collect per-group failures for transparent reporting to the admin.
    # * Benign Telegram refusals (user not in chat, chat gone, bot demoted)
    # * are logged but excluded from the operator-facing failed count so a
    # * ban does not look partially failed when there was nothing to enforce.
    failed_groups = [
        (grp, r)
        for grp, r in zip(groups, results, strict=False)
        if isinstance(r, BaseException)
    ]
    transient_groups = [
        (grp, r) for grp, r in failed_groups if not is_benign_telegram_error(r)
    ]
    failed = count_transient_errors(results)
    for grp, exc in failed_groups:
        log.warning(
            "Ban enforcement failed for user=%d in group=%s (%d): %s",
            target_id,
            grp.get("title", ""),
            grp["chat_id"],
            exc,
        )
    log.info(
        "Ban enforced: target=%d applied=%d/%d",
        target_id,
        len(groups) - failed,
        len(groups),
    )
    if failed:
        # * Error-level so the failure ships to LOG_ERRORS automatically;
        # * per-group warnings above stay console-only.
        log.error(
            "Ban fan-out had %d/%d transient failures for target=%d; "
            "see warnings above and retry manually where needed",
            failed,
            len(groups),
            target_id,
        )

    # * Build the applied-to line, surfacing a clear warning when no group was updated
    total_groups = len(groups)
    if total_groups == 0:
        applied_line = "No connected groups configured."
    elif failed == total_groups:
        sample = ", ".join(
            grp.get("title") or str(grp["chat_id"]) for grp, _ in transient_groups[:5]
        )
        applied_line = (
            f"WARNING: ban not enforced in any group ({total_groups}/{total_groups} failed)."
            f" Check bot admin rights in: {esc(sample)}"
            + (" ..." if len(transient_groups) > 5 else "")
        )
    elif failed > 0:
        sample = ", ".join(
            grp.get("title") or str(grp["chat_id"]) for grp, _ in transient_groups[:3]
        )
        applied_line = (
            f"Applied to {total_groups - failed}/{total_groups} groups"
            f" ({failed} failed: {esc(sample)}"
            + (" ..." if len(transient_groups) > 3 else ")")
        )
    else:
        applied_line = f"Applied to {total_groups}/{total_groups} groups."

    # * Build PM content before the conditional so it can fire in parallel with
    # * both upsert_user and (optionally) edit_message_text.  All three operations
    # * are independent: no output of one is an input to another.
    _pm_text = (
        f"You have been federation-banned from {esc(cfg.community_name)}.\n"
        f"Reason: {esc(reason)}\n\n"
        "You may submit an appeal using the button below."
    )
    _pm_kb = (
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Submit Appeal",
                        url=appeal_deep_link(bot_username, ban_id),
                    )
                ]
            ]
        )
        if bot_username
        else None
    )

    # * Edit prompt summary + cache user + notify banned user in one round-trip.
    summary = (
        f"{user_ref(target_id, target_fname)} has been banned.\n"
        f"Reason: {esc(reason)}\n"
        f"{applied_line}"
    )
    if prompt_msg_id and prompt_chat_id:
        _, upsert_result, pm_result = await asyncio.gather(
            bot.edit_message_text(
                summary,
                chat_id=prompt_chat_id,
                message_id=prompt_msg_id,
                parse_mode="HTML",
                reply_markup=None,
            ),
            db.users_cache.upsert_user(target_id, None, target_fname),
            bot.send_message(
                target_id, _pm_text, parse_mode="HTML", reply_markup=_pm_kb
            ),
            return_exceptions=True,
        )
        if isinstance(upsert_result, BaseException):
            log.error("upsert_user failed for target=%d: %s", target_id, upsert_result)
    else:
        upsert_result, pm_result = await asyncio.gather(
            db.users_cache.upsert_user(target_id, None, target_fname),
            bot.send_message(
                target_id, _pm_text, parse_mode="HTML", reply_markup=_pm_kb
            ),
            return_exceptions=True,
        )
        if isinstance(upsert_result, BaseException):
            log.error(
                "upsert_user (no-prompt path) failed for target=%d: %s",
                target_id,
                upsert_result,
            )
    if isinstance(pm_result, telegram.error.Forbidden):
        log.info(
            "Cannot DM banned user %d: user has not started the bot or has blocked it",
            target_id,
        )
    elif isinstance(pm_result, BaseException):
        log.warning("Failed to send ban PM to user %d: %s", target_id, pm_result)


# ──────────────────────── Ban update / create helpers ───────────────── #
# * Extracted from _execute_ban to keep the main flow under 200 lines.


async def _execute_ban_update(
    bot: Bot,
    existing: BanDoc,
    meta: dict[str, Any],
    proof_msg_id: int | None,
    proof_link: str | None,
    prev_proof_link: str | None,
    logs_chat: int,
    logs_thread: int | None,
) -> int:
    """Build log text, keyboard, and DB update for an existing ban re-enforcement."""
    target_id: int = meta.get("ban_target_id") or 0
    target_fname: str = meta.get("ban_target_fname", str(target_id))
    admin_id: int = meta.get("ban_admin_id") or 0
    admin_fname: str = meta.get("ban_admin_fname", "Admin")
    reason: str = meta.get("ban_reason", replies.NO_REASON)
    ban_id = str(existing.get("ban_id", ""))
    old_admin_id = int(existing.get("admin_user_id", admin_id))
    bot_username = bot.username or ""
    old_proof_msg_id = int(existing.get("proof_message_id", 0))
    old_log_msg_id = int(existing.get("log_message_id", 0))
    new_proof_msg_id = proof_msg_id if proof_msg_id else old_proof_msg_id

    _old_admin_fname_task = asyncio.create_task(
        db.users_cache.get_first_name(old_admin_id, "Admin")
    )
    try:
        old_admin_fname = await _old_admin_fname_task
    except Exception:
        old_admin_fname = "Admin"

    log_text = parse_logmsg.ban_update_log(
        target_id,
        target_fname,
        admin_id,
        admin_fname,
        old_admin_id,
        old_admin_fname,
        reason,
        ban_id,
        to_utc(existing.get("timestamp", utc_now())),
        proof_link,
        prev_proof_link,
    )
    _appeal_url = appeal_deep_link(bot_username, ban_id)
    kb = (
        keyboards.ban_log_update(target_id, proof_link, prev_proof_link, _appeal_url)
        if proof_link and prev_proof_link
        else (
            keyboards.ban_log_new(target_id, proof_link, _appeal_url)
            if proof_link
            else None
        )
    )

    send_kwargs: dict = {"parse_mode": "HTML", "message_thread_id": logs_thread}
    if kb:
        send_kwargs["reply_markup"] = kb
    db_result, log_result = await asyncio.gather(
        db.bans_db.update_ban(
            ban_id,
            reason,
            admin_id,
            new_proof_msg_id,
            0,
            old_proof_msg_id,
            old_log_msg_id,
        ),
        bot.send_message(logs_chat, log_text, **send_kwargs),
        return_exceptions=True,
    )
    if isinstance(db_result, BaseException):
        log.error("update_ban failed for ban_id=%s: %s", ban_id, db_result)

    log_msg_id: int = 0
    if not isinstance(log_result, BaseException):
        log_msg_id = log_result.message_id
        log.info("Ban log posted: ban_id=%s msg_id=%s", ban_id, log_msg_id)
    else:
        log.error("Ban log send failed: %s", log_result)
    return log_msg_id


async def _execute_new_ban(
    bot: Bot,
    meta: dict[str, Any],
    proof_msg_id: int | None,
    proof_link: str | None,
    now: datetime,
    logs_chat: int,
    logs_thread: int | None,
    ban_id: str,
) -> int:
    """Build log text, keyboard, and DB insert for a fresh ban."""
    target_id: int = meta.get("ban_target_id") or 0
    target_fname: str = meta.get("ban_target_fname", str(target_id))
    admin_id: int = meta.get("ban_admin_id") or 0
    admin_fname: str = meta.get("ban_admin_fname", "Admin")
    reason: str = meta.get("ban_reason", replies.NO_REASON)
    bot_username = bot.username or ""

    log_text = parse_logmsg.ban_log(
        target_id,
        target_fname,
        admin_id,
        admin_fname,
        reason,
        ban_id,
        proof_link,
        now,
    )
    kb = (
        keyboards.ban_log_new(
            target_id, proof_link, appeal_deep_link(bot_username, ban_id)
        )
        if proof_link
        else None
    )

    send_kwargs = {"parse_mode": "HTML", "message_thread_id": logs_thread}
    if kb:
        send_kwargs["reply_markup"] = kb
    db_result, log_result = await asyncio.gather(
        db.bans_db.create_ban(
            target_id, reason, admin_id, proof_msg_id or 0, 0, ban_id
        ),
        bot.send_message(logs_chat, log_text, **send_kwargs),
        return_exceptions=True,
    )
    if isinstance(db_result, BaseException):
        log.error("create_ban failed for ban_id=%s: %s", ban_id, db_result)

    log_msg_id: int = 0
    if not isinstance(log_result, BaseException):
        log_msg_id = log_result.message_id
        log.info("Ban log posted: ban_id=%s msg_id=%s", ban_id, log_msg_id)
    else:
        log.error("Ban log send failed: %s", log_result)
    return log_msg_id


# ───────────────── Proof collection state handlers ──────────────── #


async def on_proof_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle incoming proof media: buffer albums or execute the ban immediately."""
    msg = update.effective_message
    if msg is None:
        return WAITING_PROOF

    if msg.media_group_id:
        mgid = msg.media_group_id
        if mgid not in _albums and ctx.user_data is not None:
            meta_snapshot = dict(ctx.user_data)
            _albums[mgid] = []
            _album_meta[mgid] = meta_snapshot
            _album_userdata[mgid] = ctx.user_data
            task = asyncio.create_task(
                _flush_album(mgid, ctx.bot, meta_snapshot, ctx.user_data)
            )
            _album_tasks[mgid] = task
            task.add_done_callback(lambda t, mgid=mgid: _album_tasks.pop(mgid, None))
        _albums[mgid].append(msg)
        return WAITING_PROOF

    # * Single media file - execute immediately.
    # * Double-submit guard: the album path already deduplicates via mgid; for
    # * single-media we guard with an executing flag so a rapid second proof
    # * message (e.g. two quick photo sends) cannot invoke _execute_ban twice.
    if ctx.user_data is None:
        return ConversationHandler.END
    if ctx.user_data.get("ban_executing"):
        return ConversationHandler.END
    ctx.user_data["ban_executing"] = True
    # * Clear state even when the executor raises so the next proof is not
    # * wedged by a stale ban_executing flag; mirrors _flush_album try/finally.
    try:
        await _execute_ban(ctx.bot, [msg], dict(ctx.user_data))
    finally:
        _clear_ban_state(ctx.user_data)
    return ConversationHandler.END


async def _flush_album(
    mgid: str, bot: Bot, meta: dict[str, Any], user_data: dict[str, Any] | None
) -> None:
    await asyncio.sleep(cfg.album_debounce)
    if meta.get("_cancelled"):
        log.info("Album flush aborted: cancelled flag set for %s", mgid)
        return
    msgs = _albums.pop(mgid, [])
    if not msgs:
        _clear_ban_state(user_data)
        return
    if not meta.get("ban_target_id") or not meta.get("ban_admin_id"):
        log.warning(
            "Album flush aborted for %s: meta missing target_id or admin_id", mgid
        )
        _clear_ban_state(user_data)
        return
    log.info("Flushing album %s with %d media items", mgid, len(msgs))
    try:
        await _execute_ban(bot, msgs, meta)
    except Exception:
        log.exception("_execute_ban raised in _flush_album for album %s", mgid)
    finally:
        _clear_ban_state(user_data)


async def on_proof_unexpected(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Reject unexpected message types during proof collection."""
    if update.effective_message:
        try:
            await update.effective_message.reply_text(_MSG_PROOF_EXPECTED)
        except Exception as exc:
            log.debug("Ban proof-unexpected reply failed: %s", exc)
    return WAITING_PROOF


async def on_cancel_proof(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Acknowledge the cancel button and end the proof-collection conversation."""
    q = update.callback_query
    if q is None:
        return ConversationHandler.END
    await q.answer()

    _cancel_proof_session(ctx.user_data)

    if update.effective_message:
        try:
            await update.effective_message.reply_text(_MSG_CANCELLED)
        except Exception as exc:
            log.debug("Ban cancel reply failed: %s", exc)
    return ConversationHandler.END


async def on_proof_timeout(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Notify the user that the proof window expired and end the conversation."""
    _cancel_proof_session(ctx.user_data)

    if update.effective_message:
        try:
            await update.effective_message.reply_text(_MSG_TIMEOUT)
        except Exception as exc:
            log.debug("Ban proof-timeout reply failed: %s", exc)
    return ConversationHandler.END


# ─────────────────── ConversationHandler factory ────────────────── #


def ban_conversation(
    entry_fn: Callable[..., Any], entry_filter: BaseFilter
) -> ConversationHandler:
    """Return the ban ConversationHandler with the given entry-point function.

    Note: ``conversation_timeout`` is intentionally omitted.  PTB's timeout
    support requires the ``job-queue`` extra (APScheduler 3.x backend) which
    conflicts with this project's persistent MongoDBJobStore setup.  Conversations
    are ended via the fallback ``on_proof_timeout`` handler (triggered on any
    command) or by the user pressing Cancel.
    """
    return ConversationHandler(
        entry_points=[MessageHandler(entry_filter, entry_fn)],
        states={
            WAITING_PROOF: [
                CallbackQueryHandler(
                    on_cancel_proof, pattern=rf"^{proof.action}_cancel$"
                ),
                MessageHandler(filters.PHOTO | filters.VIDEO, on_proof_received),
                MessageHandler(
                    ~filters.PHOTO & ~filters.VIDEO & ~ALL_PREFIXES_CMD_FILTER,
                    on_proof_unexpected,
                ),
            ],
        },
        fallbacks=[MessageHandler(ALL_PREFIXES_CMD_FILTER, on_proof_timeout)],
        per_chat=True,
        per_user=True,
        per_message=False,
    )
