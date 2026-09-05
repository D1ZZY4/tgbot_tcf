# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Auth decorators, execution tracer, per-user rate limiter, and shared permission helpers."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Any, TypeVar

from telegram.ext import ApplicationHandlerStop, ContextTypes

if TYPE_CHECKING:
    from collections.abc import Coroutine

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper.identity import ANONYMOUS_BOT_ID
from tcbot.utils.time_and_date import elapsed_ms, monotonic

if TYPE_CHECKING:
    from collections.abc import Callable

    from telegram import Message, Update

log = logging.getLogger(__name__)
R = TypeVar("R")


# ────────────── Per-user sliding-window rate limiter ────────────── #


class _RateLimiter:
    """Synchronous in-process sliding-window per-user rate limiter (fallback)."""

    __slots__ = ("_buckets", "max_calls", "window")

    def __init__(self, max_calls: int, window: float) -> None:
        self.max_calls = max_calls
        self.window = window
        self._buckets: dict[int, deque[float]] = {}

    def check(self, uid: int) -> float:
        """Return 0.0 if allowed (call recorded), or seconds to wait if denied."""
        now = monotonic()
        dq = self._buckets.get(uid)

        if dq is None:
            self._buckets[uid] = deque([now])
            # * Periodic cleanup to bound memory; mirrors Redis PEXPIRE behavior.
            # * Only runs when bucket count grows past threshold to avoid O(n) on every call.
            if len(self._buckets) > 10_000:
                cutoff = now - self.window * 2
                self._buckets = {
                    k: v for k, v in self._buckets.items() if v and v[0] > cutoff
                }
            return 0.0

        # * drop timestamps outside the current window
        while dq and now - dq[0] >= self.window:
            dq.popleft()

        if not dq:
            # * bucket fully cleared - recycle slot and allow
            self._buckets[uid] = deque([now])
            return 0.0

        if len(dq) >= self.max_calls:
            # * blocked - tell caller how long until the oldest slot expires
            return round(self.window - (now - dq[0]), 1)

        dq.append(now)
        return 0.0


# * Lua script for atomic sorted-set sliding-window rate limit.
# * KEYS[1] = rate-limit key; ARGV[1] = now (float), ARGV[2] = window (float),
# * ARGV[3] = max_calls (int), ARGV[4] = unique member (nanosecond timestamp).
# * Returns 0 if allowed, or ceil(wait_tenths) (int) if denied (wait = result/10 s).
_RL_LUA = """
local key   = KEYS[1]
local now   = tonumber(ARGV[1])
local win   = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - win)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, math.ceil(win * 1000) + 1000)
    return 0
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
if oldest and oldest[2] then
    local wait = win - (now - tonumber(oldest[2]))
    if wait < 0.1 then wait = 0.1 end
    return math.ceil(wait * 10)
end
return math.ceil(win * 10)
"""


class _AsyncRateLimiter:
    """Sliding-window rate limiter: Redis sorted-set backend with in-process fallback.

    When Redis is available the sliding window state persists across bot restarts.
    When Redis is unavailable (or errors), the call falls through to the in-process
    ``_RateLimiter``, so rate limiting is never silently disabled.
    """

    __slots__ = ("_local", "_prefix", "max_calls", "window")

    def __init__(self, max_calls: int, window: float, prefix: str) -> None:
        self.max_calls = max_calls
        self.window = window
        self._prefix = prefix
        self._local = _RateLimiter(max_calls=max_calls, window=window)

    async def check(self, uid: int) -> float:
        """Return 0.0 if allowed, or seconds to wait if denied."""
        r = db.redis_client.client()
        if r is None:
            return self._local.check(uid)

        key = f"rl:{self._prefix}:{uid}"
        now = time.time()
        # * nanosecond timestamp string as unique member - avoids ZADD collision
        member = str(time.time_ns())

        try:
            result = await r.eval(
                _RL_LUA,
                1,
                key,
                str(now),
                str(self.window),
                str(self.max_calls),
                member,
            )
            if result == 0:
                return 0.0
            # * result = ceil(wait * 10) - convert back to seconds
            return round(int(result) / 10.0, 1)
        except Exception as exc:
            log.debug("Redis rate limiter error, using local fallback: %s", exc)
            return self._local.check(uid)


# * Commands : 8 calls per 30 s - comfortable for regular moderation
_cmd_limiter = _AsyncRateLimiter(max_calls=8, window=30.0, prefix="cmd")

# * Buttons  : 20 presses per 10 s - allows snappy navigation
_cbq_limiter = _AsyncRateLimiter(max_calls=20, window=10.0, prefix="cbq")


async def global_rate_limit_handler(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """Universal per-user rate limiter - registered at group -1."""
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return

    # * ── button press ─────────────────────────────────────────────────────────
    if update.callback_query:
        wait = await _cbq_limiter.check(uid)
        if wait:
            try:
                await update.callback_query.answer(
                    f"Slow down - try again in {wait:.0f} seconds.",
                    show_alert=True,
                )
            except Exception as exc:
                log.debug("CBQ rate-limit answer failed: %s", exc)
            raise ApplicationHandlerStop
        return

    # * ── command message ──────────────────────────────────────────────────────
    msg = update.effective_message
    text = (msg.text or "") if msg else ""
    if not text:
        return

    if not any(text.startswith(p) for p in cfg.prefixes if p):
        return  # * plain chat message - never rate-limit

    wait = await _cmd_limiter.check(uid)
    if wait:
        if msg:
            try:
                await msg.reply_text(f"Slow down - try again in {wait:.0f} seconds.")
            except Exception as exc:
                log.debug("Command rate-limit reply failed: %s", exc)
        raise ApplicationHandlerStop


# ──────────────────── Per-handler rate limiter factory ──────────────────── #


def ratelimiter(
    limit: int = 5, period: float = 60.0
) -> Callable[
    [Callable[..., Coroutine[Any, Any, R]]],
    Callable[..., Coroutine[Any, Any, R | None]],
]:
    """Per-handler sliding-window rate limiter factory.

    Backed by Redis when available; falls through to in-process sliding window
    otherwise.  The Redis key prefix includes the wrapped function name so each
    handler gets an independent quota bucket per user.
    """

    def decorator(
        func: Callable[..., Coroutine[Any, Any, R]],
    ) -> Callable[..., Coroutine[Any, Any, R | None]]:
        """Wrap ``func`` with a sliding-window per-user rate check."""
        _limiter = _AsyncRateLimiter(
            max_calls=limit, window=period, prefix=f"h:{func.__name__}"
        )

        @functools.wraps(func)
        async def _wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> R | None:
            """Block the call and notify the user if the rate limit is exceeded."""
            uid = update.effective_user.id if update.effective_user else None
            if uid:
                wait = await _limiter.check(uid)
                if wait:
                    if update.callback_query:
                        try:
                            await update.callback_query.answer(
                                f"Slow down - try again in {wait:.0f}s.",
                                show_alert=True,
                            )
                        except Exception as exc:
                            log.debug("Callback rate-limit answer failed: %s", exc)
                        return None
                    if update.effective_message:
                        try:
                            await update.effective_message.reply_text(
                                f"Slow down - try again in {wait:.0f} seconds."
                            )
                        except Exception as exc:
                            log.debug("Message rate-limit reply failed: %s", exc)
                        return None
            return await func(update, ctx)

        return _wrapper

    return decorator


# ──────────────────────── Execution tracer ──────────────────────── #


def log_execution(
    func: Callable[..., Coroutine[Any, Any, R]],
) -> Callable[..., Coroutine[Any, Any, R]]:
    """Wrap a handler to emit entry / exit / exception traces at DEBUG level."""

    @functools.wraps(func)
    async def _wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> R:
        """Emit entry, exit, and exception traces at DEBUG level around ``func``."""
        uid = update.effective_user.id if update.effective_user else "?"
        name = func.__name__
        t0 = monotonic()
        log.debug("[%s] uid=%s enter", name, uid)
        try:
            result = await func(update, ctx)
        except Exception:
            log.exception("[%s] uid=%s raised after %.1fms", name, uid, elapsed_ms(t0))
            raise
        log.debug("[%s] uid=%s ok (%.1fms)", name, uid, elapsed_ms(t0))
        return result

    return _wrapper


# ───────────────────────── Auth decorators ──────────────────────── #

# * `ANONYMOUS_BOT_ID` is defined in `tcbot.modules.helper.identity` as the
# * single source of truth for the GroupAnonymousBot placeholder. Re-exported
# * under the historical name `_ANON_BOT_ID` so existing in-module references
# * stay valid; new code should import from `identity` directly.
_ANON_BOT_ID = ANONYMOUS_BOT_ID

# * User-facing refusal messages for each auth tier. Centralised here so voice
# * changes and translations only need to happen in one place.
_ERR_OWNER_ONLY = "This command is reserved for the Founder - you're not authorized."
_ERR_STAFF_ONLY = "Staff and Founder only for this one - you don't have the rank."
_ERR_MOD_ONLY = "You need Developer rank or above for this - not your call."
_ERR_BASIC_MOD_ONLY = "You need at least a Tester role for this - not your call."
_ERR_RANK_INSUFFICIENT = "You don't have the rank for this one."
_ERR_ROLE_LOOKUP = (
    "I couldn't verify federation roles right now. Please try again in a moment."
)
_ERR_ANON_ADMIN = (
    "Anonymous admin mode is not supported for federation commands. "
    "Please send this command from your personal account."
)


def _is_anon_admin(update: Update) -> bool:
    """Return True when the effective user is the anonymous admin placeholder bot."""
    u = update.effective_user
    return u is not None and u.id == _ANON_BOT_ID


def owner_only(func: Callable) -> Callable:
    """Restrict handler to the Founder only."""

    @functools.wraps(func)
    async def _wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Allow the call only when the invoking user is the Founder."""
        if _is_anon_admin(update):
            if update.effective_message:
                try:
                    await update.effective_message.reply_text(_ERR_ANON_ADMIN)
                except Exception as exc:
                    log.debug("owner_only anon-admin reply failed: %s", exc)
            return None
        uid = update.effective_user.id if update.effective_user else None
        if uid and await db.users_roles.is_owner(uid):
            return await func(update, ctx)
        if update.effective_message:
            try:
                await update.effective_message.reply_text(_ERR_OWNER_ONLY)
            except Exception as exc:
                log.debug("owner_only refusal reply failed: %s", exc)
        return None

    return _wrapper


def staff_only(func: Callable) -> Callable:
    """Restrict handler to Founder and Admin."""

    @functools.wraps(func)
    async def _wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Allow the call only when the invoking user is Founder or Admin."""
        if _is_anon_admin(update):
            if update.effective_message:
                try:
                    await update.effective_message.reply_text(_ERR_ANON_ADMIN)
                except Exception as exc:
                    log.debug("staff_only anon-admin reply failed: %s", exc)
            return None
        uid = update.effective_user.id if update.effective_user else None
        if uid and await db.users_roles.is_staff(uid):
            return await func(update, ctx)
        if update.effective_message:
            try:
                await update.effective_message.reply_text(_ERR_STAFF_ONLY)
            except Exception as exc:
                log.debug("staff_only refusal reply failed: %s", exc)
        return None

    return _wrapper


def mod_only(func: Callable) -> Callable:
    """Restrict handler to Founder, Admin, Developer (ban/unban level)."""

    @functools.wraps(func)
    async def _wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Allow the call only when the invoking user holds Developer rank or above."""
        if _is_anon_admin(update):
            if update.effective_message:
                try:
                    await update.effective_message.reply_text(_ERR_ANON_ADMIN)
                except Exception as exc:
                    log.debug("mod_only anon-admin reply failed: %s", exc)
            return None
        uid = update.effective_user.id if update.effective_user else None
        if uid and db.users_roles.role_rank(
            await db.users_roles.get_effective_role(uid)
        ) >= db.users_roles.role_rank("developer"):
            return await func(update, ctx)
        if update.effective_message:
            try:
                await update.effective_message.reply_text(_ERR_MOD_ONLY)
            except Exception as exc:
                log.debug("mod_only refusal reply failed: %s", exc)
        return None

    return _wrapper


def basic_mod_only(func: Callable) -> Callable:
    """Restrict handler to Founder, Admin, Developer, Tester (kick/mute/warn level)."""

    @functools.wraps(func)
    async def _wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Allow the call only when the invoking user holds Tester rank or above."""
        if _is_anon_admin(update):
            if update.effective_message:
                try:
                    await update.effective_message.reply_text(_ERR_ANON_ADMIN)
                except Exception as exc:
                    log.debug("basic_mod_only anon-admin reply failed: %s", exc)
            return None
        uid = update.effective_user.id if update.effective_user else None
        if uid and db.users_roles.role_rank(
            await db.users_roles.get_effective_role(uid)
        ) >= db.users_roles.role_rank("tester"):
            return await func(update, ctx)
        if update.effective_message:
            try:
                await update.effective_message.reply_text(_ERR_BASIC_MOD_ONLY)
            except Exception as exc:
                log.debug("basic_mod_only refusal reply failed: %s", exc)
        return None

    return _wrapper


# ────────────────── Shared executor-vs-target check ─────────────── #


async def resolve_and_check(
    msg: Message,
    executor_id: int,
    target_id: int,
    *,
    min_role: str,
) -> tuple[str | None, str | None]:
    """Validate executor permission and target eligibility for moderation actions.

    Replies on the message itself when the executor lacks rank or the target
    outranks the executor, then returns ``(None, None)``.

    On success, returns ``(executor_role, target_role)``.
    """
    executor_role, target_role = await asyncio.gather(
        db.users_roles.get_effective_role(executor_id),
        db.users_roles.get_effective_role(target_id),
        return_exceptions=True,
    )
    if isinstance(executor_role, asyncio.CancelledError):
        raise executor_role
    if isinstance(target_role, asyncio.CancelledError):
        raise target_role
    executor_lookup_failed = isinstance(executor_role, BaseException)
    target_lookup_failed = isinstance(target_role, BaseException)
    if isinstance(executor_role, BaseException):
        log.warning(
            "resolve_and_check executor role lookup failed for %d: %s",
            executor_id,
            executor_role,
        )
        executor_role = None
    if isinstance(target_role, BaseException):
        log.warning(
            "resolve_and_check target role lookup failed for %d: %s",
            target_id,
            target_role,
        )
        target_role = None
    if executor_lookup_failed or target_lookup_failed:
        try:
            await msg.reply_text(_ERR_ROLE_LOOKUP)
        except Exception as exc:
            log.debug("resolve_and_check role-lookup reply failed: %s", exc)
        return None, None
    if db.users_roles.role_rank(executor_role) < db.users_roles.role_rank(min_role):
        try:
            await msg.reply_text(_ERR_RANK_INSUFFICIENT)
        except Exception as exc:
            log.debug("resolve_and_check rank-insufficient reply failed: %s", exc)
        return None, None

    if target_role and db.users_roles.role_rank(
        executor_role
    ) <= db.users_roles.role_rank(target_role):
        label = db.users_roles.ROLE_LABEL.get(target_role, target_role.capitalize())
        try:
            await msg.reply_text(
                f"That's a {label} - they outrank you here, can't take action on them."
            )
        except Exception as exc:
            log.debug("resolve_and_check outrank reply failed: %s", exc)
        return None, None

    return executor_role, target_role
