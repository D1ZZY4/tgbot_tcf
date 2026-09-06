# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Network diagnostics: /ping (alias /p) and /speedtest (alias /st)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from speedtest import ConfigRetrievalError, Speedtest
from telegram.ext import ContextTypes, MessageHandler

from tcbot.modules.helper import decorators, replies
from tcbot.modules.helper.formatter import bold, code
from tcbot.utils.prefixes import build_prefixed_filters
from tcbot.utils.time_and_date import elapsed_ms, monotonic

if TYPE_CHECKING:
    from telegram import Update

log = logging.getLogger(__name__)

# ─────────────────────── Rate-limiter constants ──────────────────── #

_RL_PERIOD_S: int = 60
_RL_CMD_LIMIT: int = 3

# * Upper bound for one Ookla run (best-server + download + upload +
# * share). Normal runs finish well under a minute.
_SPEEDTEST_TIMEOUT: int = 180

# ──────────────────────── Module metadata ───────────────────────── #

__module_name__ = "Netspeed"
__help_text__ = (
    "Network diagnostics: ping for Telegram API round-trip latency, "
    "speedtest for full upload and download bandwidth measurement."
)
__help_sections__: list[tuple[str, str]] = [
    (
        replies.SEC_COMMANDS,
        f"{code('/ping')} (alias: {code('/p')})\n"
        f"{code('/speedtest')} (alias: {code('/st')})",
    ),
    replies.who_section(replies.PERM_FOUNDER_ONLY),
    replies.where_section(replies.CONTEXT_BOT_OR_GROUP),
    (
        replies.SEC_WHAT,
        f"{bold('ping')}: Measures the round-trip time from the bot to "
        "Telegram's servers.\n"
        f"{bold('speedtest')}: Runs a full network speed test and reports "
        "ping, upload, download, bytes transferred, client IP, ISP, "
        "and best-server details.",
    ),
    (
        replies.SEC_EXAMPLES,
        f"{code('/ping')}\n{code('/p')}\n{code('/speedtest')}\n{code('/st')}",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ──────────────────────── Size formatter ────────────────────────── #


def _readable_size(size_bytes: float) -> str:
    """Convert a byte count to a human-readable string (B to TB)."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


# ────────────── Blocking speedtest worker (thread executor) ─────── #


def _run_speedtest() -> dict:
    """Run a full speedtest synchronously; intended for thread-executor use only."""
    # * Retry config fetch once; Ookla sometimes returns transient 403s.
    for attempt in range(2):
        try:
            st = Speedtest()
            st.get_best_server()
            st.download()
            st.upload()
            st.results.share()
            return st.results.dict()
        except ConfigRetrievalError as exc:
            if attempt == 0:
                log.warning("Speedtest config retrieval failed (attempt 1/2): %s", exc)
            else:
                raise
    return {}


# ──────────────────────── Command handlers ──────────────────────── #


@decorators.ratelimiter(limit=_RL_CMD_LIMIT, period=_RL_PERIOD_S)
@decorators.owner_only
@decorators.log_execution
async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with Telegram API round-trip latency in milliseconds."""
    msg = update.effective_message
    if msg is None:
        return
    t0 = monotonic()
    try:
        sent = await msg.reply_text("Pinging...")
    except Exception as exc:
        log.debug("cmd_ping initial reply failed: %s", exc)
        return
    ping_ms = elapsed_ms(t0)
    try:
        await sent.edit_text(
            f"Pong! Round-trip: {code(f'{ping_ms:.1f} ms')}",
            parse_mode="HTML",
        )
    except Exception as exc:
        log.debug("cmd_ping edit failed: %s", exc)


@decorators.ratelimiter(limit=_RL_CMD_LIMIT, period=_RL_PERIOD_S)
@decorators.owner_only
@decorators.log_execution
async def cmd_speedtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a full network speed test and reply with detailed results."""
    msg = update.effective_message
    if msg is None:
        return
    try:
        notice = await msg.reply_text("Running speed test, please wait...")
    except Exception as exc:
        log.debug("cmd_speedtest initial reply failed: %s", exc)
        return
    try:
        loop = asyncio.get_running_loop()
        # * Ookla can hang for minutes on a bad route; bound the whole run
        # * so the handler (and its executor thread) cannot stall forever.
        async with asyncio.timeout(_SPEEDTEST_TIMEOUT):
            result: dict = await loop.run_in_executor(None, _run_speedtest)
    except TimeoutError:
        log.warning("Speedtest timed out after %ds", _SPEEDTEST_TIMEOUT)
        try:
            await notice.edit_text("Speed test timed out. Please try again later.")
        except Exception as edit_exc:
            log.debug("cmd_speedtest timeout-edit failed: %s", edit_exc)
        return
    except Exception:
        log.exception("Speedtest failed")
        try:
            await notice.edit_text("Speed test failed. Check the bot logs for details.")
        except Exception as edit_exc:
            log.debug("cmd_speedtest failure-edit failed: %s", edit_exc)
        return

    try:
        dl = _readable_size(result["download"] / 8)
        ul = _readable_size(result["upload"] / 8)
        sent_bytes = _readable_size(int(result["bytes_sent"]))
        recv_bytes = _readable_size(int(result["bytes_received"]))
        client = result["client"]
        server = result["server"]

        text = (
            f"{bold('Speed Test Results')}\n\n"
            f"{bold('Ping:')} {code(str(result['ping']) + ' ms')}\n"
            f"{bold('Timestamp:')} {code(str(result['timestamp']))}\n"
            f"{bold('Download:')} {code(dl + '/s')}\n"
            f"{bold('Upload:')} {code(ul + '/s')}\n"
            f"{bold('Sent:')} {code(sent_bytes)}\n"
            f"{bold('Received:')} {code(recv_bytes)}\n\n"
            f"{bold('Client Info')}\n"
            f"{bold('IP:')} {code(str(client['ip']))}\n"
            f"{bold('ISP:')} {code(str(client['isp']))}\n"
            f"{bold('ISP Rating:')} {code(str(client.get('isprating', 'N/A')))}\n"
            f"{bold('Country:')} {code(str(client['country']))}\n"
            f"{bold('Latitude:')} {code(str(client['lat']))}\n"
            f"{bold('Longitude:')} {code(str(client['lon']))}\n\n"
            f"{bold('Server Info')}\n"
            f"{bold('Name:')} {code(str(server['name']))}\n"
            f"{bold('Sponsor:')} {code(str(server.get('sponsor', 'N/A')))}\n"
            f"{bold('Latency:')} {code(str(server['latency']))}\n"
            f"{bold('Country:')} {code(str(server['country']) + ', ' + str(server['cc']))}\n"
            f"{bold('Latitude:')} {code(str(server['lat']))}\n"
            f"{bold('Longitude:')} {code(str(server['lon']))}"
        )
    except KeyError:
        log.exception("Speedtest result parsing failed")
        try:
            await notice.edit_text(
                "Speed test completed but result parsing failed. Check bot logs."
            )
        except Exception as edit_exc:
            log.debug("cmd_speedtest parse-fail edit failed: %s", edit_exc)
        return
    except TypeError:
        log.exception("Speedtest result parsing failed")
        try:
            await notice.edit_text(
                "Speed test completed but result parsing failed. Check bot logs."
            )
        except Exception as edit_exc:
            log.debug("cmd_speedtest parse-fail edit failed: %s", edit_exc)
        return
    share_url: str | None = result.get("share")
    if share_url and not share_url.startswith("https://"):
        log.warning("Speedtest returned non-https share URL; sending text only")
        share_url = None
    try:
        if share_url:
            # * Edit the "please wait" notice to the result text and send the
            # * share photo as a separate reply to the original command message,
            # * both in parallel. This avoids deleting the notice (consistent
            # * with the edit pattern used in cmd_ping and other action modules).
            await asyncio.gather(
                notice.edit_text(text, parse_mode="HTML"),
                msg.reply_photo(share_url),
                return_exceptions=True,
            )
        else:
            await notice.edit_text(text, parse_mode="HTML")
    except Exception as exc:
        log.debug("cmd_speedtest result edit failed: %s", exc)


# ──────────────────────────── Handlers ──────────────────────────── #

_PING_CMDS = build_prefixed_filters("ping") | build_prefixed_filters("p")
_SPEEDTEST_CMDS = build_prefixed_filters("speedtest") | build_prefixed_filters("st")

__handlers__ = [
    MessageHandler(_PING_CMDS, cmd_ping),
    MessageHandler(_SPEEDTEST_CMDS, cmd_speedtest),
]
