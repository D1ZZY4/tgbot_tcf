# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Async circuit breaker for external service calls (Telegram API, MongoDB).

A circuit breaker protects the bot from wasting time on repeated timeouts when
a downstream service is unresponsive.  It tracks consecutive failures for a
named service and trips to OPEN after ``failure_threshold`` failures, at which
point all further calls are rejected immediately with ``CircuitOpenError``
rather than waiting for another timeout.

After ``recovery_timeout`` seconds the circuit enters HALF_OPEN and allows one
probe call through.  A successful probe closes the circuit; a failed probe
resets the timer and the circuit stays OPEN.

Usage::

    from tcbot.utils.circuit_breaker import telegram as tg_cb, CircuitOpenError

    try:
        result = await tg_cb.call(bot.send_message(chat_id=..., text=...))
    except CircuitOpenError:
        log.warning("Telegram circuit is OPEN; call skipped.")
    except Exception:
        pass  # downstream error already counted against the circuit

Module-level singletons ``telegram`` and ``mongodb`` are ready to use without
instantiation.  Import additional ``CircuitBreaker`` instances for other
services as needed.

All state mutations happen inside the asyncio event loop (cooperative
multitasking), so no explicit locking is required.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

from tcbot.utils.time_and_date import monotonic

if TYPE_CHECKING:
    from collections.abc import Awaitable

log = logging.getLogger(__name__)

_DEFAULT_FAILURE_THRESHOLD: int = 5
_DEFAULT_RECOVERY_TIMEOUT: float = 60.0


class CircuitState(Enum):
    """Operational state of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is attempted while the circuit is OPEN or HALF_OPEN full."""


class CircuitBreaker:
    """Lightweight async circuit breaker with automatic OPEN/HALF_OPEN/CLOSED transitions.

    Args:
        name: Human-readable service name used in log messages.
        failure_threshold: Consecutive failures before the circuit opens.
        recovery_timeout: Seconds to wait in OPEN before allowing a probe.

    """

    __slots__ = (
        "_failure_count",
        "_failure_threshold",
        "_half_open_probe_in_flight",
        "_name",
        "_opened_at",
        "_recovery_timeout",
        "_state",
    )

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout: float = _DEFAULT_RECOVERY_TIMEOUT,
    ) -> None:
        """Initialise a new circuit breaker for the named service."""
        self._name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._half_open_probe_in_flight: bool = False
        self._opened_at: float = 0.0

    # ── public API ── #

    @property
    def name(self) -> str:
        """Service name for this circuit."""
        return self._name

    @property
    def state(self) -> CircuitState:
        """Current state; auto-transitions OPEN to HALF_OPEN after timeout."""
        if (
            self._state is CircuitState.OPEN
            and monotonic() - self._opened_at >= self._recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            log.info(
                "Circuit [%s]: OPEN -> HALF_OPEN (recovery probe allowed).",
                self._name,
            )
        return self._state

    @property
    def is_open(self) -> bool:
        """True when the circuit is OPEN and calls should be rejected."""
        return self.state is CircuitState.OPEN

    async def call[T](self, coro: Awaitable[T]) -> T:
        """Execute *coro* through the circuit breaker.

        Raises:
            CircuitOpenError: Circuit is OPEN; the call was rejected without
                touching the downstream service.
            Any exception the coroutine itself raises (also recorded as a
                failure; the exception propagates to the caller unchanged).

        """
        if not self.try_acquire():
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise CircuitOpenError(f"Circuit [{self._name}] is OPEN; call rejected.")

        try:
            result: T = await coro
        except Exception as exc:
            self._record_failure()
            raise exc from None
        except BaseException:
            # * Cancellation must not leave a HALF_OPEN probe permanently reserved.
            self._release_probe()
            raise

        self._record_success()
        return result

    def try_acquire(self) -> bool:
        """Reserve permission for one call, allowing only one HALF_OPEN probe."""
        state = self.state
        if state is CircuitState.OPEN:
            return False
        if state is CircuitState.HALF_OPEN:
            if self._half_open_probe_in_flight:
                return False
            self._half_open_probe_in_flight = True
        return True

    def release_probe(self) -> None:
        """Release a HALF_OPEN probe without changing the circuit state."""
        self._release_probe()

    def record_success(self) -> None:
        """Record a successful outcome (use when not wrapping via call()).

        Intended for callers that manage their own try/except and need to
        inform the circuit without delegating the entire coroutine to it.
        """
        self._record_success()

    def record_failure(self) -> None:
        """Record a failed outcome (use when not wrapping via call()).

        Increments the consecutive-failure counter and may trip the circuit
        to OPEN.  Intended for callers that manage their own try/except.
        """
        self._record_failure()

    def reset(self) -> None:
        """Force-close the circuit (useful after operator confirms service recovery)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._release_probe()
        log.info("Circuit [%s]: manually reset to CLOSED.", self._name)

    # ── private helpers ── #

    def _record_success(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            log.info("Circuit [%s]: HALF_OPEN -> CLOSED (probe succeeded).", self._name)
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._release_probe()

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._state is CircuitState.HALF_OPEN:
            log.warning(
                "Circuit [%s]: HALF_OPEN -> OPEN (probe failed; retry in %.0fs).",
                self._name,
                self._recovery_timeout,
            )
            self._state = CircuitState.OPEN
            self._opened_at = monotonic()
        elif self._failure_count >= self._failure_threshold:
            log.warning(
                "Circuit [%s]: CLOSED -> OPEN (%d consecutive failures; "
                "retry in %.0fs).",
                self._name,
                self._failure_count,
                self._recovery_timeout,
            )
            self._state = CircuitState.OPEN
            self._opened_at = monotonic()
        self._release_probe()

    def _release_probe(self) -> None:
        self._half_open_probe_in_flight = False


# ── module-level singletons ── #

#: Circuit breaker for outbound Telegram Bot API calls (fan-out, send, etc.).
#: Trips after 5 consecutive timeout/network failures; recovers after 60s.
telegram: CircuitBreaker = CircuitBreaker(
    "telegram",
    failure_threshold=_DEFAULT_FAILURE_THRESHOLD,
    recovery_timeout=_DEFAULT_RECOVERY_TIMEOUT,
)

#: Circuit breaker for MongoDB operations.
#: Trips after 5 consecutive failures; recovers after 60s.
mongodb: CircuitBreaker = CircuitBreaker(
    "mongodb",
    failure_threshold=_DEFAULT_FAILURE_THRESHOLD,
    recovery_timeout=_DEFAULT_RECOVERY_TIMEOUT,
)


__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "mongodb",
    "telegram",
]
