# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Throttled multi-group dispatcher: runs coroutines concurrently with a semaphore cap.

Wraps fan_out slots with the Telegram circuit breaker so that repeated
network timeouts do not saturate the semaphore pool with stalled tasks.
Only ``telegram.error.TimedOut`` and ``telegram.error.NetworkError`` are
counted against the circuit; expected API refusals (403, 400) are not.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING

from telegram.error import BadRequest, NetworkError, TimedOut

from tcbot.utils import circuit_breaker as _cb

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

log = logging.getLogger(__name__)

# * Telegram allows 30 msg/s globally; 10 concurrent is safe and fast.
_MAX_CONCURRENT: int = 10

# * Substrings inside `BadRequest` messages that indicate the user was not
# * actually present in the target chat, the bot was demoted, the chat no
# * longer exists, or the user had a status that prevents the action. These
# * are real Telegram API responses (not exceptions we should retry) but they
# * are not "failures" in the sense the operator cares about -- the user was
# * never there to begin with, or the chat is gone, or we lack permission.
# * The list is intentionally conservative: only patterns that have been
# * observed in production logs. Each entry is stored in upper-case; the
# * matcher in ``is_benign_telegram_error`` upper-cases the Telegram
# * message before substring search.
_BENIGN_BAD_REQUEST_SUBSTRINGS: tuple[str, ...] = (
    "USER_NOT_PARTICIPANT",  # user was not in the target group
    "USER_ID_INVALID",  # bad user id at protocol level
    "CHAT_NOT_FOUND",  # group was deleted or bot lost access
    "PEER_ID_INVALID",  # user or chat id rejected by Telegram
    "USER_NOT_FOUND",  # Telegram does not know the user
    "USER_IS_A_BOT",  # bot users cannot be banned/muted/etc.
    "USER_IS_ANONYMOUS",  # GroupAnonymousBot placeholder
    "PARTICIPANT_ID_INVALID",  # user not a participant of the chat
    "CHAT_ADMIN_REQUIRED",  # bot was demoted; not a per-user failure
)


# ──────────────── Throttled Multi-Group Dispatcher ──────────────── #


async def fan_out[T](
    coros: Sequence[Awaitable[T]],
    *,
    max_concurrent: int = _MAX_CONCURRENT,
) -> list[T | BaseException]:
    """Run coros concurrently up to max_concurrent at once; never raises.

    Integrates the Telegram circuit breaker: slots that run while the circuit
    is OPEN are skipped immediately (returning a ``CircuitOpenError``) instead
    of firing a Telegram request that will time out.  TimedOut and
    NetworkError results trip the circuit; all other exceptions (403, 400,
    etc.) are treated as expected API refusals and do not affect the circuit.

    Tasks are admitted lazily through the semaphore so the number of in-flight
    Telegram calls stays bounded. Callers may pass already-created coroutine
    objects; a coroutine skipped by the open circuit is explicitly closed.
    """
    if not coros:
        return []

    if max_concurrent < 1:
        max_concurrent = 1

    sem = asyncio.Semaphore(max_concurrent)

    async def _slot(thunk: Callable[[], Awaitable[T]]) -> T | BaseException:
        async with sem:
            if not _cb.telegram.try_acquire():
                log.warning(
                    "fan_out: Telegram circuit OPEN; skipping slot to avoid timeout."
                )
                skipped = thunk()
                if inspect.iscoroutine(skipped):
                    skipped.close()
                return _cb.CircuitOpenError("Telegram circuit is OPEN; call skipped.")
            try:
                result = await thunk()
                _cb.telegram.record_success()
                return result
            except TimedOut as exc:
                _cb.telegram.record_failure()
                log.debug("fan_out: network error (counted against circuit): %s", exc)
                return exc
            except NetworkError as exc:
                # ! CRITICAL: BadRequest subclasses NetworkError in PTB, but a
                # ! 400 is an API refusal (user not in chat, chat gone), never
                # ! congestion. Counting refusals tripped the breaker after 5
                # ! consecutive fan-out refusals and skipped the remaining
                # ! groups unenforced. Refusals must not touch the circuit.
                if isinstance(exc, BadRequest):
                    _cb.telegram.release_probe()
                    log.debug(
                        "fan_out: API refusal (not counted against circuit): %s", exc
                    )
                    return exc
                _cb.telegram.record_failure()
                log.debug("fan_out: network error (counted against circuit): %s", exc)
                return exc
            except asyncio.CancelledError:
                _cb.telegram.release_probe()
                raise
            except Exception as exc:
                _cb.telegram.release_probe()
                log.debug(
                    "fan_out: coroutine failed (not counted against circuit): %s", exc
                )
                return exc

    return list(
        await asyncio.gather(
            *(_slot(lambda c=c: c) for c in coros), return_exceptions=True
        )
    )


def count_errors(results: Sequence[object]) -> int:
    """Return the number of BaseException items in a fan_out result list."""
    return sum(1 for r in results if isinstance(r, BaseException))


def is_benign_telegram_error(exc: BaseException) -> bool:
    """Return True if the exception is a known-benign Telegram API refusal.

    A "benign" refusal is one that does not represent a real failure: the
    user was never in the chat, the bot was demoted, the chat was
    deleted, or the request was for a bot/anonymous user. These are real
    Telegram responses (the API call did fail) but they are not
    "failures" in the moderation sense -- there was nothing for the bot
    to do in the first place.

    Matching is done against both the raw message and a "normalized" form
    where non-alphanumeric characters are stripped (so the API code
    ``USER_NOT_PARTICIPANT`` matches the human-readable ``User not
    participant`` produced by PTB).
    """
    if isinstance(exc, BadRequest):
        raw = str(exc).upper()
        # * Normalize: replace any non-alphanumeric character with empty
        # * string, so "USER_NOT_PARTICIPANT" matches "USER NOT PARTICIPANT".
        normalized = "".join(c for c in raw if c.isalnum())
        for s in _BENIGN_BAD_REQUEST_SUBSTRINGS:
            target = "".join(c for c in s if c.isalnum())
            if target in raw or target in normalized:
                return True
    return False


def count_transient_errors(results: Sequence[object]) -> int:
    """Count fan_out results that represent real (non-benign) failures.

    Benign Telegram API refusals (see ``is_benign_telegram_error``) are
    excluded from the count. Use this instead of ``count_errors`` for
    operator-facing success/failure summaries where a "user was not in
    this chat" response should not count as a failed group.
    """
    return sum(
        1
        for r in results
        if isinstance(r, BaseException) and not is_benign_telegram_error(r)
    )
