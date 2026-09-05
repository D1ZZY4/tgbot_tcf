# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""HTML text formatters: the single source of truth for all Telegram HTML markup.

All modules (including tcbot.utils) import from here.  The shim at
tcbot/modules/helper/formatter.py re-exports every name for backward
compatibility with the modules layer import paths.
"""

from __future__ import annotations

import html
import re

# * Telegram usernames are restricted to ASCII letters, digits, and
# * underscores (5-32 chars). Anything else in a cached username means the
# * value did not come from Telegram intact, so never put it into a URL.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def safe_username(username: str | None) -> str | None:
    """Return ``username`` only when it is a valid Telegram username shape."""
    if username and _USERNAME_RE.match(username):
        return username
    return None


def bold(text: str) -> str:
    """Wrap text in HTML bold tags, escaping any HTML special characters."""
    return f"<b>{html.escape(str(text))}</b>"


def italic(text: str) -> str:
    """Wrap text in HTML italic tags, escaping any HTML special characters."""
    return f"<i>{html.escape(str(text))}</i>"


def code(text: str) -> str:
    """Wrap text in HTML code tags, escaping any HTML special characters."""
    return f"<code>{html.escape(str(text))}</code>"


def pre(text: str) -> str:
    """Wrap text in HTML pre (monospace block) tags, escaping HTML special characters."""
    return f"<pre>{html.escape(str(text))}</pre>"


def link(text: str, url: str) -> str:
    """Wrap text in an HTML anchor tag pointing to url, escaping both text and url."""
    return f'<a href="{html.escape(str(url), quote=True)}">{html.escape(str(text))}</a>'


def mention(user_id: int, name: str, username: str | None = None) -> str:
    """Create a user mention with username link and always-included user ID link.

    Historical alias for :func:`user_ref`; both names format the same
    output. New code should prefer :func:`user_ref` directly.
    """
    return user_ref(user_id, name, username)


def esc(text: str) -> str:
    """Escape HTML special characters in text for safe inline inclusion in HTML messages."""
    return html.escape(str(text))


def user_ref(user_id: int, name: str, username: str | None = None) -> str:
    """Format a complete user reference for action confirmation messages.

    Always a clickable ``FullName`` resolving via ``tg://user?id=ID``: the
    numeric ID is the single source of truth for identity. Usernames are
    never used: they can change, be reused, or be missing, while the
    numeric ID always resolves. The ``username`` parameter is accepted for
    backward compatibility and ignored.
    """
    _ = username
    return f'<a href="tg://user?id={user_id}">{html.escape(str(name))}</a>'
