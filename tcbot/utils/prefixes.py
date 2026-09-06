# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Command filter builder for all configured prefixes (/, !, .) and alt-prefix dispatcher."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Protocol

from telegram.ext import filters

from tcbot import cfg

if TYPE_CHECKING:
    from telegram import Message

log = logging.getLogger(__name__)

# * Precompiled: _parse_prefixed_command runs on every incoming message, so
# * the command/mention shapes must not recompile per call (re module cache
# * would save us, but explicit is cheaper to reason about).
_COMMAND_RE = re.compile(r"[a-z][a-z0-9]*")
_MENTION_RE = re.compile(r"[A-Za-z0-9_]{5,32}")


class _BotLike(Protocol):
    username: str | None


class _MessageLike(Protocol):
    text: str | None

    def get_bot(self) -> _BotLike:
        """Return the bot instance associated with this message."""
        ...


# ──────────────────────── Prefix Resolution ─────────────────────── #


def _get_prefixes() -> list[str]:
    """Return command prefixes from the already-validated runtime configuration.

    Filters out empty strings and whitespace-only entries so a misconfigured
    ``PREFIXES`` value cannot produce a prefix that matches every message.
    """
    return [p for p in (cfg.prefixes or ["/"]) if p and p.strip()]


def _never_match_filter() -> filters.BaseFilter:
    """Return a valid filter that intentionally never matches any message text."""
    return filters.Regex(re.compile(r"a^"))


def _bot_username_from_message(message: Any) -> str | None:
    """Return the current bot username from a PTB message when available."""
    try:
        bot = message.get_bot()
    except AttributeError:
        return None
    except RuntimeError:
        return None
    return (getattr(bot, "username", None) or "").lstrip("@") or None


def _parse_prefixed_command(
    text: str,
    prefixes: tuple[str, ...],
    bot_username: str | None,
) -> tuple[str, str | None] | None:
    """Parse a lowercase prefixed command and validate any bot mention suffix."""
    if not prefixes:
        return None

    # * Longest prefix first so "..cmd" prefers ".." over "."; callers pass
    # * the pre-sorted tuple prepared at filter construction (front-loads
    # * the sort out of this per-message hot path).
    prefix = next((p for p in prefixes if text.startswith(p)), None)
    if prefix is None:
        return None

    _parts = text[len(prefix) :].split(None, 1)
    if not _parts:
        return None
    token = _parts[0]
    if not token:
        return None

    command, separator, mention = token.partition("@")
    if not command or command != command.lower() or not command.isascii():
        return None
    if not _COMMAND_RE.fullmatch(command):
        return None
    if separator:
        if not mention or not _MENTION_RE.fullmatch(mention):
            return None
        if bot_username is None or mention.casefold() != bot_username.casefold():
            return None

    return command, mention or None


class _PrefixedCommandFilter(filters.MessageFilter):
    """PTB message filter for exact lowercase commands and self-only bot mentions."""

    def __init__(self, command: str, prefixes: list[str]) -> None:
        super().__init__(name=f"PrefixedCommand({command})")
        self.command = command
        self.prefixes = tuple(sorted(set(prefixes), key=len, reverse=True))

    def filter(self, message: Message) -> bool:
        """Return True when the message matches this filter's specific command."""
        text = message.text or ""
        parsed = _parse_prefixed_command(
            text,
            self.prefixes,
            _bot_username_from_message(message),
        )
        return parsed is not None and parsed[0] == self.command


class _AnyPrefixedCommandFilter(filters.MessageFilter):
    """PTB message filter for any valid lowercase command with configured prefixes."""

    def __init__(self, prefixes: list[str], *, name: str) -> None:
        super().__init__(name=name)
        self.prefixes = tuple(sorted(set(prefixes), key=len, reverse=True))

    def filter(self, message: Message) -> bool:
        """Return True when the message is any valid prefixed command."""
        text = message.text or ""
        return (
            _parse_prefixed_command(
                text,
                self.prefixes,
                _bot_username_from_message(message),
            )
            is not None
        )


# ───────────────────────── Filter Builders ──────────────────────── #


def build_prefixed_filters(command: str) -> filters.BaseFilter:
    """Return a filter matching exact lowercase <prefix><command> for configured prefixes."""
    return _PrefixedCommandFilter(command.lower(), _get_prefixes())


# * Includes /, !, .; use in ConversationHandler fallbacks to catch all commands
_prefixes = _get_prefixes()
ALL_PREFIXES_CMD_FILTER: filters.BaseFilter = (
    _AnyPrefixedCommandFilter(_prefixes, name="AnyPrefixedCommand")
    if _prefixes
    else _never_match_filter()
)


# ──────────────────────── Argument Parsing ──────────────────────── #


def parse_cmd_args(text: str | None) -> list[str]:
    """Extract arguments from a prefixed command message text."""
    if not text:
        return []
    parts = text.strip().split(None, 1)
    if len(parts) < 2:
        return []
    return parts[1].split()
