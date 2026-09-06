# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Federation unban command entry point: validates permissions and delegates to unban_flow."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from telegram.ext import ContextTypes, MessageHandler

from tcbot import database as db
from tcbot.modules.helper import decorators, extraction, identity, replies
from tcbot.modules.helper.decorators import resolve_and_check
from tcbot.modules.helper.formatter import bold, code, mention
from tcbot.modules.helper.workflows.demote_flow import Demote
from tcbot.modules.helper.workflows.unban_flow import execute_unban
from tcbot.utils.prefixes import build_prefixed_filters, parse_cmd_args

if TYPE_CHECKING:
    from telegram import Update

    from tcbot.database.documents import BanDoc

log = logging.getLogger(__name__)

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 60
_RL_LIMIT: int = 5


# ────────────────────── Module & Help Message ───────────────────── #

__module_name__ = "Unban"
__help_text__ = (
    f"Lifts an active federation ban across {bold('all connected groups')} at once."
)

__help_sections__: list[tuple[str, str]] = [
    (
        replies.SEC_COMMANDS,
        f"{code('/tcunban')} (alias: {code('/tcunb')})",
    ),
    replies.who_section(replies.PERM_DEV_ABOVE),
    replies.where_section(replies.CONTEXT_EXEC_OR_GROUP),
    (
        replies.SEC_WHAT,
        "Lifts an active federation ban on the target user. The unban is applied across "
        f"{bold('all connected groups')} simultaneously so they can rejoin freely. A log entry "
        "is posted to the federation logs channel.\n\n"
        "If the user has no active federation ban, the bot will let you know and take no "
        "action.\n"
        "A pending appeal review card is left untouched; resolving appeals stays on the appeal flow.\n"
        "A staff member re-promoted while banned is demoted first so the stale ban can be cleared.",
    ),
    replies.target_section(),
    (
        replies.SEC_EXAMPLES,
        f"{code('/tcunban @username')}\n"
        f"{code('/tcunb 123456789')}\n"
        f"Or reply to a message and run {code('/tcunb')}.",
    ),
]

__help__: replies.HelpEntry = {
    "name": __module_name__,
    "overview": __help_text__,
    "sections": __help_sections__,
}


# ──────────────────── Command Unban </tcunban> ──────────────────── #


@decorators.ratelimiter(limit=_RL_LIMIT, period=_RL_PERIOD_S)
@decorators.mod_only
@decorators.log_execution
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Lift the federation ban on a target user after identity and refusal checks.

    Speculatively pre-fetches the active ban record in parallel with identity
    classification so that ``execute_unban`` skips a redundant DB round-trip when
    the refusal check passes.
    """
    msg = update.effective_message
    admin = update.effective_user
    if msg is None or admin is None:
        return
    args = parse_cmd_args(msg.text)
    target_id, target_fname = await extraction.extract_target(update, args, ctx.bot)
    if not target_id:
        try:
            await msg.reply_text(replies.ERR_CANNOT_RESOLVE)
        except Exception as exc:
            log.debug("unban no-target reply failed: %s", exc)
        return

    # * Classify, pre-fetch the active ban record, and run the
    # * executor-vs-target rank check in parallel. All three depend only on
    # * already-resolved IDs so there is no need to wait for them sequentially.
    # * return_exceptions=True prevents a DB failure from aborting the others.
    # * The rank check (min_role="developer") prevents a low-rank mod from
    # * unbanning a Founder or Admin -- unbanning a higher-ranked target would
    # * silently invert the role-vs-state invariant because unban also clears
    # * all active bans for the user, which is irreversible.
    ident, pre_ban, role_result = await asyncio.gather(
        identity.classify(ctx.bot, admin.id, target_id, target_fname or str(target_id)),
        db.bans_db.get_active_ban(target_id),
        resolve_and_check(msg, admin.id, target_id, min_role="developer"),
        return_exceptions=True,
    )
    if isinstance(ident, BaseException):
        log.exception("identity.classify failed in cmd_unban: %s", ident)
        return
    if isinstance(pre_ban, BaseException):
        log.error(
            "get_active_ban speculative pre-fetch failed for user=%d: %s",
            target_id,
            pre_ban,
        )
        pre_ban = None
    if isinstance(role_result, BaseException):
        log.exception("resolve_and_check failed in cmd_unban: %s", role_result)
        return
    if role_result == (None, None):
        # * resolve_and_check already replied and rejected (insufficient rank
        # * or target outranks executor); end the handler.
        return

    refusal = identity.refuse_message("unban", ident)
    if refusal is not None and ident.kind not in ("admin", "developer", "tester"):
        try:
            await msg.reply_text(refusal, parse_mode="HTML")
        except Exception as exc:
            log.debug("unban refusal reply failed: %s", exc)
        return

    if refusal is not None:
        # * Staff targets are never federation-bannable, so "nothing to undo"
        # * is normally the right reply. Exception: the target was re-promoted
        # * while banned (race or manual grant); then the active ban blocks
        # * cleanup and greeting-time demote never runs (a banned-everywhere
        # * user cannot rejoin to trigger it). Demote first, then fall through
        # * to execute_unban. The speculative pre-fetch above may have failed,
        # * so re-read; a failed re-read keeps the refusal rather than
        # * demoting a staff member with no proven ban.
        target_role = role_result[1]
        ban_record = pre_ban
        if ban_record is None:
            try:
                ban_record = await db.bans_db.get_active_ban(target_id)
            except Exception:
                log.exception(
                    "unban staff re-read failed for target=%d; keeping refusal",
                    target_id,
                )
                ban_record = None
        if ban_record is None:
            try:
                await msg.reply_text(refusal, parse_mode="HTML")
            except Exception as exc:
                log.debug("unban refusal reply failed: %s", exc)
            return
        if target_role:
            try:
                await Demote.execute(
                    ctx.bot,
                    target_id,
                    target_fname or str(target_id),
                    target_role,
                    admin.id,
                    admin.first_name,
                    trigger=None,
                )
            except Exception:
                log.exception(
                    "Demote before unban-cleanup failed for target=%d role=%s",
                    target_id,
                    target_role,
                )
                try:
                    await msg.reply_text(
                        f"{mention(target_id, target_fname or str(target_id))} "
                        f"holds a federation role ({target_role}) and the demote "
                        "step failed, so the unban cannot proceed safely. Demote "
                        "them manually with /tcdemote and retry the unban.",
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    log.debug("unban demote-fail reply failed: %s", exc)
                return

    try:
        await execute_unban(
            update,
            ctx,
            target_id,
            target_fname or str(target_id),
            pre_ban=cast("BanDoc | None", pre_ban),
        )
    except Exception:
        log.exception("execute_unban failed for target=%s", target_id)


# ──────────────────────────── Handlers ──────────────────────────── #

_UNBAN_CMDS = build_prefixed_filters("tcunban") | build_prefixed_filters("tcunb")

__handlers__ = [MessageHandler(_UNBAN_CMDS, cmd_unban)]
