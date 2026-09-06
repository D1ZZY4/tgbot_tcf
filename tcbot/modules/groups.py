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

log = logging.getLogger(__name__)

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 30
_RL_CMD_LIMIT: int = 8
_RL_CB_LIMIT: int = 15

# * Telegram caps message text at ~4096 chars; _render() truncates to this
# * budget so large federations get a partial list instead of an error.
_MAX_RENDER_CHARS: int = 3800

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
    """Render the group list, truncating to fit Telegram's message limit.

    Telegram rejects messages over ~4096 chars; an unbounded render raises
    BadRequest (swallowed by callers, leaving the user with nothing) on
    large federations. Cap the body and name the remainder instead.
    """
    header = f"{bold('Connected Groups')}\n\nCount: {len(groups)}\n"
    lines = [header.rstrip("\n")]
    used = len(header)
    for i, g in enumerate(groups):
        title = g.get("title", "Unknown")
        if detailed:
            line = f"- {esc(title)} - {code(str(g.get('chat_id', 0)))}"
        else:
            line = f"- {esc(title)}"
        if used + len(line) + 1 > _MAX_RENDER_CHARS:
            lines.append(f"...and {len(groups) - i} more not shown.")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


# ────────── Command for see Connected Groups </tcgroups> ────────── #


@decorators.ratelimiter(limit=_RL_CMD_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def cmd_tcfgroups(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the list of all currently active connected groups (truncated to fit)."""
    msg = update.effective_message
    if msg is None:
        return

    try:
        groups = await db.groups_db.active_groups()
    except Exception:
        log.exception("active_groups failed during tcgroups")
        try:
            await msg.reply_text(replies.ERR_GROUPS_LOAD_FAILED)
        except Exception as exc:
            log.debug("tcgroups groups-failed reply failed: %s", exc)
        return
    if not groups:
        try:
            await msg.reply_text(f"No groups are currently connected to {_CNAME}.")
        except Exception as exc:
            log.debug("tcgroups no-groups reply failed: %s", exc)
        return

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
    # * No per-user snapshot: active_groups() is already L1+L2 cached
    # * (30 s), so every toggle reads the shared cache instead of a
    # * per-user copy with its own divergent TTL.
    # * q.answer() and active_groups() are independent; run in parallel.
    _, groups_r = await asyncio.gather(
        q.answer(), db.groups_db.active_groups(), return_exceptions=True
    )
    if isinstance(groups_r, BaseException):
        # * Never render an empty list from a failed read: during an
        # * outage that would claim "Count: 0" for a healthy federation.
        # * Keep the toggle keyboard so re-tapping retries the fetch.
        log.warning("tcgroups toggle groups fetch failed: %s", groups_r)
        await safe_edit(
            cbq_msg,  # type: ignore[arg-type]
            replies.ERR_GROUPS_LOAD_FAILED,
            reply_markup=tcgroups_kb(detailed=detailed),
        )
        return
    groups = groups_r
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
