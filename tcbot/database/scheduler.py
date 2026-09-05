# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""Persistent moderation scheduler backed by APScheduler 3.x + MongoDB.

All scheduled moderation actions (warn expiry) survive bot restarts
because APScheduler stores its job state in MongoDB via MongoDBJobStore.
Member-cache cleanup is handled by a MongoDB TTL index on ``last_updated``,
not by a scheduler job.

The scheduler runs inside a dedicated asyncio background task so that
``scheduler.start()`` is called inside the running event loop.

Usage pattern (lifecycle managed by ``tcbot/__main__.py``)::

    await scheduler.start(mongodb_uri, db_name, warn_expiry_days)
    # ... bot runs ...
    await scheduler.stop()

Scheduling a one-off action::

    schedule_id = await scheduler.schedule_unban(ban_id, user_id, run_at)
    # cancel if user manually unbanned before expiry:
    await scheduler.cancel_schedule(schedule_id)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.jobstores.mongodb import MongoDBJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

# * Direct module imports (not through tcbot.database.__init__) to avoid circular
# * imports: tcbot.database.__init__ → scheduler → tcbot.database.__init__
from tcbot.database.bans_db import deactivate_ban as _bans_deactivate
from tcbot.database.mongos import col as _col
from tcbot.database.mongos import db_call as _db_call
from tcbot.utils.timedate_format import utc_now

log = logging.getLogger(__name__)

# ──────────────── Recurring job schedule IDs ──────────────────── #
# * Stable IDs prevent duplicate schedules across restarts.
# * (replace_existing=True updates the trigger without creating duplicates)

_WARN_EXPIRY_SCHEDULE_ID: str = "tcbot.warn_expiry_daily"
# * Legacy ID kept so _register_periodic_schedules can remove the old schedule
# * from any MongoDB datastore that was created before the TTL-index migration.
_CLEANUP_SCHEDULE_ID: str = "tcbot.db_cleanup_weekly"

# * Maximum seconds to wait for the scheduler background task to exit cleanly
# * before declaring it stuck.  10 s matches the PTB shutdown grace window.
_STOP_TIMEOUT_S: float = 10.0

# ──────────────── Module-level scheduler state ──────────────────── #
# * _scheduler:   live AsyncIOScheduler reference (set inside background task)
# * _sched_task:  the asyncio Task that runs scheduler.start() + stop wait
# * _sched_ready: event set when the scheduler is initialised and available
# * _sched_stop:  event set by stop() to trigger graceful shutdown
# * _sched_error: captured exception if background task crashes

_scheduler: AsyncIOScheduler | None = None
_sched_task: asyncio.Task | None = None  # type: ignore[type-arg]
_sched_ready: asyncio.Event | None = None
_sched_stop: asyncio.Event | None = None
_sched_error: BaseException | None = None


# ══════════════════════════════════════════════════════════════════ #
#  Persistent job functions
#  Must be module-level callables so APScheduler can serialise their
#  import paths into MongoDB and call them after bot restarts.
# ══════════════════════════════════════════════════════════════════ #


async def _expire_old_warns(warn_expiry_days: int) -> None:
    """Delete expired warn records from both ``warns`` and ``warn_counts``.

    Both collections must be pruned together. Deleting only ``warn_counts``
    leaves individual ``warns`` documents intact; ``_sync_warn_count`` then
    reconstructs the counter from those documents on the next warn operation,
    making expiry a no-op. Deleting both collections atomically (in parallel)
    prevents this backfill from restoring stale counts.

    Called daily by APScheduler when ``WARN_EXPIRY_DAYS > 0``.
    """
    cutoff = utc_now() - timedelta(days=warn_expiry_days)
    counts_res, warns_res = await asyncio.gather(
        _db_call(_col("warn_counts").delete_many({"updated_at": {"$lt": cutoff}})),
        _db_call(_col("warns").delete_many({"timestamp": {"$lt": cutoff}})),
        return_exceptions=True,
    )
    if isinstance(counts_res, BaseException):
        log.error("Warn expiry: warn_counts delete failed: %s", counts_res)
    if isinstance(warns_res, BaseException):
        log.error("Warn expiry: warns delete failed: %s", warns_res)
    counts_del = (
        counts_res.deleted_count if not isinstance(counts_res, BaseException) else 0
    )
    warns_del = (
        warns_res.deleted_count if not isinstance(warns_res, BaseException) else 0
    )
    log.info(
        "Warn expiry: removed %d warn_count and %d warn records older than %d days.",
        counts_del,
        warns_del,
        warn_expiry_days,
    )


async def _cleanup_old_records() -> None:
    """No-op migration shim for the retired weekly member_cache cleanup job.

    member_cache cleanup is now handled automatically by the MongoDB TTL index on
    ``last_updated`` (``expireAfterSeconds=_MEMBER_CACHE_EXPIRE_S``, 90 days), added
    in ``mongos.ensure_indexes()``.  The APScheduler schedule is removed on startup
    in ``_register_periodic_schedules``; this function exists solely so that any
    schedule record persisted from a previous bot version can be deserialised and
    called without raising an ``AttributeError``.  It is safe to remove once all
    running instances have been restarted and the MongoDB datastore no longer contains
    the ``tcbot.db_cleanup_weekly`` schedule entry.
    """
    log.info(
        "DB cleanup job called but is now a no-op; "
        "cleanup is handled by the MongoDB TTL index on member_cache.last_updated."
    )


async def _execute_scheduled_unban(ban_id: str, user_id: int) -> None:
    """Deactivate a timed ban record in MongoDB when its scheduled expiry fires.

    NOTE: This only updates the DB record (``is_active = False``). The actual
    Telegram unban is handled by the timed ``restrict_chat_member`` call with
    ``until_date`` at ban time, which Telegram enforces natively.
    """
    try:
        deactivated = await _bans_deactivate(ban_id)
    except Exception:
        log.exception(
            "Scheduled unban DB failure for ban_id=%s user_id=%d",
            ban_id,
            user_id,
        )
        return
    if deactivated:
        log.info(
            "Scheduled unban: deactivated ban_id=%s for user_id=%d.", ban_id, user_id
        )
    else:
        log.debug(
            "Scheduled unban: ban_id=%s not found or already inactive (user_id=%d).",
            ban_id,
            user_id,
        )


# ══════════════════════════════════════════════════════════════════ #
#  Background task: owns the scheduler lifecycle
# ══════════════════════════════════════════════════════════════════ #


async def _scheduler_background(
    mongodb_uri: str, db_name: str, warn_expiry_days: int
) -> None:
    """Long-running background task that owns the AsyncIOScheduler lifecycle.

    Calls ``scheduler.start()`` (synchronous, must be in the running event loop),
    registers periodic schedules, then waits for the stop event before calling
    ``scheduler.shutdown()``.
    """
    global _scheduler, _sched_error
    jobstores = {"mongodb": MongoDBJobStore(database=db_name, host=mongodb_uri)}
    scheduler = AsyncIOScheduler(jobstores=jobstores)
    _scheduler = scheduler
    try:
        _register_periodic_schedules(scheduler, warn_expiry_days)
        scheduler.start()
        if _sched_ready is None:
            raise RuntimeError(
                "_sched_ready event not initialised before _scheduler_background ran"
            )
        if _sched_stop is None:
            raise RuntimeError(
                "_sched_stop event not initialised before _scheduler_background ran"
            )
        # Signal readiness only after the recurring schedules are registered
        # and the scheduler has accepted background execution.
        _sched_ready.set()
        await _sched_stop.wait()
        scheduler.shutdown(wait=False)
    except Exception as exc:
        log.exception("APScheduler background task crashed.")
        _sched_error = exc
        if _sched_ready is not None and not _sched_ready.is_set():
            _sched_ready.set()  # unblock start() so it doesn't hang forever
    finally:
        _scheduler = None
        log.info("APScheduler background task exited.")


# ══════════════════════════════════════════════════════════════════ #
#  Periodic schedule registration
# ══════════════════════════════════════════════════════════════════ #


def _register_periodic_schedules(
    scheduler: AsyncIOScheduler, warn_expiry_days: int
) -> None:
    """Register recurring maintenance schedules (idempotent via replace_existing)."""
    if warn_expiry_days > 0:
        scheduler.add_job(
            _expire_old_warns,
            trigger=IntervalTrigger(hours=24),
            id=_WARN_EXPIRY_SCHEDULE_ID,
            args=[warn_expiry_days],
            replace_existing=True,
        )
        log.info("Scheduled warn expiry: every 24h, expiry_days=%d.", warn_expiry_days)
    else:
        # * Warn expiry disabled: remove stale schedule if previously active.
        try:
            scheduler.remove_job(_WARN_EXPIRY_SCHEDULE_ID)
            log.info("Warn expiry schedule removed (WARN_EXPIRY_DAYS=0).")
        except Exception as exc:
            log.debug("Warn expiry schedule not present, skipping removal: %s", exc)

    # * member_cache cleanup is now handled by a MongoDB TTL index on last_updated.
    # * Remove the legacy weekly schedule if it was persisted from a prior bot version.
    try:
        scheduler.remove_job(_CLEANUP_SCHEDULE_ID)
        log.info("Removed legacy weekly cleanup schedule (now handled by TTL index).")
    except Exception as exc:
        log.debug("Legacy cleanup schedule not present, nothing to remove: %s", exc)


# ══════════════════════════════════════════════════════════════════ #
#  Lifecycle helpers
# ══════════════════════════════════════════════════════════════════ #


async def start(mongodb_uri: str, db_name: str, warn_expiry_days: int) -> None:
    """Initialise and start the APScheduler background scheduler.

    Spawns a dedicated asyncio task that calls ``scheduler.start()`` inside the
    running event loop.  Uses ``MongoDBJobStore`` so all schedules and job state
    survive bot restarts.

    Blocks until the scheduler is ready to accept schedule operations.

    Args:
        mongodb_uri: MongoDB connection string (same as ``MONGODB_URI``).
        db_name: MongoDB database name (same as ``DB_NAME``).
        warn_expiry_days: Days after which warn_counts are expired (0 = disabled).

    """
    global _sched_task, _sched_ready, _sched_stop, _sched_error
    _sched_ready = asyncio.Event()
    _sched_stop = asyncio.Event()
    _sched_error = None
    _sched_task = asyncio.create_task(
        _scheduler_background(mongodb_uri, db_name, warn_expiry_days),
        name="tcbot.scheduler",
    )
    await _sched_ready.wait()
    if _sched_error is not None:
        startup_error = _sched_error
        if _sched_task is not None:
            await _sched_task
        _sched_task = None
        _sched_ready = None
        _sched_stop = None
        _sched_error = None
        raise RuntimeError("APScheduler failed to start.") from startup_error
    log.info("APScheduler ready (MongoDBJobStore → %s).", db_name)


async def stop() -> None:
    """Stop the scheduler and release all resources.

    Sets the stop event so the background task can exit cleanly.
    Safe to call even if :func:`start` was never called.
    """
    global _sched_task, _sched_ready, _sched_stop, _sched_error
    if _sched_stop is not None:
        _sched_stop.set()
    if _sched_task is not None:
        try:
            await asyncio.wait_for(_sched_task, timeout=_STOP_TIMEOUT_S)
        except TimeoutError:
            log.warning(
                "APScheduler background task did not stop within %.0fs.",
                _STOP_TIMEOUT_S,
            )
    _sched_task = None
    _sched_ready = None
    _sched_stop = None
    _sched_error = None
    log.info("APScheduler stopped.")


def _get() -> AsyncIOScheduler:
    """Return the active scheduler; raises if not started."""
    if _scheduler is None:
        raise RuntimeError("Scheduler not started; call start() first.")
    return _scheduler


def is_ready() -> bool:
    """Return True only after scheduler startup has completed successfully."""
    return (
        _scheduler is not None
        and _sched_ready is not None
        and _sched_ready.is_set()
        and _sched_error is None
    )


# ══════════════════════════════════════════════════════════════════ #
#  Public scheduling helpers
# ══════════════════════════════════════════════════════════════════ #


async def schedule_unban(ban_id: str, user_id: int, run_at: datetime) -> str:
    """Schedule a persistent DB-side unban at *run_at* (UTC).

    Returns the APScheduler schedule ID which can be passed to
    :func:`cancel_schedule` if the user is manually unbanned before expiry.

    The schedule is stored in MongoDB so it survives bot restarts.
    """
    schedule_id = f"unban.{ban_id}"
    _get().add_job(
        _execute_scheduled_unban,
        trigger=DateTrigger(run_at),
        id=schedule_id,
        args=[ban_id, user_id],
        replace_existing=True,
        # * A restart straddling run_at must still deactivate the ban: the
        # * default 1s misfire window would silently drop it. One hour
        # * covers deploys while staying far below real ban durations.
        # * coalesce collapses duplicate queued firings into one run.
        misfire_grace_time=3600,
        coalesce=True,
    )
    log.info(
        "Scheduled persistent unban: ban_id=%s user_id=%d run_at=%s.",
        ban_id,
        user_id,
        run_at.isoformat(),
    )
    return schedule_id


async def cancel_schedule(schedule_id: str) -> bool:
    """Cancel a persistent schedule by ID. Returns True if it existed.

    Safe to call with a non-existent ID (returns False, does not raise).
    """
    try:
        _get().remove_job(schedule_id)
        log.info("Cancelled schedule: %s.", schedule_id)
        return True
    except Exception:
        log.debug(
            "cancel_schedule: %s not found (already fired or never created).",
            schedule_id,
        )
        return False
