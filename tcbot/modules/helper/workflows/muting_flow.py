# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Mute/unmute executor + conversation factory."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from telegram import Bot, ChatPermissions, Update

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper import keyboards, parse_logmsg, replies
from tcbot.modules.helper.formatter import (
    bold,
    esc,
    user_ref,
)
from tcbot.modules.helper.parse_link import message_link
from tcbot.modules.helper.workflows.demote_flow import Demote
from tcbot.modules.helper.workflows.proof_flow import BuildProof, upload_proof
from tcbot.modules.helper.workflows.reason_flow import BuildReason, build_modaction_conv
from tcbot.utils.dispatch import count_transient_errors, fan_out
from tcbot.utils.time_and_date import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable

    from telegram.ext import ContextTypes
    from telegram.ext.filters import BaseFilter

log = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^(\d+)(ye|mo|[smhdw])$", re.IGNORECASE)

# * Time-math constants for fmt_duration and parse_duration.
_SECS_PER_HOUR: int = 3_600
_SECS_PER_DAY: int = 86_400
_DAYS_PER_YEAR: int = 365
# * Upper bound for an accepted duration (100 years in days). Larger inputs
# * would overflow timedelta construction or the utc_now() + duration date
# * math below, crashing the command on malicious input such as 9999999999ye.
# * Rejected values return None so the token falls back to reason text,
# * matching the existing invalid-token behavior. All documented examples
# * (up to 2ye) are far below this cap.
_MAX_DURATION_DAYS: int = 36500

# * Per-action BuildReason and BuildProof instances; imported by muting.py
reason = BuildReason("mute")
proof = BuildProof("mute")

_ERR_DB_RETRY = "I couldn't reach the database right now. Please try again in a moment."


# ──────────────────────── Duration helpers ──────────────────────── #


def parse_duration(raw: str) -> timedelta | None:
    """Parse a single duration token like '3d', '1mo', '2ye'. Returns None if invalid."""
    m = _DURATION_RE.match(raw.strip())
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2).lower()
    try:
        mapping = {
            "s": timedelta(seconds=value),
            "m": timedelta(minutes=value),
            "h": timedelta(hours=value),
            "d": timedelta(days=value),
            "w": timedelta(weeks=value),
            "mo": timedelta(days=value * 30),
            "ye": timedelta(days=value * _DAYS_PER_YEAR),
        }
        result = mapping.get(unit)
    except OverflowError:
        return None
    if result is None or result.days > _MAX_DURATION_DAYS:
        return None
    return result


def fmt_duration(td: timedelta | None) -> str:
    """Human-readable duration string for use in replies."""
    if td is None:
        return "permanently"
    total = int(td.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < _SECS_PER_HOUR:
        return f"{total // 60}m"
    if total < _SECS_PER_DAY:
        return f"{total // _SECS_PER_HOUR}h"
    days = total // _SECS_PER_DAY
    if days < 7:
        return f"{days}d"
    if days < 30:
        return f"{days // 7}w"
    if days < _DAYS_PER_YEAR:
        return f"{days // 30}mo"
    return f"{days // _DAYS_PER_YEAR}ye"


# ────────────────────────── Mute executor ───────────────────────── #


async def _execute_mute(bot: Bot, update: Update, meta: dict) -> None:
    """Apply a federation-wide mute across all connected groups and edit the prompt to a summary."""
    target_id = meta["mute_target_id"]
    target_fname = meta["mute_target_fname"]
    reason_text = meta.get("mute_reason") or replies.NO_REASON
    admin_id = meta["mute_admin_id"]
    duration = meta.get("mute_duration")
    proof_msgs = meta.get("mute_proof_msgs")
    prompt_chat = meta.get("mute_prompt_chat")
    prompt_id = meta.get("mute_prompt_id")
    dur_str = fmt_duration(duration)

    until = utc_now() + duration if duration else None
    perms = ChatPermissions(can_send_messages=False)
    duration_secs = int(duration.total_seconds()) if duration else None
    admin_fname = meta.get("mute_admin_fname", "Admin")

    # * Guard up front: without the origin chat there is no audit row to
    # * write and no summary target, so bail before any side effect.
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        log.warning("_execute_mute called without effective_chat")
        return

    # * Fail closed like the ban flow and the warn auto-ban: a groups-fetch
    # * outage aborts before anything is touched.
    try:
        groups = await db.groups_db.active_groups()
    except Exception:
        log.exception("_execute_mute: active_groups failed for target=%d", target_id)
        try:
            await bot.edit_message_text(
                f"{user_ref(target_id, target_fname)} could not be muted: "
                "the group list could not be loaded from the database, so no "
                "groups were touched. Check the logs and retry with /tcmute "
                "once the database recovers.",
                chat_id=prompt_chat,
                message_id=prompt_id,
                parse_mode="HTML",
            )
        except Exception as exc:
            log.debug("_execute_mute groups-fail edit failed: %s", exc)
        return
    _primary_ids = [cid for cid in (cfg.main_group, cfg.exec_group) if cid]
    _existing_ids = {cid for cid in (grp.get("chat_id") for grp in groups) if cid}
    groups = groups + [
        {"chat_id": pid, "title": ""}
        for pid in _primary_ids
        if pid not in _existing_ids
    ]

    # * Persist the mute record before touching any group. Enforcing chats
    # * without an active_mutes row leaves an un-unmutable split brain:
    # * /tcunmute guards on get_active_mute and would refuse, forcing a
    # * manual per-group unrestrict. A failed write aborts with no group
    # * touched; the moderator retries once the database recovers.
    log_r, active_r = await asyncio.gather(
        db.mutes_db.log_mute(
            target_id, chat_id, reason_text, admin_id, duration_secs=duration_secs
        ),
        db.mutes_db.set_active_mute(target_id, until=until),
        return_exceptions=True,
    )
    if isinstance(log_r, BaseException) or isinstance(active_r, BaseException):
        log.error(
            "_execute_mute: mute DB write failed for target=%d (log=%s active=%s)",
            target_id,
            log_r,
            active_r,
        )
        try:
            await bot.edit_message_text(
                f"{user_ref(target_id, target_fname)} could not be muted: "
                "the mute record could not be written to the database, so no "
                "groups were touched. Check the logs and retry with /tcmute "
                "once the database recovers.",
                chat_id=prompt_chat,
                message_id=prompt_id,
                parse_mode="HTML",
            )
        except Exception as exc:
            log.debug("_execute_mute DB-fail edit failed: %s", exc)
        return
    # * Re-check the target's effective role immediately before the
    # * restrict fan-out. The auto-demote in ``cmd_mute`` ran before the
    # * proof collection window; if the target was re-promoted by a
    # * concurrent command during that window, the role cache and the
    # * live DB would both report them as staff. Demote again here to
    # * preserve the role-vs-state invariant right up to the restrict
    # * fan-out. Best-effort like the entry-point demote.
    # * The lookup itself is guarded too: entry authorization already
    # * fail-closed, and this re-check is defense-in-depth, so a transient
    # * lookup failure logs loudly and proceeds as non-staff instead of
    # * aborting an authorized mute with no reply.
    try:
        pre_fanout_role = await db.users_roles.get_effective_role(target_id)
    except Exception:
        log.exception(
            "_execute_mute: pre-fanout role lookup failed for target %d; "
            "proceeding with mute anyway",
            target_id,
        )
        pre_fanout_role = None
    if pre_fanout_role:
        try:
            await Demote.execute(
                bot,
                target_id,
                target_fname,
                pre_fanout_role,
                admin_id,
                admin_fname,
                trigger="mute",
            )
            log.info(
                "_execute_mute: re-demoted target %d (role=%s) before "
                "fan-out to close the proof-collection TOCTOU window",
                target_id,
                pre_fanout_role,
            )
        except Exception:
            log.exception(
                "_execute_mute: re-demote before fan-out failed for target %d "
                "(role=%s); proceeding with mute anyway",
                target_id,
                pre_fanout_role,
            )
    results = await fan_out(
        [
            bot.restrict_chat_member(
                grp.get("chat_id", 0),
                target_id,
                permissions=perms,
                until_date=until,
            )
            for grp in groups
        ]
    )
    failed = count_transient_errors(results)
    if failed:
        log.error(
            "Mute fan-out had %d/%d transient failures for target=%d",
            failed,
            len(groups),
            target_id,
        )

    admin_fname = meta.get("mute_admin_fname", "Admin")

    proof_link: str | None = None
    if proof_msgs:
        try:
            pc, pt = cfg.proofs
            caption = parse_logmsg.proof_caption_new(
                target_id, admin_id, admin_fname, utc_now()
            )
            pmid = await upload_proof(bot, proof_msgs, caption, pc, pt)
            if pmid:
                proof_link = message_link(pc, pmid, pt)
        except Exception:
            log.warning("Mute proof upload skipped for target=%d", target_id)

    proof_kb = keyboards.action_proof_kb(target_id, proof_link)
    summary = (
        f"{user_ref(target_id, target_fname)} "
        f"has been muted {bold(dur_str)}.\n"
        f"Reason: {esc(reason_text)}\n"
        f"Applied to {len(groups) - failed}/{len(groups)} groups."
    )

    lc, lt = cfg.logs
    log_text = parse_logmsg.mute_log(
        target_id,
        target_fname,
        admin_id,
        admin_fname,
        reason_text,
        dur_str,
    )

    # * Post to the log channel and edit the prompt summary in parallel.
    # * The audit row and the active-mute record already landed before the
    # * fan-out above, so only Telegram deliveries remain here.
    log_send_r, edit_r = await asyncio.gather(
        bot.send_message(
            lc, log_text, parse_mode="HTML", message_thread_id=lt, reply_markup=proof_kb
        ),
        bot.edit_message_text(
            summary,
            chat_id=prompt_chat,
            message_id=prompt_id,
            parse_mode="HTML",
            reply_markup=proof_kb,
        ),
        return_exceptions=True,
    )
    if isinstance(log_send_r, BaseException):
        log.error("Mute log send failed: %s", log_send_r)
    if isinstance(edit_r, BaseException):
        msg = update.effective_message
        if msg:
            try:
                await msg.reply_text(summary, parse_mode="HTML", reply_markup=proof_kb)
            except Exception as exc:
                log.debug("Mute summary fallback reply failed: %s", exc)


# ───────────────────────── Unmute executor ──────────────────────── #


async def execute_unmute(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    target_id: int,
    target_name: str,
) -> None:
    """Restore full send permissions across all connected groups.

    Guards against issuing a federation-wide unrestrict when no active mute
    record exists, mirroring the ``get_active_ban`` guard in ``execute_unban``.
    """
    msg = update.effective_message
    admin = update.effective_user
    if msg is None or admin is None:
        return

    # * Guard: only proceed if an active mute record exists.
    # * Without this check, execute_unmute would fan restrict_chat_member to all
    # * connected groups even when the user was never muted (or already unmuted),
    # * producing a misleading "restored N/N groups" success reply for a no-op.
    # * A failed read fails closed with a retry reply: treating an outage as
    # * "no active mute" would refuse a legitimate unmute.
    try:
        active_mute = await db.mutes_db.get_active_mute(target_id)
    except Exception:
        log.exception("get_active_mute failed for target=%d", target_id)
        try:
            await msg.reply_text(_ERR_DB_RETRY)
        except Exception as exc:
            log.debug("execute_unmute DB-fail reply failed: %s", exc)
        return
    if active_mute is None:
        try:
            await msg.reply_text(
                f"{user_ref(target_id, target_name)} has no active federation mute.",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.debug("execute_unmute no-mute reply failed: %s", exc)
        return

    full_perms = ChatPermissions(
        can_send_messages=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
    )

    # * Fail closed like _execute_mute: a groups-fetch outage aborts before
    # * anything is touched, so the active record stays and a retry works.
    # * Letting the exception propagate would skip the unmute with no reply.
    try:
        groups = await db.groups_db.active_groups()
    except Exception:
        log.exception("active_groups failed during unmute of %d", target_id)
        try:
            await msg.reply_text(_ERR_DB_RETRY)
        except Exception as exc:
            log.debug("execute_unmute groups-fail reply failed: %s", exc)
        return
    # * Unrestrict across all connected groups + primary groups - semaphore-bounded
    _pri_ids = [cid for cid in (cfg.main_group, cfg.exec_group) if cid]
    _ex_ids = {cid for cid in (grp.get("chat_id") for grp in groups) if cid}
    groups = groups + [
        {"chat_id": pid, "title": ""} for pid in _pri_ids if pid not in _ex_ids
    ]
    results = await fan_out(
        [
            ctx.bot.restrict_chat_member(
                grp.get("chat_id", 0),
                target_id,
                permissions=full_perms,
            )
            for grp in groups
        ]
    )
    failed = count_transient_errors(results)
    if failed:
        log.error(
            "Unmute fan-out had %d/%d transient failures for target=%d",
            failed,
            len(groups),
            target_id,
        )

    lc, lt = cfg.logs
    log_text = parse_logmsg.unmute_log(
        target_id,
        target_name,
        admin.id,
        admin.first_name,
    )

    reply = (
        f"{user_ref(target_id, target_name)} has been unmuted - "
        f"restored in {len(groups) - failed}/{len(groups)} groups."
    )

    # * Clear active mute record, send log to channel, and reply - all in parallel
    if lc:
        results2 = await asyncio.gather(
            db.mutes_db.clear_active_mute(target_id),
            ctx.bot.send_message(lc, log_text, parse_mode="HTML", message_thread_id=lt),
            msg.reply_text(reply, parse_mode="HTML"),
            return_exceptions=True,
        )
        if isinstance(results2[0], BaseException):
            log.error(
                "clear_active_mute failed for target=%d: %s", target_id, results2[0]
            )
        if isinstance(results2[1], BaseException):
            log.error("Unmute log send failed: %s", results2[1])
        if isinstance(results2[2], BaseException):
            log.debug("execute_unmute reply failed: %s", results2[2])
    else:
        results2 = await asyncio.gather(
            db.mutes_db.clear_active_mute(target_id),
            msg.reply_text(reply, parse_mode="HTML"),
            return_exceptions=True,
        )
        if isinstance(results2[0], BaseException):
            log.error(
                "clear_active_mute failed for target=%d: %s", target_id, results2[0]
            )
        if isinstance(results2[1], BaseException):
            log.debug("execute_unmute no-log reply failed: %s", results2[1])


# ──────────────────────── Executor adapter ──────────────────────── #


async def _exec_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Copy mute data from user_data, clean up, then call _execute_mute."""
    meta: dict[str, Any] = {}
    if ctx.user_data is not None:
        meta = {k: v for k, v in ctx.user_data.items() if k.startswith("mute_")}
        for k in list(meta):
            ctx.user_data.pop(k, None)
    await _execute_mute(ctx.bot, update, meta)


# ─────────────────── ConversationHandler factory ────────────────── #


def mute_conversation(
    entry_fn: Callable[..., Any],
    entry_filter: BaseFilter,
    *,
    escape_filter: BaseFilter | None = None,
) -> object:
    """Return the mute ConversationHandler via the central reason_flow factory."""
    return build_modaction_conv(
        reason,
        proof,
        entry_fn,
        _exec_mute,
        entry_filter,
        escape_filter=escape_filter,
    )
