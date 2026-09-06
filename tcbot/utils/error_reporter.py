# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Centralized error reporter: classifies, formats, dedupes, and ships errors to LOG_ERRORS."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import platform
import re
import sys
import traceback
from typing import TYPE_CHECKING

import telegram.error as _te

from tcbot.utils.formatter import bold, code, pre
from tcbot.utils.time_and_date import monotonic, utc_now

if TYPE_CHECKING:
    from telegram import Bot

log = logging.getLogger(__name__)


# ─────────────────────── Module-Level State ─────────────────────── #
# * Set once during bot post-init via attach(); owner_id is refreshable
# * via set_owner() so that ownership-transfer DMs route to the new owner.

_bot: Bot | None = None
_chat_id: int = 0
_thread_id: int | None = None
_owner_id: int = 0


def attach(
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    *,
    owner_id: int = 0,
) -> None:
    """Inject live bot instance, log channel config, and owner DM target.

    Validates the inputs at attach-time so a misconfiguration is logged
    at startup rather than producing silent no-ops at error-report time.
    A zero or negative ``chat_id`` / ``owner_id`` is accepted (the bot
    may legitimately be deployed without a log channel or before the
    initial owner is seeded) but is logged at WARNING so the operator
    notices on first boot.
    """
    global _bot, _chat_id, _thread_id, _owner_id
    _bot = bot
    _chat_id = chat_id
    _thread_id = thread_id
    _owner_id = owner_id
    if chat_id <= 0:
        log.warning(
            "error_reporter.attach called with chat_id=%d; LOG_ERRORS "
            "shipping is disabled until a positive chat_id is configured",
            chat_id,
        )
    if owner_id <= 0:
        log.warning(
            "error_reporter.attach called with owner_id=%d; owner-DM "
            "shipping of infra errors is disabled until a positive owner_id "
            "is configured (typically after OWNER_ID env var is read)",
            owner_id,
        )


def set_owner(owner_id: int) -> None:
    """Update the owner-DM target after a runtime ownership transfer.

    Called from ``cmd_transfer`` after the new founder is set, so that the
    next infra error DM goes to the new owner instead of the old one.
    """
    global _owner_id
    _owner_id = owner_id


# ────────────── Filter benign + noisy log records ───────────────── #
# * Benign errors are caught/recovered by safe_edit; we never ship them.
# * Log-noise patterns come from log_execution wrapping every handler.

_BENIGN_PATTERNS: tuple[str, ...] = (
    "message is not modified",
    "message to edit not found",
    "message to delete not found",
    "query is too old",
    "message is too old",
    "message can't be edited",
)

# * log_execution emits a per-handler exception summary of the form
# * "<action> raised after <delta>s: <type>". PTB's global error handler
# * then follows up with a richer report. The substring is a stable,
# * documented contract between the two paths (see decorators.py).
_LOG_NOISE_PATTERNS: tuple[str, ...] = (" raised after ",)

# * Owner-only errors: infra-level issues that should reach the owner
# * privately via DM, but must NOT be posted to the shared logs_errors
# * channel. Conflict arises when two bot instances poll simultaneously;
# * InvalidToken means the token was revoked or is wrong -- both are
# * operator concerns, not bugs the on-call moderator needs to see.
_OWNER_ONLY_TYPES: tuple[type[BaseException], ...] = (
    _te.Conflict,
    _te.InvalidToken,
)


def _benign(exc: BaseException | None) -> bool:
    """Return True when the exception is a recoverable, well-known no-op."""
    if exc is None:
        return False
    # * Shutdown cancellation is normal lifecycle, not a reportable error.
    if isinstance(exc, asyncio.CancelledError):
        return True
    msg = str(exc).lower()
    return any(p in msg for p in _BENIGN_PATTERNS)


def _log_noise(record: logging.LogRecord | None) -> bool:
    """Return True when the log record duplicates info already reported elsewhere."""
    if record is None:
        return False
    msg = record.getMessage()
    return any(p in msg for p in _LOG_NOISE_PATTERNS)


def _owner_only(exc: BaseException | None) -> bool:
    """Return True when the error is an infra/operator issue that goes to owner DM only."""
    if exc is None:
        return False
    return isinstance(exc, _OWNER_ONLY_TYPES)


# ─────────────────── Dedupe within a short window ────────────────── #
# * Same exception object travels through log_execution + PTB error handler;
# * a fingerprinted TTL set keeps the channel to ONE report per incident.

_DEDUPE_WINDOW = 30.0
_RECENT_MAX: int = 1000
_recent: dict[tuple, float] = {}

# ── Owner-DM repeat suppression ── #
# * Owner-only errors (a duplicate instance's Conflict storm, a revoked
# * token) repeat identically forever; the 30 s dedupe above only spaces
# * them to one DM per 30 s with no backstop. Past a small hourly budget
# * per fingerprint the owner already knows, so suppress further repeats.
_OWNER_WINDOW: float = 3600.0
_OWNER_BUDGET: int = 3
_OWNER_MAX: int = 1000
_owner_sent: dict[tuple, tuple[float, int]] = {}


def _owner_suppressed(fp: tuple) -> bool:
    """Return True when this owner fingerprint exhausted its hourly budget."""
    now = monotonic()
    if len(_owner_sent) >= _OWNER_MAX:
        for k in [k for k, (s, _) in _owner_sent.items() if now - s >= _OWNER_WINDOW]:
            del _owner_sent[k]
    start, count = _owner_sent.get(fp, (now, 0))
    if now - start >= _OWNER_WINDOW:
        start, count = now, 0
    if count >= _OWNER_BUDGET:
        _owner_sent[fp] = (start, count)
        return True
    _owner_sent[fp] = (start, count + 1)
    return False


# * Maximum characters captured from an exception or log message in a fingerprint.
_MAX_CONTEXT_LEN: int = 120


def _fingerprint_exc(exc: BaseException) -> tuple:
    """Build a coarse identity for an exception that survives class+location+message."""
    tb = exc.__traceback__
    last = None
    while tb is not None:
        last = tb
        tb = tb.tb_next
    line = last.tb_lineno if last else 0
    file_part = ""
    if last is not None:
        with contextlib.suppress(AttributeError):
            file_part = last.tb_frame.f_code.co_filename
    return (
        "exc",
        type(exc).__name__,
        file_part,
        line,
        str(exc)[:_MAX_CONTEXT_LEN],
    )


def _fingerprint_record(record: logging.LogRecord) -> tuple:
    """Build a coarse identity for a log record."""
    return (
        "log",
        record.name,
        record.lineno,
        record.getMessage()[:_MAX_CONTEXT_LEN],
    )


def _seen_recently(fp: tuple) -> bool:
    """Mark fp as seen now; return True if it was already seen within the window."""
    now = monotonic()
    for k in list(_recent):
        if now - _recent[k] > _DEDUPE_WINDOW:
            del _recent[k]
    if len(_recent) >= _RECENT_MAX:
        # * Evict the oldest 10% when the cap is hit to avoid unbounded memory growth
        # * during storm scenarios with many distinct error fingerprints.
        for k, _ in sorted(_recent.items(), key=lambda x: x[1])[: _RECENT_MAX // 10]:
            del _recent[k]
    if fp in _recent:
        return True
    _recent[fp] = now
    return False


def _dedup(exc: BaseException | None, record: logging.LogRecord | None) -> bool:
    """Return True if (exc, record) has been reported within the dedupe window.

    Accepts both shapes so callers don't have to re-implement the
    fingerprint selection. When both are present (typical for
    ``log.exception()`` which produces a record with embedded exc_info)
    the exception fingerprint is preferred because it carries more
    identifying context.
    """
    if exc is not None:
        return _seen_recently(_fingerprint_exc(exc))
    if record is not None:
        return _seen_recently(_fingerprint_record(record))
    return False


# ────────────────────── Error Classification ────────────────────── #


_ACTION_HINTS: tuple[tuple[str, str], ...] = (
    ("[DB]", "Check MongoDB reachability and credentials, then retry the action."),
    ("Rate Limit", "Telegram asked to slow down; no action needed unless it persists."),
    ("Timed Out", "Transient Telegram hiccup; retry the action."),
    ("Polling Conflict", "Two bot instances are polling; stop the duplicate."),
    ("Invalid Token", "Token revoked or wrong; update BOT_TOKEN and restart."),
    ("Forbidden", "Bot lacks admin rights in the target chat; re-promote it."),
    ("Bad Request", "Check the reported call arguments for a bad ID or text."),
    ("Network", "Transient connectivity issue; retry the action."),
    ("Timeout", "Operation exceeded its deadline; retry the action."),
    ("Code Bug", "Needs a code fix; see the traceback below."),
)


def _action_hint(label: str) -> str:
    """Return the operator action line matching a classify label."""
    for marker, hint in _ACTION_HINTS:
        if marker in label:
            return hint
    return "Needs a code fix; see the traceback below."


def _classify(exc: BaseException | None) -> str:
    """Return a human-readable label tag for the exception."""
    if exc is None:
        return "[?] Unknown"

    # * Specific Telegram error subclasses first; BadRequest inherits NetworkError.
    if isinstance(exc, _te.RetryAfter):
        return "[~] Rate Limit: Flood Wait"
    if isinstance(exc, _te.TimedOut):
        return "[~] Telegram Timed Out"
    if isinstance(exc, _te.Conflict):
        return "[~] Polling Conflict"
    if isinstance(exc, _te.BadRequest):
        return "[!] Telegram Bad Request"
    if isinstance(exc, _te.Forbidden):
        return "[!] Telegram Forbidden"
    if isinstance(exc, _te.InvalidToken):
        return "[!] Telegram Invalid Token"
    if isinstance(exc, _te.NetworkError):
        return "[~] Telegram Network Error"
    if isinstance(exc, _te.TelegramError):
        return "[!] Telegram API Error"

    mod = type(exc).__module__ or ""
    if any(x in mod for x in ("motor", "pymongo", "mongo")):
        return "[DB] Database Error"

    if isinstance(exc, asyncio.TimeoutError):
        return "[~] Async Timeout"
    if isinstance(exc, asyncio.CancelledError):
        return "[-] Task Cancelled"

    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or any(
        x in mod for x in ("httpx", "aiohttp", "urllib3", "ssl")
    ):
        return "[~] Network / Server Error"

    return "[!] Code Bug"


# ─────────────────────── Message Formatting ─────────────────────── #
# * Telegram hard-caps a message at 4096 chars (incl. HTML). Budget below
# * keeps the rendered output safely under that limit even with HTML tags.

_MAX_TB = 2200
_MAX_MSG = 250
_MAX_CTX = 250
_TB_FRAMES = 8
_MAX_LINE_CONTENT: int = 100
_REPORT_SEP_LEN: int = 30


def _esc(s: str | None) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    if s is None:
        return ""
    return html.escape(str(s))


def _shorten_path(path: str) -> str:
    """Convert raw filesystem path to a compact project-relative form."""
    p = path.replace("\\", "/")
    if "tcbot/" in p:
        return "tcbot/" + p.split("tcbot/")[-1]
    if ".venv/Lib/site-packages/" in p:
        return p.split(".venv/Lib/site-packages/")[-1]
    if ".venv/lib/python" in p:
        # *nix venv
        return p.split("site-packages/")[-1]
    return p.rsplit("/", 1)[-1]


def _condensed_tb(exc: BaseException) -> str:
    """Build a compact `file:line in func` traceback with the last few frames."""
    frames = traceback.extract_tb(exc.__traceback__)
    last = frames[-_TB_FRAMES:]
    lines: list[str] = []
    for f in last:
        path = _shorten_path(f.filename or "?")
        lines.append(f"  {path}:{f.lineno} in {f.name}")
        if f.line:
            lines.append(f"      {f.line.strip()[:_MAX_LINE_CONTENT]}")
    lines.append(f"{type(exc).__name__}: {exc}")
    out = "\n".join(lines)
    if len(out) > _MAX_TB:
        out = "...(trimmed)\n" + out[-_MAX_TB:]
    return out


def _location(
    exc: BaseException | None,
    record: logging.LogRecord | None,
) -> tuple[str, str, int]:
    """Return (file, func, line) for the report header."""
    if record is not None:
        path = _shorten_path(record.pathname)
        return path, record.funcName, record.lineno
    if exc is not None and exc.__traceback__ is not None:
        frames = traceback.extract_tb(exc.__traceback__)
        if frames:
            last = frames[-1]
            return _shorten_path(last.filename or "?"), last.name, last.lineno or 0
    return "?", "?", 0


_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
# * The user part is optional so password-only authorities (redis://:pass@host)
# * redact too; a bare host without credentials never matches (nothing to hide).
_MONGO_AUTH_RE = re.compile(r"://(?:[^:@/\s]+)?:[^@/\s]+@")


def _scrub_secrets(text: str) -> str:
    """Redact credential-shaped substrings before shipping to the log channel.

    Mongo auth/network errors can echo connection strings, and any bug that
    interpolates config may leak the bot token. Redaction is pattern-based
    (bot ``id:hash`` shape, URI ``[user]:pass@`` authority with optional user,
    so ``redis://:pass@host`` is covered), so legitimate surrounding text is
    preserved.
    """
    text = _TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    return _MONGO_AUTH_RE.sub("://[REDACTED]@", text)


def scrub_text(text: str) -> str:
    """Public wrapper for secret redaction of console-bound strings."""
    return _scrub_secrets(text)


def build_error_message(
    *,
    exc: BaseException | None = None,
    record: logging.LogRecord | None = None,
    context: str | None = None,
) -> str:
    """Build a complete HTML-formatted error message for Telegram."""
    now = utc_now()
    time_str = now.strftime("%H:%M:%S UTC")
    date_str = now.strftime("%d-%m-%Y")

    if record and record.exc_info and record.exc_info[1]:
        exc = exc or record.exc_info[1]

    if record:
        raw_msg = record.getMessage()
    elif exc:
        raw_msg = str(exc)
    else:
        raw_msg = "No detail available."
    raw_msg = _scrub_secrets(raw_msg)

    file_part, func_name, line_no = _location(exc, record)
    label = _classify(exc)
    action = _action_hint(label)

    tb_block = ""
    if exc and exc.__traceback__:
        # * _condensed_tb embeds str(exc), which can echo credential-shaped
        # * substrings (bot token, Mongo URI); scrub before shipping.
        tb_block = (
            f"\n\n{bold('Traceback:')}\n{pre(_scrub_secrets(_condensed_tb(exc)))}"
        )

    ctx_block = ""
    if context:
        ctx_block = (
            f"\n\n{bold('Context:')}\n{code(_scrub_secrets(str(context))[:_MAX_CTX])}"
        )

    py_ver = sys.version.split()[0]
    host = platform.node() or "?"
    sep = "-" * _REPORT_SEP_LEN

    return (
        f"{bold('Error Report')}\n"
        f"{sep}\n"
        f"{bold('Type:')} {label}\n"
        f"{bold('Action:')} {_esc(action)}\n"
        f"{bold('Where:')} {code(f'{file_part}:{line_no}')} in {code(func_name)}\n"
        f"{bold('When:')} {time_str} - {date_str}\n"
        f"{bold('Host:')} Python {py_ver} @ {_esc(host)}\n"
        f"{sep}\n"
        f"{bold('Message:')}\n{code(raw_msg[:_MAX_MSG])}"
        f"{tb_block}"
        f"{ctx_block}"
    )


# ───────────────────────── Low-Level Send ───────────────────────── #
# * Shipping is throttled: at most _SEND_BUDGET reports per window go to
# * LOG_ERRORS. Overflow is counted, and a single summary is sent when the
# * next window opens. Without this, escalating more paths to error-level
# * would risk Telegram FloodWait during incident storms.

_SEND_BUDGET: int = 20
_SEND_WINDOW: float = 60.0
_send_window_start: float = 0.0
_sent_in_window: int = 0
_suppressed_in_window: int = 0


async def _ship_throttled(text: str) -> None:
    """Send to LOG_ERRORS within budget, else count for the summary."""
    global _send_window_start, _sent_in_window, _suppressed_in_window
    if not _bot or not _chat_id:
        return
    now = monotonic()
    if now - _send_window_start >= _SEND_WINDOW:
        _send_window_start = now
        _sent_in_window = 0
        if _suppressed_in_window:
            suppressed = _suppressed_in_window
            _suppressed_in_window = 0
            try:
                await _bot.send_message(
                    _chat_id,
                    f"Error reporter: {suppressed} further error(s) "
                    f"suppressed in the last {_SEND_WINDOW:.0f}s window.",
                    parse_mode="HTML",
                    message_thread_id=_thread_id,
                )
            except Exception as exc:
                logging.getLogger().warning(
                    "Failed to ship error summary to LOG_ERRORS: %s", exc
                )
                return
    if _sent_in_window >= _SEND_BUDGET:
        _suppressed_in_window += 1
        return
    _sent_in_window += 1
    try:
        await _bot.send_message(
            _chat_id,
            text,
            parse_mode="HTML",
            message_thread_id=_thread_id,
        )
    except Exception as exc:
        # * Log via the root logger at WARNING, not through the dedicated
        # * error_reporter logger (which is in the _SUPPRESS_PREFIXES list
        # * in logger.py to prevent recursion). Falling back to the root
        # * logger ensures the failure surfaces in console output even if
        # * the Telegram error handler is misconfigured.
        logging.getLogger().warning(
            "Failed to ship error to Telegram LOG_ERRORS: %s", exc
        )


async def send_to_log_errors(text: str) -> None:
    """Fire-and-forget send to LOG_ERRORS channel (throttled, see above)."""
    if not _bot or not _chat_id:
        return
    await _ship_throttled(text)


async def send_to_owner(text: str) -> None:
    """Fire-and-forget DM to the bot owner; used for infra/operator-only errors."""
    if not _bot or not _owner_id:
        return
    try:
        await _bot.send_message(
            _owner_id,
            text,
            parse_mode="HTML",
        )
    except Exception as exc:
        logging.getLogger().warning("Failed to send owner DM for infra error: %s", exc)


# ────────────────────── Convenience Wrappers ────────────────────── #


async def report_exc(
    exc: BaseException,
    context: str | None = None,
) -> None:
    """Report an exception; owner-only errors go to owner DM, others to log channel."""
    if _benign(exc):
        return
    if _dedup(exc, None):
        return
    text = build_error_message(exc=exc, context=context)
    if _owner_only(exc):
        if _owner_suppressed(_fingerprint_exc(exc)):
            logging.getLogger().debug("Owner DM suppressed: hourly budget spent.")
            return
        await send_to_owner(text)
    else:
        await send_to_log_errors(text)


async def report_record(record: logging.LogRecord) -> None:
    """Report a logging.LogRecord (from log.error() / log.critical()); deduped and noise-filtered."""
    exc = record.exc_info[1] if record.exc_info else None
    if _benign(exc):
        return
    if _log_noise(record):
        # * log_execution emits a per-handler summary that the PTB error handler
        # * follows up with a richer report; skip the noisier first one.
        return
    if _dedup(exc, record):
        return
    text = build_error_message(record=record)
    if _owner_only(exc):
        fp = _fingerprint_exc(exc) if exc is not None else _fingerprint_record(record)
        if _owner_suppressed(fp):
            logging.getLogger().debug("Owner DM suppressed: hourly budget spent.")
            return
        await send_to_owner(text)
    else:
        await send_to_log_errors(text)
