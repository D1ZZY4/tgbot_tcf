# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Serverless lifecycle for Vercel: shared PTB Application without long-lived transports."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any

from telegram import LinkPreviewOptions, Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    Defaults,
)

from tcbot import cfg
from tcbot import database as db

# * Imported from __main__ (not duplicated) so the webhook, polling, and
# * serverless transports register exactly the same handlers.  Accept the
# * Flask import chain this pulls in: Flask is already a project dependency
# * and no server is started at import time (guarded by __main__'s
# * ``if __name__ == "__main__"`` block).
from tcbot.__main__ import _register_handlers
from tcbot.database import redis_client
from tcbot.database.mongos import connect, ensure_indexes, is_connected
from tcbot.database.scheduler import expire_old_warns
from tcbot.utils import error_reporter
from tcbot.utils.transport import (
    API_POOL_SIZE,
    HTTP_CONNECT_TIMEOUT,
    HTTP_POOL_TIMEOUT,
    HTTP_READ_TIMEOUT,
    HTTP_WRITE_TIMEOUT,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

log = logging.getLogger(__name__)

# ────────────────── Transport tuning (mirrors __main__) ────────────────── #
# * Same outbound HTTP tuning as the long-lived transports so Telegram API
# * behaviour (pooling, timeouts, pacing) is identical on Vercel. Values
# * live in tcbot.utils.transport (single owner); only the link-preview
# * default stays local because __main__ keeps that constant private.

_HTTP_READ_TIMEOUT: float = 60
_HTTP_WRITE_TIMEOUT: float = 30
_HTTP_CONNECT_TIMEOUT: float = 30
_HTTP_POOL_TIMEOUT: float = 15
_API_POOL_SIZE: int = 8

# * Applied globally via Defaults so every bot message suppresses link preview cards.
_LINK_PREVIEW_DISABLED: LinkPreviewOptions = LinkPreviewOptions(is_disabled=True)


# ─────────────── Instance event loop (warm-invocation reuse) ────────────── #
# * Vercel freezes the process between invocations, so a module-level asyncio
# * loop survives across warm invocations while a per-request asyncio.run()
# * loop would orphan every cached client (Motor, httpx) on a closed loop.
# * One shared loop keeps the PTB Application, MongoDB client, and Redis
# * client usable across invocations.  Access is serialised: overlapping
# * invocations on one instance take turns instead of racing the loop.

_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_LOCK = threading.Lock()


def _instance_loop() -> asyncio.AbstractEventLoop:
    """Return the process-wide event loop, creating it on first use."""
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            _LOOP = asyncio.new_event_loop()
        return _LOOP


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive ``coro`` on the instance loop, serialised against concurrent invocations."""
    loop = _instance_loop()
    with _LOOP_LOCK:
        return loop.run_until_complete(coro)


# ─────────────────── Shared PTB Application ─────────────────── #
# * Built lazily on first invocation and reused while the instance is warm.
# * Deliberately NOT started with app.start(): PTB's process_update() only
# * requires initialize() (verified against the installed PTB 22.8 source),
# * and there is no Updater lane (updater=None) and no JobQueue to run.

_app: Application | None = None


def build_serverless_app() -> Application:
    """Build the PTB Application for serverless use (no Updater, no scheduler)."""
    return (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .updater(None)
        .defaults(Defaults(link_preview_options=_LINK_PREVIEW_DISABLED))
        .concurrent_updates(True)  # noqa: FBT003
        .connection_pool_size(API_POOL_SIZE)
        .read_timeout(HTTP_READ_TIMEOUT)
        .write_timeout(HTTP_WRITE_TIMEOUT)
        .connect_timeout(HTTP_CONNECT_TIMEOUT)
        .pool_timeout(HTTP_POOL_TIMEOUT)
        .rate_limiter(AIORateLimiter())
        .build()
    )


async def _init_subsystems(app: Application) -> None:
    """Connect MongoDB/Redis and attach the error reporter (no scheduler on serverless).

    APScheduler is intentionally NOT started here: a frozen serverless
    instance cannot fire persistent schedules.  Recurring maintenance
    (warn expiry) runs through the Vercel Cron endpoint instead.
    """
    log.info("serverless: connecting to MongoDB...")
    await connect()

    async def _try_redis() -> None:
        if cfg.redis_url:
            try:
                await redis_client.connect(cfg.redis_url)
            except Exception as exc:
                log.warning(
                    "Redis connection failed; running with in-memory cache only: %s",
                    exc,
                )
        else:
            log.info("REDIS_URL not set; in-memory cache only.")

    indexes_r, owner_r, _ = await asyncio.gather(
        ensure_indexes(),
        db.users_roles.ensure_initial_owner(cfg.initial_owner_id),
        _try_redis(),
        return_exceptions=True,
    )
    if isinstance(indexes_r, BaseException):
        raise indexes_r
    if isinstance(owner_r, BaseException):
        log.warning("ensure_initial_owner failed (non-fatal): %s", owner_r)

    lec, let = cfg.logs_errors
    error_reporter.attach(app.bot, lec, let, owner_id=cfg.initial_owner_id)
    log.info("serverless: subsystems ready.")


async def get_app() -> Application:
    """Return the shared Application, building and initialising it once per instance."""
    global _app
    if _app is not None:
        return _app
    app = build_serverless_app()
    _register_handlers(app)
    await app.initialize()
    await _init_subsystems(app)
    _app = app
    return app


# ─────────────────────── Update handling ─────────────────────── #


async def handle_telegram_payload(data: dict[str, Any]) -> tuple[int, str]:
    """Process one Telegram update dict; return an (HTTP status, body) pair.

    Status mapping mirrors the Flask receiver in ``alive.py``: 200 for
    handled-or-benign updates (Telegram must not retry), 400 for malformed
    payloads, 500 for transient failures (Telegram will retry).
    """
    try:
        app = await get_app()
    except Exception:
        log.exception("serverless: subsystem init failed")
        return 503, "Service unavailable"

    try:
        update = Update.de_json(data, app.bot)
    except Exception:
        log.exception("serverless: failed to decode update")
        return 500, "Internal error"
    if update is None:
        # * de_json returns None for update types unknown to this PTB version.
        # * Acknowledge so Telegram does not retry.
        log.debug("serverless: unrecognized update type; skipping.")
        return 200, "OK"

    try:
        await app.process_update(update)
    except Exception:
        # * Handler exceptions already went through the PTB error handler
        # * (attached reporter).  500 asks Telegram to retry the delivery.
        log.exception("serverless: update processing failed")
        return 500, "Internal error"
    return 200, "OK"


# ─────────────────────── Cron maintenance ─────────────────────── #


async def run_warn_expiry() -> tuple[int, str]:
    """Run warn expiry on demand for the Vercel Cron endpoint.

    Returns an (HTTP status, body) pair describing the outcome.
    """
    days = cfg.warn_expiry_days
    if days <= 0:
        return 200, "warn expiry disabled (WARN_EXPIRY_DAYS=0)"
    try:
        if not is_connected():
            await connect()
        await expire_old_warns(days)
    except Exception:
        log.exception("serverless: warn expiry failed")
        return 500, "warn expiry failed"
    return 200, f"warn expiry ran (older than {days}d pruned)"
