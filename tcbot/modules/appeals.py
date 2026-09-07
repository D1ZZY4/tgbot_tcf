# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Appeal handlers: routes incoming appeals and admin review decisions."""

from __future__ import annotations

from telegram.ext import CallbackQueryHandler, filters

from tcbot.modules.helper import replies
from tcbot.modules.helper.workflows.appeal_flow import (
    LOCK_HOURS,
    appeal,
    reviewer_locked_out,
    starts_with_appeal_tag,
    text_references_log_message,
)
from tcbot.utils.formatter import bold, code, pre

# * Re-exported for backward-compatible imports.
__all__ = (
    "appeal",
    "reviewer_locked_out",
    "starts_with_appeal_tag",
    "text_references_log_message",
)


# ────────────────────── Module & Help Message ───────────────────── #

__module_name__ = "Appeal"

__help_text__ = (
    "Submit an appeal for an active federation ban. Staff review with a "
    f"{bold(f'{LOCK_HOURS}-hour priority window')} for the banning admin."
)

__help_sections__: list[tuple[str, str]] = [
    (
        "How to start",
        f"Tap the {bold('Submit Appeal')} button on your ban notification (sent by the bot in PM), "
        f"or use {code('/checkme')} and tap the appeal button that appears.",
    ),
    replies.who_section(
        "Anyone with an active federation ban. You can only have one active appeal at a time."
    ),
    (
        "Where to start",
        "Bot PM only.",
    ),
    (
        "How it works",
        f"Once the appeal flow is open, send a single message starting with {code('#appeal')} "
        "that includes all three of the following sections:\n\n"
        f"- {bold('Log link')}: the link to your ban log entry in the federation logs channel\n"
        f"- {bold('Clarification')}: your honest explanation of why the ban was issued or was a "
        "mistake\n"
        f"- {bold('Agreement')}: your commitment to follow community rules going forward",
    ),
    (
        "Format example",
        pre(
            "#appeal\n"
            "Log link: https://t.me/TranssionCoreFederationLogs/123\n"
            "Clarification: I shared links in multiple groups without reading the rules.\n"
            "Agreement: I will follow all community guidelines going forward."
        ),
    ),
    (
        "What happens next",
        "Your appeal is forwarded to TC admins for review. The admin who issued the original "
        f"ban has a {bold(f'{LOCK_HOURS}-hour priority window')} to respond; after that, any admin can act.\n\n"
        "If approved: your ban is lifted immediately across all connected groups.\n"
        "If rejected: your ban remains in place.\n"
        "You will be notified by the bot either way.",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ──────────────────────────── Handlers ──────────────────────────── #

_APPEAL_START_CMDS = filters.ChatType.PRIVATE & filters.Regex(
    r"^/start\s+appeal_[a-z0-9]{10}$"
)

__handlers__ = [
    appeal.build_handler(_APPEAL_START_CMDS),
    CallbackQueryHandler(appeal.on_decision, pattern=r"^appeal_(approve|reject)_\S+$"),
]
