# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""About callback: shows the community description from the start menu."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from telegram.ext import CallbackQueryHandler, ContextTypes

from tcbot import cfg
from tcbot.modules.helper import decorators, keyboards
from tcbot.utils.formatter import bold, esc, italic

if TYPE_CHECKING:
    from telegram import Update

__module_name__ = None

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 30
_RL_CB_LIMIT: int = 15


# ────────────────────────── About Message ───────────────────────── #

# * Raw community name; bold()/italic()/esc() at each use site escape it.
# * Pre-escaping here would double-escape inside bold()/italic() (both escape).
_CNAME = cfg.community_name

__about_msg__ = (
    f"{bold(_CNAME)}\n\n"
    f"A community-driven federation for Infinix, Tecno, and Itel device groups. "
    f"The focus is straightforward: keep connected groups safe, well-moderated, and "
    "free of spam, scams, and bad actors.\n\n"
    f"{bold('How it works')}\n"
    f"Groups that join the federation share a single moderation layer. "
    "A ban issued in one connected group is applied across all of them automatically. "
    "The same goes for mutes and broadcasts from TC Staff.\n\n"
    f"{bold('History')}\n"
    "Founded in 2024 under the name TFI, which was later disbanded following internal "
    f"issues. {esc(_CNAME)} was formed shortly after to continue the work with a cleaner structure "
    "and better long-term stability.\n\n"
    f"{italic(f'{_CNAME} is not affiliated with or endorsed by Transsion Holdings. This is an independent community.')}"
)


# ──────────────────────── Callback Handler ──────────────────────── #


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_about_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the About page when the About button is tapped."""
    q = update.callback_query
    if q is None:
        return

    # * q.answer() and edit are independent; run in parallel.
    await asyncio.gather(
        q.answer(),
        q.edit_message_text(
            __about_msg__,
            parse_mode="HTML",
            reply_markup=keyboards.back_to_start_kb(),
        ),
        return_exceptions=True,
    )


# ──────────────────────────── Handlers ──────────────────────────── #

__handlers__ = [
    CallbackQueryHandler(on_about_menu, pattern=r"^about_menu$"),
]
