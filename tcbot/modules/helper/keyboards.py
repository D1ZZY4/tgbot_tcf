# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""All inline-keyboard factory functions used across moderation, appeal, and admin flows."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import KeyboardButtonStyle

from tcbot import cfg
from tcbot import database as db
from tcbot.modules.helper.parse_link import appeal_deep_link

log = logging.getLogger(__name__)

# * Semantic button colors (PTB 22.7+, Bot API style field; older clients
# * render the same buttons without color, so styling is purely additive):
# * SUCCESS marks a final approval, DANGER a rejection or destructive
# * confirm, PRIMARY a continue/select step into a flow. Cancel, Back,
# * navigation, menus, toggles, and URL buttons stay unstyled (neutral).
# * See docs/reference/keyboard-styles.md for the full convention.


def _https_url(url: str, label: str) -> str | None:
    """Return ``url`` only for http(s) community links.

    Operator-supplied env URLs reach ``InlineKeyboardButton(url=...)``
    verbatim; a non-http(s) value would fail late at the Telegram API.
    Reject it here so the button is omitted and the misconfiguration is
    visible in logs instead of a broken menu.
    """
    if url and url.startswith(("https://", "http://")):
        return url
    if url:
        log.warning("Ignoring non-http(s) community URL for %s: %s", label, url)
    return None


# ──────────────────────────── Ban flow ──────────────────────────── #


def ban_log_new(
    target_id: int,
    proof_link: str,
    appeal_url: str,
) -> InlineKeyboardMarkup:
    """Ban-log keyboard with explicit appeal URL."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Proof {target_id}",
                    url=proof_link,
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    "Submit Appeal",
                    url=appeal_url,
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
        ]
    )


def ban_log_update(
    target_id: int,
    proof_link: str,
    previous_proof_link: str,
    appeal_url: str,
) -> InlineKeyboardMarkup:
    """Return the ban-log keyboard with a previous-proof button and explicit appeal URL."""
    # * One URL button per row (keyboard-styles "Detail view" convention):
    # * proof labels embed the target ID, so two of them side by side
    # * truncate on narrow clients.
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Proof {target_id}",
                    url=proof_link,
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    f"Previous Proof {target_id}",
                    url=previous_proof_link,
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    "Submit Appeal",
                    url=appeal_url,
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
        ]
    )


def appeal_button_kb(
    bot_username: str,
    ban_id: str,
) -> InlineKeyboardMarkup | None:
    """Single Submit Appeal URL button, or None when the bot username is unknown."""
    if not bot_username:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Submit Appeal",
                    url=appeal_deep_link(bot_username, ban_id),
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ]
        ]
    )


# ─────────────── Mute / Kick / Warn proof button ────────────────── #


def action_proof_kb(
    target_id: int,
    proof_link: str | None,
) -> InlineKeyboardMarkup | None:
    """Single-button keyboard with a proof URL, or None when no proof link is available."""
    if not proof_link:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Proof {target_id}",
                    url=proof_link,
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ]
        ]
    )


# ───────────────────────── Admin promotion ──────────────────────── #


def promote_role_kb(target_id: int, available_roles: list[str]) -> InlineKeyboardMarkup:
    """Role selection keyboard shown when /tcpromote is used without a role argument."""
    buttons = [
        InlineKeyboardButton(
            db.users_roles.ROLE_LABEL[r],
            callback_data=f"promo_role:{r}:{target_id}",
            style=KeyboardButtonStyle.PRIMARY,
        )
        for r in available_roles
        if r in db.users_roles.ROLE_LABEL
    ]
    rows: list[list[InlineKeyboardButton]] = [
        buttons[i : i + 2] for i in range(0, len(buttons), 2)
    ]
    rows.append(
        [InlineKeyboardButton("Cancel", callback_data=f"promo_role_cancel:{target_id}")]
    )
    return InlineKeyboardMarkup(rows)


def demote_confirm_kb(target_id: int) -> InlineKeyboardMarkup:
    """Confirm/Cancel keyboard for the demotion confirmation flow."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Confirm",
                    callback_data=f"demote_confirm:{target_id}",
                    style=KeyboardButtonStyle.DANGER,
                ),
                InlineKeyboardButton(
                    "Cancel", callback_data=f"demote_cancel:{target_id}"
                ),
            ]
        ]
    )


def promo_decision_kb(request_id: str) -> InlineKeyboardMarkup:
    """Approve/Reject keyboard for promotion request review cards."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Approve",
                    callback_data=f"promo_approve:{request_id}",
                    style=KeyboardButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    "Reject",
                    callback_data=f"promo_reject:{request_id}",
                    style=KeyboardButtonStyle.DANGER,
                ),
            ]
        ]
    )


# ───────────────────────────── Check-me ────────────────────────── #


def checkme_ban_kb(
    bot_username: str,
    ban_id: str,
    proof_link: str | None = None,
) -> InlineKeyboardMarkup | None:
    """Summary view keyboard - Details | Proof (row 1), Appeal (row 2)."""
    if not bot_username:
        return None
    appeal_url = appeal_deep_link(bot_username, ban_id)
    row1 = [
        InlineKeyboardButton(
            "Details",
            callback_data=f"checkme_detail:{ban_id}",
            style=KeyboardButtonStyle.PRIMARY,
        )
    ]
    if proof_link:
        row1.append(
            InlineKeyboardButton(
                "Proof", url=proof_link, style=KeyboardButtonStyle.PRIMARY
            )
        )
    return InlineKeyboardMarkup(
        [
            row1,
            [
                InlineKeyboardButton(
                    "Appeal", url=appeal_url, style=KeyboardButtonStyle.PRIMARY
                )
            ],
        ]
    )


def checkme_detail_back_kb(
    ban_id: str,
    proof_link: str | None = None,
) -> InlineKeyboardMarkup:
    """Detail view keyboard - optional Proof (row 1), Back (row 2)."""
    rows = []
    if proof_link:
        rows.append(
            [
                InlineKeyboardButton(
                    "Proof", url=proof_link, style=KeyboardButtonStyle.PRIMARY
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("« Back", callback_data=f"checkme_back:{ban_id}")]
    )
    return InlineKeyboardMarkup(rows)


# ─────────────────────── Start / Help menus ─────────────────────── #


def main_menu_kb() -> InlineKeyboardMarkup:
    """Top-level start-menu keyboard: About, Help, Additional, Privacy."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "About",
                    callback_data="about_menu",
                    style=KeyboardButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    "Help",
                    callback_data="help_menu",
                    style=KeyboardButtonStyle.PRIMARY,
                ),
            ],
            [
                InlineKeyboardButton(
                    "Additional",
                    callback_data="additional_menu",
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    "Privacy",
                    callback_data="privacy_menu",
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
        ]
    )


def group_start_kb(bot_username: str) -> InlineKeyboardMarkup:
    """Keyboard for /start sent inside a group - sends user to PM."""
    rows: list[list[InlineKeyboardButton]] = []
    if bot_username:
        rows.append(
            [
                InlineKeyboardButton(
                    "Open in PM",
                    url=f"https://t.me/{bot_username}?start=menu",
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "Help",
                callback_data="help_menu_group",
                style=KeyboardButtonStyle.PRIMARY,
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _build_topic_rows(
    topics: list[tuple[str, str]],
    *,
    style: KeyboardButtonStyle | None = None,
) -> list[list[InlineKeyboardButton]]:
    """Pair topics into two-column rows, with any odd item on its own row."""
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(topics), 2):
        chunk = topics[i : i + 2]
        rows.append(
            [
                InlineKeyboardButton(text, callback_data=cb, style=style)
                for text, cb in chunk
            ]
        )
    return rows


def help_topics_menu_kb(topics: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Help index when reached via the start menu - includes « Back to start."""
    rows = _build_topic_rows(topics, style=KeyboardButtonStyle.PRIMARY)
    rows.append([InlineKeyboardButton("« Back", callback_data="back_to_start")])
    return InlineKeyboardMarkup(rows)


def help_topics_kb(topics: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Help index when reached via /help command (PM or group) - no back to start."""
    return InlineKeyboardMarkup(
        _build_topic_rows(topics, style=KeyboardButtonStyle.PRIMARY)
    )


def back_to_start_kb() -> InlineKeyboardMarkup:
    """Single Back button that returns the user to the start menu."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("« Back", callback_data="back_to_start"),
            ]
        ]
    )


def back_to_help_kb() -> InlineKeyboardMarkup:
    """Back to help index - used from menu-path topics (goes to help_menu)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("« Back", callback_data="help_menu"),
            ]
        ]
    )


def back_to_help_cmd_kb() -> InlineKeyboardMarkup:
    """Back to help index - used from command-path topics (goes to helpc_main)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("« Back", callback_data="helpc_main"),
            ]
        ]
    )


def privacy_kb() -> InlineKeyboardMarkup:
    """Privacy section keyboard: policy link + Back to start."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Privacy Policy",
                    callback_data="privacy_policy_menu",
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
            [InlineKeyboardButton("« Back", callback_data="back_to_start")],
        ]
    )


def privacy_policy_sections_kb(section_labels: list[str]) -> InlineKeyboardMarkup:
    """Policy index keyboard: one button per section + Back to privacy data."""
    pairs: list[tuple[str, str]] = [
        (label, f"privacy_section_{idx}") for idx, label in enumerate(section_labels)
    ]
    rows = _build_topic_rows(pairs, style=KeyboardButtonStyle.PRIMARY)
    rows.append([InlineKeyboardButton("« Back", callback_data="privacy_menu")])
    return InlineKeyboardMarkup(rows)


def back_to_privacy_policy_kb() -> InlineKeyboardMarkup:
    """Back to privacy policy section index from an individual section view."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Back", callback_data="privacy_policy_menu")]]
    )


# ─────────────────── Additional / Groups menus ──────────────────── #


def additional_menu_kb() -> InlineKeyboardMarkup:
    """Return the community links menu shown from the start menu.

    Each button row is only included when the corresponding env var URL is
    non-empty (COMMUNITY_CHANNEL_URL, COMMUNITY_GROUP_URL, COMMUNITY_LOGS_URL,
    COMMUNITY_EXEC_URL, COMMUNITY_TRAVEL_URL).  Rows with no configured URL are
    silently omitted so the keyboard stays clean for deployments that do not
    configure every link.
    """
    rows: list[list[InlineKeyboardButton]] = []

    channel_url = _https_url(cfg.community_channel_url, "channel")
    group_url = _https_url(cfg.community_group_url, "group")
    channel_btn = (
        InlineKeyboardButton(
            "Main Channel", url=channel_url, style=KeyboardButtonStyle.PRIMARY
        )
        if channel_url
        else None
    )
    group_btn = (
        InlineKeyboardButton(
            "Discussion Group", url=group_url, style=KeyboardButtonStyle.PRIMARY
        )
        if group_url
        else None
    )
    if channel_btn or group_btn:
        rows.append([b for b in (channel_btn, group_btn) if b is not None])

    logs_url = _https_url(cfg.community_logs_url, "logs")
    exec_url = _https_url(cfg.community_exec_url, "exec")
    logs_btn = (
        InlineKeyboardButton(
            "Logs Channel", url=logs_url, style=KeyboardButtonStyle.PRIMARY
        )
        if logs_url
        else None
    )
    exec_btn = (
        InlineKeyboardButton(
            "Exec Group", url=exec_url, style=KeyboardButtonStyle.PRIMARY
        )
        if exec_url
        else None
    )
    if logs_btn or exec_btn:
        rows.append([b for b in (logs_btn, exec_btn) if b is not None])

    travel_url = _https_url(cfg.community_travel_url, "travel")
    if travel_url:
        rows.append(
            [
                InlineKeyboardButton(
                    "TRAVEL - Transsion Development (Community)",
                    url=travel_url,
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ]
        )

    rows.append([InlineKeyboardButton("« Back", callback_data="back_to_start")])
    return InlineKeyboardMarkup(rows)


def groups_menu_kb(*, detailed: bool) -> InlineKeyboardMarkup:
    """Detailed/Simple toggle keyboard for the start-menu groups list."""
    toggle = InlineKeyboardButton(
        "Simple" if detailed else "Details",
        callback_data="menu_groups_simple" if detailed else "menu_groups_details",
        style=KeyboardButtonStyle.PRIMARY,
    )
    back = InlineKeyboardButton("« Back", callback_data="back_to_start")
    return InlineKeyboardMarkup([[toggle, back]])


def tcgroups_kb(*, detailed: bool) -> InlineKeyboardMarkup:
    """Toggle keyboard for the /tcgroups command: Simple/Details switch."""
    label = "Simple" if detailed else "Details"
    callback = "groups_simple" if detailed else "groups_details"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    label,
                    callback_data=callback,
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ]
        ]
    )


# ───────────────────── Module help sub-menu ─────────────────────── #


def module_help_kb(
    section_buttons: list[tuple[str, str]],
    back_callback: str,
) -> InlineKeyboardMarkup:
    """Per-module help view: pair sub-section buttons + Back, with Back last."""
    rows = _build_topic_rows(section_buttons, style=KeyboardButtonStyle.PRIMARY)
    rows.append([InlineKeyboardButton("« Back", callback_data=back_callback)])
    return InlineKeyboardMarkup(rows)


def back_to_module_kb(module_callback: str) -> InlineKeyboardMarkup:
    """Single « Back button that returns to the module help view."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Back", callback_data=module_callback)]]
    )
