# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Federation stats: overview, staff roster, users, connected chats, bans, search."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper.ban_info import build_ban_detail
from tcbot.modules.helper.extraction import (
    identity_needs_refresh,
    launch_identity_refresh,
)
from tcbot.modules.helper.formatter import bold, code, esc, mention, user_ref
from tcbot.utils.pagination import date_or_unknown, nav_row, paginate
from tcbot.utils.time_and_date import TELEGRAM_LOOKUP_TIMEOUT

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

    from tcbot.database.documents import BanDoc

_PAGE_SIZE = 6
_BTNS_PER_ROW = 3

# * Cap for stats name search: bounds the $in fetch and the per-user
# * search-result state kept in user_data.
_SEARCH_LIMIT = 30

# * Search panel state lives on ``ctx.user_data`` while the user composes a
# * query. Kept here so the runtime callback handlers and the message-input
# * fallback can share the same key set without circular imports.
SEARCH_KEY = "stats_search_active"
RESULTS_KEY = "stats_search_results"
MSG_KEY = "stats_search_msg_id"
CHAT_KEY = "stats_search_chat_id"

# ──────────────── User-facing reply constants ──────────────────── #

_ERR_USER_NOT_FOUND = "User not found in this page."
_ERR_GROUP_NOT_FOUND = "Group not found in this page."
_ERR_BAN_NOT_FOUND = "Ban record not found in this page."
_ERR_RESULT_UNAVAILABLE = "Result no longer available."


# * Strong references to in-flight background-refresh tasks; prevents GC
# * before the coroutine completes (same pattern as harvest task sets).
_refresh_tasks: set[asyncio.Task[None]] = set()


async def _refresh_group_title(bot: Bot, chat_id: int) -> None:
    """Verify a group title live and persist renames (background, best-effort)."""
    try:
        live_chat = await asyncio.wait_for(
            bot.get_chat(chat_id), timeout=TELEGRAM_LOOKUP_TIMEOUT
        )
    except Exception as exc:
        log.debug("background title check failed for %d: %s", chat_id, exc)
        return
    if live_chat is None or not live_chat.title:
        return
    try:
        await db.groups_db.refresh_group_title(chat_id, live_chat.title)
    except Exception as exc:
        log.debug("background title refresh failed for %d: %s", chat_id, exc)


def launch_group_title_refresh(bot: Bot, chat_id: int) -> None:
    """Fire-and-forget group title sync for detail views (zero added latency)."""
    try:
        task = asyncio.get_running_loop().create_task(
            _refresh_group_title(bot, chat_id)
        )
    except RuntimeError:
        log.debug("group title refresh skipped: no running event loop.")
        return
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)


def _back_main() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("« Back", callback_data="stats_main")]


# ─────────────────────── Keyboard builders ──────────────────────── #


def main_kb() -> InlineKeyboardMarkup:
    """Top-level ``/tcstats`` menu: Staff / Users / Chats / Bans drill-downs."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Staff Roster", callback_data="stats_admins"),
                InlineKeyboardButton("Users", callback_data="stats_users:0"),
            ],
            [
                InlineKeyboardButton("Connected Chats", callback_data="stats_chats:0"),
                InlineKeyboardButton("User Bans", callback_data="stats_bans:0"),
            ],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    """Single ``« Back`` returning to the stats main menu."""
    return InlineKeyboardMarkup([_back_main()])


def _list_kb(
    page: int,
    total_pages: int,
    n_items: int,
    cb_prefix: str,
    item_cb_prefix: str,
    *,
    extra_row: list[InlineKeyboardButton] | None = None,
    item_ids: list[str] | None = None,
) -> InlineKeyboardMarkup:
    """Compose nav + numbered detail buttons + optional extra row + back.

    ``item_ids`` carries one stable entity ID per button (user ID, chat ID,
    or ban ID). Detail handlers verify the resolved record still carries
    that ID so a list mutation between render and tap cannot silently show
    a different record. Older buttons without the segment keep working.
    """
    rows: list[list[InlineKeyboardButton]] = []
    nav = nav_row(page, total_pages, cb_prefix)
    if nav:
        rows.append(nav)

    def _callback(i: int) -> str:
        base = f"{item_cb_prefix}:{page}:{i}"
        if item_ids is not None and i < len(item_ids):
            return f"{base}:{item_ids[i]}"
        return base

    num_btns = [
        InlineKeyboardButton(str(i + 1), callback_data=_callback(i))
        for i in range(n_items)
    ]
    rows.extend(
        num_btns[i : i + _BTNS_PER_ROW] for i in range(0, len(num_btns), _BTNS_PER_ROW)
    )

    if extra_row:
        rows.append(extra_row)
    rows.append(_back_main())
    return InlineKeyboardMarkup(rows)


# ────────────────────────── Stats class ─────────────────────────── #


class Stats:
    """All view builders for ``/tcstats``.

    The class is the single integration point for federation statistics: main
    overview, staff roster, member roster, connected chats, active bans, and
    the search panel. Every method is a classmethod returning ``(text, markup)``
    so callers can ``await q.answer()`` and ``safe_edit_cb`` without further
    work.
    """

    PAGE_SIZE = _PAGE_SIZE

    # ── Main overview ────────────────────────────────────────────────────

    @classmethod
    async def main(cls) -> tuple[str, InlineKeyboardMarkup]:
        """Federation overview: Founder, staff total, user cache, bans, chats."""
        (
            owner_id,
            admin_count,
            developers,
            testers,
            ban_count,
            group_count,
            user_count,
        ) = await asyncio.gather(
            db.users_roles.get_owner_id(),
            db.users_roles.admin_count(),
            db.users_roles.all_by_role("developer"),
            db.users_roles.all_by_role("tester"),
            db.bans_db.active_ban_count(),
            db.groups_db.active_group_count(),
            db.users_cache.total_users(),
            return_exceptions=True,
        )
        owner_id = (
            0 if isinstance(owner_id, BaseException) else cast("int | None", owner_id)
        )
        admin_count = (
            0 if isinstance(admin_count, BaseException) else cast("int", admin_count)
        )
        developers = (
            [] if isinstance(developers, BaseException) else cast("list", developers)
        )
        testers = [] if isinstance(testers, BaseException) else cast("list", testers)
        if isinstance(ban_count, BaseException):
            ban_count = 0
        if isinstance(group_count, BaseException):
            group_count = 0
        if isinstance(user_count, BaseException):
            user_count = 0

        # Fetch owner mention data in parallel with building the response
        if owner_id:
            owner_id_int = cast("int", owner_id)
            try:
                owner_fname, owner_uname = await db.users_cache.get_user_mention_data(
                    owner_id_int
                )
            except Exception as exc:
                log.debug(
                    "stats main: get_user_mention_data failed for owner %d: %s",
                    owner_id_int,
                    exc,
                )
                owner_fname, owner_uname = str(owner_id_int), None
            owner_line = mention(owner_id_int, owner_fname, owner_uname)
        else:
            owner_line = "Not set"

        staff_total = (
            (1 if owner_id else 0) + admin_count + len(developers) + len(testers)
        )

        text = (
            f"{bold(esc(cfg.community_name))} {bold('Stats')}\n\n"
            f"Founder: {owner_line}\n"
            f"Staff: {bold(str(staff_total))} "
            f"(Admins {admin_count}, Devs {len(developers)}, Testers {len(testers)})\n"
            f"Users tracked: {bold(str(user_count))}\n"
            f"Active bans: {bold(str(ban_count))}\n"
            f"Connected chats: {bold(str(group_count))}"
        )
        return text, main_kb()

    # ── Staff roster ─────────────────────────────────────────────────────

    @classmethod
    async def staff_roster(cls) -> tuple[str, InlineKeyboardMarkup]:
        """Full staff breakdown: Founder, Admins, Developers, Testers."""
        owner_id, admins, developers, testers = await asyncio.gather(
            db.users_roles.get_owner_id(),
            db.users_roles.all_admins(),
            db.users_roles.all_by_role("developer"),
            db.users_roles.all_by_role("tester"),
            return_exceptions=True,
        )
        if isinstance(owner_id, BaseException):
            owner_id = None
        if isinstance(admins, BaseException):
            admins = []
        if isinstance(developers, BaseException):
            developers = [] if isinstance(developers, BaseException) else developers
        if isinstance(testers, BaseException):
            testers = [] if isinstance(testers, BaseException) else testers

        # * Resolve user mention data in one batch query instead of individual queries
        all_user_ids = []
        owner_idx = None
        owner_id_int = 0
        if owner_id:
            owner_id_int = cast("int", owner_id)
            owner_idx = 0
            all_user_ids.append(owner_id_int)
        all_user_ids.extend(a.get("user_id", 0) for a in admins)
        all_user_ids.extend(d.get("user_id", 0) for d in developers)
        all_user_ids.extend(t.get("user_id", 0) for t in testers)

        # Single batch query for all users
        mention_data_map = await db.users_cache.get_mention_data_batch(all_user_ids)

        lines = [f"{bold('Staff Roster')} - {esc(cfg.community_name)}\n"]

        if owner_idx is not None:
            lines.append(bold("Founder"))
            owner_fname, owner_uname = mention_data_map[owner_id_int]
            lines.append(f"- {mention(owner_id_int, owner_fname, owner_uname)}\n")

        def _section(label: str, docs: list) -> None:
            lines.append(bold(f"{label} ({len(docs)})"))
            if docs:
                for doc in docs:
                    uid = doc.get("user_id", 0)
                    fname, uname = mention_data_map[uid]
                    lines.append(f"- {mention(uid, fname, uname)}")
            else:
                lines.append("- No staff assigned")
            lines.append("")

        _section("Admins", admins)
        _section("Developers", developers)
        _section("Testers", testers)

        return "\n".join(lines).rstrip(), back_kb()

    # ── Users drill-down ─────────────────────────────────────────────────

    @classmethod
    async def users_list(cls, page: int) -> tuple[str, InlineKeyboardMarkup]:
        """Paginated list of every cached user."""
        # * Server-side count + page fetch: the 200-doc cap in all_users()
        # * made deeper pages unreachable; page from the full collection.
        total = await db.users_cache.total_users()
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        chunk = await db.users_cache.all_users_page(
            skip=page * _PAGE_SIZE, limit=_PAGE_SIZE
        )

        if total == 0:
            text = (
                f"{bold('Users')}\n\nNo cached users yet. The bot caches users "
                "as it sees them across connected groups."
            )
            return text, back_kb()

        lines = [f"{bold('Users')} - {total} total - page {page + 1}/{total_pages}\n"]
        base_idx = page * _PAGE_SIZE
        for i, u in enumerate(chunk, start=1):
            uid = u.get("user_id", 0)
            fname = u.get("first_name") or str(uid)
            uname = u.get("username")
            lines.append(f"{base_idx + i}. {user_ref(uid, fname, uname)}")

        return "\n".join(lines), _list_kb(
            page,
            total_pages,
            len(chunk),
            cb_prefix="stats_users",
            item_cb_prefix="stats_user_item",
            item_ids=[str(u.get("user_id", 0)) for u in chunk],
        )

    @classmethod
    async def user_detail(
        cls, bot: Bot, page: int, idx: int, stable: str | None = None
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Detail card for a single cached user, with a link back into the list page."""
        chunk = await db.users_cache.all_users_page(
            skip=page * _PAGE_SIZE, limit=_PAGE_SIZE
        )
        if idx < 0 or idx >= len(chunk):
            text = _ERR_USER_NOT_FOUND
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data=f"stats_users:{page}")]]
            )
            return text, kb

        u = chunk[idx]
        uid = u.get("user_id", 0)
        if stable is not None and str(uid) != stable:
            text = _ERR_USER_NOT_FOUND
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data=f"stats_users:{page}")]]
            )
            return text, kb
        fname = u.get("first_name") or str(uid)
        uname = u.get("username")
        last_name = u.get("last_name") or "-"
        # * Stale-while-revalidate: render instantly from cache; refresh in
        # * background so the next view is current. Zero added latency.
        if identity_needs_refresh(u):
            launch_identity_refresh(bot, uid)
        commit = date_or_unknown(u.get("commit_date"))
        seen = date_or_unknown(u.get("last_updated"))

        text = (
            f"{bold('User Details')}\n\n"
            f"Name: {mention(uid, fname, uname)}\n"
            f"ID: {code(str(uid))}\n"
            f"Username: {('@' + esc(uname)) if uname else '-'}\n"
            f"Last name: {esc(str(last_name))}\n\n"
            f"First seen: {commit}\n"
            f"Last seen: {seen}\n\n"
            f"Use {code(f'/check {uid}')} for the full profile."
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Back", callback_data=f"stats_users:{page}")]]
        )
        return text, kb

    # ── Connected chats drill-down ───────────────────────────────────────

    @classmethod
    async def chats_list(cls, page: int) -> tuple[str, InlineKeyboardMarkup]:
        """Paginated list of every active connected group."""
        groups = await db.groups_db.active_groups()
        chunk, total_pages, page = paginate(groups, page, _PAGE_SIZE)

        if not groups:
            text = f"{bold('Connected Chats')}\n\nNo connected groups yet."
            return text, back_kb()

        lines = [
            f"{bold('Connected Chats')} - {len(groups)} total - page {page + 1}/{total_pages}\n"
        ]
        base_idx = page * _PAGE_SIZE
        for i, grp in enumerate(chunk, start=1):
            lines.append(
                f"{base_idx + i}. {esc(grp.get('title', 'Unknown'))} "
                f"- {code(str(grp.get('chat_id', 0)))}"
            )

        return "\n".join(lines), _list_kb(
            page,
            total_pages,
            len(chunk),
            cb_prefix="stats_chats",
            item_cb_prefix="stats_chat_item",
            item_ids=[str(grp.get("chat_id", 0)) for grp in chunk],
        )

    @classmethod
    async def chat_detail(
        cls, bot: Bot, page: int, idx: int, stable: str | None = None
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Detail card for a connected group."""
        groups = await db.groups_db.active_groups()
        chunk, _total, page = paginate(groups, page, _PAGE_SIZE)
        if idx < 0 or idx >= len(chunk):
            text = _ERR_GROUP_NOT_FOUND
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data=f"stats_chats:{page}")]]
            )
            return text, kb

        grp = chunk[idx]
        chat_id = grp.get("chat_id", 0)
        if stable is not None and str(chat_id) != stable:
            text = _ERR_GROUP_NOT_FOUND
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data=f"stats_chats:{page}")]]
            )
            return text, kb
        title = grp.get("title", "Unknown")
        # * Stale-while-revalidate: render instantly; renames persist in
        # * background for the next view. Zero added latency.
        launch_group_title_refresh(bot, chat_id)
        added_by = grp.get("added_by", 0)
        adder_fname, adder_uname = await db.users_cache.get_user_mention_data(added_by)
        date_str = date_or_unknown(grp.get("added_date"))

        text = (
            f"{bold('Group Details')}\n\n"
            f"Name: {bold(esc(title))}\n"
            f"Chat ID: {code(str(chat_id))}\n\n"
            f"Connected by: {mention(added_by, adder_fname, adder_uname)}\n"
            f"Date: {date_str}"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Back", callback_data=f"stats_chats:{page}")]]
        )
        return text, kb

    # ── Bans drill-down ──────────────────────────────────────────────────

    @classmethod
    async def bans_list(cls, page: int) -> tuple[str, InlineKeyboardMarkup]:
        """Paginated list of every active federation ban."""
        # * Server-side count + page fetch: only the visible slice travels
        # * over the wire regardless of federation size (no full-list load).
        total = await db.bans_db.active_ban_count()
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        chunk = await db.bans_db.active_bans_page(page * _PAGE_SIZE, _PAGE_SIZE)

        if total == 0:
            text = f"{bold('User Bans')}\n\nNo active federation bans."
            return text, back_kb()

        # * Pre-resolve banned-user names with batch query
        uids = [b.get("banned_user_id", 0) for b in chunk]
        fname_map = await db.users_cache.get_first_names_batch(uids) if uids else {}

        lines = [
            f"{bold('User Bans')} - {total} total - page {page + 1}/{total_pages}\n"
        ]
        base_idx = page * _PAGE_SIZE
        for i, ban in enumerate(chunk, start=1):
            uid = ban.get("banned_user_id", 0)
            fname = fname_map.get(uid, str(uid))
            lines.append(f"{base_idx + i}. {esc(fname)} - {code(str(uid))}")

        search_row = [
            InlineKeyboardButton("Search", callback_data="stats_bans_search"),
        ]
        return "\n".join(lines), _list_kb(
            page,
            total_pages,
            len(chunk),
            cb_prefix="stats_bans",
            item_cb_prefix="stats_ban_item",
            extra_row=search_row,
            item_ids=[str(ban.get("ban_id", "")) for ban in chunk],
        )

    @classmethod
    async def ban_detail(
        cls, page: int, idx: int, stable: str | None = None
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Detail card for a banned user, reusing ``build_ban_detail``.

        When the button carries the stable ban ID, the record is fetched
        directly by ID (one indexed read) instead of re-reading the whole
        active-ban list: faster and immune to list shifts between render
        and tap. Inactive or missing records still report not-found, matching
        the list-derived path. Buttons without the stable segment keep the
        legacy list-index lookup.
        """
        if stable is not None:
            ban = await db.bans_db.get_ban(stable)
            if not ban or not ban.get("is_active"):
                text = _ERR_BAN_NOT_FOUND
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "« Back", callback_data=f"stats_bans:{page}"
                            )
                        ]
                    ]
                )
                return text, kb
            text, proof_link = await build_ban_detail(ban)
            rows: list[list[InlineKeyboardButton]] = []
            if proof_link:
                rows.append([InlineKeyboardButton("View Proof", url=proof_link)])
            rows.append(
                [InlineKeyboardButton("« Back", callback_data=f"stats_bans:{page}")]
            )
            return text, InlineKeyboardMarkup(rows)
        # * Legacy list-index lookup: fetch only the tapped page
        # * server-side instead of the whole active-ban list.
        chunk = await db.bans_db.active_bans_page(page * _PAGE_SIZE, _PAGE_SIZE)
        if idx < 0 or idx >= len(chunk):
            text = _ERR_BAN_NOT_FOUND
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data=f"stats_bans:{page}")]]
            )
            return text, kb
        ban = chunk[idx]
        text, proof_link = await build_ban_detail(ban)
        rows: list[list[InlineKeyboardButton]] = []
        if proof_link:
            rows.append([InlineKeyboardButton("View Proof", url=proof_link)])
        rows.append(
            [InlineKeyboardButton("« Back", callback_data=f"stats_bans:{page}")]
        )
        return text, InlineKeyboardMarkup(rows)

    # ── Search panel ─────────────────────────────────────────────────────

    @staticmethod
    def _search_panel_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Cancel", callback_data="stats_search_cancel")]]
        )

    @staticmethod
    def _search_results_kb(n: int) -> InlineKeyboardMarkup:
        num_btns = [
            InlineKeyboardButton(str(i + 1), callback_data=f"stats_search_item:{i}")
            for i in range(n)
        ]
        rows: list[list[InlineKeyboardButton]] = [
            num_btns[i : i + _BTNS_PER_ROW]
            for i in range(0, len(num_btns), _BTNS_PER_ROW)
        ]
        rows.append(
            [InlineKeyboardButton("New Search", callback_data="stats_bans_search")]
        )
        rows.append(
            [InlineKeyboardButton("Cancel", callback_data="stats_search_cancel")]
        )
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _search_detail_kb(proof_link: str | None = None) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if proof_link:
            rows.append([InlineKeyboardButton("View Proof", url=proof_link)])
        rows.append([InlineKeyboardButton("« Back", callback_data="stats_search_back")])
        return InlineKeyboardMarkup(rows)

    @classmethod
    def open_search(
        cls, ctx: ContextTypes.DEFAULT_TYPE, q: CallbackQuery
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Open the search prompt; remember chat/message so input edits the right card.

        When the callback carries no accessible message (inline-message edge),
        the prompt still renders but no card IDs are stored, so a later search
        input degrades to a no-op edit instead of crashing on ``None``.
        """
        text = f"{bold('Search User Bans')}\n\nSend a name or user ID in the chat."
        msg = q.message
        if not isinstance(msg, Message):
            return text, cls._search_panel_kb()
        ud = cast("dict[str, object]", ctx.user_data)
        ud[SEARCH_KEY] = True
        ud[MSG_KEY] = msg.message_id
        ud[CHAT_KEY] = msg.chat_id
        return text, cls._search_panel_kb()

    @staticmethod
    def clear_search(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Forget any in-flight search context."""
        ud = cast("dict[str, object]", ctx.user_data)
        for key in (SEARCH_KEY, RESULTS_KEY, MSG_KEY, CHAT_KEY, "stats_last_query"):
            ud.pop(key, None)

    @classmethod
    async def search_run(
        cls,
        query: str,
    ) -> list[BanDoc]:
        """Resolve a search query against active bans (ID or name match).

        Name matching is server-side: an anchored prefix lookup in the
        member cache (same semantics as target resolution), capped at
        ``_SEARCH_LIMIT`` hits, then a single ``$in`` fetch of their
        active bans. Never loads the whole ban list.
        """
        q = query.strip()
        if q.isdigit():
            ban = await db.bans_db.get_active_ban(int(q))
            return [ban] if ban else []

        matches = await db.users_cache.search_by_name(q, limit=_SEARCH_LIMIT)
        if not matches:
            return []
        uids = [u.get("user_id", 0) for u in matches if u.get("user_id")]
        return await db.bans_db.active_bans_for_users(uids)

    @classmethod
    async def search_results(
        cls,
        query: str,
        results: list[BanDoc],
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Render search results: empty state or numbered hits."""
        if not results:
            text = f'{bold("Search:")} "{esc(query)}"\n\nNo results found.'
            return text, cls._search_results_kb(0)

        # Batch query for all user names
        uids = [b.get("banned_user_id", 0) for b in results]
        fname_map = await db.users_cache.get_first_names_batch(uids)
        lines = [f'{bold("Search:")} "{esc(query)}" ({len(results)} found)\n']
        for i, ban in enumerate(results, start=1):
            uid = ban.get("banned_user_id", 0)
            fname = fname_map.get(uid, str(uid))
            lines.append(f"{i}. {esc(fname)} - {code(str(uid))}")
        return "\n".join(lines), cls._search_results_kb(len(results))

    @classmethod
    async def search_detail(
        cls, results: list[BanDoc], idx: int
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Detail card for a single search hit."""
        if idx < 0 or idx >= len(results):
            text = _ERR_RESULT_UNAVAILABLE
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data="stats_search_back")]]
            )
            return text, kb
        text, proof_link = await build_ban_detail(results[idx])
        return text, cls._search_detail_kb(proof_link)


__all__ = ("CHAT_KEY", "MSG_KEY", "RESULTS_KEY", "SEARCH_KEY", "Stats")
