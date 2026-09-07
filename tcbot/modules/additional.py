# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Additional links callback: shows official channels and groups from the start menu."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram.ext import CallbackQueryHandler, ContextTypes

from tcbot import cfg
from tcbot.modules.helper import decorators, keyboards
from tcbot.utils.formatter import bold, esc

if TYPE_CHECKING:
    from telegram import Update

log = logging.getLogger(__name__)

__module_name__ = None

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 30
_RL_CB_LIMIT: int = 15


# ─────────────────────── Additional Message ─────────────────────── #

__additional_msg__ = (
    f"{esc(cfg.community_name)} {bold('Official Links')}\n\n"
    "Use the buttons below to access our channels and groups. "
    "For developers interested in contributing to Transsion device development, "
    "join TRAVEL, an independent community for collaboration and networking."
)


# ──────────────────────── Callback Handler ──────────────────────── #


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_additional_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the Additional Info page when the button is tapped."""
    q = update.callback_query
    if q is None:
        return
    try:
        await q.answer()
    except Exception as exc:
        log.debug("additional_menu q.answer failed: %s", exc)
    try:
        await q.edit_message_text(
            __additional_msg__,
            parse_mode="HTML",
            reply_markup=keyboards.additional_menu_kb(),
        )
    except Exception as exc:
        log.debug("additional_menu edit failed: %s", exc)


# ──────────────────────────── Handlers ──────────────────────────── #

__handlers__ = [
    CallbackQueryHandler(on_additional_menu, pattern=r"^additional_menu$"),
]
