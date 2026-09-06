# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Keep-alive server - Flask health check and webhook receiver on a single port."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import json
import logging
import threading
from typing import TYPE_CHECKING

from flask import Flask, request
from telegram import Update

from tcbot import cfg
from tcbot.database import mongos, redis_client
from tcbot.database import scheduler as sched_mod
from tcbot.utils import circuit_breaker as _cb
from tcbot.utils.circuit_breaker import CircuitState
from tcbot.utils.time_and_date import utc_now

if TYPE_CHECKING:
    import telegram

log = logging.getLogger(__name__)


# ───────────────────────── Flask App Setup ──────────────────────── #
# * Initialize the Flask application for health checks and webhook.
_app = Flask(__name__)


@_app.route("/")
def index() -> str:
    """Keep-alive endpoint that returns OK for basic uptime monitoring."""
    return "OK"


@_app.route("/health")
def health() -> tuple[str, int, dict[str, str]]:
    """Detailed health status as JSON: mongodb, redis, scheduler, and timestamp.

    Returns HTTP 200 when all core subsystems are ready, 503 when degraded.
    Note: mongodb and scheduler state reflect the last-known connection status
    established during startup; no live pings are issued (those require async).
    """
    mongodb_ok = mongos.is_connected()
    scheduler_ok = sched_mod.is_ready()

    # * is_connected() is a sticky startup flag (never cleared on partition),
    # * so AND it with the live circuit state: after 5 consecutive DB failures
    # * the mongodb breaker opens and the field must read error, matching the
    # * overall verdict below.
    db_state = _cb.mongodb.state
    if db_state is CircuitState.OPEN:
        mongodb_ok = False

    rc = redis_client.client()
    if rc is not None:
        redis_status = "ok"
    elif cfg.redis_url:
        redis_status = "error"
    else:
        redis_status = "disabled"

    tg_state = _cb.telegram.state

    overall = (
        "ok"
        if (
            mongodb_ok
            and scheduler_ok
            and tg_state is not CircuitState.OPEN
            and db_state is not CircuitState.OPEN
        )
        else "degraded"
    )
    payload = {
        "status": overall,
        "mongodb": "ok" if mongodb_ok else "error",
        "redis": redis_status,
        "scheduler": "ok" if scheduler_ok else "error",
        "circuit_telegram": tg_state.value,
        "circuit_mongodb": db_state.value,
        "ts": utc_now().isoformat(timespec="seconds"),
    }
    code = 200 if overall == "ok" else 503
    return json.dumps(payload), code, {"Content-Type": "application/json"}


# ───────────────────────── Webhook Receiver ─────────────────────── #
# * State injected by __main__.py after PTB Application is initialized.
# * Flask runs in a daemon thread; PTB runs in the main asyncio loop.
# * Updates are passed between threads via asyncio.run_coroutine_threadsafe.

_wh_queue: asyncio.Queue[object] | None = None
_wh_loop: asyncio.AbstractEventLoop | None = None
_wh_secret: str = ""
_wh_bot: telegram.Bot | None = None

# * A Telegram webhook request must receive a retryable response when the PTB
# * loop cannot accept the update promptly.  This bounds the synchronous Flask
# * worker instead of leaving it blocked behind a full update queue.
_WEBHOOK_ENQUEUE_TIMEOUT_S: float = 1.0


def register_webhook(
    queue: asyncio.Queue[object],
    loop: asyncio.AbstractEventLoop,
    secret: str,
    bot: telegram.Bot,
) -> None:
    """Wire PTB's update_queue and event loop into the Flask webhook route.

    Called from __main__.py after app.initialize() and before waiting for
    shutdown.  Thread-safe: only written once at startup.
    """
    global _wh_queue, _wh_loop, _wh_secret, _wh_bot
    _wh_queue = queue
    _wh_loop = loop
    _wh_secret = secret
    _wh_bot = bot
    log.info("Webhook receiver registered.")


@_app.route("/webhook", methods=["POST"])
def webhook_route() -> tuple[str, int]:
    """Receive Telegram update via webhook POST, validate, and enqueue for PTB."""
    # * Validate secret token before touching the payload (OWASP guideline).
    # * Use hmac.compare_digest for timing-safe comparison to prevent timing
    # * attacks that could be used to brute-force the secret token byte-by-byte.
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not _wh_secret or not hmac.compare_digest(token, _wh_secret):
        log.warning("Webhook: rejected request with invalid or missing secret token.")
        return "Forbidden", 403

    if _wh_queue is None or _wh_loop is None or _wh_bot is None:
        log.warning("Webhook: received update before PTB is ready.")
        return "Service unavailable", 503

    data = request.get_json(silent=True)
    if not data:
        log.warning("Webhook: received request with empty or non-JSON body.")
        return "Bad request", 400

    try:
        update = Update.de_json(data, _wh_bot)
        if update is None:
            # * de_json returns None for update types not recognized by this PTB version.
            # * Silently acknowledge so Telegram does not retry.
            log.debug("Webhook: received unrecognized update type; skipping.")
            return "OK", 200
        # * Thread-safe: Flask runs in a sync daemon thread; PTB loop is in main thread.
        put_coro = _wh_queue.put(update)
        try:
            future = asyncio.run_coroutine_threadsafe(put_coro, _wh_loop)
        except Exception:
            # * The coroutine was never scheduled: close it to avoid a
            # * "never awaited" RuntimeWarning on top of the outage log.
            put_coro.close()
            log.exception("Webhook: PTB event loop rejected the update.")
            return "Service unavailable", 503
        try:
            future.result(timeout=_WEBHOOK_ENQUEUE_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            future.cancel()
            log.warning(
                "Webhook: PTB update queue did not accept the update within %.1fs.",
                _WEBHOOK_ENQUEUE_TIMEOUT_S,
            )
            return "Service unavailable", 503
        except Exception:
            log.exception("Webhook: PTB update queue rejected the update.")
            return "Service unavailable", 503
    except Exception:
        log.exception("Webhook: failed to decode or enqueue update.")
        return "Internal error", 500

    return "OK", 200


# ──────────────────────── Server Execution ──────────────────────── #
# * Internal function to run the Flask server
# * Blocking call - must be run in a separate thread
def _run() -> None:
    """Start the Flask server with production-safe settings."""
    _app.run(
        host="0.0.0.0",
        port=cfg.port,
        debug=False,
        use_reloader=False,
    )


# ─────────────────────────── Public API ─────────────────────────── #
# * Entry point to start the keep-alive server from the main bot
def start_keepalive() -> None:
    """Launch the Flask server in a daemon thread."""
    t = threading.Thread(
        target=_run,
        name="keepalive",
        daemon=True,
    )
    t.start()
    log.info("Keep-alive server started on 0.0.0.0:%d", cfg.port)
