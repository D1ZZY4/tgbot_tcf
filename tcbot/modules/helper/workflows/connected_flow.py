# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Group connection flow: in-group join prompt, permission check, pending monitoring."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from telegram import (
    Bot,
    ChatMember,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper import parse_logmsg
from tcbot.modules.helper.formatter import bold, code
from tcbot.utils.dispatch import count_transient_errors, fan_out
from tcbot.utils.time_and_date import TELEGRAM_LOOKUP_TIMEOUT

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

log = logging.getLogger(__name__)


# ────────────────── Admin Identity Harvest ──────────────────────── #
# * Called as fire-and-forget task when the bot gains access to a new group.
# * Caches every admin's identity so name lookups are available without extra
# * MongoDB or Telegram API round-trips later.

# * Strong references to in-flight admin-harvest background tasks; prevents GC
# * before the coroutine completes (RUF006 compliance).
_harvest_tasks: set[asyncio.Task[None]] = set()


async def _harvest_admin_identities(
    chat_id: int,
    admins: list[ChatMember],
) -> None:
    """Persist identity data for every admin using change-detection writes.

    Uses ``harvest_user_identity`` (live User objects: absent fields clear
    stale values) so the DB write is skipped when the cached identity
    already matches, keeping the harvest nearly free on subsequent runs.
    """
    coros = []
    for member in admins:
        user = getattr(member, "user", None)
        if user is None or user.is_bot or not user.first_name:
            continue
        coros.append(
            db.users_cache.harvest_user_identity(
                user.id,
                user.username,
                user.first_name,
                user.last_name,
            )
        )
    if not coros:
        return
    results = await asyncio.gather(*coros, return_exceptions=True)
    errors = sum(1 for r in results if isinstance(r, BaseException))
    if errors:
        log.debug(
            "Admin identity harvest for chat=%d: %d/%d writes failed",
            chat_id,
            errors,
            len(coros),
        )
    else:
        log.debug(
            "Admin identity harvest for chat=%d: %d identities cached",
            chat_id,
            len(coros),
        )


# ──────────────── User-facing reply constants ──────────────────── #

_ERR_ROLE_CHECK_FAILED = "Could not verify your role."
_ERR_OWNER_ONLY = "Only the group owner can decide."
_ERR_BOT_PERMS_VERIFY = (
    "Could not verify my own permissions. Please promote me as admin and try again."
)
_ERR_COMPLETE_JOIN = (
    "Connection failed due to a database error. Please try again later."
)

_REQUIRED_PERMS: tuple[str, ...] = (
    "can_delete_messages",
    "can_restrict_members",
    "can_invite_users",
)


@dataclass(frozen=True)
class BuildConnection:
    """Configurable group-connection flow builder."""

    community_name: str
    required_perms: tuple[str, ...] = field(default=_REQUIRED_PERMS, kw_only=True)
    join_label: str = field(default="Connect", kw_only=True)
    cancel_label: str = field(default="Cancel", kw_only=True)
    join_callback: str = field(default="tc_join", kw_only=True)
    cancel_callback: str = field(default="tc_cancel", kw_only=True)

    # ── Text factories ─────────────────────────────────────────────────────

    def join_prompt(self) -> str:
        """Return the initial prompt to send when the bot is first added to a group."""
        return f"Want to connect this group to {self.community_name}?"

    def connected_message(self) -> str:
        """Shown (or edited into the prompt) on a successful connection."""
        return (
            f"This community is now connected to {self.community_name}. "
            "Authorized staff can use federation commands here."
        )

    def declined_message(self) -> str:
        """Shown when the owner taps Cancel on the join prompt."""
        return "Connection declined. I'll leave the group now."

    def already_connected_message(self) -> str:
        """Shown when the group is already part of the federation."""
        return f"This group is already connected to {self.community_name}."

    def connecting_message(self) -> str:
        """Progress state edited into the prompt before the ban/mute replay.

        The replay fans every active ban/mute into the new group, which
        takes minutes on large federations; without this the owner stares
        at a dead prompt with live buttons for the whole duration.
        """
        return (
            "Connecting... applying existing federation bans and mutes. "
            "This can take a moment for large federations."
        )

    def perms_required_message(self) -> str:
        """Shown when the bot lacks the required admin permissions."""
        return (
            "Please make the bot an admin with the required permissions "
            "(delete messages, ban users, invite users) and try again."
        )

    # ── Keyboard factory ───────────────────────────────────────────────────

    def join_keyboard(self) -> InlineKeyboardMarkup:
        """Connect / Cancel inline keyboard attached to the join prompt."""
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        self.join_label, callback_data=self.join_callback
                    ),
                    InlineKeyboardButton(
                        self.cancel_label, callback_data=self.cancel_callback
                    ),
                ]
            ]
        )

    # ── Permission check ───────────────────────────────────────────────────

    def check_perms(self, member: ChatMember) -> bool:
        """Return True when member holds every permission in required_perms."""
        return all(getattr(member, p, False) for p in self.required_perms)

    # ── Connection executor ────────────────────────────────────────────────

    async def complete_join(
        self,
        chat_id: int,
        chat_title: str,
        owner_id: int,
        owner_fname: str,
        bot: Bot,
    ) -> None:
        """Connect the group, apply all active federation bans, and notify LOG_CHANNEL."""
        # * Fetch chat info + active ban IDs + active mute docs + admin list + register group + clear pending.
        # * All Telegram and DB calls fire in parallel; bounded timeouts prevent stalls.
        (
            chat_result,
            ban_uids,
            mute_docs,
            admins_result,
            add_group_r,
        ) = await asyncio.gather(
            asyncio.wait_for(bot.get_chat(chat_id), timeout=TELEGRAM_LOOKUP_TIMEOUT),
            db.bans_db.active_ban_user_ids(),
            db.mutes_db.active_mute_docs(),
            asyncio.wait_for(
                bot.get_chat_administrators(chat_id), timeout=TELEGRAM_LOOKUP_TIMEOUT
            ),
            db.groups_db.add_group(chat_id, chat_title, owner_id),
            return_exceptions=True,
        )
        admins_list = (
            list(admins_result) if not isinstance(admins_result, BaseException) else []
        )
        # * add_group is the critical write; if it failed the group is not in the DB.
        # * Re-raise so callers can detect failure and avoid sending a false confirmation.
        if isinstance(add_group_r, BaseException):
            raise RuntimeError(f"add_group failed for chat {chat_id}") from add_group_r
        # * Clear the pending row only after the group record lands. The two
        # * calls ran in parallel before, so an add_group failure still wiped
        # * the pending row and left the owner with no retry path but re-add.
        try:
            await db.groups_db.remove_pending(chat_id)
        except Exception:
            log.exception("remove_pending failed for chat %d after connect", chat_id)
        chat_username: str | None = (
            getattr(chat_result, "username", None)
            if not isinstance(chat_result, BaseException)
            else None
        )
        if isinstance(ban_uids, BaseException):
            ban_uids = []
        if isinstance(mute_docs, BaseException):
            mute_docs = []

        # * Harvest admin identities into the member cache (fire-and-forget, best-effort).
        # * Strong reference kept in _harvest_tasks to prevent GC before completion.
        if not isinstance(admins_result, BaseException) and admins_result:
            try:
                task = asyncio.get_running_loop().create_task(
                    _harvest_admin_identities(chat_id, admins_list)
                )
                _harvest_tasks.add(task)
                task.add_done_callback(_harvest_tasks.discard)
            except RuntimeError:
                log.debug("Harvest task skipped: no running event loop.")

        # * Apply all existing federation bans concurrently - semaphore-bounded.
        # * Use count_transient_errors (not count_errors) so a "user was not in
        # * this chat" BadRequest does not count as a real failure. For a
        # * ban-replay, the user not being in the chat is the desired end
        # * state -- counting it as a failure would overstate the failure rate.
        results = await fan_out([bot.ban_chat_member(chat_id, uid) for uid in ban_uids])
        applied_bans = len(results) - count_transient_errors(results)

        # * Apply all existing federation mutes concurrently - semaphore-bounded.
        # * Use count_transient_errors (not count_errors) so a benign
        # * "user not in chat" or "user is a bot" BadRequest does not count
        # * as a real failure -- the desired end state is that the user is
        # * restricted, and they were never in the chat to begin with.
        _mute_perms = ChatPermissions(can_send_messages=False)
        mute_results = await fan_out(
            [
                bot.restrict_chat_member(
                    chat_id,
                    doc.get("user_id", 0),
                    permissions=_mute_perms,
                    until_date=doc.get("until_date"),
                )
                for doc in mute_docs
            ]
        )
        applied_mutes = len(mute_results) - count_transient_errors(mute_results)

        lc, lt = cfg.logs
        try:
            await bot.send_message(
                lc,
                parse_logmsg.group_connected_log(
                    chat_id, chat_title, owner_id, owner_fname, chat_username
                ),
                parse_mode="HTML",
                message_thread_id=lt,
            )
        except Exception:
            log.exception("Group connect log failed")

        log.info(
            "Group %d ('%s') connected. %d bans and %d mutes applied.",
            chat_id,
            chat_title,
            applied_bans,
            applied_mutes,
        )

    # ── PTB event handlers ─────────────────────────────────────────────────

    async def on_bot_added(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle every change to the bot's own member status in any chat."""
        cmc = update.my_chat_member
        if not cmc:
            return

        chat = cmc.chat
        if chat.type not in ("group", "supergroup"):
            return

        new_status = cmc.new_chat_member.status
        old_status = cmc.old_chat_member.status if cmc.old_chat_member else None
        by_user = cmc.from_user
        lc, lt = cfg.logs

        if new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            # * is_connected, deactivate, and remove_pending all run in parallel
            was_connected, *_ = await asyncio.gather(
                db.groups_db.is_connected(chat.id),
                db.groups_db.deactivate_group(chat.id),
                db.groups_db.remove_pending(chat.id),
                return_exceptions=True,
            )
            if isinstance(was_connected, BaseException):
                log.debug(
                    "is_connected check failed on bot removal from %d: %s",
                    chat.id,
                    was_connected,
                )
                was_connected = False
            if was_connected:
                try:
                    await ctx.bot.send_message(
                        lc,
                        parse_logmsg.group_bot_removed_log(
                            chat.id, chat.title or "Unknown"
                        ),
                        parse_mode="HTML",
                        message_thread_id=lt,
                    )
                except Exception:
                    log.exception("Bot removed log failed for %d", chat.id)
            log.info("Bot removed from %d; group deactivated", chat.id)
            return

        # Demotion: bot lost admin rights (was administrator, now member or restricted).
        # The group is NOT deactivated because permissions may be restored shortly, but
        # a warning is sent to the mod channel so staff are aware of the enforcement gap.
        # Primary groups (MAIN_GROUP, EXTEND_GROUP) are excluded from this warning since
        # they are managed separately and are not in the federated_groups collection.
        if (
            new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED)
            and old_status == ChatMemberStatus.ADMINISTRATOR
        ):
            if not cfg.is_primary_group(chat.id):
                warning_text = (
                    f"Bot was demoted in group"
                    f" {bold(chat.title or str(chat.id))}"
                    f" (id: {code(str(chat.id))})."
                    " Federation bans cannot be enforced there until"
                    " admin rights are restored."
                )
                try:
                    await ctx.bot.send_message(
                        lc,
                        warning_text,
                        parse_mode="HTML",
                        message_thread_id=lt,
                    )
                except Exception as exc:
                    log.warning(
                        "Failed to send admin-rights-lost warning for chat=%d: %s",
                        chat.id,
                        exc,
                    )
            return

        if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
            # * Primary groups must never enter the federation join flow: no
            # * prompt, no pending row. They are required enforcement
            # * destinations, not connectable members.
            if cfg.is_primary_group(chat.id):
                return
            # * Speculatively pre-fetch both reads in parallel; is_already_connected
            # * is only consumed if the pending+ADMINISTRATOR fast path is not taken.
            pending, is_already_connected = await asyncio.gather(
                db.groups_db.get_pending(chat.id),
                db.groups_db.is_connected(chat.id),
                return_exceptions=True,
            )
            if isinstance(pending, BaseException):
                pending = None
            if isinstance(is_already_connected, BaseException):
                is_already_connected = False

            if pending and new_status == ChatMemberStatus.ADMINISTRATOR:
                if self.check_perms(cmc.new_chat_member):
                    owner_fname = await db.users_cache.get_first_name(
                        pending.get("owner_id", 0), "Owner"
                    )
                    # * complete_join (applies bans/mutes, sends log) is the
                    # * authoritative state write; run it first so we only
                    # * edit the join prompt to "connected" when the DB write
                    # * actually succeeded. Editing the prompt optimistically
                    # * in parallel was a bug: a complete_join failure would
                    # * leave the owner with a false confirmation while the
                    # * group remained absent from federated_groups.
                    # * Show progress first: the replay below can take
                    # * minutes on large federations. Stripping the buttons
                    # * also closes the double-tap window. Best-effort: the
                    # * final edit still lands when this one fails.
                    with contextlib.suppress(Exception):
                        await ctx.bot.edit_message_text(
                            self.connecting_message(),
                            chat_id=chat.id,
                            message_id=pending.get("message_id", 0),
                            reply_markup=None,
                        )
                    try:
                        await self.complete_join(
                            chat.id,
                            chat.title or "",
                            pending.get("owner_id", 0),
                            owner_fname,
                            ctx.bot,
                        )
                    except Exception:
                        log.exception(
                            "complete_join failed in on_bot_added for chat %d",
                            chat.id,
                        )
                        return
                    try:
                        await ctx.bot.edit_message_text(
                            self.connected_message(),
                            chat_id=chat.id,
                            message_id=pending.get("message_id", 0),
                            reply_markup=None,
                        )
                    except Exception as exc:
                        log.debug("Failed to edit pending connect prompt: %s", exc)
                return

            if is_already_connected:
                return

            if pending:
                return

            # * from_user is None when an anonymous admin adds the bot.
            # * We cannot store a valid owner_id in that case, so skip silently.
            if not by_user:
                log.info(
                    "Bot added to %d by anonymous admin; skipping join prompt",
                    chat.id,
                )
                return

            try:
                prompt = await ctx.bot.send_message(
                    chat.id,
                    self.join_prompt(),
                    reply_markup=self.join_keyboard(),
                )
                await db.groups_db.add_pending(
                    chat.id,
                    chat.title or "",
                    by_user.id,
                    prompt.message_id,
                )
            except Exception:
                log.exception("Join prompt send failed for %d", chat.id)

    async def on_join_decision(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle Connect / Cancel button callbacks on the join prompt."""
        q = update.callback_query
        chat = update.effective_chat
        user = update.effective_user
        if q is None or chat is None or user is None:
            return
        lc, lt = cfg.logs

        # * Gather q.answer() + member check in parallel so the spinner
        # * disappears immediately regardless of Telegram API latency.
        member_res, _ = await asyncio.gather(
            asyncio.wait_for(
                ctx.bot.get_chat_member(chat.id, user.id),
                timeout=TELEGRAM_LOOKUP_TIMEOUT,
            ),
            q.answer(),
            return_exceptions=True,
        )
        msg = update.effective_message
        if isinstance(member_res, BaseException):
            log.debug("Join decision role check failed: %s", member_res)
            coros: list = [q.edit_message_reply_markup(None)]
            if msg:
                coros.append(msg.reply_text(_ERR_ROLE_CHECK_FAILED))
            await asyncio.gather(*coros, return_exceptions=True)
            return

        if member_res.status != ChatMemberStatus.OWNER:
            coros = [q.edit_message_reply_markup(None)]
            if msg:
                coros.append(msg.reply_text(_ERR_OWNER_ONLY))
            await asyncio.gather(*coros, return_exceptions=True)
            return

        action = q.data

        if action == self.join_callback:
            try:
                bot_member = await asyncio.wait_for(
                    ctx.bot.get_chat_member(chat.id, ctx.bot.id),
                    timeout=TELEGRAM_LOOKUP_TIMEOUT,
                )
            except Exception as exc:
                log.debug("Join decision permission check failed: %s", exc)
                try:
                    await q.edit_message_text(_ERR_BOT_PERMS_VERIFY, reply_markup=None)
                except Exception as exc2:
                    log.debug("Join decision perms-verify edit failed: %s", exc2)
                return

            if not self.check_perms(bot_member):
                prompt_msg_id = q.message.message_id if q.message else 0
                # * Persist the pending row before overwriting the prompt: if
                # * add_pending fails there is nothing to approve later, so
                # * report the error instead of showing perms-required.
                try:
                    await db.groups_db.add_pending(
                        chat.id,
                        chat.title or "",
                        user.id,
                        prompt_msg_id,
                    )
                except Exception:
                    log.exception("add_pending failed for chat %d", chat.id)
                    try:
                        await q.edit_message_text(_ERR_COMPLETE_JOIN, reply_markup=None)
                    except Exception as exc:
                        log.debug("Join decision db-error edit failed: %s", exc)
                    return
                try:
                    await q.edit_message_text(
                        self.perms_required_message(), reply_markup=None
                    )
                except Exception as exc:
                    log.debug("Join decision perms-required edit failed: %s", exc)
                return

            # * Guarded like every neighbouring DB call: an outage here must
            # * report the error instead of leaving the prompt hanging.
            try:
                already_connected = await db.groups_db.is_connected(chat.id)
            except Exception:
                log.exception("is_connected failed for chat %d", chat.id)
                with contextlib.suppress(Exception):
                    await q.edit_message_text(_ERR_COMPLETE_JOIN, reply_markup=None)
                return
            if already_connected:
                try:
                    await q.edit_message_text(
                        self.already_connected_message(), reply_markup=None
                    )
                except Exception as exc:
                    log.debug("Join decision already-connected edit failed: %s", exc)
                return

            # * complete_join must succeed before showing the success message.
            # * Running both in a gather was a bug: if complete_join raised, the group
            # * was never persisted but the owner saw "connected" anyway.
            # * Show progress first (same rationale as the on_bot_added path
            # * above): the ban/mute replay can take minutes, and stripping
            # * the buttons closes the double-tap window.
            with contextlib.suppress(Exception):
                await q.edit_message_text(self.connecting_message(), reply_markup=None)
            try:
                await self.complete_join(
                    chat.id, chat.title or "", user.id, user.first_name, ctx.bot
                )
            except Exception:
                log.exception("complete_join failed for chat %d", chat.id)
                with contextlib.suppress(Exception):
                    await q.edit_message_text(_ERR_COMPLETE_JOIN, reply_markup=None)
                return
            with contextlib.suppress(Exception):
                await q.edit_message_text(self.connected_message(), reply_markup=None)

        elif action == self.cancel_callback:
            # * Remove the pending row first: if it survives while the bot
            # * leaves, the next on_bot_added sees a stale pending and drops
            # * the join silently (deadlock with no prompt).
            try:
                await db.groups_db.remove_pending(chat.id)
            except Exception:
                log.exception("remove_pending failed for chat %d on cancel", chat.id)
            await asyncio.gather(
                q.edit_message_text(self.declined_message(), reply_markup=None),
                ctx.bot.send_message(
                    lc,
                    parse_logmsg.group_connection_rejected_log(
                        chat.id,
                        chat.title or "Unknown",
                        user.id,
                        user.first_name,
                    ),
                    parse_mode="HTML",
                    message_thread_id=lt,
                ),
                ctx.bot.leave_chat(chat.id),
                return_exceptions=True,
            )


# ────────────────────── Module-level instance ───────────────────── #

connection = BuildConnection(cfg.community_name)
