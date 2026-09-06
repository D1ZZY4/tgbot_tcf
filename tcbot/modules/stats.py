# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""``/tcstats`` command and callback handlers: federation-wide overview and drill-downs."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from tcbot.modules.helper import decorators, replies
from tcbot.modules.helper.formatter import bold, code
from tcbot.modules.helper.parse_editmsg import safe_edit_cb
from tcbot.modules.helper.workflows.stats_flow import (
    CHAT_KEY,
    MSG_KEY,
    RESULTS_KEY,
    SEARCH_KEY,
    Stats,
)
from tcbot.utils.prefixes import ALL_PREFIXES_CMD_FILTER, build_prefixed_filters

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from telegram import CallbackQuery, InlineKeyboardMarkup, Update

log = logging.getLogger(__name__)

# ────────────────────── Module & Help Message ───────────────────── #

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 30
_RL_CMD_LIMIT: int = 8
_RL_CB_LIMIT: int = 15

__module_name__ = "Stats"
__help_text__ = (
    "Live federation overview: Founder, staff, users, active bans, and "
    "connected groups, with drill-down menus for every section."
)

__help_sections__: list[tuple[str, str]] = [
    (
        replies.SEC_COMMANDS,
        f"{code('/tcstats')} (alias: {code('/tcs')})",
    ),
    replies.who_section(replies.CONTEXT_ANYONE),
    replies.where_section(replies.CONTEXT_BOT_OR_GROUP),
    (
        replies.SEC_WHAT,
        "Shows a live federation summary: Founder, total staff broken down by "
        "role, the number of cached users, active federation bans, and "
        "connected chats.",
    ),
    (
        "Drill-downs",
        f"{bold('Staff Roster')}: Founder, Admins, Developers, Testers, all listed "
        "with mentions.\n"
        f"{bold('Users')}: paginated list of every cached user. Numbered buttons "
        "open a per-user detail card.\n"
        f"{bold('Connected Chats')}: paginated list of every active group; "
        "drill-in shows owner, ID, and connect date.\n"
        f"{bold('User Bans')}: paginated list of every active ban with a "
        f"{bold('Search')} shortcut to look up a user by name or ID.\n\n"
        f"Every view ends with a {bold('Back')} button to the main summary.",
    ),
    (
        replies.SEC_EXAMPLES,
        f"{code('/tcstats')}\n{code('/tcs')}",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ──────────────────────── Command Handlers ──────────────────────── #


@decorators.ratelimiter(limit=_RL_CMD_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the federation overview message."""
    msg = update.effective_message
    if msg is None:
        return
    text, kb = await Stats.main()
    try:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as exc:
        log.debug("cmd_stats reply failed: %s", exc)


# ──────────────────────── Callback Helpers ──────────────────────── #


def _parse_item_callback(
    q: CallbackQuery,
) -> tuple[int, int, str | None] | None:
    """Parse ``prefix:page:idx[:stable]`` item callbacks.

    Four-part callbacks carry the stable entity ID so detail views can
    reject a record shifted by a concurrent list mutation. Three-part
    callbacks from older keyboards keep working without the check.
    """
    try:
        parts = (q.data or "").split(":")
        if len(parts) == 4:
            _, page_str, idx_str, stable = parts
        else:
            _, page_str, idx_str = parts
            stable = None
        return int(page_str), int(idx_str), stable
    except ValueError:
        return None
    except IndexError:
        return None


async def _ack_and_render(
    q: CallbackQuery, data_coro: Awaitable[tuple[str, InlineKeyboardMarkup | None]]
) -> None:
    """Acknowledge the callback query and run the data coroutine in parallel, then edit.

    ``data_coro`` must be a coroutine that returns ``(text, kb)``. Gathering
    ``q.answer()`` with the DB fetch starts both simultaneously, cutting latency
    versus the old sequential pattern.
    """
    _, result = await asyncio.gather(q.answer(), data_coro, return_exceptions=True)
    if isinstance(result, BaseException):
        log.error("_ack_and_render data fetch failed: %s", result)
        return
    text, kb = result
    await safe_edit_cb(q, text, reply_markup=kb)


# ──────────────────────── Callback Handlers ─────────────────────── #


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the top-level stats menu."""
    q = update.callback_query
    if q is None:
        return
    # * q.answer() and Stats.main() are independent; run in parallel.
    await _ack_and_render(q, Stats.main())


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the current staff roster page."""
    q = update.callback_query
    if q is None:
        return
    await _ack_and_render(q, Stats.staff_roster())


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render a paginated list of cached users."""
    q = update.callback_query
    if q is None:
        return
    try:
        page = int((q.data or "").split(":")[1])
    except ValueError:
        await q.answer()
        return
    except IndexError:
        await q.answer()
        return
    await _ack_and_render(q, Stats.users_list(page))


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_user_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the detail view for a single cached user entry."""
    q = update.callback_query
    if q is None:
        return
    parsed = _parse_item_callback(q)
    if parsed is None:
        await q.answer()
        return
    page, idx, stable = parsed
    await _ack_and_render(q, Stats.user_detail(ctx.bot, page, idx, stable))


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_chats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render a paginated list of connected groups."""
    q = update.callback_query
    if q is None:
        return
    try:
        page = int((q.data or "").split(":")[1])
    except ValueError:
        await q.answer()
        return
    except IndexError:
        await q.answer()
        return
    await _ack_and_render(q, Stats.chats_list(page))


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_chat_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the detail view for a single connected group entry."""
    q = update.callback_query
    if q is None:
        return
    parsed = _parse_item_callback(q)
    if parsed is None:
        await q.answer()
        return
    page, idx, stable = parsed
    await _ack_and_render(q, Stats.chat_detail(ctx.bot, page, idx, stable))


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_bans(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render a paginated ban-list page and clear any active search state."""
    q = update.callback_query
    if q is None:
        return
    try:
        page = int((q.data or "").split(":")[1])
    except ValueError:
        await q.answer()
        return
    except IndexError:
        await q.answer()
        return
    Stats.clear_search(ctx)
    await _ack_and_render(q, Stats.bans_list(page))


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_ban_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the detail view for a single ban entry from the stats list."""
    q = update.callback_query
    if q is None:
        return
    parsed = _parse_item_callback(q)
    if parsed is None:
        await q.answer()
        return
    page, idx, stable = parsed
    await _ack_and_render(q, Stats.ban_detail(page, idx, stable))


# ── Search panel ─────────────────────────────────────────────────────


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_bans_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the user search prompt within the stats ban view."""
    q = update.callback_query
    if q is None:
        return
    # * Stats.open_search is synchronous; answer and edit run in parallel.
    text, kb = Stats.open_search(ctx, q)
    await asyncio.gather(
        q.answer(),
        safe_edit_cb(q, text, reply_markup=kb),
        return_exceptions=True,
    )


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_bans_search_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text query message handler; only reacts when the search panel is active."""
    if ctx.user_data is None or not ctx.user_data.get(SEARCH_KEY):
        return
    ctx.user_data.pop(SEARCH_KEY, None)

    # * Guard instead of assert: a search-panel input without a message object
    # * is a no-op (state was already popped above), not a crash.
    msg = update.effective_message
    if msg is None:
        return
    query = (msg.text or "").strip()

    # * Run the search and delete the user's input message in parallel.
    results, _ = await asyncio.gather(
        Stats.search_run(query),
        msg.delete(),
        return_exceptions=True,
    )
    if isinstance(results, BaseException):
        results = []

    ctx.user_data[RESULTS_KEY] = results
    ctx.user_data["stats_last_query"] = query

    chat_id = ctx.user_data.get(CHAT_KEY)
    msg_id = ctx.user_data.get(MSG_KEY)
    text, kb = await Stats.search_results(query, results)
    if chat_id is not None and msg_id is not None:
        try:
            await ctx.bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception as exc:
            log.debug("Stats search result edit failed: %s", exc)
    else:
        # * No result card to edit (message-less search panel): the query was
        # * already deleted above, so reply with the results instead of
        # * dropping them silently. The stored RESULTS_KEY keeps paging working.
        try:
            await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception as exc:
            log.debug("Stats search result fallback reply failed: %s", exc)


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_search_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the detail view for a search result selected by the user."""
    q = update.callback_query
    if q is None:
        return
    try:
        idx = int((q.data or "").split(":")[1])
    except ValueError:
        await q.answer()
        return
    except IndexError:
        await q.answer()
        return
    results = ctx.user_data.get(RESULTS_KEY, []) if ctx.user_data else []
    await _ack_and_render(q, Stats.search_detail(results, idx))


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_search_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to search results (or the open-search prompt) without re-running the query."""
    q = update.callback_query
    if q is None:
        return
    results = ctx.user_data.get(RESULTS_KEY, []) if ctx.user_data else []
    if not results:
        # * open_search is synchronous; data is already available, so answer + edit
        # * run in parallel.
        text, kb = Stats.open_search(ctx, q)
        await asyncio.gather(
            q.answer(),
            safe_edit_cb(q, text, reply_markup=kb),
            return_exceptions=True,
        )
    else:
        # * Re-render the previous results without re-running the query.
        previous_query = (
            ctx.user_data.get("stats_last_query", "") if ctx.user_data else ""
        )
        await _ack_and_render(q, Stats.search_results(previous_query, results))


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_stats_search_cancel(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """Clear the active search and return to the first page of the ban list."""
    q = update.callback_query
    if q is None:
        return
    Stats.clear_search(ctx)
    await _ack_and_render(q, Stats.bans_list(0))


# ──────────────────────────── Handlers ──────────────────────────── #

_STATS_CMDS = build_prefixed_filters("tcstats") | build_prefixed_filters("tcs")

__handlers__ = [
    MessageHandler(_STATS_CMDS, cmd_stats),
    CallbackQueryHandler(on_stats_main, pattern=r"^stats_main$"),
    CallbackQueryHandler(on_stats_admins, pattern=r"^stats_admins$"),
    CallbackQueryHandler(on_stats_users, pattern=r"^stats_users:\d+$"),
    CallbackQueryHandler(
        on_stats_user_item, pattern=r"^stats_user_item:\d+:\d+(:[^:]+)?$"
    ),
    CallbackQueryHandler(on_stats_chats, pattern=r"^stats_chats:\d+$"),
    CallbackQueryHandler(
        on_stats_chat_item, pattern=r"^stats_chat_item:\d+:\d+(:[^:]+)?$"
    ),
    CallbackQueryHandler(on_stats_bans, pattern=r"^stats_bans:\d+$"),
    CallbackQueryHandler(
        on_stats_ban_item, pattern=r"^stats_ban_item:\d+:\d+(:[^:]+)?$"
    ),
    CallbackQueryHandler(on_stats_bans_search, pattern=r"^stats_bans_search$"),
    CallbackQueryHandler(on_stats_search_item, pattern=r"^stats_search_item:\d+$"),
    CallbackQueryHandler(on_stats_search_back, pattern=r"^stats_search_back$"),
    CallbackQueryHandler(on_stats_search_cancel, pattern=r"^stats_search_cancel$"),
    # * Scoped to private chat: search input is only meaningful in PM where the
    # * user opened the bans panel. Avoids absorbing every non-command text in
    # * groups.
    MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~ALL_PREFIXES_CMD_FILTER,
        on_bans_search_input,
    ),
]
