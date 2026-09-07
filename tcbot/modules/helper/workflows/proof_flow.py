# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Proof-step infrastructure: keyboards, prompts, media recording, and channel upload."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from tcbot.modules.helper.formatter import bold

log = logging.getLogger(__name__)


# ─────────────────────────── BuildProof ─────────────────────────── #


@dataclass(frozen=True)
class BuildProof:
    """Configurable proof-step keyboard, prompts, and media recording."""

    action: str
    skip_allowed: bool = field(default=True, kw_only=True)
    skip_label: str = field(default="Skip", kw_only=True)
    cancel_label: str = field(default="Cancel", kw_only=True)

    def keyboard(self) -> InlineKeyboardMarkup:
        """Proof-step keyboard. Includes Skip only when skip_allowed is True."""
        buttons: list[InlineKeyboardButton] = []
        if self.skip_allowed:
            buttons.append(
                InlineKeyboardButton(
                    self.skip_label, callback_data=f"{self.action}_skip_proof"
                )
            )
        buttons.append(
            InlineKeyboardButton(
                self.cancel_label, callback_data=f"{self.action}_cancel"
            )
        )
        return InlineKeyboardMarkup([buttons])

    def step_prompt(
        self,
        target_mention: str,
        action_label: str,
        reason: str,
        extra_info: str = "",
    ) -> str:
        """Proof-step prompt after reason was collected in-conversation."""
        suffix = f" {extra_info}" if extra_info else ""
        skip_hint = (
            f", or tap {bold(self.skip_label)} to proceed" if self.skip_allowed else ""
        )
        return (
            f"Reason noted; {action_label.lower()}ing {target_mention}{suffix}.\n"
            f"Reason: {bold(reason)}\n\n"
            f"Got any proof? Send a photo or video{skip_hint}."
        )

    def noted_prompt(
        self,
        action_label: str,
        inline_reason: str,
        target_mention: str,
        extra_info: str = "",
    ) -> str:
        """Proof-step prompt when an inline reason was already provided."""
        suffix = f" {extra_info}" if extra_info else ""
        skip_hint = (
            f", or tap {bold(self.skip_label)} to proceed" if self.skip_allowed else ""
        )
        return (
            f"{action_label.capitalize()}ing {target_mention}{suffix}.\n"
            f"Reason: {bold(inline_reason)}\n\n"
            f"Got any proof? Send a photo or video{skip_hint}."
        )

    @staticmethod
    def record(msg: Message) -> str | None:
        """Return a short proof description from a photo/video message, or None."""
        if msg.photo:
            return f"Photo (msg {msg.message_id})"
        if msg.video:
            return f"Video (msg {msg.message_id})"
        return None


# ───────────────────────── Channel upload ───────────────────────── #


async def upload_proof(
    bot: Bot,
    msgs: list[Message],
    caption: str,
    proof_chat: int,
    proof_thread: int | None,
) -> int | None:
    """Upload proof media to the proof channel. Returns proof_message_id or None on failure."""
    # * Fast fail without Telegram I/O: empty input or an album with no
    # * photo/video item cannot produce an upload. Callers already guard on
    # * truthy proof_msgs, so this only bites on programming errors.
    if not msgs:
        return None
    try:
        if len(msgs) > 1:
            media: list[InputMediaPhoto | InputMediaVideo] = []
            first_caption_set = False
            for m in msgs:
                if m.photo:
                    cap = caption if not first_caption_set else None
                    media.append(
                        InputMediaPhoto(
                            m.photo[-1].file_id, caption=cap, parse_mode="HTML"
                        )
                    )
                    first_caption_set = True
                elif m.video:
                    cap = caption if not first_caption_set else None
                    media.append(
                        InputMediaVideo(m.video.file_id, caption=cap, parse_mode="HTML")
                    )
                    first_caption_set = True
            if not media:
                return None
            sent = await bot.send_media_group(
                proof_chat, media, message_thread_id=proof_thread
            )
            proof_msg_id = sent[0].message_id
            log.info(
                "Proof album uploaded: %d items, message_id=%s", len(sent), proof_msg_id
            )
            return proof_msg_id
        first = msgs[0]
        if first.photo:
            sent = await bot.send_photo(
                proof_chat,
                first.photo[-1].file_id,
                caption=caption,
                parse_mode="HTML",
                message_thread_id=proof_thread,
            )
            log.info("Proof photo uploaded: message_id=%s", sent.message_id)
            return sent.message_id
        if first.video:
            sent = await bot.send_video(
                proof_chat,
                first.video.file_id,
                caption=caption,
                parse_mode="HTML",
                message_thread_id=proof_thread,
            )
            log.info("Proof video uploaded: message_id=%s", sent.message_id)
            return sent.message_id
    except Exception:
        log.exception("Proof upload failed")
    return None
