# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Help command and callback handlers: renders module help index, module overview, and per-section topics."""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import TYPE_CHECKING

from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler

from tcbot import cfg
from tcbot.modules import ALL_MODULES
from tcbot.modules.helper import decorators, keyboards
from tcbot.modules.helper.formatter import bold, code, esc
from tcbot.modules.helper.parse_editmsg import safe_edit_cb
from tcbot.utils.prefixes import build_prefixed_filters, parse_cmd_args

if TYPE_CHECKING:
    from telegram import CallbackQuery, Update

log = logging.getLogger(__name__)

# ──────────────── User-facing reply constants ──────────────────── #

_ERR_TOPIC_NOT_FOUND = "Topic not found."
_ERR_SECTION_NOT_FOUND = "Section not found."
_ERR_INVALID_SECTION = "Invalid section."

# ─────────────────────── Rate-limiter constants ──────────────────── #
_RL_PERIOD_S: int = 30
_RL_CMD_LIMIT: int = 8
_RL_CB_LIMIT: int = 15

__module_name__ = None


# ────────────────────── Help Content Builder ────────────────────── #


def _builder_help() -> dict[str, tuple[str, str, list[tuple[str, str]]]]:
    """Collect help content from every loaded module.

    Returns a dict keyed by ``help_<module>`` mapping to
    ``(display_name, overview_text, sections)``.

    Prefers the unified ``__help__: HelpEntry`` attribute introduced in P3 #5.
    Falls back to legacy ``__module_name__`` / ``__help_text__`` / ``__help_sections__``
    for modules that have not been migrated yet.
    """
    content: dict[str, tuple[str, str, list[tuple[str, str]]]] = {}
    for mod_name in ALL_MODULES:
        try:
            mod = importlib.import_module(f"tcbot.modules.{mod_name}")
            h = getattr(mod, "__help__", None)
            if h is not None:
                content[f"help_{mod_name}"] = (
                    h["name"],
                    h["overview"],
                    list(h.get("sections", [])),
                )
                continue
            # Backward-compat path for un-migrated modules.
            name = getattr(mod, "__module_name__", None)
            text = getattr(mod, "__help_text__", None)
            sections: list[tuple[str, str]] = list(
                getattr(mod, "__help_sections__", []) or []
            )
            if name and text:
                content[f"help_{mod_name}"] = (name, text, sections)
        except Exception as exc:
            log.warning("Could not read help from %s: %s", mod_name, exc)
    return content


# ─────────────────────── Module-Level State ─────────────────────── #

HELP_CONTENT = _builder_help()

# * Sorted by display name (case-insensitive)
_TOPICS_SORTED: list[tuple[str, str]] = sorted(
    ((name, key) for key, (name, _, _) in HELP_CONTENT.items()),
    key=lambda t: t[0].lower(),
)

# * Menu-path topics; callback keys stay as "help_<mod>"
HELP_TOPICS_MENU: list[tuple[str, str]] = _TOPICS_SORTED

# * Command-path topics; callback keys become "helpc_<mod>"
HELP_TOPICS_CMD: list[tuple[str, str]] = [
    (name, "helpc_" + key[5:]) for name, key in _TOPICS_SORTED
]

# * Module name → help key mapping for /help <module> lookup
_MODULE_NAME_MAP: dict[str, str] = {}
for _key, (_dname, _, _) in HELP_CONTENT.items():
    _module_slug = _key[5:]
    _MODULE_NAME_MAP[_module_slug.lower()] = _key
    _MODULE_NAME_MAP[_dname.lower()] = _key

_CNAME = esc(cfg.community_name)


def _help_index_text(botname: str) -> str:
    """Build the help index header for the given plain-text bot display name."""
    return (
        f"{bold(f'{botname} Help')}\n"
        f"Federation management bot for {_CNAME}.\n\n"
        f"Select a module below to read its overview and commands, "
        f"or use {code('/help <module>')} to jump straight to a topic."
    )


# ──────────────────────── Shared Renderers ──────────────────────── #


def _prefix_note() -> str:
    """Return an HTML footer listing every configured command prefix."""
    codes = " ".join(code(p) for p in cfg.prefixes)
    return f"\n{bold('Note:')} All commands also work with {codes}"


def _section_buttons(
    mod_slug: str,
    sections: list[tuple[str, str]],
    *,
    is_menu_path: bool,
) -> list[tuple[str, str]]:
    """Build (label, callback_data) pairs for each section."""
    prefix = "helps_" if is_menu_path else "helpcs_"
    return [
        (label, f"{prefix}{mod_slug}:{idx}") for idx, (label, _) in enumerate(sections)
    ]


def _module_text(name: str, overview: str) -> str:
    """Compose the module-overview HTML body."""
    return f"{bold(f'Help for {name}')}\n\n{overview}\n{_prefix_note()}"


async def _render_help_index(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    with_back_to_start: bool,
) -> None:
    """Edit the help index message on the appropriate callback query."""
    q = update.callback_query
    if q is None:
        return

    botname = ctx.bot.first_name or ""
    kb = (
        keyboards.help_topics_menu_kb(HELP_TOPICS_MENU)
        if with_back_to_start
        else keyboards.help_topics_kb(HELP_TOPICS_CMD)
    )
    # * q.answer() and safe_edit_cb() are independent; run in parallel.
    await asyncio.gather(
        q.answer(),
        safe_edit_cb(q, _help_index_text(botname), reply_markup=kb),
        return_exceptions=True,
    )


async def _show_module(
    q: CallbackQuery,
    menu_key: str,
    *,
    is_menu_path: bool,
) -> None:
    """Render a module overview with sub-section buttons + back to help index."""
    if menu_key not in HELP_CONTENT:
        back_kb = (
            keyboards.back_to_help_kb()
            if is_menu_path
            else keyboards.back_to_help_cmd_kb()
        )
        # * q.answer() and safe_edit_cb() are independent; run in parallel.
        await asyncio.gather(
            q.answer(),
            safe_edit_cb(q, _ERR_TOPIC_NOT_FOUND, reply_markup=back_kb),
            return_exceptions=True,
        )
        return

    name, overview, sections = HELP_CONTENT[menu_key]
    mod_slug = menu_key[5:]  # strip "help_"

    back_cb = "help_menu" if is_menu_path else "helpc_main"
    if sections:
        section_btns = _section_buttons(mod_slug, sections, is_menu_path=is_menu_path)
        kb = keyboards.module_help_kb(section_btns, back_callback=back_cb)
    else:
        kb = (
            keyboards.back_to_help_kb()
            if is_menu_path
            else keyboards.back_to_help_cmd_kb()
        )

    # * q.answer() and safe_edit_cb() are independent; run in parallel.
    await asyncio.gather(
        q.answer(),
        safe_edit_cb(q, _module_text(name, overview), reply_markup=kb),
        return_exceptions=True,
    )


async def _show_section(
    q: CallbackQuery,
    mod_slug: str,
    idx: int,
    *,
    is_menu_path: bool,
) -> None:
    """Render a single help section + back-to-module button."""
    menu_key = f"help_{mod_slug}"
    back_module_cb = ("help_" if is_menu_path else "helpc_") + mod_slug

    if menu_key not in HELP_CONTENT:
        # * q.answer() and safe_edit_cb() are independent; run in parallel.
        await asyncio.gather(
            q.answer(),
            safe_edit_cb(
                q,
                _ERR_TOPIC_NOT_FOUND,
                reply_markup=keyboards.back_to_module_kb(back_module_cb),
            ),
            return_exceptions=True,
        )
        return

    name, _, sections = HELP_CONTENT[menu_key]
    if idx < 0 or idx >= len(sections):
        # * q.answer() and safe_edit_cb() are independent; run in parallel.
        await asyncio.gather(
            q.answer(),
            safe_edit_cb(
                q,
                _ERR_SECTION_NOT_FOUND,
                reply_markup=keyboards.back_to_module_kb(back_module_cb),
            ),
            return_exceptions=True,
        )
        return

    label, content = sections[idx]
    body = f"{bold(f'{name} > {label}')}\n\n{content}"
    # * q.answer() and safe_edit_cb() are independent; run in parallel.
    await asyncio.gather(
        q.answer(),
        safe_edit_cb(q, body, reply_markup=keyboards.back_to_module_kb(back_module_cb)),
        return_exceptions=True,
    )


# ──────────────────────── Command Handlers ──────────────────────── #


@decorators.ratelimiter(limit=_RL_CMD_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the help index, or a specific topic when an argument is given."""
    msg = update.effective_message
    if msg is None:
        return

    botname = ctx.bot.first_name or ""
    args = parse_cmd_args(msg.text)

    if args:
        query = " ".join(args).strip().lower()
        help_key = _MODULE_NAME_MAP.get(query)

        if help_key and help_key in HELP_CONTENT:
            name, overview, sections = HELP_CONTENT[help_key]
            mod_slug = help_key[5:]
            if sections:
                section_btns = _section_buttons(mod_slug, sections, is_menu_path=False)
                kb = keyboards.module_help_kb(section_btns, back_callback="helpc_main")
            else:
                kb = keyboards.back_to_help_cmd_kb()
            try:
                await msg.reply_text(
                    _module_text(name, overview),
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            except Exception as exc:
                log.debug("cmd_help module reply failed: %s", exc)
            return

        candidates = sorted(
            _MODULE_NAME_MAP,
            key=lambda k: (query not in k, abs(len(k) - len(query))),
        )[:3]
        suggestion = ", ".join(code(f"/help {c}") for c in candidates if c)
        hint = f"\n\nDid you mean: {suggestion}?" if suggestion else ""
        try:
            await msg.reply_text(
                f"Module {bold(query)} not found.{hint}",
                parse_mode="HTML",
                reply_markup=keyboards.help_topics_kb(HELP_TOPICS_CMD),
            )
        except Exception as exc:
            log.debug("cmd_help not-found reply failed: %s", exc)
        return

    try:
        await msg.reply_text(
            _help_index_text(botname),
            parse_mode="HTML",
            reply_markup=keyboards.help_topics_kb(HELP_TOPICS_CMD),
        )
    except Exception as exc:
        log.debug("cmd_help index reply failed: %s", exc)


# ──────────────────────── Callback Handlers ─────────────────────── #


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_help_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the top-level help index from the start-menu help button (includes back-to-start)."""
    await _render_help_index(update, ctx, with_back_to_start=True)


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_help_menu_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Help tapped from group /start inline; answer with alert, no edit."""
    q = update.callback_query
    if q is None:
        return

    await q.answer(
        "Run /help in this group to browse all modules and commands.",
        show_alert=True,
    )


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_helpc_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the top-level help index from a /helpc command callback (no back-to-start button)."""
    await _render_help_index(update, ctx, with_back_to_start=False)


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_help_topic_any(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle help_<mod> and helpc_<mod> module overview callbacks."""
    q = update.callback_query
    if q is None or not q.data:
        return

    data = q.data
    if data.startswith("helpc_"):
        await _show_module(q, "help_" + data[len("helpc_") :], is_menu_path=False)
    else:
        await _show_module(q, data, is_menu_path=True)


@decorators.ratelimiter(limit=_RL_CB_LIMIT, period=_RL_PERIOD_S)
@decorators.log_execution
async def on_help_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle helps_<mod>:<idx> and helpcs_<mod>:<idx> section callbacks."""
    q = update.callback_query
    if q is None or not q.data:
        return

    data = q.data
    is_menu_path = data.startswith("helps_")
    body = data[len("helps_") :] if is_menu_path else data[len("helpcs_") :]
    try:
        mod_slug, idx_str = body.split(":", 1)
        idx = int(idx_str)
    except ValueError:
        # * split(":", 1) unpacking raises ValueError (never IndexError).
        await q.answer(_ERR_INVALID_SECTION, show_alert=True)
        return
    await _show_section(q, mod_slug, idx, is_menu_path=is_menu_path)


# ──────────────────────────── Handlers ──────────────────────────── #

_HELP_CMDS = build_prefixed_filters("help")

__handlers__ = [
    MessageHandler(_HELP_CMDS, cmd_help),
    CallbackQueryHandler(on_help_menu, pattern=r"^help_menu$"),
    CallbackQueryHandler(on_help_menu_group, pattern=r"^help_menu_group$"),
    CallbackQueryHandler(on_helpc_main, pattern=r"^helpc_main$"),
    # * Section callbacks (helps_<mod>:<idx> for the menu path,
    # * helpcs_<mod>:<idx> for the command path) registered before the
    # * module-level catch-all so the more-specific pattern wins.
    CallbackQueryHandler(on_help_section, pattern=r"^(helps|helpcs)_\w+:\d+$"),
    CallbackQueryHandler(on_help_topic_any, pattern=r"^(help|helpc)_\w+$"),
]
