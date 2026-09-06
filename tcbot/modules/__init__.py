# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""Module discovery, filtering, and handler collection."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from tcbot import cfg

log = logging.getLogger(__name__)


# ──────────────────────── Module Discovery ──────────────────────── #


def _discover_modules() -> list[str]:
    """Return all .py module names in this directory, excluding __init__.py."""
    this_dir = Path(__file__).parent
    return [
        p.stem for p in this_dir.glob("*.py") if p.is_file() and p.name != "__init__.py"
    ]


def _filter_modules(modules: list[str]) -> list[str]:
    """Apply load / no-load filters from the central configuration."""
    to_load = cfg.modules_load
    no_load = cfg.modules_no_load

    if to_load:
        invalid = [m for m in to_load if m not in modules]
        if invalid:
            log.error("MODULES_LOAD contains invalid names: %s. Exiting.", invalid)
            raise SystemExit(1)
        modules = [m for m in to_load if m in modules]

    if no_load:
        log.info("Not loading modules: %s", no_load)
        modules = [m for m in modules if m not in no_load]

    return modules


# ───────────────────────── Module Registry ──────────────────────── #

ALL_MODULES = _filter_modules(_discover_modules())
log.info("Modules to load: %s", ALL_MODULES)

__all__ = [*ALL_MODULES, "ALL_MODULES"]  # type: ignore[reportUnsupportedDunderAll]  # noqa: PLE0604


# ─────────────────────── Handler Collection ─────────────────────── #


def get_handlers() -> list[Any]:
    """Import all active modules and collect their __handlers__ lists."""
    handlers: list[Any] = []
    mods_found: dict[str, Any] = {}

    failed: list[str] = []
    for mod_name in ALL_MODULES:
        try:
            mod = importlib.import_module(f"tcbot.modules.{mod_name}")
            mods_found[mod_name] = mod
        except Exception:
            failed.append(mod_name)
            log.exception("Failed to import tcbot.modules.%s", mod_name)

    if failed:
        raise SystemExit(f"Module import failed for: {', '.join(failed)}")

    for mod_name in ALL_MODULES:
        mod = mods_found.get(mod_name)
        if mod is None:
            continue
        mod_handlers = getattr(mod, "__handlers__", [])
        if mod_handlers:
            handlers.extend(mod_handlers)
            log.debug("Loaded %d handler(s) from %s", len(mod_handlers), mod_name)

    return handlers
