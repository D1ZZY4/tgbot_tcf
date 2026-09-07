# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Central reason-step infrastructure."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from tcbot.modules.helper import replies
from tcbot.modules.helper.formatter import bold, esc, mention
from tcbot.modules.helper.workflows.proof_flow import BuildProof
from tcbot.utils.prefixes import ALL_PREFIXES_CMD_FILTER

if TYPE_CHECKING:
    from collections.abc import Callable

from telegram.ext.filters import BaseFilter

log = logging.getLogger(__name__)

# * State constants used by all moderation ConversationHandlers
WAITING_REASON = 0
WAITING_PROOF = 1

# * Maximum characters accepted for a moderation reason.
# * Telegram hard-caps messages at 4096 chars; action summaries include names,
# * IDs, and other metadata on top of the reason.  1000 chars is generous for
# * any real reason while guaranteeing the combined message stays under the cap.
# * Public name so command entries can fail fast on overlong inline reasons
# * without paying for target resolution or DB work first.
MAX_REASON_LEN: int = 1000

_MAX_REASON_LEN: int = MAX_REASON_LEN


# ───────────────────────── Reason parsing ───────────────────────── #


def parse_inline_reason(
    args: list[str],
    *,
    has_explicit_target: bool,
) -> str:
    """Extract any inline reason text from command arguments."""
    tokens = args[1:] if has_explicit_target else args
    return " ".join(tokens).strip()


def is_reason_too_long(text: str) -> bool:
    """Return True when ``text`` exceeds the shared reason length cap."""
    return len(text) > MAX_REASON_LEN


def reason_too_long_text(actual_len: int) -> str:
    """Single source of truth for the overlong-reason reply text."""
    return (
        f"Reason is too long (max {MAX_REASON_LEN} characters, "
        f"you sent {actual_len}). Please shorten it."
    )


# ─────────────────────────── BuildReason ────────────────────────── #


@dataclass(frozen=True)
class BuildReason:
    """Configurable reason-step keyboard and prompt builder."""

    action: str
    skip_allowed: bool = field(default=True, kw_only=True)
    skip_label: str = field(default="Skip", kw_only=True)
    cancel_label: str = field(default="Cancel", kw_only=True)

    def keyboard(self) -> InlineKeyboardMarkup:
        """Reason-step keyboard. Includes Skip only when skip_allowed is True."""
        buttons: list[InlineKeyboardButton] = []
        if self.skip_allowed:
            buttons.append(
                InlineKeyboardButton(
                    self.skip_label, callback_data=f"{self.action}_skip_reason"
                )
            )
        buttons.append(
            InlineKeyboardButton(
                self.cancel_label, callback_data=f"{self.action}_cancel"
            )
        )
        return InlineKeyboardMarkup([buttons])

    def prompt(
        self,
        target_mention: str,
        action_label: str,
        extra_info: str = "",
    ) -> str:
        """Prompt asking the moderator to type a reason."""
        suffix = f" {extra_info}" if extra_info else ""
        skip_hint = f", or tap {bold(self.skip_label)}" if self.skip_allowed else ""
        return (
            f"About to {action_label} {target_mention}{suffix}.\n"
            f"What's the reason? Type it below{skip_hint}."
        )


# ─────────────── Generic ConversationHandler factory ────────────── #


class _ModActionFlow:
    """Per-action ConversationHandler state and callback container.

    Replaces the previous closure-heavy ``build_modaction_conv`` body so that
    each handler is a named method on an explicit state object.  This keeps the
    public factory function under 10 lines and makes individual handlers
    testable without reproducing the full closure environment.
    """

    def __init__(
        self,
        action: str,
        reason: BuildReason,
        proof: BuildProof,
        executor: Callable[..., Any],
    ) -> None:
        self.action = action
        self.reason = reason
        self.proof = proof
        self.executor = executor
        self._reason_key = f"{action}_reason"
        self._proof_msgs_key = f"{action}_proof_msgs"
        self._extra_info_key = f"{action}_extra_info"
        self._prompt_chat_key = f"{action}_prompt_chat"
        self._prompt_id_key = f"{action}_prompt_id"
        self._exec_key = f"{action}_executing"
        self._mgid_key = f"{action}_seen_mgid"

    # ── Helpers ───────────────────────────────────────────────────── #

    def _get_target(self, ctx: ContextTypes.DEFAULT_TYPE) -> str:
        if ctx.user_data is None:
            return "target"
        raw: str = (
            ctx.user_data.get(f"{self.action}_target_name")
            or ctx.user_data.get(f"{self.action}_target_fname")
            or "target"
        )
        tid: int | None = ctx.user_data.get(f"{self.action}_target_id")
        if tid:
            return mention(tid, raw)
        return esc(raw)

    def _clear_user_data(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Remove all ``{action}_*`` keys from user_data on cancel / timeout."""
        if ctx.user_data is None:
            return
        prefix = f"{self.action}_"
        for key in [k for k in ctx.user_data if k.startswith(prefix)]:
            ctx.user_data.pop(key, None)

    # ── WAITING_REASON handlers ──────────────────────────────────── #

    async def _on_reason_text(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        msg = update.effective_message
        if msg is None or msg.text is None:
            return WAITING_REASON
        if ctx.user_data is None:
            return WAITING_REASON

        text = msg.text.strip()
        if is_reason_too_long(text):
            try:
                await msg.reply_text(reason_too_long_text(len(text)))
            except Exception as exc:
                log.debug("%s reason-too-long reply failed: %s", self.action, exc)
            return WAITING_REASON

        ctx.user_data[self._reason_key] = text
        extra_info = ctx.user_data.get(self._extra_info_key, "")
        prompt_txt = self.proof.step_prompt(
            self._get_target(ctx), self.action, text, extra_info
        )
        prompt_chat = ctx.user_data.get(self._prompt_chat_key)
        prompt_id = ctx.user_data.get(self._prompt_id_key)
        prompt_sent = False
        if prompt_id is not None and prompt_chat is not None:
            try:
                await ctx.bot.edit_message_text(
                    prompt_txt,
                    chat_id=prompt_chat,
                    message_id=prompt_id,
                    parse_mode="HTML",
                    reply_markup=self.proof.keyboard(),
                )
                prompt_sent = True
            except Exception:
                log.exception("%s prompt edit failed (reason step)", self.action)
        else:
            try:
                await msg.reply_text(
                    prompt_txt,
                    parse_mode="HTML",
                    reply_markup=self.proof.keyboard(),
                )
                prompt_sent = True
            except Exception as exc:
                log.debug("%s reason-text fallback reply failed: %s", self.action, exc)
        if not prompt_sent:
            self._clear_user_data(ctx)
            return ConversationHandler.END
        return WAITING_PROOF

    async def _on_skip_reason(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        q = update.callback_query
        if q is None or ctx.user_data is None:
            return WAITING_REASON

        ctx.user_data[self._reason_key] = replies.NO_REASON
        extra_info = ctx.user_data.get(self._extra_info_key, "")
        prompt_txt = self.proof.step_prompt(
            self._get_target(ctx), self.action, replies.NO_REASON, extra_info
        )
        results = await asyncio.gather(
            q.answer(),
            q.edit_message_text(
                prompt_txt, parse_mode="HTML", reply_markup=self.proof.keyboard()
            ),
            return_exceptions=True,
        )
        if isinstance(results[1], BaseException):
            log.debug(
                "%s prompt edit failed (skip-reason step): %s", self.action, results[1]
            )
            # * Proof prompt is invisible; clear state to avoid locking
            # * them in WAITING_PROOF with no visible UI element to interact with.
            self._clear_user_data(ctx)
            return ConversationHandler.END
        return WAITING_PROOF

    # ── WAITING_PROOF handlers ───────────────────────────────────── #

    async def _on_proof(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        if msg is None or ctx.user_data is None:
            return WAITING_PROOF

        # * Double-submit guard: a previous _on_proof or _on_skip_proof call is
        # * already running the executor.  Discard this duplicate update silently.
        if ctx.user_data.get(self._exec_key):
            return ConversationHandler.END

        # * Album dedup: Telegram delivers each photo in a multi-photo album as a
        # * separate update.  Without this guard every photo would invoke the
        # * executor independently, producing duplicate DB records and log messages.
        # * We record the media_group_id of the first photo we process and discard
        # * any further photos from the same album.
        if msg.media_group_id:
            if ctx.user_data.get(self._mgid_key) == msg.media_group_id:
                return ConversationHandler.END
            ctx.user_data[self._mgid_key] = msg.media_group_id

        # * Set the executing flag before the first await to close the race window.
        ctx.user_data[self._exec_key] = True

        # * Fast path: only photo/video reach here via the filter. Store the
        # * Message for the proof-channel upload. The short text description
        # * from BuildProof.record() has no reader, so it stays out of
        # * user_data to keep conversation state lean.
        if msg.photo or msg.video:
            existing: list = ctx.user_data.get(self._proof_msgs_key, [])
            ctx.user_data[self._proof_msgs_key] = [*existing, msg]
        try:
            await self.executor(update, ctx)
        except BaseException:
            # * Mirror _on_skip_proof: clear state before propagating so a
            # * failed executor does not leak {action}_* keys into the next
            # * conversation. CancelledError is re-raised unchanged.
            self._clear_user_data(ctx)
            raise
        # * Clear state so the conversation does not leak keys across sessions.
        self._clear_user_data(ctx)
        return ConversationHandler.END

    async def _on_skip_proof(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        q = update.callback_query
        if q is None or ctx.user_data is None:
            return WAITING_PROOF

        # * Double-submit guard: user tapped Skip twice before the first call
        # * returned END.  Acknowledge and discard the duplicate.
        if ctx.user_data.get(self._exec_key):
            try:
                await q.answer()
            except Exception as exc:
                log.debug("%s skip-proof dup q.answer failed: %s", self.action, exc)
            return ConversationHandler.END
        ctx.user_data[self._exec_key] = True

        # * The executor may raise (DB outage, Telegram API failure on a DM,
        # * a runtime bug). The q.answer() must always run; the executor
        # * exception must NOT be silently discarded -- PTB's global error
        # * handler reports it to LOGS_ERRORS. We gather them in parallel
        # * for speed but inspect the executor result and re-raise if it
        # * failed. The q.answer() is best-effort: its failure is logged
        # * at debug and does not block the executor failure propagation.
        qa_result, exec_result = await asyncio.gather(
            q.answer(),
            self.executor(update, ctx),
            return_exceptions=True,
        )
        if isinstance(qa_result, BaseException):
            log.debug("%s skip-proof q.answer failed: %s", self.action, qa_result)
        if isinstance(exec_result, BaseException):
            # * Surface the executor failure to PTB's error handler instead
            # * of swallowing it. Re-raise after clearing state so the
            # * conversation is properly ended.
            self._clear_user_data(ctx)
            raise exec_result
        self._clear_user_data(ctx)
        return ConversationHandler.END

    # ── Cancel / fallback ────────────────────────────────────────── #

    async def _on_reason_unexpected(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Reject non-text messages during reason collection."""
        if update.effective_message:
            try:
                await update.effective_message.reply_text(
                    f"Please type your {self.action} reason as text, or press Skip / Cancel."
                )
            except Exception as exc:
                log.debug("%s reason-unexpected reply failed: %s", self.action, exc)
        return WAITING_REASON

    async def _on_proof_unexpected(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Reject unexpected message types during proof collection."""
        if update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "Please send a photo or video as proof, or press Skip / Cancel."
                )
            except Exception as exc:
                log.debug("%s proof-unexpected reply failed: %s", self.action, exc)
        return WAITING_PROOF

    async def _on_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        if q is None:
            return ConversationHandler.END

        self._clear_user_data(ctx)
        results = await asyncio.gather(
            q.answer(),
            q.edit_message_text(
                f"Got it, {self.action} cancelled. No action was taken."
            ),
            return_exceptions=True,
        )
        if isinstance(results[1], BaseException):
            log.debug(
                "%s cancel edit failed (message may already be gone): %s",
                self.action,
                results[1],
            )
        return ConversationHandler.END

    async def _on_end_conv(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        self._clear_user_data(ctx)
        if update.effective_message:
            try:
                await update.effective_message.reply_text(
                    f"{self.action.capitalize()} operation cancelled."
                )
            except Exception as exc:
                log.debug("%s cancel-via-command reply failed: %s", self.action, exc)
        return ConversationHandler.END

    # ── Build states ─────────────────────────────────────────────── #

    def build(
        self,
        entry_fn: Callable[..., Any],
        entry_filter: BaseFilter,
        escape_filter: BaseFilter | None = None,
    ) -> ConversationHandler:
        """Assemble the ConversationHandler from bound callbacks."""
        reason_state: list = [
            MessageHandler(
                filters.TEXT & ~ALL_PREFIXES_CMD_FILTER, self._on_reason_text
            ),
            CallbackQueryHandler(self._on_cancel, pattern=rf"^{self.action}_cancel$"),
            MessageHandler(
                ~filters.TEXT & ~ALL_PREFIXES_CMD_FILTER,
                self._on_reason_unexpected,
            ),
        ]
        if self.reason.skip_allowed:
            reason_state.insert(
                1,
                CallbackQueryHandler(
                    self._on_skip_reason,
                    pattern=rf"^{self.action}_skip_reason$",
                ),
            )

        proof_state = [
            MessageHandler(filters.PHOTO | filters.VIDEO, self._on_proof),
            CallbackQueryHandler(
                self._on_skip_proof, pattern=rf"^{self.action}_skip_proof$"
            ),
            CallbackQueryHandler(self._on_cancel, pattern=rf"^{self.action}_cancel$"),
            MessageHandler(
                ~filters.PHOTO & ~filters.VIDEO & ~ALL_PREFIXES_CMD_FILTER,
                self._on_proof_unexpected,
            ),
        ]

        fallback_filter = ALL_PREFIXES_CMD_FILTER
        if escape_filter is not None:
            fallback_filter = fallback_filter & ~escape_filter

        return ConversationHandler(
            entry_points=[MessageHandler(entry_filter, entry_fn)],
            states={
                WAITING_REASON: reason_state,
                WAITING_PROOF: proof_state,
            },
            fallbacks=[
                CallbackQueryHandler(
                    self._on_cancel, pattern=rf"^{self.action}_cancel$"
                ),
                MessageHandler(fallback_filter, self._on_end_conv),
            ],
            per_user=True,
            per_chat=True,
            per_message=False,
        )


def build_modaction_conv(
    reason: BuildReason,
    proof: BuildProof,
    entry_fn: Callable[..., Any],
    executor: Callable[..., Any],
    entry_filter: BaseFilter,
    escape_filter: BaseFilter | None = None,
) -> ConversationHandler:
    """Build a generic reason + proof ConversationHandler."""
    return _ModActionFlow(reason.action, reason, proof, executor).build(
        entry_fn, entry_filter, escape_filter
    )
