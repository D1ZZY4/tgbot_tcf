# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Shared reply and help-text strings used across multiple command modules."""

from __future__ import annotations

from typing import TypedDict


class HelpEntry(TypedDict):
    """Typed container for a module's user-facing help content."""

    name: str
    overview: str
    sections: list[tuple[str, str]]


# ──────────────────────── Target Syntax ─────────────────────────── #

TARGET_SYNTAX = (
    "Reply to a message, or provide a user ID / @username after the command."
)
ERR_CANNOT_RESOLVE = "Cannot resolve target. Reply to a message or provide a user ID."

# ─────────────────────── Role / Auth Errors ─────────────────────── #

ERR_ROLE_VERIFY = "Could not verify your group role."
ERR_GROUP_ONLY = "Use this command in a group."
ERR_NO_CONNECTED_GROUPS = "No connected groups."
ERR_GROUP_NOT_FOUND = "Group not found or already removed."
ERR_PERM_EXPIRED = "You no longer have permission to do this."
ERR_UNKNOWN_ROLE = "Unknown role."

# ───────────────────────── Server Errors ────────────────────────── #

ERR_GROUPS_LOAD_FAILED = (
    "Could not load the group list due to a server error. Please try again."
)

# ──────────────────────── Context / Scope ───────────────────────── #

CONTEXT_BOT_OR_GROUP = "Bot PM, exec group, or any connected group."
CONTEXT_EXEC_OR_GROUP = "Exec group, any connected group, or bot PM."
CONTEXT_ANYONE = "Anyone, no special permissions needed."
WHERE_CONNECTED_GROUP = "Inside any connected group."

# ─────────────────────── Permission Tiers ───────────────────────── #

PERM_FOUNDER_ONLY = "Founder only."
PERM_STAFF_ONLY = "TC Staff (Admin and above)."
PERM_ADMIN_ABOVE = "Admin and above (Founder / Admin)."
PERM_DEV_ABOVE = "Developer and above (Founder / Admin / Developer)."
PERM_TESTER_ABOVE = "Tester and above (Founder / Admin / Developer / Tester)."

# ─────────────────────── Action Defaults ────────────────────────── #

NO_REASON = "No reason provided"

# ────────────── Help-section header labels ───────────────────────── #

SEC_COMMANDS = "Commands & Aliases"
SEC_WHO = "Who can use"
SEC_WHERE = "Where to use"
SEC_WHAT = "What it does"
SEC_EXAMPLES = "Examples"
SEC_TARGET = "Target syntax"


# ─────────── Help-section constructor helpers ──────────────────── #


def who_section(perm: str) -> tuple[str, str]:
    """Build a 'Who can use' section entry."""
    return (SEC_WHO, perm)


def where_section(ctx: str) -> tuple[str, str]:
    """Build a 'Where to use' section entry."""
    return (SEC_WHERE, ctx)


def target_section() -> tuple[str, str]:
    """Build a standard 'Target syntax' section entry."""
    return (SEC_TARGET, TARGET_SYNTAX)
