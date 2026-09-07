# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Identity helpers: classify users and produce identity-aware moderation replies.

Classify a user (self / bot / Telegram / Founder / staff / regular) and produce
friendly, identity-aware replies for moderation commands.

The bot voice is professional, friendly, and formal with light dry humour. Plain text
only; no pictograph emoji, no text emoticons. One short witty line per identity is
enough; no exclamation cascades.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tcbot import database as db
from tcbot.modules.helper.formatter import esc, user_ref

if TYPE_CHECKING:
    from telegram import Bot

# ───────────────────────────── Constants ────────────────────────────── #

# * Telegram's official service / system account ID. The 777000 channel ID
# * also surfaces on forwarded service messages but never as an action target.
TELEGRAM_USER_ID = 777000

# * Telegram's internal ID for the GroupAnonymousBot placeholder, which appears
# * as the sender when a real admin posts using "send message as group" mode.
# * The true identity is unknown to the bot, so federation commands are refused.
ANONYMOUS_BOT_ID = 1087968824

IdentityKind = Literal[
    "self",  # the executor targeted themselves
    "this_bot",  # this bot is the target
    "other_bot",  # any other Telegram bot
    "telegram",  # Telegram service account
    "anon_admin",  # GroupAnonymousBot
    "founder",  # the federation Founder
    "admin",  # federation Admin (staff)
    "developer",  # federation Developer (custom role)
    "tester",  # federation Tester (custom role)
    "user",  # regular user, no federation role
]


@dataclass(frozen=True)
class Identity:
    """Resolved identity for a moderation target."""

    kind: IdentityKind
    target_id: int
    fname: str
    username: str | None = None
    is_bot: bool = False

    @property
    def role_label(self) -> str | None:
        """Public-facing role label or ``None`` for non-staff identities."""
        return (
            db.users_roles.ROLE_LABEL.get(self.kind)
            if self.kind in db.users_roles.ROLE_LABEL
            else None
        )


# ────────────────────────── Resolution ──────────────────────────── #


async def classify(
    bot: Bot,
    executor_id: int,
    target_id: int,
    target_fname: str | None = None,
    *,
    target_is_bot: bool | None = None,
) -> Identity:
    """Return an :class:`Identity` for ``target_id`` relative to ``executor_id``.

    Parameters
    ----------
    bot:
        Live PTB bot. Used to detect targets that are *this* bot.
    executor_id:
        The user invoking the moderation command.
    target_id:
        The user being acted on.
    target_fname:
        Best-effort first name from upstream resolution; falls back to a cache
        lookup, then to the bare numeric ID string (``str(target_id)``).
    target_is_bot:
        Optional pre-known bot flag (e.g. from ``bot.get_chat`` upstream).

    Notes
    -----
    Both the name cache lookup and the role cache lookup are independent reads;
    they run in parallel via :func:`asyncio.gather` so the total latency is one
    round-trip rather than two, even when the role is ultimately unused (e.g.
    self / bot / Telegram early-returns).

    Caller contract: a role-lookup failure degrades to ``kind="user"`` (fail
    open), so every moderation caller MUST pair this with an independent
    fail-closed role read: ``resolve_and_check`` for ban/kick/mute/warn/unban
    entries, or a guarded ``get_effective_role`` fetch for promote/demote.
    The paired read rejects the action on lookup failure, which is what keeps
    the degraded ``"user"`` kind from becoming an authorization bypass.

    """
    # * Both are independent cached reads; run in parallel.
    # * return_exceptions=True prevents a transient DB error from propagating
    # * to every moderation command that calls classify().
    results = await asyncio.gather(
        db.users_cache.get_user_mention_data(target_id),
        db.users_roles.get_effective_role(target_id),
        return_exceptions=True,
    )
    (cached_fname, target_username) = (
        results[0] if not isinstance(results[0], BaseException) else (None, None)
    )
    role = results[1] if not isinstance(results[1], BaseException) else None
    # * Override fname when it looks like a fallback: missing, the legacy
    # * "User <id>" format, or a bare numeric string returned by _best_name().
    if (
        not target_fname
        or target_fname.startswith("User ")
        or target_fname.lstrip("-").isdigit()
    ):
        target_fname = cached_fname
    # * Guard: cached_fname is None when the DB call itself raised an exception.
    if not target_fname:
        target_fname = str(target_id)

    if target_id == executor_id:
        return Identity("self", target_id, target_fname, target_username, is_bot=False)
    if target_id == bot.id:
        return Identity(
            "this_bot", target_id, target_fname, target_username, is_bot=True
        )
    if target_id == TELEGRAM_USER_ID:
        return Identity(
            "telegram", target_id, target_fname, target_username, is_bot=False
        )
    if target_id == ANONYMOUS_BOT_ID:
        return Identity(
            "anon_admin", target_id, target_fname, target_username, is_bot=True
        )
    if target_is_bot:
        return Identity(
            "other_bot", target_id, target_fname, target_username, is_bot=True
        )

    if role == "founder":
        return Identity("founder", target_id, target_fname, target_username)
    if role == "admin":
        return Identity("admin", target_id, target_fname, target_username)
    if role == "developer":
        return Identity("developer", target_id, target_fname, target_username)
    if role == "tester":
        return Identity("tester", target_id, target_fname, target_username)
    return Identity("user", target_id, target_fname, target_username)


def _line(ident: Identity) -> str:
    """Build the canonical ``mention - <id>`` chunk for an identity."""
    return user_ref(ident.target_id, ident.fname, ident.username)


# ────────────────── Per-action witty refusals ───────────────────── #
# * Each map covers identities that should *not* be acted on. ``user`` and
# * lower-rank staff are never returned here; those go through the normal
# * moderation flow. The reply is one short professional-but-friendly line.

# * Staff roles (admin/developer/tester) are intentionally absent from the
# * ban/kick/mute tables: those entries auto-demote the target via
# * Demote.execute before enforcing, so refusing here would make the
# * documented auto-demote unreachable.
_BAN_REFUSE: dict[IdentityKind, str] = {
    "self": "Self-ban? Creative, but no. Federation needs you here.",
    "this_bot": "I keep this place running. Banning me is a no-go.",
    "telegram": "Telegram itself? Bold move. Not happening.",
    "anon_admin": "Cannot ban the anonymous admin placeholder.",
    "founder": "{line} runs the place, can't ban them through here.",
}

_KICK_REFUSE: dict[IdentityKind, str] = {
    "self": "Kicking yourself? Just leave the group instead.",
    "this_bot": "Kick me? I run this place.",
    "telegram": "Pretty sure I can't kick Telegram from its own group.",
    "anon_admin": "Cannot kick the anonymous admin placeholder.",
    "founder": "{line} runs the place, not getting kicked here.",
}

_MUTE_REFUSE: dict[IdentityKind, str] = {
    "self": "Mute yourself? That's not how this works.",
    "this_bot": "Muting me won't do much - I don't message on my own anyway.",
    "telegram": "Telegram service messages aren't muteable from here.",
    "anon_admin": "Cannot mute the anonymous admin placeholder.",
    "founder": "{line} runs the place, mute button doesn't apply.",
}

_WARN_REFUSE: dict[IdentityKind, str] = {
    "self": "Self-warning is just journaling. Ask a mod if needed.",
    "this_bot": "Warn me? I'm the one tracking warnings around here.",
    "telegram": "Telegram doesn't take warnings, sorry.",
    "other_bot": "{line} is a bot. Bots do not accumulate federation warnings.",
    "anon_admin": "Cannot warn the anonymous admin placeholder.",
    "founder": "{line} runs the place. Warnings do not apply to the Founder.",
}

_UNBAN_REFUSE: dict[IdentityKind, str] = {
    "self": "Can't unban yourself. Use /checkme and submit an appeal instead.",
    "this_bot": "{line} - I manage the bans, not collect them.",
    "telegram": "Telegram was never on the ban list anyway.",
    "anon_admin": "Anonymous admin was never on the ban list.",
    "founder": "{line} - never been banned, nothing to undo.",
    "admin": "{line} is an Admin. Staff aren't federation-bannable, nothing to undo.",
    "developer": "{line} is a Developer. Staff aren't federation-bannable, nothing to undo.",
    "tester": "{line} is a Tester. Staff aren't federation-bannable, nothing to undo.",
}

_UNMUTE_REFUSE: dict[IdentityKind, str] = {
    "self": "Can't unmute yourself - ask a mod.",
    "this_bot": "{line} - bots aren't muteable, nothing to undo.",
    "telegram": "Telegram service was never muted.",
    "other_bot": "{line} is a bot. Bots cannot be muted, so there is nothing to undo.",
    "anon_admin": "Anonymous admin was never muted.",
    "founder": "{line} - definitely not muted.",
}

_PROMOTE_REFUSE: dict[IdentityKind, str] = {
    "self": "Promoting yourself? Nice try, the hierarchy doesn't bend for that.",
    "this_bot": "Already running things, no role needed.",
    "telegram": "Telegram doing fine without a role here.",
    "other_bot": "Other bots can't hold federation roles - humans only.",
    "anon_admin": "Anonymous admin cannot hold a federation role.",
    "founder": "{line} already runs the place, promoting them is a circular move.",
    "admin": "{line} is already an Admin. Use /tcpromote for a different role.",
}

_DEMOTE_REFUSE: dict[IdentityKind, str] = {
    "self": "Demoting yourself? Bold. Ask a higher-up if you really mean it.",
    "this_bot": "No role to lose here.",
    "telegram": "Telegram has no role to take.",
    "other_bot": "{line} is a bot. Bots cannot hold federation roles, nothing to demote.",
    "anon_admin": "Anonymous admin has no role to take.",
    "founder": "{line} is the Founder - try /transferowner if you really mean it.",
}

_TRANSFER_REFUSE: dict[IdentityKind, str] = {
    "self": "Already the Founder, transferring to yourself is a no-op.",
    "this_bot": "Tempting, but I'm not running the place under my own name.",
    "telegram": "Telegram doesn't want my keys, sorry.",
    "other_bot": "Other bots can't hold the keys - humans only.",
    "anon_admin": "Cannot transfer ownership to the anonymous admin.",
}

_UNWARN_REFUSE: dict[IdentityKind, str] = {
    "self": "Erasing your own warnings? Nice try, ask a mod.",
    "this_bot": "{line} - zero warnings, ever. Nothing to undo.",
    "telegram": "Telegram doesn't get warned here.",
    "other_bot": "{line} - bots don't pile up warnings, nothing to remove.",
    "anon_admin": "Anonymous admin doesn't get warned here.",
    "founder": "{line} - clean record, nothing to undo.",
}

_RESETWARNS_REFUSE: dict[IdentityKind, str] = {
    "self": "You don't get to reset your own warnings, ask a mod.",
    "this_bot": "{line} - already at zero, always was.",
    "telegram": "Nothing on Telegram to clear.",
    "other_bot": "{line} - bots stay at zero by default.",
    "anon_admin": "Nothing on anonymous admin to clear.",
    "founder": "{line} - nothing on the record to clear.",
}


_REFUSE_TABLES: dict[str, dict[IdentityKind, str]] = {
    "ban": _BAN_REFUSE,
    "kick": _KICK_REFUSE,
    "mute": _MUTE_REFUSE,
    "warn": _WARN_REFUSE,
    "unban": _UNBAN_REFUSE,
    "unmute": _UNMUTE_REFUSE,
    "promote": _PROMOTE_REFUSE,
    "demote": _DEMOTE_REFUSE,
    "transfer": _TRANSFER_REFUSE,
    "unwarn": _UNWARN_REFUSE,
    "resetwarns": _RESETWARNS_REFUSE,
}


def refuse_message(action: str, ident: Identity) -> str | None:
    """Return a witty refusal line for ``action`` against ``ident``, or ``None``.

    ``None`` means the action is allowed against this identity and the caller
    should proceed with the normal moderation flow.
    """
    table = _REFUSE_TABLES.get(action, {})
    template = table.get(ident.kind)
    if template is None:
        return None
    return template.format(line=_line(ident))


# ─────────── Recognition notes (read-only views) ──────────────── #
# * Single owner for "who is this?" copy on read-only surfaces such as
# * /check: moderation entries use refuse_message/staff_notice instead, so
# * no caller builds these lines inline. Staff and Founder are absent on
# * purpose: the profile Role line already labels them.

_PROFILE_NOTE: dict[IdentityKind, str] = {
    "this_bot": "That's me - this bot. I run moderation here, not collect bans.",
    "self": "That's you - checking your own record, good habit.",
    "telegram": "That's Telegram itself - outside federation jurisdiction.",
    "anon_admin": "That's the anonymous-admin placeholder, not an individual user.",
}


def profile_note(ident: Identity) -> str | None:
    """Return a recognition note for special identities, or ``None``.

    ``None`` means the identity needs no note (regular users, staff, and
    Founder: staff and Founder are already labeled by the profile Role
    line, so a note would only restate it).
    """
    return _PROFILE_NOTE.get(ident.kind)


# ─────────────── Staff heads-up (action proceeds) ───────────────── #
# * For warn / unwarn / unmute / resetwarns on staff targets, the action
# * proceeds but we surface a short heads-up so the executor knows the
# * target is staff; useful when an Admin is cleaning up a stale record.


def staff_notice(action: str, ident: Identity, community_name: str) -> str | None:
    """Return a heads-up line when acting on staff, or ``None`` otherwise."""
    if ident.kind not in ("admin", "developer", "tester"):
        return None
    return (
        f"Heads up - {_line(ident)} is a {esc(community_name)} {ident.role_label}. "
        f"Proceeding with {action} anyway."
    )
