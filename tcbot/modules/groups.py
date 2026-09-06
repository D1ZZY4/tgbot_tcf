# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""tcgroups command handler: lists all connected federation groups."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler

from tcbot import cfg
from tcbot import database as db

if TYPE_CHECKING:
    from telegram import Update
from tcbot.database.documents import GroupDoc
from tcbot.modules.helper import decorators, replies
from tcbot.modules.helper.formatter import bold, code, esc
from tcbot.modules.helper.keyboards import tcgroups_kb
from tcbot.modules.helper.parse_editmsg import safe_edit
from tcbot.utils.prefixes import build_prefixed_filters
from tcbot.utils.time_and_date import monotonic

log = logging.getLogger(__name__)

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 30
_RL_CMD_LIMIT: int = 8
_RL_CB_LIMIT: int = 15

# * Per-user groups snapshot: invalidated by age so connect / disconnect /
# * cleanup / migration become visible without restarting the conversation.
_GROUPS_CACHE_TTL_S: float = 120.0

# ────────────────────── Module & Help Message ───────────────────── #

_CNAME = esc(cfg.community_name)

__module_name__ = "Groups"
__help_text__ = (
    f"Lists every group currently connected to {_CNAME}, with optional details view."
)

__help_sections__: list[tuple[str, str]] = [
    (
        replies.SEC_COMMANDS,
        f"{code('/tcgroups')} (alias: {code('/tcg')})",
    ),
    replies.who_section(replies.CONTEXT_ANYONE),
    replies.where_section(replies.CONTEXT_BOT_OR_GROUP),
    (
        replies.SEC_WHAT,
        f"Lists all groups currently connected to {_CNAME}, along with the total count.\n\n"
        f"The default view shows group names only. Tap {bold('Details')} to expand the list and show each group's chat ID alongside its name. Tap {bold('Simple')} to collapse back.",
    ),
    (
        "Example",
        f"{code('/tcgroups')} or {code('/tcg')}",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ──────────────────────── Helper Functions ──────────────────────── #


def _render(groups: list[GroupDoc], *, detailed: bool) -> str:
    lines = [f"{bold('Connected Groups')}\n\nCount: {len(groups)}\n"]
    for g in groups:
        title = g.get("title", "Unknown")
        if detailed:
            lines.append(f"- {esc(title)} - {code(str(g.get('chat_id', 0)))}")
        else:
            lines.append(f"- {esc(title)}")
    return "\n".join(lines)


# ────────── Command for see Connected Groups </tcgroups> ────────── #


@decorators.ratelimiter(limit=_RL_CMD_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def cmd_tcfgroups(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with a paginated list of all currently active connected groups."""
    msg = update.effective_message
    if msg is None:
        return

    try:
        groups = await db.groups_db.active_groups()
    except Exception:
        log.exception("active_groups failed during tcgroups")
        try:
            await msg.reply_text(
                "Could not load the group list due to a server error. Please try again."
            )
        except Exception as exc:
            log.debug("tcgroups groups-failed reply failed: %s", exc)
        return
    if not groups:
        try:
            await msg.reply_text(f"No groups are currently connected to {_CNAME}.")
        except Exception as exc:
            log.debug("tcgroups no-groups reply failed: %s", exc)
        return

    if ctx.user_data is not None:
        ctx.user_data["groups_cache"] = groups
        ctx.user_data["groups_cache_at"] = monotonic()

    try:
        await msg.reply_text(
            _render(groups, detailed=False),
            parse_mode="HTML",
            reply_markup=tcgroups_kb(detailed=False),
        )
    except Exception as exc:
        log.debug("tcgroups list reply failed: %s", exc)


# ────────────── Callback Handlers (Details & Simple) ────────────── #


async def _toggle(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, detailed: bool
) -> None:
    q = update.callback_query
    if q is None or q.message is None:
        return

    cbq_msg = q.message  # type: ignore[assignment]
    groups = None
    if ctx.user_data is not None:
        cached_at = ctx.user_data.get("groups_cache_at", 0.0)
        if monotonic() - float(cached_at) < _GROUPS_CACHE_TTL_S:
            groups = ctx.user_data.get("groups_cache")
    if groups:
        # * Answer first so an expired query surfaces immediately instead of
        # * hiding behind the edit result.
        try:
            await q.answer()
        except Exception as exc:
            log.debug("tcgroups toggle answer failed: %s", exc)
            return
        await safe_edit(
            cbq_msg,  # type: ignore[arg-type]
            _render(groups, detailed=detailed),
            reply_markup=tcgroups_kb(detailed=detailed),
        )
    else:
        # * q.answer() and active_groups() are independent; run in parallel.
        _, groups = await asyncio.gather(
            q.answer(), db.groups_db.active_groups(), return_exceptions=True
        )
        if isinstance(groups, BaseException):
            groups = []
        if ctx.user_data is not None:
            ctx.user_data["groups_cache"] = groups
            ctx.user_data["groups_cache_at"] = monotonic()
        await safe_edit(
            cbq_msg,  # type: ignore[arg-type]
            _render(groups, detailed=detailed),
            reply_markup=tcgroups_kb(detailed=detailed),
        )


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_groups_details(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch the groups listing to detailed view (shows full chat IDs)."""
    await _toggle(update, ctx, detailed=True)


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_groups_simple(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch the groups listing to simple view (condensed, no full chat IDs)."""
    await _toggle(update, ctx, detailed=False)


# ──────────────────────────── Handlers ──────────────────────────── #

_GROUPS_CMDS = build_prefixed_filters("tcgroups") | build_prefixed_filters("tcg")

__handlers__ = [
    MessageHandler(_GROUPS_CMDS, cmd_tcfgroups),
    CallbackQueryHandler(on_groups_details, pattern=r"^groups_details$"),
    CallbackQueryHandler(on_groups_simple, pattern=r"^groups_simple$"),
]
