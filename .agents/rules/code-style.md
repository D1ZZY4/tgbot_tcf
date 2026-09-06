# Code Style and Architecture Rules

This file defines Python style, module boundaries, handler safety, database
access, workflows, and runtime behavior for TCF Bot. Authorization and secret
handling live in [`security-rules.md`](security-rules.md), async patterns live
in [`asyncio-gather-rules.md`](asyncio-gather-rules.md). Validation commands
live in [`tooling-validation.md`](tooling-validation.md), and comment and
Markdown conventions live in [`comment-style.md`](comment-style.md).

---

## Language and File Structure

- Use Python 3.14 syntax and four-space indentation.
- Every Python module starts with the project copyright header, a one-line
  module docstring, and `from __future__ import annotations` as the first
  non-comment code line.
- Use two blank lines between top-level functions and classes after formatting.
- Prefer focused functions and early returns over deeply nested conditionals.
- Keep source compatible with the Ruff configuration in `pyproject.toml`.

Example module header:

```python
# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Studio

"""One-line description of what this module does."""

from __future__ import annotations
```

## Imports

Order imports as follows:

1. `from __future__ import annotations`
2. Standard library
3. Third-party packages
4. Internal `tcbot.*` imports

Rules:

- Use one blank line between import groups.
- Do not use wildcard imports or inline imports inside functions.
- Let Ruff's `I` rules handle import sorting; do not fight the formatter with
  manual spacing.
- Prefer `from tcbot import cfg` for configuration.
- Prefer `from tcbot import database as db` in feature modules.
- Do not import raw environment variables outside `tcbot/__init__.py`.

## Typing and Naming

- Prefer built-in generics such as `list[str]`, `dict[str, int]`, and
  `int | None`.
- Prefer `collections.abc` for callable and iterable abstractions.
- Use `TypedDict` document shapes from `tcbot/database/documents.py`.
- Use `NewType` primitives from `tcbot/database/types.py` when the database API
  already uses them.
- Public functions and methods require explicit parameter and return types.
- Use `str | None`, not `Optional[str]`, and built-in generics over legacy
  typing aliases.
- Return `None` explicitly for not-found lookups.
- Keep handler return type `None` unless the framework requires otherwise.
- Do not use `Any` as a shortcut for unclear data shapes, and do not use
  `cast()` to silence a problem that should be modeled or checked.
- Use `_snake_case` for private helpers and `_UPPER_CASE` for private constants.
- Use `snake_case` for public functions and `PascalCase` for classes.
- Name async command handlers `cmd_*`, event and callback handlers `on_*`, and
  conversation states `WAITING_*`.

## Code Quality

- Remove unused imports, variables, functions, and commented-out code.
- Never remove meaningful code only to silence a diagnostic.
- Extract shared renderers, parsers, keyboards, database helpers, and workflows
  instead of duplicating logic.
- Keep one source of truth for cross-cutting behavior. Extend the existing
  owning helper or domain module before creating another utility or parallel
  abstraction.
- Do not use bare `except:` or swallow exceptions with `except Exception: pass`.
- Use module loggers instead of `print()` in application code.
- Log I/O failures with actionable context.
- Use `try/except` at Telegram and database I/O boundaries.
- Keep one failed group from aborting a multi-group operation.

## Telegram Messages and Handlers

- Bot messages are English-only and use `parse_mode="HTML"`.
- Never use Markdown parse mode.
- Escape user-provided text with `esc()`.
- Use `mention()` for clickable user names, `code()` for IDs, and `bold()` for
  static bold labels.
- Do not combine `mention(x)` and `code(x)` for the same value.
- Bot replies should be professional, friendly, concise, and lightly humorous.
- Pictograph emoji and text emoticons are forbidden in bot replies and audit
  logs.

Command and callback handlers use this decorator order:

```python
@decorators.ratelimiter(limit=5, period=60)
@decorators.mod_only
@decorators.log_execution
async def cmd_example(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ...
```

Rules:

1. `ratelimiter()` is outermost.
2. The required authorization guard is second.
3. `log_execution` is innermost.
4. Message-event handlers are exempt from per-handler rate-limit decorators.
5. Use `owner_only` for Founder-only actions, `staff_only` for Founder/Admin
   actions, `mod_only` for federation moderation, and `basic_mod_only` for
   kick, mute, and warn.
6. Always call `await q.answer()` before other callback-query work.
7. Never use private PTB attributes such as `q._bot`; use `ctx.bot`.
8. Do not store `Update`, `Message`, or `CallbackQuery` objects beyond the
   handler call lifetime.
9. Wrap repeated Telegram calls so one failure does not stop a fan-out.
10. Use `tcbot.utils.dispatch.fan_out()` for multi-group Telegram operations;
    concurrency and failure-count details live in
    [`asyncio-gather-rules.md`](asyncio-gather-rules.md).

## Database and Cache Access

- Keep all MongoDB writes and collection access in `tcbot/database/` helpers.
- Command modules must not call `col()` directly or perform raw
  insert/update/delete operations.
- Put new database concerns in descriptive `*_db.py` files.
- Add indexes for new collections and index-sensitive queries in
  `mongos.ensure_indexes()`.
- Keep database helpers async and fully typed.
- Invalidate relevant caches after cache-sensitive writes.
- Use `documents.py` for document shapes and Literal aliases.
- Use `types.py` for domain primitive types.
- Do not rename or delete stored fields without a migration plan and updates to
  every read path.

## Conversation Flows and Keyboards

- Conversation handlers live in `tcbot/modules/helper/workflows/`.
- Flow files use the `*_flow.py` suffix. Never create `*_conv.py` files.
- Kick, mute, and warn use `reason_flow.build_modaction_conv()`.
- Ban uses `ban_flow.ban_conversation()`.
- Appeals use `appeal_flow.build_handler()`.
- New standalone flows should model `appeal_flow.py`.
- Every flow has a cancel fallback.
- Use `WAITING_*` state constants.
- Use `cfg.proof_timeout` and `cfg.appeal_timeout` instead of hardcoded
  conversation timeouts.
- Define inline keyboard builders only in `tcbot/modules/helper/keyboards.py`.
- Use formatter helpers from `tcbot/modules/helper/formatter.py`.

## Datetime Handling

- Do not use `datetime.utcnow()`.
- Do not inline `datetime.now(timezone.utc)` outside
  `tcbot.utils.time_and_date`.
- Use `utc_now()` for database timestamps and elapsed-time checks.
- Use `to_utc(dt)` before arithmetic when a database value may be naive.
- Use `fmt_dt(dt)` or `utc_now_str()` for user-visible timestamps.

Concurrency, `asyncio.gather()`, fan-out, timeouts, and cancellation follow
[`asyncio-gather-rules.md`](asyncio-gather-rules.md).

## Runtime Architecture

- `tcbot/__main__.py` owns application startup, Flask keep-alive startup,
  handler registration, database initialization, error reporting, and transport.
- Webhook transport is the production path; local development without a public
  URL may fall back to polling.
- `tcbot/modules/__init__.py` owns module discovery and the
  `MODULES_LOAD`/`MODULES_NO_LOAD` filters.
- Handlers use database helpers instead of direct collection access.
- Cross-group actions use the bounded dispatch helper.
- Preserve explicit PTB lifecycle management and scheduler readiness behavior.
- Keep webhook enqueue failures retryable by returning HTTP `503`.
- Preserve FIFO Redis mutations, the `v2` namespace, and typed JSON values.
- Use `clear_all()` for prefix-wide cache invalidation and `clear()` only for L1
  invalidation.

## Forbidden Patterns

- Creating `*_conv.py` files.
- Writing to MongoDB from command modules.
- Adding keyboard builders outside `keyboards.py`.
- Using Markdown parse mode.
- Duplicating reason or proof workflow state handlers.
- Calling raw `col()` from feature modules.
- Leaving dead or commented-out code.
- Swallowing exceptions silently.
- Adding dependencies without updating `pyproject.toml` and `uv.lock` through
  `uv`.
- Editing secrets or unrelated project files during a scoped task.
- Using sequential awaits for independent operations.
- Inlining self, bot, Telegram, Founder, or staff branches instead of using
  `identity.classify`.
- Hardcoding bot tokens, MongoDB URIs, passwords, API keys, webhook secrets,
  deployment chat IDs, or other credentials.
- Using em dashes (U+2014) anywhere; see the character rule in
  [`comment-style.md`](comment-style.md#em-dashes).