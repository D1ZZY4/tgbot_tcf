# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Unban flow - invoked directly by the unban command."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tcbot import cfg
from tcbot import database as db
from tcbot.database import documents as docs
from tcbot.modules.helper import parse_logmsg
from tcbot.utils.dispatch import count_transient_errors, fan_out
from tcbot.utils.formatter import user_ref

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

log = logging.getLogger(__name__)


# ───────────────────────── Unban executor ───────────────────────── #


async def execute_unban(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    target_id: int,
    target_fname: str,
    *,
    pre_ban: docs.BanDoc | None = None,
) -> None:
    """Lift a federation ban: deactivate the DB record and unban across all connected groups.

    Fetches the active ban (unless ``pre_ban`` is supplied), loads the group
    list first (abort with the record intact when it cannot be loaded, so a
    retry re-drives the full fan-out), then deactivates and unbans across all
    connected groups, sends the log and replies concurrently. Replies inline
    if no active ban is found.

    When ``pre_ban`` is provided by the caller the function skips the ``get_active_ban``
    DB round-trip entirely, saving one network hop on the hot path. Pass ``None`` (the
    default) to let this function fetch the record itself.
    """
    msg = update.effective_message
    admin = update.effective_user

    # * Use the caller-supplied record when available; fall back to a DB fetch.
    ban: docs.BanDoc | None
    if pre_ban is not None:
        ban = pre_ban
    else:
        ban = await db.bans_db.get_active_ban(target_id)

    if not ban:
        if msg is not None:
            try:
                await msg.reply_text(
                    f"{user_ref(target_id, target_fname)} has no active federation ban.",
                    parse_mode="HTML",
                )
            except Exception as exc:
                log.debug(
                    "Unban no-record reply failed for user %d: %s", target_id, exc
                )
        return

    ban_id = ban.get("ban_id", "")

    # * Fetch groups BEFORE deactivating: if the list cannot be loaded we
    # * must abort with the ban record intact so a retry re-drives the full
    # * fan-out. Deactivating first and then discovering an empty list
    # * leaves chats unbanned-nowhere with no record to retry from (the
    # * next /tcunban would report "no active ban" while chats still ban
    # * the user). Mirrors _execute_mute's groups-first fail-closed order.
    # * Costs one sequential read, but active_groups is L1-cached (30 s).
    try:
        groups = await db.groups_db.active_groups()
    except Exception:
        log.exception("active_groups failed during unban of %d", target_id)
        if msg is not None:
            try:
                await msg.reply_text(
                    f"{user_ref(target_id, target_fname)} could not be unbanned: "
                    "the group list could not be loaded from the database, so "
                    "nothing was changed. Check the logs and retry.",
                    parse_mode="HTML",
                )
            except Exception as exc:
                log.debug("Unban groups-fail reply failed: %s", exc)
        return

    # * Deactivate ALL active bans for this user (not only the one found by
    # * get_active_ban) and cancel any pending APScheduler unban job in
    # * parallel. The cancel call is a no-op when no timed-ban schedule
    # * exists; it future-proofs the flow for when timed bans are added.
    deactivate_r, _ = await asyncio.gather(
        db.bans_db.deactivate_all_active_bans(target_id),
        db.scheduler.cancel_schedule(f"unban.{ban_id}"),
        return_exceptions=True,
    )
    if isinstance(deactivate_r, BaseException):
        # * The DB deactivation is the only authoritative state write for the
        # * unban. If it fails, the user is still banned in the DB even if
        # * we proceed to unban from chats. This produces a split-brain state
        # * that `get_active_ban` will treat as still banned, so the join-auto-
        # * ban path in `greeting.py` will re-ban the user the next time they
        # * join. Do not produce a false "unbanned" reply: bail out and tell
        # * the operator that manual cleanup is needed.
        log.error(
            "deactivate_all_active_bans failed for user=%d; aborting unban to "
            "avoid split-brain state: %s",
            target_id,
            deactivate_r,
        )
        if msg is not None:
            try:
                await msg.reply_text(
                    f"{user_ref(target_id, target_fname)} could not be unbanned: "
                    "the database deactivation failed, so the user is still "
                    "marked as banned even if they are now unmuted in chats. "
                    "Check the logs and retry.",
                    parse_mode="HTML",
                )
            except Exception as exc:
                log.debug("Unban DB-fail reply failed: %s", exc)
        return

    # * Include primary groups not already in the connected list
    _primary_ids = [cid for cid in (cfg.main_group, cfg.exec_group) if cid]
    _existing_ids = {grp.get("chat_id", 0) for grp in groups}
    for _pid in _primary_ids:
        if _pid not in _existing_ids:
            groups = [*groups, {"chat_id": _pid, "title": ""}]

    # * unban from all groups - semaphore-bounded for rate safety
    results = await fan_out(
        [
            ctx.bot.unban_chat_member(
                grp.get("chat_id", 0), target_id, only_if_banned=True
            )
            for grp in groups
        ]
    )
    failed = count_transient_errors(results)
    if failed:
        log.error(
            "Unban fan-out had %d/%d transient failures for target=%d; "
            "user may still be banned in those chats",
            failed,
            len(groups),
            target_id,
        )

    lc, lt = cfg.logs
    # * effective_user can be None for anonymous admins; fall back to target info.
    if admin is not None:
        _log_admin_id: int = admin.id
        _log_admin_fname: str = admin.first_name
    else:
        _log_admin_id = target_id
        _log_admin_fname = target_fname
    log_text = parse_logmsg.unban_log(
        target_id,
        target_fname,
        _log_admin_id,
        _log_admin_fname,
        ban_id,
    )

    # * send log; reply only if we have an effective_message.
    if msg is not None:
        log_r, reply_r = await asyncio.gather(
            ctx.bot.send_message(lc, log_text, parse_mode="HTML", message_thread_id=lt),
            msg.reply_text(
                f"{user_ref(target_id, target_fname)} has been unbanned - "
                f"removed from {len(groups) - failed}/{len(groups)} groups.",
                parse_mode="HTML",
            ),
            return_exceptions=True,
        )
        if isinstance(reply_r, BaseException):
            log.debug("Unban reply failed for user %d: %s", target_id, reply_r)
    else:
        log_r = await ctx.bot.send_message(
            lc, log_text, parse_mode="HTML", message_thread_id=lt
        )
    if isinstance(log_r, BaseException):
        log.error("Unban log send failed for user %d: %s", target_id, log_r)
