# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Appeal conversation: entry via /start appeal<ban_id> deep link, DM only."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    User,
)
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from tcbot import cfg
from tcbot import database as db
from tcbot.database.documents import BanDoc
from tcbot.modules.helper import parse_logmsg
from tcbot.modules.helper.formatter import bold, code, esc, mention, pre
from tcbot.modules.helper.parse_link import message_link
from tcbot.utils.dispatch import count_transient_errors, fan_out
from tcbot.utils.prefixes import ALL_PREFIXES_CMD_FILTER
from tcbot.utils.time_and_date import to_utc, utc_now

if TYPE_CHECKING:
    from telegram.ext.filters import BaseFilter

log = logging.getLogger(__name__)

WAITING_APPEAL = 0

# * Maximum character length for an appeal message.
_MAX_APPEAL_LEN: int = 2000
LOCK_HOURS: int = 12
_LOCK_WINDOW = timedelta(hours=LOCK_HOURS)
_STALE_REVIEW_HOURS: int = 72
_STALE_REVIEW_WINDOW = timedelta(hours=_STALE_REVIEW_HOURS)
_REJECTION_COOLDOWN_HOURS: int = 24
_REJECTION_COOLDOWN = timedelta(hours=_REJECTION_COOLDOWN_HOURS)
_SECONDS_PER_HOUR: int = 3600

_ID_RE = re.compile(r"^/start\s+appeal_([a-z0-9]{10})$")

# ──────────────── User-facing reply constants ──────────────────── #

_ERR_NOT_PRIVATE = "Please open this link in my private messages."
_ERR_INVALID_LINK = "This appeal link is invalid or has expired."
_ERR_WRONG_ACCOUNT = "This appeal link doesn't belong to your account."
_ERR_PENDING_REVIEW = (
    f"You already have a pending appeal under review."
    f" If no decision is reached within {_STALE_REVIEW_HOURS} hours, you may try again."
)
_ERR_REJECTION_COOLDOWN = (
    f"Your previous appeal was rejected."
    f" Please wait {_REJECTION_COOLDOWN_HOURS} hours before submitting a new one."
)
_MSG_CANCELLED = "Appeal cancelled. Nothing was submitted."
_MSG_CANCELLED_UNEXPECTED = "Please send your appeal message, or press Cancel."
_MSG_SESSION_ENDED = "Appeal session ended."
_ERR_SESSION_EXPIRED = "Session expired - please start the appeal again."
_ERR_INVALID_LOG = "Invalid log link. Please check and try again."
_ERR_NOT_AUTHORIZED = "You are not authorized."
_ERR_ROLE_LOOKUP = (
    "I couldn't verify federation roles right now. Please try again in a moment."
)
_ERR_BAN_NOT_FOUND = "Ban record not found."
_ERR_ALREADY_RESOLVED = "Appeal already resolved (ban is no longer active)."
_ERR_REVIEW_LOCKED = (
    f"Only the admin who issued this ban can review it within the first {LOCK_HOURS}h."
)
_MSG_APPEAL_SUBMITTED = "Your appeal has been submitted. The team will review it shortly - we'll get back to you."
_MSG_APPEAL_DELIVERY_FAILED = (
    "We could not deliver your appeal to the moderation team right now. "
    "Please try again in a few minutes; if this keeps happening, contact "
    "a staff member directly."
)


# ─────────────────────── Appeal pure helpers ────────────────────── #


def starts_with_appeal_tag(text: str) -> bool:
    """Return True when text (stripped) starts with #appeal (case-insensitive)."""
    return text.strip().lower().startswith("#appeal")


def text_references_log_message(text: str, msg_id: int) -> bool:
    """Return True when text contains msg_id as a standalone integer token."""
    return bool(re.search(rf"\b{msg_id}\b", text))


@dataclass(frozen=True)
class BuildAppeal:
    """Configurable appeal ConversationHandler builder."""

    community_name: str
    log_channel: str
    cancel_label: str = field(default="Cancel", kw_only=True)
    cancel_callback: str = field(default="cancel_appeal", kw_only=True)

    # ── Keyboard and text factories ────────────────────────────────────────

    def instruction_text(self) -> str:
        """Multi-line HTML instruction prompt sent when the user opens an appeal."""
        log_handle = self.log_channel.lstrip("@")
        pre_content = (
            f"#appeal\n"
            f"Log link: https://t.me/{log_handle}/1\n"
            "Clarification: I spammed unintentionally due to an auto-clicker.\n"
            "Agreement: I will not use any automation tools in the group again."
        )
        return (
            f"{esc(self.community_name)} Ban Appeal\n\n"
            f"To submit your appeal, reply with a message starting with {code('#appeal')}, containing:\n"
            f"- {bold('Log link:')} (the link to your ban log from the log channel)\n"
            f"- {bold('Clarification:')} (your honest explanation of what happened)\n"
            f"- {bold('Agreement:')} (your commitment not to repeat the violation)\n\n"
            f"{bold('Example:')}\n"
            f"{pre(pre_content)}\n\n"
            f"Log Channel: {esc(self.log_channel)}"
        )

    def cancel_keyboard(self) -> InlineKeyboardMarkup:
        """Single-button keyboard attached to the instruction prompt."""
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        self.cancel_label, callback_data=self.cancel_callback
                    )
                ]
            ]
        )

    def review_keyboard(self, ban_id: str) -> InlineKeyboardMarkup:
        """Approve / Reject keyboard attached to the staff review card."""
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Approve", callback_data=f"appeal_approve_{ban_id}"
                    ),
                    InlineKeyboardButton(
                        "Reject", callback_data=f"appeal_reject_{ban_id}"
                    ),
                ]
            ]
        )

    # ── Private helpers ────────────────────────────────────────────────────

    @staticmethod
    async def _update_or_send_log(
        bot: Bot,
        lc: int,
        lt: int | None,
        msg_id: int | None,
        text: str,
    ) -> None:
        """Edit the existing appeal log message, or post a new one as fallback."""
        if msg_id:
            try:
                await bot.edit_message_text(
                    text, chat_id=lc, message_id=msg_id, parse_mode="HTML"
                )
                return
            except Exception as exc:
                log.warning("Could not edit appeal submitted log: %s", exc)
        try:
            await bot.send_message(lc, text, parse_mode="HTML", message_thread_id=lt)
        except Exception as exc:
            log.debug("Could not send appeal submitted log: %s", exc)

    # ── ConversationHandler step methods ──────────────────────────────────

    async def _start(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, ban_id: str
    ) -> int:
        """Validate the deep-link and open the WAITING_APPEAL state."""
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return ConversationHandler.END

        uid = user.id

        if update.effective_chat is None or update.effective_chat.type != "private":
            try:
                await msg.reply_text(_ERR_NOT_PRIVATE)
            except Exception as exc:
                log.debug("Appeal not-private reply failed: %s", exc)
            return ConversationHandler.END

        try:
            ban = await db.bans_db.get_ban(ban_id)
        except Exception:
            log.exception("Appeal _start: DB error fetching ban_id=%s", ban_id)
            with contextlib.suppress(Exception):
                await msg.reply_text(_ERR_INVALID_LINK)
            return ConversationHandler.END
        if not ban or not ban.get("is_active"):
            try:
                await msg.reply_text(_ERR_INVALID_LINK)
            except Exception as exc:
                log.debug(
                    "Appeal invalid-link reply failed for ban_id=%s: %s", ban_id, exc
                )
            return ConversationHandler.END

        if ban.get("banned_user_id") != uid:
            try:
                await msg.reply_text(_ERR_WRONG_ACCOUNT)
            except Exception as exc:
                log.debug("Appeal wrong-account reply failed for user %d: %s", uid, exc)
            return ConversationHandler.END

        if ban.get("review_message_id"):
            review_ts = ban.get("review_timestamp")
            stale = review_ts is None or (
                to_utc(review_ts) < utc_now() - _STALE_REVIEW_WINDOW
            )
            if stale:
                # * Review card is older than _STALE_REVIEW_HOURS (message may have been
                # * deleted from the discussion topic or staff never acted).  Clear the
                # * stale review state so the user can submit a fresh appeal.
                try:
                    await db.bans_db.clear_review(ban_id)
                except Exception:
                    log.exception(
                        "Appeal _start: failed to clear stale review for ban_id=%s"
                        " user=%d; blocking appeal to avoid corrupt state",
                        ban_id,
                        uid,
                    )
                    with contextlib.suppress(Exception):
                        await msg.reply_text(_ERR_PENDING_REVIEW)
                    return ConversationHandler.END
                log.info(
                    "Appeal _start: stale review cleared for ban_id=%s user=%d"
                    " (review_ts=%s)",
                    ban_id,
                    uid,
                    review_ts,
                )
            else:
                try:
                    await msg.reply_text(_ERR_PENDING_REVIEW)
                except Exception as exc:
                    log.debug(
                        "Appeal pending-review reply failed for user %d: %s", uid, exc
                    )
                return ConversationHandler.END

        rejected_at = ban.get("rejected_at")
        if rejected_at is not None:
            elapsed = utc_now() - to_utc(rejected_at)
            if elapsed < timedelta(0):
                elapsed = timedelta(0)
            if elapsed < _REJECTION_COOLDOWN:
                remaining_h = (
                    int(
                        (_REJECTION_COOLDOWN - elapsed).total_seconds()
                        / _SECONDS_PER_HOUR
                    )
                    + 1
                )
                try:
                    await msg.reply_text(
                        f"{_ERR_REJECTION_COOLDOWN} ({remaining_h}h remaining)"
                    )
                except Exception as exc:
                    log.debug("Appeal cooldown reply failed for user %d: %s", uid, exc)
                return ConversationHandler.END

        if ctx.user_data is None:
            log.error("ctx.user_data is None in appeal_flow _start")
            return ConversationHandler.END

        ctx.user_data["appeal_ban_id"] = ban_id
        ctx.user_data["appeal_log_msg_id"] = ban.get("log_message_id", 0)

        try:
            instr = await msg.reply_text(
                self.instruction_text(),
                parse_mode="HTML",
                reply_markup=self.cancel_keyboard(),
            )
            ctx.user_data["appeal_instruction_msg_id"] = instr.message_id
        except Exception as exc:
            log.debug("Appeal instruction send failed for user %d: %s", uid, exc)
            # * Clear keys set above so user_data does not contain stale appeal state
            # * if the user retries later or starts a different conversation.
            ctx.user_data.pop("appeal_ban_id", None)
            ctx.user_data.pop("appeal_log_msg_id", None)
            return ConversationHandler.END

        return WAITING_APPEAL

    async def _on_entry(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry-point handler; parses the /start appeal_<id> deep link."""
        msg = update.effective_message
        if msg is None or msg.text is None:
            return ConversationHandler.END

        text = msg.text.strip()
        m = _ID_RE.match(text)
        if not m:
            return ConversationHandler.END
        return await self._start(update, ctx, m.group(1))

    async def _on_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel button handler; clears state and ends the conversation."""
        q = update.callback_query
        if q is None:
            return ConversationHandler.END

        if ctx.user_data is not None:
            for key in (
                "appeal_ban_id",
                "appeal_log_msg_id",
                "appeal_instruction_msg_id",
            ):
                ctx.user_data.pop(key, None)

        # * Answer before the visible edit so the client spinner clears
        # * first; mirrors ban_flow.on_cancel_proof sequential ordering.
        try:
            await q.answer()
        except Exception as exc:
            log.debug("appeal cancel answer failed: %s", exc)
        try:
            await q.edit_message_text(_MSG_CANCELLED)
        except Exception:
            log.debug("appeal cancel edit failed (message may already be gone)")
        return ConversationHandler.END

    async def _end(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Fallback handler; fires on any unrecognised command during the flow."""
        if ctx.user_data is not None:
            for key in (
                "appeal_ban_id",
                "appeal_log_msg_id",
                "appeal_instruction_msg_id",
            ):
                ctx.user_data.pop(key, None)
        msg = update.effective_message
        if msg:
            try:
                await msg.reply_text(_MSG_SESSION_ENDED)
            except Exception as exc:
                log.debug("Appeal _end reply failed: %s", exc)
        return ConversationHandler.END

    async def _on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Text message handler; validates and submits a #appeal message."""
        msg = update.effective_message
        if msg is None or msg.text is None:
            return WAITING_APPEAL

        text = msg.text.strip()

        if not starts_with_appeal_tag(text):
            try:
                await msg.reply_text(_MSG_CANCELLED_UNEXPECTED)
            except Exception as exc:
                log.debug("Appeal unexpected-text reply failed: %s", exc)
            return WAITING_APPEAL

        if len(text) > _MAX_APPEAL_LEN:
            try:
                await msg.reply_text(
                    f"Your appeal message is too long (max {_MAX_APPEAL_LEN} characters). "
                    "Please shorten it and try again.",
                )
            except Exception as exc:
                log.debug("Appeal too-long reply failed: %s", exc)
            return WAITING_APPEAL

        if ctx.user_data is None:
            log.error("ctx.user_data is None in appeal_flow _on_message")
            return ConversationHandler.END

        ban_id = ctx.user_data.get("appeal_ban_id")
        log_msg_id = ctx.user_data.get("appeal_log_msg_id", 0)

        if not ban_id:
            try:
                await msg.reply_text(_ERR_SESSION_EXPIRED)
            except Exception as exc:
                log.debug("Appeal _on_message session-expired reply failed: %s", exc)
            return ConversationHandler.END

        # * Revalidate against a fresh ban record: the ban may have been
        # * deactivated, or the cached log_message_id may be a transient 0
        # * from a ban update. Submitting against stale state would orphan
        # * a review card on an inactive ban.
        try:
            fresh_ban = await db.bans_db.get_ban(ban_id)
        except Exception:
            log.exception("Appeal _on_message get_ban failed for %s", ban_id)
            fresh_ban = None
        if not fresh_ban or not fresh_ban.get("is_active"):
            try:
                await msg.reply_text(_ERR_SESSION_EXPIRED)
            except Exception as exc:
                log.debug("Appeal _on_message expired-ban reply failed: %s", exc)
            for key in (
                "appeal_ban_id",
                "appeal_log_msg_id",
                "appeal_instruction_msg_id",
            ):
                ctx.user_data.pop(key, None)
            return ConversationHandler.END

        # * Re-check the pending-review and rejection-cooldown gates from
        # * _start: the ban may have gained a review (concurrent submit from
        # * another session) or a rejection while this conversation waited
        # * for the user to type. Submitting anyway would orphan a review
        # * card or bypass the cooldown.
        if fresh_ban.get("review_message_id"):
            review_ts = fresh_ban.get("review_timestamp")
            stale = review_ts is None or (
                to_utc(review_ts) < utc_now() - _STALE_REVIEW_WINDOW
            )
            if stale:
                try:
                    await db.bans_db.clear_review(ban_id)
                except Exception:
                    _who = update.effective_user
                    log.exception(
                        "Appeal _on_message: failed to clear stale review for "
                        "ban_id=%s user=%d; blocking submit to avoid corrupt state",
                        ban_id,
                        _who.id if _who is not None else 0,
                    )
                    with contextlib.suppress(Exception):
                        await msg.reply_text(_ERR_PENDING_REVIEW)
                    for key in (
                        "appeal_ban_id",
                        "appeal_log_msg_id",
                        "appeal_instruction_msg_id",
                    ):
                        ctx.user_data.pop(key, None)
                    return ConversationHandler.END
            else:
                try:
                    await msg.reply_text(_ERR_PENDING_REVIEW)
                except Exception as exc:
                    log.debug("Appeal _on_message pending-review reply failed: %s", exc)
                for key in (
                    "appeal_ban_id",
                    "appeal_log_msg_id",
                    "appeal_instruction_msg_id",
                ):
                    ctx.user_data.pop(key, None)
                return ConversationHandler.END

        rejected_at = fresh_ban.get("rejected_at")
        if rejected_at is not None:
            elapsed = utc_now() - to_utc(rejected_at)
            if elapsed < timedelta(0):
                elapsed = timedelta(0)
            if elapsed < _REJECTION_COOLDOWN:
                remaining_h = (
                    int(
                        (_REJECTION_COOLDOWN - elapsed).total_seconds()
                        / _SECONDS_PER_HOUR
                    )
                    + 1
                )
                try:
                    await msg.reply_text(
                        f"{_ERR_REJECTION_COOLDOWN} ({remaining_h}h remaining)"
                    )
                except Exception as exc:
                    log.debug("Appeal _on_message cooldown reply failed: %s", exc)
                for key in (
                    "appeal_ban_id",
                    "appeal_log_msg_id",
                    "appeal_instruction_msg_id",
                ):
                    ctx.user_data.pop(key, None)
                return ConversationHandler.END

        if not log_msg_id:
            log_msg_id = fresh_ban.get("log_message_id", 0)

        if log_msg_id and not text_references_log_message(text, log_msg_id):
            try:
                await msg.reply_text(_ERR_INVALID_LOG)
            except Exception as exc:
                log.debug("Appeal _on_message invalid-log reply failed: %s", exc)
            return WAITING_APPEAL

        user = update.effective_user
        if user is None:
            for key in (
                "appeal_ban_id",
                "appeal_log_msg_id",
                "appeal_instruction_msg_id",
            ):
                ctx.user_data.pop(key, None)
            return ConversationHandler.END

        uid = user.id

        appeal_chat, appeal_thread = cfg.appeals
        appeal_msg_id: int | None = None
        try:
            fwd = await msg.forward(appeal_chat, message_thread_id=appeal_thread)
            appeal_msg_id = fwd.message_id
        except Exception:
            log.exception("Appeal forward failed")

        appeal_link = (
            message_link(appeal_chat, appeal_msg_id, appeal_thread)
            if appeal_msg_id
            else ""
        )
        review_text = parse_logmsg.appeal_received_log(
            uid, user.first_name, ban_id, appeal_link
        )
        lc, lt = cfg.logs

        # * Send review post + log message in parallel
        rv, sent_log = await asyncio.gather(
            ctx.bot.send_message(
                cfg.main_group,
                review_text,
                parse_mode="HTML",
                message_thread_id=cfg.appeal_discussion_topic or None,
                reply_markup=self.review_keyboard(ban_id),
            ),
            ctx.bot.send_message(
                lc,
                parse_logmsg.appeal_submitted_log(
                    uid, user.first_name, ban_id, appeal_link
                ),
                parse_mode="HTML",
                message_thread_id=lt,
            ),
            return_exceptions=True,
        )

        review_msg_id: int | None = (
            rv.message_id if not isinstance(rv, BaseException) else None
        )
        if isinstance(rv, BaseException):
            log.error("Appeal review post failed: %s", rv)

        appeal_log_sent_id: int | None = (
            sent_log.message_id if not isinstance(sent_log, BaseException) else None
        )
        if isinstance(sent_log, BaseException):
            log.error("Appeal log failed: %s", sent_log)

        # * Claim the pending-review slot atomically before storing anything
        # * else: a concurrent submit for the same ban (another session that
        # * passed the re-check above) must not overwrite the winner and
        # * orphan its review card. The loser deletes its own orphan card
        # * best-effort and tells the user the appeal is already pending.
        claimed = False
        if review_msg_id:
            try:
                claimed = await db.bans_db.set_review_if_absent(ban_id, review_msg_id)
            except Exception:
                log.exception("Appeal claim-review failed for ban_id=%s", ban_id)
                claimed = False
            if not claimed:
                log.warning(
                    "Appeal submit lost the review race for ban_id=%s; "
                    "discarding duplicate review_msg_id=%d",
                    ban_id,
                    review_msg_id,
                )
                try:
                    await ctx.bot.delete_message(
                        chat_id=cfg.main_group, message_id=review_msg_id
                    )
                except Exception as exc:
                    log.debug(
                        "Appeal orphan-card delete failed for ban_id=%s: %s",
                        ban_id,
                        exc,
                    )
                try:
                    await msg.reply_text(_ERR_PENDING_REVIEW)
                except Exception as exc:
                    log.debug("Appeal race-loser reply failed: %s", exc)
                for key in (
                    "appeal_ban_id",
                    "appeal_log_msg_id",
                    "appeal_instruction_msg_id",
                ):
                    ctx.user_data.pop(key, None)
                return ConversationHandler.END
        if appeal_log_sent_id and ban_id:
            try:
                await db.bans_db.set_appeal_log_msg(
                    ban_id, appeal_log_sent_id, appeal_link=appeal_link
                )
            except Exception:
                log.exception("Appeal DB write failed for ban_id=%s", ban_id)

        # * Edit instruction message + cache user in parallel
        instr_mid = ctx.user_data.get("appeal_instruction_msg_id")
        edit_coro = (
            ctx.bot.edit_message_text(
                _MSG_APPEAL_SUBMITTED,
                chat_id=update.effective_chat.id if update.effective_chat else None,
                message_id=instr_mid,
            )
            if instr_mid and update.effective_chat
            else None
        )
        upsert_coro = db.users_cache.upsert_user(
            uid, user.username, user.first_name, user.last_name
        )

        if edit_coro is not None:
            edit_r, upsert_r = await asyncio.gather(
                edit_coro, upsert_coro, return_exceptions=True
            )
            if isinstance(edit_r, BaseException):
                log.debug(
                    "Appeal submitted-edit failed for ban_id=%s: %s", ban_id, edit_r
                )
            if isinstance(upsert_r, BaseException):
                log.warning(
                    "Appeal user-cache upsert failed for user=%d: %s",
                    uid,
                    upsert_r,
                )
        else:
            try:
                await upsert_coro
            except Exception as exc:
                log.warning("Appeal user-cache upsert failed for user=%d: %s", uid, exc)

        # * If BOTH the review post (``rv``) and the log post (``sent_log``)
        # * failed, staff will never see the appeal and the user is left
        # * waiting for a reply that will never come. Edit the instruction
        # * message to a clear "we could not deliver your appeal" reply,
        # * otherwise the user believes the appeal was received and may not
        # * re-submit for a long time.
        if review_msg_id is None and appeal_log_sent_id is None:
            log.error(
                "submit_appeal: BOTH review post and appeal log post failed "
                "for user=%d ban=%s; the appeal was not delivered to staff",
                uid,
                ban_id,
            )
            if instr_mid and update.effective_chat:
                try:
                    await ctx.bot.edit_message_text(
                        _MSG_APPEAL_DELIVERY_FAILED,
                        chat_id=update.effective_chat.id,
                        message_id=instr_mid,
                    )
                except Exception as exc:
                    log.debug("submit_appeal delivery-failed reply failed: %s", exc)
            # * Stay in WAITING_APPEAL with user_data intact so the user can
            # * retry by sending another #appeal message without reopening
            # * the deep link. Returning END here would end the conversation
            # * while keeping stale keys, leaving no in-place retry path.
            return WAITING_APPEAL

        # * Clear appeal keys so user_data is clean after successful submission.
        for key in (
            "appeal_ban_id",
            "appeal_log_msg_id",
            "appeal_instruction_msg_id",
        ):
            ctx.user_data.pop(key, None)

        return ConversationHandler.END

    # ── Public callback handler (registered outside the ConversationHandler) ─

    async def on_decision(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Approve / Reject callback for the staff review card in the main group."""
        q = update.callback_query
        if q is None:
            return

        admin = update.effective_user
        if admin is None:
            try:
                await q.answer()
            except Exception as exc:
                log.debug("Appeal decision answer failed with no user: %s", exc)
            return

        data = q.data
        if not data or not data.startswith(("appeal_approve_", "appeal_reject_")):
            try:
                await q.answer()
            except Exception as exc:
                log.debug("Appeal decision answer failed for unknown data: %s", exc)
            return

        if data.startswith("appeal_approve_"):
            action = "approve"
            ban_id = data[len("appeal_approve_") :]
        else:
            action = "reject"
            ban_id = data[len("appeal_reject_") :]

        # * Pre-fetch the reviewer role alongside the ban record and q.answer();
        # * ban_id is known from callback data so the DB calls fire speculatively.
        # * get_effective_role (not is_staff) is used so a database outage
        # * surfaces as an exception and gets a retry hint instead of being
        # * coerced to False and misreported as "not authorized".
        role_result, ban_result, _ = await asyncio.gather(
            db.users_roles.get_effective_role(admin.id),
            db.bans_db.get_ban(ban_id),
            q.answer(),
            return_exceptions=True,
        )
        if isinstance(role_result, BaseException):
            if isinstance(role_result, asyncio.CancelledError):
                raise role_result
            log.warning(
                "Appeal review role lookup failed for %d: %s", admin.id, role_result
            )
            # * Never edit the shared review card on an unmade decision:
            # * the card must stay actionable for another staffer.
            try:
                await q.answer(_ERR_ROLE_LOOKUP, show_alert=True)
            except Exception as exc:
                log.debug("Appeal role-lookup answer failed: %s", exc)
            return
        if role_result not in ("founder", "admin"):
            # * Answer with an alert instead of editing: the tapper is not
            # * staff, and editing would destroy the shared review card that
            # * staff still need to act on.
            try:
                await q.answer(_ERR_NOT_AUTHORIZED, show_alert=True)
            except Exception as exc:
                log.debug("Appeal not-authorized answer failed: %s", exc)
            return
        if isinstance(ban_result, BaseException):
            log.error("get_ban failed in appeal review for %s: %s", ban_id, ban_result)
            try:
                await q.edit_message_text(_ERR_BAN_NOT_FOUND, reply_markup=None)
            except Exception as exc:
                log.debug("Appeal ban-not-found edit failed: %s", exc)
            return
        if not ban_result:
            try:
                await q.edit_message_text(_ERR_BAN_NOT_FOUND, reply_markup=None)
            except Exception as exc:
                log.debug("Appeal ban-not-found (empty) edit failed: %s", exc)
            return
        ban = ban_result

        # * An inactive ban with a live review marker is a stale card left
        # * behind by a manual /tcunban (which never touches review state).
        # * Clean it up so the card reflects reality; this is the only
        # * resolved path that edits, because no verdict exists to clobber.
        if not ban.get("is_active"):
            if ban.get("review_message_id"):
                try:
                    await db.bans_db.clear_review(ban_id)
                except Exception:
                    log.exception(
                        "Appeal stale-card clear_review failed for ban %s", ban_id
                    )
                try:
                    await q.edit_message_text(_ERR_ALREADY_RESOLVED, reply_markup=None)
                except Exception as exc:
                    log.debug("Appeal already-resolved edit failed: %s", exc)
            else:
                try:
                    await q.answer(_ERR_ALREADY_RESOLVED, show_alert=True)
                except Exception as exc:
                    log.debug("Appeal already-resolved answer failed: %s", exc)
            return

        # * A review that is already rejected or has no live review marker
        # * was decided by a concurrent tap. Acting again would double-DM
        # * the user, double-edit the card, or unban after a rejection.
        # * Alert only: the winner already edited the card with its verdict
        # * and overwriting it would destroy that outcome.
        if ban.get("rejected_at") is not None or not ban.get("review_message_id"):
            try:
                await q.answer(_ERR_ALREADY_RESOLVED, show_alert=True)
            except Exception as exc:
                log.debug("Appeal already-resolved answer failed: %s", exc)
            return

        review_ts = ban.get("review_timestamp")
        if review_ts and reviewer_locked_out(
            review_ts, ban.get("admin_user_id") or 0, admin.id
        ):
            # * Alert only: editing would destroy the shared card that the
            # * banning admin still needs to act on within their window.
            try:
                await q.answer(_ERR_REVIEW_LOCKED, show_alert=True)
            except Exception as exc:
                log.debug("Appeal review-locked answer failed: %s", exc)
            return

        target_id = ban.get("banned_user_id", 0)
        lc, lt = cfg.logs

        if action == "approve":
            await self._approve_appeal(
                ctx.bot, q, ban, ban_id, target_id, admin, lc, lt
            )
        elif action == "reject":
            await self._reject_appeal(ctx.bot, q, ban, ban_id, target_id, admin, lc, lt)

    # ── Appeal decision helpers ────────────────────────────────────────── #

    async def _approve_appeal(
        self,
        bot: Bot,
        q: CallbackQuery,
        ban: BanDoc,
        ban_id: str,
        target_id: int,
        admin: User,
        lc: int,
        lt: int | None,
    ) -> None:
        # * Deactivate ALL active bans for the user (not only the appeal ban_id)
        # * in parallel with fetching active groups, target name, and cancelling
        # * any pending timed-unban APScheduler job (future-proofing: no-op when
        # * no timed ban exists, same pattern as execute_unban in unban_flow.py).
        deactivate_result, groups, target_fname, _ = await asyncio.gather(
            db.bans_db.deactivate_all_active_bans(target_id),
            db.groups_db.active_groups(),
            db.users_cache.get_first_name(target_id, str(target_id)),
            db.scheduler.cancel_schedule(f"unban.{ban_id}"),
            return_exceptions=True,
        )
        if isinstance(deactivate_result, BaseException):
            # * Same trade-off as execute_unban in unban_flow.py: when the
            # * DB deactivation fails we must NOT continue to the fan-out,
            # * otherwise the user is unbanned in chats but the DB still
            # * marks them banned. The greeting handler's join-auto-ban
            # * would then re-ban them the next time they join any
            # * connected group. Surface the failure to the operator and
            # * tell the user that the appeal is in progress but the DB
            # * write failed.
            log.error(
                "approve_appeal: deactivate_all_active_bans failed for "
                "user=%d; aborting fan-out to avoid split-brain state: %s",
                target_id,
                deactivate_result,
            )
            try:
                await q.edit_message_text(
                    f"Appeal DB write failed for ban {code(ban_id)}. "
                    "The user is still marked as banned in the database "
                    "even though chats will be un-banned. Check the logs "
                    "and retry.",
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except Exception as exc:
                log.debug("approve_appeal DB-fail reply failed: %s", exc)
            return
        if isinstance(groups, BaseException):
            log.error(
                "active_groups failed during appeal unban of %d: %s",
                target_id,
                groups,
            )
            groups = []
        if isinstance(target_fname, BaseException):
            target_fname = str(target_id)

        # * Clear the review marker now that the ban is inactive. Without
        # * this a concurrent second decision would still see a live review
        # * and act again; the resolved-guard above relies on its absence.
        try:
            await db.bans_db.clear_review(ban_id)
        except Exception:
            log.exception("approve_appeal: clear_review failed for ban %s", ban_id)

        _primary_ids = [cid for cid in (cfg.main_group, cfg.exec_group) if cid]
        _existing_ids = {grp.get("chat_id", 0) for grp in groups}
        for _pid in _primary_ids:
            if _pid not in _existing_ids:
                groups = [*groups, {"chat_id": _pid, "title": ""}]

        unban_results = await fan_out(
            [
                bot.unban_chat_member(
                    grp.get("chat_id", 0), target_id, only_if_banned=True
                )
                for grp in groups
            ]
        )
        unban_failed = count_transient_errors(unban_results)
        if unban_failed:
            log.error(
                "Appeal-approve fan-out had %d/%d transient failures for "
                "target=%d; user may still be banned in those chats",
                unban_failed,
                len(groups),
                target_id,
            )

        appeal_link = ban.get("appeal_link") or ""
        appeal_submitted_at = ban.get("appeal_submitted_at")
        # * The four notifications are independent; capture each result so a
        # * silent DM, card-edit, or log failure is visible to operators
        # * instead of being swallowed like the reject path used to do.
        dm_r, card_r, log_r, unban_log_r = await asyncio.gather(
            bot.send_message(
                target_id,
                f"Your appeal for ban {code(ban_id)} has been approved - "
                f"you're now unbanned from {esc(self.community_name)}. Welcome back.",
                parse_mode="HTML",
            ),
            q.edit_message_text(
                f"Appeal approved by {mention(admin.id, admin.first_name)}. Unbanned.",
                parse_mode="HTML",
                reply_markup=None,
            ),
            self._update_or_send_log(
                bot,
                lc,
                lt,
                int(ban.get("appeal_log_msg_id") or 0) or None,
                parse_logmsg.appeal_approved_edit(
                    target_id,
                    target_fname,
                    admin.id,
                    admin.first_name,
                    ban_id,
                    str(appeal_link),
                    appeal_submitted_at,
                ),
            ),
            bot.send_message(
                lc,
                parse_logmsg.appeal_unban_log(
                    target_id,
                    target_fname,
                    admin.id,
                    admin.first_name,
                    ban_id,
                ),
                parse_mode="HTML",
                message_thread_id=lt,
            ),
            return_exceptions=True,
        )
        if isinstance(dm_r, BaseException):
            log.warning(
                "approve_appeal DM to %d failed for ban %s: %s",
                target_id,
                ban_id,
                dm_r,
            )
        if isinstance(card_r, BaseException):
            log.warning(
                "approve_appeal review-card edit failed for ban %s: %s", ban_id, card_r
            )
        if isinstance(log_r, BaseException):
            log.warning(
                "approve_appeal appeal-log update failed for ban %s: %s",
                ban_id,
                log_r,
            )
        if isinstance(unban_log_r, BaseException):
            log.error(
                "approve_appeal unban log send failed for user %d ban %s: %s",
                target_id,
                ban_id,
                unban_log_r,
            )

    async def _reject_appeal(
        self,
        bot: Bot,
        q: CallbackQuery,
        ban: BanDoc,
        ban_id: str,
        target_id: int,
        admin: User,
        lc: int,
        lt: int | None,
    ) -> None:
        # * The four side-effects (DM, review-card edit, clear_review, audit-log
        # * record) are independent. Each is captured separately so failures
        # * are surfaced: a missing DM is a transient Telegram problem; a
        # * failed clear_review means the user could re-appeal within the
        # * 72-hour window; a failed set_rejected_by means the audit
        # * trail is incomplete.
        # * set_rejected_by runs first and alone: the 24 h cooldown depends
        # * on rejected_at, so it must land even if the review clear fails.
        try:
            await db.bans_db.set_rejected_by(ban_id, admin.id, admin.first_name)
        except Exception:
            # * Without rejected_at the user may re-appeal immediately; the
            # * ban itself still stands, so this fails safe toward re-review.
            log.exception("reject_appeal set_rejected_by failed for ban %s", ban_id)
        results = await asyncio.gather(
            db.users_cache.get_first_name(target_id, str(target_id)),
            bot.send_message(
                target_id,
                f"Your appeal for ban {code(ban_id)} was not approved. "
                "The ban remains in place.",
                parse_mode="HTML",
            ),
            q.edit_message_text(
                f"Appeal rejected by {mention(admin.id, admin.first_name)}.",
                parse_mode="HTML",
                reply_markup=None,
            ),
            db.bans_db.clear_review(ban_id),
            return_exceptions=True,
        )
        target_fname_result = results[0]
        if isinstance(results[1], BaseException):
            log.warning("reject_appeal DM to %d failed: %s", target_id, results[1])
        if isinstance(results[2], BaseException):
            log.debug("reject_appeal review-card edit failed: %s", results[2])
        if isinstance(results[3], BaseException):
            # * ``clear_review`` failure is more serious: the user could
            # * re-submit an appeal within the 72-hour stale-review window
            # * because the DB still has the pending review. Log loudly.
            log.error(
                "reject_appeal clear_review failed for ban %s: user %d may "
                "re-appeal within the 72-hour window",
                ban_id,
                target_id,
            )
        target_fname = (
            target_fname_result
            if not isinstance(target_fname_result, BaseException)
            else str(target_id)
        )

        await self._update_or_send_log(
            bot,
            lc,
            lt,
            int(ban.get("appeal_log_msg_id") or 0) or None,
            parse_logmsg.appeal_rejected_edit(
                target_id,
                target_fname,
                admin.id,
                admin.first_name,
                ban_id,
                str(ban.get("appeal_link") or ""),
                ban.get("appeal_submitted_at"),
            ),
        )

    # ── ConversationHandler factory ────────────────────────────────────────

    def build_handler(self, entry_filter: BaseFilter) -> ConversationHandler:
        """Assemble and return the appeal ConversationHandler.

        Note: ``conversation_timeout`` is intentionally absent.  PTB's timeout
        support requires the ``job-queue`` extra (APScheduler 3.x backend) which
        is already used by this project's persistent MongoDBJobStore setup.
        Stale sessions are detected via the 72-hour ``_STALE_REVIEW_WINDOW`` guard
        in ``_start`` and ended via the ``_end`` fallback (triggered on any command)
        or Cancel.
        """
        return ConversationHandler(
            entry_points=[MessageHandler(entry_filter, self._on_entry)],
            states={
                WAITING_APPEAL: [
                    CallbackQueryHandler(
                        self._on_cancel,
                        pattern=rf"^{re.escape(self.cancel_callback)}$",
                    ),
                    MessageHandler(
                        filters.ChatType.PRIVATE
                        & filters.TEXT
                        & ~ALL_PREFIXES_CMD_FILTER,
                        self._on_message,
                    ),
                ],
            },
            fallbacks=[
                MessageHandler(ALL_PREFIXES_CMD_FILTER, self._end),
            ],
            per_chat=True,
            per_user=True,
            per_message=False,
        )


# ────────────────────── Module-level instance ───────────────────── #

appeal = BuildAppeal(cfg.community_name, cfg.appeal_log_handle)


def reviewer_locked_out(
    review_timestamp: datetime | None,
    ban_admin_id: int | None,
    reviewer_id: int,
) -> bool:
    """Check whether reviewer_id is blocked from reviewing within the lock window."""
    # * A missing or zero ban_admin_id means the ban owner is unknown
    # * (legacy record): fail open with no lock, matching the documented
    # * contract. Failing closed here would lock out every reviewer for
    # * 12 hours with nobody able to act.
    if review_timestamp is None or not ban_admin_id:
        return False
    if reviewer_id == ban_admin_id:
        return False
    elapsed = utc_now() - to_utc(review_timestamp)
    return elapsed < _LOCK_WINDOW
