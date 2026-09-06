# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Logging setup for the TCF bot."""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from tcbot.utils.time_and_date import from_timestamp

# ────────────────────── Console Log Formatter ───────────────────── #
# * Color-coded bracket format: [HH:MM] [DD/MM/YY] [LEVEL] [module:line] → message
# * Level and message color match per severity; no background badges
# * Module name: last segment only (e.g. ban_flow, not tcbot.modules.helper.workflows.ban_flow)
# * All timestamps in UTC


class BotLogFormatter(logging.Formatter):
    """Custom log formatter for console output with ANSI bracket format."""

    _R = "\033[0m"
    _BR = "\033[38;5;236m"  # bracket color (dark gray)
    _TM = "\033[38;5;242m"  # time
    _DT = "\033[38;5;238m"  # date
    _MD = "\033[38;5;75m"  # module:line
    _AW = "\033[38;5;242m"  # arrow →
    _MS = "\033[38;5;253m"  # default message

    _LEVELS: ClassVar[dict[int, tuple[str, str]]] = {
        logging.DEBUG: ("\033[38;5;246m", "DEBUG"),
        logging.INFO: ("\033[38;5;114m", "INFO"),
        logging.WARNING: ("\033[38;5;178m", "WARN"),
        logging.ERROR: ("\033[38;5;203m", "ERROR"),
        logging.CRITICAL: ("\033[38;5;177m", "CRIT"),
    }
    _COLORED_MSG: ClassVar[set[int]] = {
        logging.WARNING,
        logging.ERROR,
        logging.CRITICAL,
    }

    def _bracket(self, color: str, text: str) -> str:
        """Wrap *text* in ANSI-coloured square brackets using the given *color* code."""
        return f"{self._BR}[{self._R}{color}{text}{self._R}{self._BR}]{self._R}"

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record with ANSI-colored time, date, level, module, and message.

        Appends formatted exception and stack-info blocks when present so that
        ``log.exception()`` and ``exc_info=True`` calls produce fully colored output.
        """
        # * Use the record's creation time, not now: buffered or delayed
        # * formatting would otherwise misorder events.
        now = from_timestamp(record.created)
        level_color, level_label = self._LEVELS.get(record.levelno, ("\033[0m", "???"))
        module = record.name.split(".")[-1]
        msg_color = level_color if record.levelno in self._COLORED_MSG else self._MS

        time_part = self._bracket(self._TM, now.strftime("%H:%M"))
        date_part = self._bracket(self._DT, now.strftime("%d/%m/%y"))
        level_part = self._bracket(level_color, level_label)
        module_part = self._bracket(self._MD, f"{module}:{record.lineno}")
        arrow_part = f"{self._AW} → {self._R}"
        msg_part = f"{msg_color}{record.getMessage()}{self._R}"

        output = (
            f"{time_part} {date_part} {level_part} {module_part}{arrow_part}{msg_part}"
        )

        # * Append traceback for log.exception() and explicit exc_info=... calls.
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                output += f"\n{level_color}{record.exc_text}{self._R}"
        if record.stack_info:
            output += f"\n{level_color}{self.formatStack(record.stack_info)}{self._R}"
        return output


# ─────────────────── Telegram Error Log Handler ─────────────────── #
# * Sends ERROR/CRITICAL logs to the configured LOG_ERRORS channel
# * Zero blocking; schedules coroutine on the running asyncio event loop
# * Suppression list prevents infinite loops and network noise

# * Strong references to in-flight Telegram error report tasks (prevents GC)
_tg_tasks: set[asyncio.Task[None]] = set()

_SUPPRESS_PREFIXES: tuple[str, ...] = (
    "tcbot.utils.error_reporter",
    "httpcore",
    "httpx._client",
)


class TelegramErrorHandler(logging.Handler):
    """Async logging handler that ships errors to Telegram."""

    def __init__(self) -> None:
        """Register the handler at ERROR level."""
        super().__init__(logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        """Schedule an async Telegram error report for the given log record."""
        if any(record.name.startswith(p) for p in _SUPPRESS_PREFIXES):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.handleError(record)
            return
        try:
            from tcbot.utils import (  # noqa: PLC0415
                error_reporter,
            )
        except ImportError:
            self.handleError(record)
            return

        task = loop.create_task(error_reporter.report_record(record))
        _tg_tasks.add(task)
        task.add_done_callback(_tg_tasks.discard)


# ─────────────────────── Logging Setup Entry ────────────────────── #
# * Called once at bot startup; initializes all handlers and log levels


def setup(level: int = logging.INFO) -> None:
    """Initialize and configure the bot's logging system."""
    root = logging.getLogger()
    root.setLevel(level)

    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler) and isinstance(
            handler.formatter, BotLogFormatter
        ):
            break
    else:
        con_handler = logging.StreamHandler()
        con_handler.setFormatter(BotLogFormatter())
        root.addHandler(con_handler)

    if not any(isinstance(handler, TelegramErrorHandler) for handler in root.handlers):
        root.addHandler(TelegramErrorHandler())

    for lib in ("httpx", "telegram", "motor", "pymongo"):
        logging.getLogger(lib).setLevel(logging.WARNING)
