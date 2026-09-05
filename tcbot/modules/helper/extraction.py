# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Target extraction helpers: extract_target()."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tcbot import database as db
from tcbot.modules.helper.identity import ANONYMOUS_BOT_ID, TELEGRAM_USER_ID
from tcbot.utils.time_and_date import TELEGRAM_LOOKUP_TIMEOUT

if TYPE_CHECKING:
    from telegram import Bot, Chat, ChatFullInfo, Message, Update, User

log = logging.getLogger(__name__)

# * Telegram lookups are wrapped in wait_for so a stalled API call never blocks
# * the user-facing reply. Per-call budget is the shared project-wide value.
_GET_CHAT_TIMEOUT = TELEGRAM_LOOKUP_TIMEOUT
# * Overall deadline for the per-group get_chat_member sweep in
# * resolve_user_identity: bounds latency on large federations.
_RESOLVE_SWEEP_TIMEOUT = 15.0


async def _safe_get_chat(bot: Bot, ident: str | int) -> Chat | ChatFullInfo | None:
    """Call ``bot.get_chat`` with a bounded timeout; returns ``None`` on failure."""
    try:
        return await asyncio.wait_for(bot.get_chat(ident), timeout=_GET_CHAT_TIMEOUT)
    except Exception as exc:
        log.debug("get_chat(%s) failed: %s", ident, exc)
        return None


# ──────────────────────── Target resolution ─────────────────────── #


async def _best_name(uid: int, *primary: str | None) -> str:
    """Pick the first non-empty/non-numeric primary name; fall back to cache, then str(uid).

    Returns the raw numeric ID string rather than a decorated ``User <id>``
    form so that callers using ``user_ref()`` or the ``mention() - code(id)``
    pattern can detect a numeric fallback and avoid displaying the ID twice.
    """
    for cand in primary:
        if cand and not cand.lstrip("-").isdigit():
            return cand
    # * Try the member cache before resorting to a bare numeric ID.
    cached = await db.users_cache.get_first_name(uid, "")
    if cached and not cached.lstrip("-").isdigit():
        return cached
    return str(uid)


async def extract_target(
    update: Update,
    args: list[str],
    bot: Bot | None = None,
) -> tuple[int, str] | tuple[None, None]:
    """Return (user_id, first_name) resolved from reply, args, entity, or mention.

    Priority order:
    1. Reply (most common use case)
    2. Args with full info (numeric ID or @username)
    3. Args with partial info (search users_cache by name)
    4. Text mention entity
    5. @Mention entity

    The returned name is always a human-readable string. When Telegram returns
    no first_name and the member cache has no entry, falls back to the bare
    numeric ID string (``str(uid)``) so callers using ``user_ref()`` can detect
    the numeric fallback and avoid displaying the ID twice.
    """
    msg: Message | None = update.effective_message
    if msg is None:
        return None, None

    # * Priority 1: Reply target (most common use case)
    # * Skip GroupAnonymousBot (id 1087968824): it is a Telegram pseudo-user
    # * that appears as `from_user` when an anonymous admin sends a message.
    # * Targeting it would attempt to act on the placeholder, not a real user.
    # * Similarly skip the Telegram service account (777000) and channel posts.
    if msg.reply_to_message:
        target_msg = msg.reply_to_message
        _skip_sender_chat = False
        if target_msg.from_user:
            u: User = target_msg.from_user
            if u.id not in (ANONYMOUS_BOT_ID, TELEGRAM_USER_ID):
                return u.id, u.first_name or await _best_name(u.id)
            # * When from_user is GroupAnonymousBot (1087968824), sender_chat is the
            # * group itself (not an individual user). Returning it as the target would
            # * cause downstream fan-out to try to ban a group ID from itself, which
            # * always fails. Skip sender_chat so we fall through to args/entities.
            if u.id == ANONYMOUS_BOT_ID:
                _skip_sender_chat = True

        if not _skip_sender_chat and target_msg.sender_chat:
            c: Chat = target_msg.sender_chat
            return c.id, c.title or await _best_name(c.id)

    # * Priority 2 & 3: Explicit args (full ID/username or partial name search)
    if args:
        arg = args[0].lstrip("@")

        # * Priority 2a: Numeric ID
        if arg.lstrip("-").isdigit():
            uid = int(arg)
            chat_first: str | None = None
            chat_username: str | None = None
            if bot:
                chat = await _safe_get_chat(bot, uid)
                if chat is not None:
                    chat_first = chat.first_name
                    chat_username = chat.username
            return uid, await _best_name(uid, chat_first, chat_username)

        # * Priority 2b: @username lookup
        if bot and arg:
            chat = await _safe_get_chat(bot, f"@{arg}")
            if chat is not None:
                return chat.id, await _best_name(
                    chat.id, chat.first_name, chat.username, arg
                )

        # * Priority 3: Partial name search in users_cache
        # * Uses a server-side regex query capped at 5 results; avoids loading
        # * the entire user cache into Python for a linear scan.
        if arg:
            matches = await db.users_cache.search_by_name(arg)
            if matches:
                user = matches[0]
                uid = user.get("user_id")
                if uid:
                    return uid, user.get("first_name") or await _best_name(uid)

    # * Priority 4: Text mention entity
    for ent in msg.entities or []:
        if ent.type == "text_mention" and ent.user:
            u = ent.user
            if u.id in (ANONYMOUS_BOT_ID, TELEGRAM_USER_ID):
                continue
            return u.id, u.first_name or await _best_name(u.id)

    # * Priority 5: @Mention entity
    if bot:
        # * Telegram entity offsets are UTF-16 code units, not Python str
        # * indices. Slicing the str directly misaligns past any emoji
        # * (surrogate pair) before the mention and may resolve the wrong
        # * username. Slice the UTF-16-LE bytes instead; a split surrogate
        # * fails to decode and the entity is skipped (fail closed).
        raw = (msg.text or "").encode("utf-16-le")
        for ent in msg.entities or []:
            if ent.type == "mention":
                try:
                    uname = raw[
                        (ent.offset + 1) * 2 : (ent.offset + ent.length) * 2
                    ].decode("utf-16-le")
                except UnicodeDecodeError:
                    continue
                except ValueError:
                    continue
                if uname.lstrip("@") in (
                    "GroupAnonymousBot",
                    "Telegram",
                ):
                    continue
                chat = await _safe_get_chat(bot, f"@{uname}")
                if chat is not None:
                    return chat.id, await _best_name(chat.id, chat.first_name, uname)

    return None, None


async def _fetch_live_identity(
    bot: Bot, target_id: int
) -> tuple[str, str | None, str | None] | None:
    """Fetch a fresh (first_name, username, last_name) triple from Telegram.

    Tries one bounded ``get_chat`` call, then walks the connected groups
    with ``get_chat_member`` (which returns full ``User`` objects even for
    kicked users). Returns ``None`` when nothing resolves. The sweep is
    bounded by ``_RESOLVE_SWEEP_TIMEOUT``.

    Honest limitation: the Bot API cannot enumerate group members, so
    "fetch every member" is impossible; unknown users resolve per-ID
    across the groups the bot has joined.
    """
    fname: str = ""
    uname: str | None = None
    lname: str | None = None
    try:
        chat = await asyncio.wait_for(
            bot.get_chat(target_id), timeout=_GET_CHAT_TIMEOUT
        )
    except Exception as exc:
        log.debug("get_chat(%s) failed: %s", target_id, exc)
        chat = None
    if chat is not None:
        fname = chat.first_name or ""
        uname = chat.username
        lname = getattr(chat, "last_name", None)

    if not fname:
        groups = await db.groups_db.active_groups()
        try:
            async with asyncio.timeout(_RESOLVE_SWEEP_TIMEOUT):
                for grp in groups:
                    chat_id = grp.get("chat_id")
                    if not chat_id:
                        continue
                    try:
                        member = await asyncio.wait_for(
                            bot.get_chat_member(chat_id, target_id),
                            timeout=_GET_CHAT_TIMEOUT,
                        )
                    except Exception as exc:
                        log.debug(
                            "get_chat_member(%s, %s) failed: %s",
                            chat_id,
                            target_id,
                            exc,
                        )
                        continue
                    user = getattr(member, "user", None)
                    if user is None:
                        continue
                    if user.is_bot or not user.first_name:
                        continue
                    return user.first_name, user.username, user.last_name
        except TimeoutError:
            log.debug(
                "get_chat_member sweep timed out for target=%d after %ds",
                target_id,
                _RESOLVE_SWEEP_TIMEOUT,
            )

    if not fname:
        return None
    return fname, uname, lname


async def resolve_user_identity(
    bot: Bot, target_id: int
) -> tuple[str, str | None, str | None]:
    """Return (display_name, username_or_None, last_name_or_None) for any user.

    Shared identity resolver used by profile views (``/check``,
    ``/tcstats`` user detail). Fast path is the member cache: a document
    carrying both first name and username proves a full-identity write
    happened (sparse writers such as ban-by-ID never supply a username),
    so it is returned as-is without touching Telegram. Otherwise one live
    fetch runs; anything resolved is persisted via ``upsert_user`` plus an
    L1 triple put (upsert invalidates rather than populates), so the next
    view hits the fast path. A fruitless attempt caches the sentinel so
    repeats stay bounded by the L1 TTL. On timeout or total miss the
    numeric-ID fallback still replies.
    """
    cached = await db.users_cache.get_user(target_id)
    fname = (cached.get("first_name") or "") if cached else ""
    uname = (cached.get("username") if cached else None) or None
    lname = (cached.get("last_name") if cached else None) or None

    if fname and uname:
        return fname, uname, lname

    if not cached and db.users_cache.has_recent_identity_attempt(target_id):
        # * Unknown user: a cached entry means a recent attempt already
        # * came up empty; skip Telegram until the L1 TTL expires.
        return str(target_id), None, None

    live = await _fetch_live_identity(bot, target_id)
    if live is None:
        db.users_cache.remember_identity(target_id, None, None, None)
        return str(target_id), None, None

    fname, uname, lname = live
    # * Persist whatever Telegram told us (even a username-less profile)
    # * so the next view takes the fast path, then mirror it into L1
    # * because upsert_user invalidates rather than populates.
    try:
        await db.users_cache.upsert_user(target_id, uname, fname, lname)
    except Exception as exc:
        log.debug("users_cache upsert after resolve failed for %d: %s", target_id, exc)
    db.users_cache.remember_identity(target_id, fname, uname, lname)
    return fname, uname, lname


async def sync_user_identity(
    bot: Bot, target_id: int
) -> tuple[str, str | None, str | None]:
    """Verify the cached identity against live Telegram and update on mismatch.

    The full sync protocol for explicit detail views (``/check`` profile,
    ``/tcstats`` user card):

    1. Read the cached document (fast path, one indexed read).
    2. Fetch the live identity via :func:`_fetch_live_identity`.
    3. If live is unreachable, return the cached values (stale beats absent).
    4. If live differs field-by-field, persist and return the live triple.

    A live ``None`` means "absent on Telegram" (definitive for ``User``
    objects), so missing fields clear stale values via explicit ``""``.
    Callers holding only partial data must keep using ``upsert_user`` with
    ``None`` (unknown, preserve).
    """
    cached = await db.users_cache.get_user(target_id)
    if cached:
        have: tuple[str, str | None, str | None] = (
            cached.get("first_name") or "",
            (cached.get("username") or None),
            (cached.get("last_name") or None),
        )
    else:
        have = ("", None, None)

    live = await _fetch_live_identity(bot, target_id)
    if live is None:
        if have[0]:
            return have
        db.users_cache.remember_identity(target_id, None, None, None)
        return str(target_id), None, None

    if cached and (have[0], have[1], have[2]) == live:
        return live

    fname, uname, lname = live
    try:
        await db.users_cache.upsert_user(
            target_id,
            uname if uname is not None else "",
            fname,
            lname if lname is not None else "",
        )
    except Exception as exc:
        log.debug("users_cache upsert after sync failed for %d: %s", target_id, exc)
    db.users_cache.remember_identity(target_id, fname, uname, lname)
    return fname, uname, lname
