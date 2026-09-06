# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Target extraction helpers: extract_target()."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from tcbot import database as db
from tcbot.modules.helper.identity import ANONYMOUS_BOT_ID, TELEGRAM_USER_ID
from tcbot.utils.time_and_date import TELEGRAM_LOOKUP_TIMEOUT, to_utc, utc_now

if TYPE_CHECKING:
    from collections.abc import Mapping

    from telegram import Bot, Chat, ChatFullInfo, Message, Update, User

log = logging.getLogger(__name__)

# * Telegram lookups are wrapped in wait_for so a stalled API call never blocks
# * the user-facing reply. Per-call budget is the shared project-wide value.
_GET_CHAT_TIMEOUT = TELEGRAM_LOOKUP_TIMEOUT
# * Overall deadline for the per-group get_chat_member sweep in
# * resolve_user_identity: bounds latency on large federations.
_RESOLVE_SWEEP_TIMEOUT = 15.0
# * Upper bound for concurrent get_chat_member probes during the sweep.
# * Matches the fan_out Telegram cap; keeps the burst bounded while cutting
# * the sequential worst case (one 3 s probe per group) down to roughly one
# * probe batch. Probes never touch the Telegram circuit breaker: a timeout
# * here is only an identity miss, not congestion evidence.
_RESOLVE_SWEEP_CONCURRENCY = 10
# * Maximum age of a cached identity before detail views re-verify it
# * live. Harvest writes on every observed message keep active users far
# * below this; it only bites for silent users (banned, lurkers).
_SYNC_MAX_AGE_S = 7 * 24 * 3600

# * Strong references to in-flight background-refresh tasks; prevents GC
# * before the coroutine completes (same pattern as harvest task sets).
_refresh_tasks: set[asyncio.Task[None]] = set()


async def _safe_get_chat(bot: Bot, ident: str | int) -> Chat | ChatFullInfo | None:
    """Call ``bot.get_chat`` with a bounded timeout; returns ``None`` on failure."""
    try:
        return await asyncio.wait_for(bot.get_chat(ident), timeout=_GET_CHAT_TIMEOUT)
    except Exception as exc:
        log.debug("get_chat(%s) failed: %s", ident, exc)
        return None


# ──────────────────────── Target resolution ─────────────────────── #


def has_reply_target(msg: Message) -> bool:
    """Return True when ``msg`` replies to a sender that resolves as a target.

    Mirrors ``extract_target`` priority 1 (reply), including the
    anonymous-admin skip: a GroupAnonymousBot reply carries the group
    itself as ``sender_chat``, which must never count as a reply target.
    Command entries use this to decide whether the first arg token names
    the target or starts the reason text: with a reply target every arg
    is reason text, so a leading numeric/@ token must not be consumed.
    """
    reply = msg.reply_to_message
    if reply is None:
        return False
    from_user = reply.from_user
    if from_user is not None:
        return from_user.id not in (ANONYMOUS_BOT_ID, TELEGRAM_USER_ID)
    return reply.sender_chat is not None


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
    kicked users). Group probes run concurrently (bounded by
    ``_RESOLVE_SWEEP_CONCURRENCY``) and the first hit in group order wins;
    every probe returns the same Telegram user triple, so parallelism only
    changes latency, not the result. Returns ``None`` when nothing resolves.
    The sweep is bounded by ``_RESOLVE_SWEEP_TIMEOUT``.

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
        sem = asyncio.Semaphore(_RESOLVE_SWEEP_CONCURRENCY)

        async def _probe(
            grp: Mapping[str, object],
        ) -> tuple[str, str | None, str | None] | BaseException | None:
            raw_id = grp.get("chat_id")
            if not isinstance(raw_id, int) or not raw_id:
                return None
            chat_id: int = raw_id
            try:
                async with sem:
                    member = await asyncio.wait_for(
                        bot.get_chat_member(chat_id, target_id),
                        timeout=_GET_CHAT_TIMEOUT,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug(
                    "get_chat_member(%s, %s) failed: %s",
                    chat_id,
                    target_id,
                    exc,
                )
                return None
            user = getattr(member, "user", None)
            if user is None:
                return None
            if user.is_bot or not user.first_name:
                return None
            return user.first_name, user.username, user.last_name

        try:
            async with asyncio.timeout(_RESOLVE_SWEEP_TIMEOUT):
                probed = await asyncio.gather(
                    *(_probe(grp) for grp in groups), return_exceptions=True
                )
        except TimeoutError:
            log.debug(
                "get_chat_member sweep timed out for target=%d after %ds",
                target_id,
                _RESOLVE_SWEEP_TIMEOUT,
            )
            return None
        for result in probed:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                continue
            if result is not None:
                return result

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
    bot: Bot, target_id: int, *, max_age_seconds: float = _SYNC_MAX_AGE_S
) -> tuple[str, str | None, str | None]:
    """Verify the cached identity against live Telegram and update on mismatch.

    The full sync protocol for explicit detail views (``/check`` profile,
    ``/tcstats`` user card):

    1. Read the cached document (fast path, one indexed read).
    2. Return it untouched when complete (first name + username) and
       fresher than ``max_age_seconds``.
    3. Otherwise fetch live via :func:`_fetch_live_identity`.
    4. If live is unreachable, return the cached values (stale beats absent).
    5. Persist the live triple (bumping ``last_updated`` even when equal,
       so the next view skips re-verification for another full window)
       and return it.

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

    if have[0] and have[1]:
        age: float | None = None
        try:
            updated_at = cached.get("last_updated") if cached else None
            if updated_at is not None:
                age = (utc_now() - to_utc(updated_at)).total_seconds()
        except Exception as exc:
            log.debug("sync age check failed for %d: %s", target_id, exc)
        if age is not None and age <= max_age_seconds:
            return have

    live = await _fetch_live_identity(bot, target_id)
    if live is None:
        if have[0]:
            return have
        db.users_cache.remember_identity(target_id, None, None, None)
        return str(target_id), None, None

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


def identity_needs_refresh(doc: Mapping[str, object] | None) -> bool:
    """Return True when a cached doc should be re-verified in background.

    Missing docs, sparse docs (no username), and complete docs older than
    ``_SYNC_MAX_AGE_S`` all need a refresh; fresh complete docs do not.
    Pure predicate over an already-fetched document: never touches I/O.
    """
    if not doc:
        return True
    fname = doc.get("first_name") or ""
    uname = doc.get("username") or None
    if not (fname and uname):
        return True
    try:
        updated_at = doc.get("last_updated")
        if not isinstance(updated_at, datetime):
            return True
        age = (utc_now() - to_utc(updated_at)).total_seconds()
    except Exception as exc:
        log.debug("identity age check failed: %s", exc)
        return True
    return age > _SYNC_MAX_AGE_S


async def _refresh_identity(bot: Bot, target_id: int) -> None:
    """Run :func:`sync_user_identity` and swallow failures at debug level."""
    try:
        await sync_user_identity(bot, target_id)
    except Exception as exc:
        log.debug("background identity refresh failed for %d: %s", target_id, exc)


def launch_identity_refresh(bot: Bot, target_id: int) -> None:
    """Fire-and-forget identity sync for detail views (zero added latency).

    The view renders instantly from cache; this refreshes the stored
    identity in the background so the *next* view is current. Callers
    holding the document should gate with :func:`identity_needs_refresh`
    so fresh profiles cost nothing.
    """
    try:
        task = asyncio.get_running_loop().create_task(_refresh_identity(bot, target_id))
    except RuntimeError:
        log.debug("identity refresh skipped: no running event loop.")
        return
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)
