# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Backward-compatible re-export shim.

All formatter logic now lives in ``tcbot.utils.formatter`` so that both the
modules layer and the utils layer can import from a single source of truth
without creating circular imports.  This file re-exports every public name
so that existing callers (``from tcbot.modules.helper.formatter import bold``)
continue to work without modification.
"""

from __future__ import annotations

from tcbot.utils.formatter import (
    bold,
    code,
    esc,
    italic,
    link,
    mention,
    pre,
    safe_username,
    user_ref,
)

__all__ = [
    "bold",
    "code",
    "esc",
    "italic",
    "link",
    "mention",
    "pre",
    "safe_username",
    "user_ref",
]
