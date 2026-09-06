# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Privacy summary and full privacy-policy menu callbacks."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from telegram.ext import CallbackQueryHandler, ContextTypes

from tcbot import cfg
from tcbot.modules.helper import decorators, keyboards
from tcbot.modules.helper.formatter import bold, esc

if TYPE_CHECKING:
    from telegram import Update

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 30
_RL_CB_LIMIT: int = 15

__module_name__ = None


# ──────────────────────── Privacy Messages ──────────────────────── #

_CNAME = esc(cfg.community_name)


def _privacy_msg(botname: str) -> str:
    """Build the data-collection notice for the given bot display name."""
    return (
        f"{bold('Privacy and Data')}\n\n"
        f"{botname} stores a small amount of data to run the federation. "
        "Here is what that includes:\n\n"
        f"- {bold('User ID and name')}: recorded when you interact with the bot or any connected group.\n"
        f"- {bold('Ban records')}: if you receive a federation ban, the reason, proof, and issuing admin are stored.\n"
        f"- {bold('Warn and mute records')}: logged per group for moderation tracking.\n"
        f"- {bold('Kick logs')}: kept for staff reference.\n"
        f"- {bold('Appeal submissions')}: your messages and any attachments you send through the appeal system.\n\n"
        f"All stored data is accessible only to {_CNAME} staff. "
        "Nothing is shared with third parties.\n\n"
        f"Tap {bold('Privacy Policy')} below for the full policy, broken down by section."
    )


def _privacy_policy_index_msg(botname: str) -> str:
    """Build the privacy policy section index page."""
    return (
        f"{bold('Privacy Policy')}\n"
        f"{botname}\n\n"
        "Select a section below to read it in full. "
        "Use the back button to return here at any time."
    )


# * Section content does not include botname (uses _CNAME only) so it can
# * be defined at module level and reused across handler calls.
_PRIVACY_POLICY_SECTIONS: list[tuple[str, str]] = [
    (
        "What We Collect",
        f"When you interact with a connected group or this bot, the following is stored:\n\n"
        f"- {bold('User ID')}: your numeric Telegram user ID.\n"
        f"- {bold('Name and username')}: your first name and @username at the time of interaction.\n"
        f"- {bold('Ban records')}: reason, proof link, issuing admin, and timestamps.\n"
        f"- {bold('Appeal submissions')}: your appeal message and any supporting files you send.\n"
        f"- {bold('Warn and mute records')}: logged per group for moderation purposes.\n"
        f"- {bold('Kick logs')}: recorded for staff reference and audit.",
    ),
    (
        "Why We Collect It",
        f"Everything stored is used solely for {_CNAME} federation moderation: "
        "keeping connected groups safe, enforcing bans consistently across all groups, "
        "and maintaining an auditable record of moderation actions.\n\n"
        "We do not collect or use data for advertising, profiling, or any purpose "
        "outside of moderation.",
    ),
    (
        "Who Can Access It",
        f"Only {_CNAME} staff (Admins and the Founder) have access to stored data. "
        "Access is restricted by role and is not granted to regular members.\n\n"
        "No data is sold, rented, or shared with third parties under any circumstances.",
    ),
    (
        "How Long We Keep It",
        "Ban records are kept indefinitely as part of the federation log, to support "
        "cross-group enforcement and appeal review.\n\n"
        "Cached identity data (name, username) is updated on each interaction and may be "
        "pruned if a user has had no activity for an extended period.\n\n"
        "Appeal records are retained after resolution for reference and audit.",
    ),
    (
        "Your Rights",
        f"You can request a review or deletion of your personal data by reaching out to a "
        f"{_CNAME} Admin or the Founder directly.\n\n"
        "Requests are handled on a best-effort basis. Active ban records may be retained "
        "even after a deletion request if they are required for ongoing enforcement or "
        "federation integrity.",
    ),
    (
        "Contact",
        f"Reach {_CNAME} staff through:\n\n"
        f"- The main {_CNAME} discussion group.\n"
        f"- This bot's appeal system (for ban-related matters).\n\n"
        "For general data inquiries, contact a staff member directly in the main group.",
    ),
]

_POLICY_SECTION_LABELS: list[str] = [label for label, _ in _PRIVACY_POLICY_SECTIONS]


# ──────────────────────── Callback Handlers ─────────────────────── #


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_privacy_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the data-collection privacy notice when the Privacy button is tapped."""
    q = update.callback_query
    if q is None:
        return

    botname = esc(ctx.bot.first_name or "This bot")
    # * q.answer() and edit are independent; run in parallel.
    await asyncio.gather(
        q.answer(),
        q.edit_message_text(
            _privacy_msg(botname),
            parse_mode="HTML",
            reply_markup=keyboards.privacy_kb(),
        ),
        return_exceptions=True,
    )


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_privacy_policy_menu(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """Render the privacy policy section index when Privacy Policy is tapped."""
    q = update.callback_query
    if q is None:
        return

    botname = esc(ctx.bot.first_name or "This bot")
    # * q.answer() and edit are independent; run in parallel.
    await asyncio.gather(
        q.answer(),
        q.edit_message_text(
            _privacy_policy_index_msg(botname),
            parse_mode="HTML",
            reply_markup=keyboards.privacy_policy_sections_kb(_POLICY_SECTION_LABELS),
        ),
        return_exceptions=True,
    )


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_privacy_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render a single privacy policy section."""
    q = update.callback_query
    if q is None or not q.data:
        return

    try:
        idx = int(q.data[len("privacy_section_") :])
    except ValueError:
        # * Slicing never raises IndexError; only non-numeric tails land here.
        await q.answer("Invalid section.", show_alert=True)
        return
    if idx < 0 or idx >= len(_PRIVACY_POLICY_SECTIONS):
        await q.answer("Section not found.", show_alert=True)
        return

    label, content = _PRIVACY_POLICY_SECTIONS[idx]
    body = f"{bold(label)}\n\n{content}"
    # * q.answer() and edit are independent; run in parallel.
    await asyncio.gather(
        q.answer(),
        q.edit_message_text(
            body,
            parse_mode="HTML",
            reply_markup=keyboards.back_to_privacy_policy_kb(),
        ),
        return_exceptions=True,
    )


# ──────────────────────────── Handlers ──────────────────────────── #

__handlers__ = [
    CallbackQueryHandler(on_privacy_menu, pattern=r"^privacy_menu$"),
    CallbackQueryHandler(on_privacy_policy_menu, pattern=r"^privacy_policy_menu$"),
    CallbackQueryHandler(on_privacy_section, pattern=r"^privacy_section_\d+$"),
]
