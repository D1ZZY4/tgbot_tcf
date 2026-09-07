# TCF Bot Project Guide

This file is the top-level guide for agents and contributors working in this repository. It summarizes the current project layout, development commands, style rules, and safety requirements.

For user-facing setup, see [`README.md`](README.md). For contribution workflow,
see [`CONTRIBUTING.md`](CONTRIBUTING.md). For Replit deployment, see
[`replit.md`](replit.md). For detailed developer documentation, see
[`docs/README.md`](docs/README.md).

---

## Mandatory Read-Before-Work and Update-After-Work

Every new conversation in this repository must start by reading the canonical rules and end by updating the related markdown. The user should NEVER need to remind you.

**Read at the start of every conversation:**

- [`.agents/rules/tooling-validation.md`](.agents/rules/tooling-validation.md):
  workflow, dependency, documentation, and validation rules
- [`.agents/rules/code-style.md`](.agents/rules/code-style.md): Python style,
  architecture, handler, and database rules
- [`.agents/rules/comment-style.md`](.agents/rules/comment-style.md): comments,
  docstrings, section dividers, and Markdown rules
- [`.agents/rules/docs-rules.md`](.agents/rules/docs-rules.md): documentation
  scope, style, maintenance workflow, and detailed-guide rules
- [`.agents/rules/security-rules.md`](.agents/rules/security-rules.md):
  authorization boundaries, role safety, secrets, and compatibility
- [`.agents/rules/asyncio-gather-rules.md`](.agents/rules/asyncio-gather-rules.md):
  async handlers, `gather()` use, bounded fan-out, timeouts, and cancellation
- [`AGENTS.md`](AGENTS.md) (this file), [`CHANGELOG.md`](CHANGELOG.md)
- The relevant [`.agents/skills/`](.agents/skills/), [`docs/`](docs/), and project-root docs for the task

**Update in the same turn after every change:**

- [`CHANGELOG.md`](CHANGELOG.md): entry under `[Unreleased]` (Added / Changed / Fixed / Removed / Documentation)
- Every related `docs/**/*.md`, `.agents/**/*.md`, [`README.md`](README.md), [`replit.md`](replit.md) whose content is now stale

See [`tooling-validation.md`](.agents/rules/tooling-validation.md#read-before-work-and-update-after-work)
for the complete read/update rules. Skipping either step is a serious defect.

## Skills and Sub-Agents Policy

**Skills in `.agents/skills/` auto-invoke whenever their trigger matches**: no need for the user to ask. If you are about to write code in `tcbot/`, read [`code-style.md`](.agents/rules/code-style.md), plus [`security-rules.md`](.agents/rules/security-rules.md) for authorization work or [`asyncio-gather-rules.md`](.agents/rules/asyncio-gather-rules.md) for async work. If you are about to edit docs, read [`docs-rules`](.agents/rules/docs-rules.md). Same for `mongodb-query-optimizer`, `feature-reviewer`. When several skills match, load them all, prioritize by task relevance, and read every loaded skill's full instructions: never half. Compose multiple skills when one task spans multiple areas.

The skills directory also previously included meta-tools for the agent itself (`find-skills`, `general-sub-agent`, `skill-creator`) that have been removed: they were tooling for the agent harness, not for the bot codebase, and had no project-specific value.

## Autonomous Engineering Loop

For each improvement, update, fix, or audit, work through this bounded loop
autonomously:

1. **Scope** the concern and list the affected runtime, docs, configuration, and
   validation surfaces.
2. **Inspect** the canonical rules, current implementation, repository status,
   existing helpers, and possible duplicate or dead paths.
3. **Verify** version-sensitive library behavior with Context7 latest. Resolve
   the exact library before querying docs, use one concept per query, and never
   put credentials or private project data in a query.
4. **Design** the smallest modular change and centralize shared behavior in its
   owning helper or domain module. Do not create parallel utilities for logic
   that already has a project owner.
5. **Implement** focused typed Python 3.14 code with HTML-safe output,
   intentional comments, and explicit error handling.
6. **Validate** targeted behavior, the full relevant checks, startup logs for
   runtime changes, and stale/dead/duplicate paths.
7. **Review** every explicit requirement, synchronize related docs and
   changelog entries, and repeat only if a concrete defect remains. Stop when
   the checks are clean or report the exact blocker after bounded attempts.

Optimize for measured efficiency and bounded concurrency, not unverified
performance guarantees. Preserve correctness when ordering, dependencies,
authorization, or side effects require sequential execution.

## Project Overview

TCF Bot is a Python Telegram bot for the Transsion Core Federation community. It manages federation-wide moderation actions, appeal workflows, staff roles, connected groups, audit logging, and health checks.

Current stack:

- Python 3.14 project target (`pyproject.toml` requires `>=3.13`)
- `python-telegram-bot[rate-limiter]>=22.8,<23`, tracking the latest compatible release
- APScheduler 3.11.3 with `AsyncIOScheduler` + `MongoDBJobStore` for persistent scheduled jobs
- Motor async MongoDB driver with connection pool configuration
- Flask keep-alive / health-check server
- Optional Redis L2 cache (`redis[hiredis]>=8.0,<9`) with in-memory L1 fallback
- Tagged JSON values for Redis serialization (`cbor2` remains pinned in
  `pyproject.toml` but no current code imports it)
- `cachetools` for L1 TTLCache
- `uv` for dependency management and lockfile-based installs
- Ruff for formatting and lint checks
- pyright for static type checking

## Repository Layout

```text
<project root>/
├── tcbot/                    Main bot package
│   ├── __init__.py           Environment config loader and `cfg` adapter
│   ├── __main__.py           Runtime entry point, handler registration, webhook/polling transport
│   ├── alive.py              Flask keep-alive and webhook receiver
│   ├── serverless.py         Vercel lifecycle: shared PTB app, update dispatch, cron expiry
│   ├── database/             MongoDB helpers, one file per collection/domain
│   │   ├── users_cache.py    Member profile cache operations
│   │   ├── users_roles.py    Role system: owners/admins/roles
│   │   ├── bans_db.py        Federation bans
│   │   ├── groups_db.py      Connected and pending groups
│   │   ├── warns_db.py       Warnings
│   │   ├── kicks_db.py       Kicks
│   │   ├── mutes_db.py       Mutes
│   │   ├── queues_db.py      Promotion requests
│   │   ├── cache.py          TwoLevelCache (L1 in-memory + L2 Redis)
│   │   ├── redis_client.py   Optional Redis client
│   │   ├── scheduler.py      APScheduler background task + MongoDBJobStore
│   │   ├── mongos.py         MongoDB client/indexes
│   │   ├── documents.py      Typed document shapes
│   │   └── types.py          Domain primitive types
│   ├── modules/              Telegram command modules and handlers
│   │   ├── helper/           Shared helper code and conversation workflows
  │   │   │   ├── workflows/    ConversationHandler flows (`*_flow.py` only)
  │   │   │   ├── keyboards.py  Inline keyboard builders
│   │   │   ├── decorators.py  Rate limiter, role checks, execution logging
│   │   │   ├── extraction.py Target/user extraction helpers
│   │   │   ├── identity.py   Self/bot/Telegram/Founder/staff classification
│   │   │   ├── parse_link.py  `t.me/c/...` deep-link builders
│   │   │   ├── parse_logmsg.py  Federation log message renderers
│   │   │   ├── parse_editmsg.py  Safe `edit_text` / `edit_message_text` wrappers
│   │   │   ├── ban_info.py    Shared ban-detail builder for /check and /checkme
│   │   │   └── replies.py     User-facing reply constants (HelpEntry, error strings)
│   │   ├── banning.py        /ban command
│   │   ├── kicking.py        /kick command
│   │   ├── muting.py         /mute command
│   │   ├── warnings.py       /warn command
│   │   ├── appeals.py        /appeal command
│   │   ├── admins.py         /admin command
│   │   ├── connecting.py     /connect command
│   │   ├── disconnecting.py  /disconnect command
│   │   ├── groups.py         Group management
│   │   ├── checking.py       /check command
│   │   ├── unbanning.py      /unban command
│   │   ├── broadcasting.py   /broadcast command
│   │   ├── greeting.py       Greeting messages
│   │   ├── about.py          /about command
│   │   ├── additional.py     Additional menu
│   │   ├── help.py           Help command
│   │   ├── stats.py          Statistics
│   │   ├── maintenance.py    Maintenance commands
│   │   ├── netspeed.py       Network speed test
│   │   ├── privacy.py        Privacy commands
│   │   └── start.py          /start command
│   └── utils/                Logging, dispatch, prefixes, datetime helpers
│       ├── circuit_breaker.py  Telegram/MongoDB circuit breaker
│       ├── dispatch.py        fan_out() bounded concurrency dispatcher
│       ├── error_reporter.py  Error reporting to LOGS_ERRORS
│       ├── formatter.py       HTML-safe formatter (esc, code, mention, bold)
│       ├── logger.py          Logging setup
│       ├── pagination.py      Paginated message rendering
│       ├── prefixes.py        Command prefix resolution
│       └── time_and_date.py    Central clock: UTC storage/display + monotonic measure
├── docs/                     Developer documentation grouped by category
├── .agents/                   Coding skills and style rules
├── api/                       Vercel serverless endpoints (webhook, cron)
├── config.env.example        Environment variable template
├── docker-compose.yml        Local bot + MongoDB + Redis compose setup
├── Dockerfile                Container image definition
├── vercel.json               Vercel functions, timeouts, and cron schedule
├── .python-version           Pinned Python for Vercel and uv (3.14)
├── pyproject.toml            Dependencies and Ruff settings
├── uv.lock                   Locked dependency graph
├── pyrightconfig.json        pyright type checker configuration
├── CONTRIBUTING.md           Contribution workflow and review checklist
├── README.md                 User-facing setup and architecture overview
├── AGENTS.md                 Top-level project guide (this file)
└── replit.md                 Replit deployment notes
```

Core ownership rules:

- Command handlers live in `tcbot/modules/`. See [`docs/architecture/modules.md`](docs/architecture/modules.md) for module boundaries.
- Shared handler helpers live in `tcbot/modules/helper/`. See [`docs/architecture/helpers.md`](docs/architecture/helpers.md) for helper docs.
- Conversation flows live in `tcbot/modules/helper/workflows/` and must be named `*_flow.py`. See [`docs/architecture/workflows.md`](docs/architecture/workflows.md) for conversation internals.
- MongoDB access lives in `tcbot/database/`; keep new database helpers in `*_db.py` files. See [`docs/architecture/database.md`](docs/architecture/database.md) for database layer notes.
- Runtime utilities live in `tcbot/utils/`. See [`docs/architecture/utilities.md`](docs/architecture/utilities.md) for utility docs.

## Development Commands

Install dependencies from the lockfile (Replit only):

```bash
uv sync --frozen
```

Run the bot locally:

```bash
uv run python -m tcbot
```

Format, lint, and type-check:

```bash
uv run ruff format .
uv run ruff check --fix .
uv run --with pyright pyright .
```

Run with Docker Compose:

```bash
docker compose up --build
```

## Configuration and Secrets

Configuration is loaded from environment variables. For local development, `python-dotenv` loads `config.env` (falling back to `.env`) when present. For Replit or hosted deployment, store secrets in the platform secret manager instead of committing them. See [`docs/getting-started/setup.md`](docs/getting-started/setup.md) for detailed setup instructions and [`replit.md`](replit.md) for Replit-specific notes.

Never commit real credentials. Required secret values include:

- `BOT_TOKEN`: Telegram bot token from BotFather.
- `MONGODB_URI`: MongoDB connection string.

Important non-secret/runtime variables include:

- `OWNER_ID`: initial federation founder Telegram user ID.
- `DB_NAME`: MongoDB database name, default `tcbot`.
- `COMMUNITY_NAME`: display name used in bot messages and logs.
- `PREFIXES`: command prefix list, default `['/', '!', '.']`.
- `PORT`: Flask keep-alive port, default `5000`; invalid or out-of-range values fall back to `5000`.
- `MAIN_GROUP`, `MAIN_CHANNEL`, `EXTEND_GROUP`: community chat IDs.
- `PROOFS`, `LOGS`, `LOGS_ERRORS`, `APPEALS`: log/proof/appeal destinations; values may be `chat_id` or `chat_id/thread_id`.
- `APPEAL_LOG_HANDLE`: channel handle shown in appeal instructions.
- `APPEAL_DISCUSSION_TOPIC`: thread ID in `MAIN_GROUP` for appeal review cards.
- `PROOF_TIMEOUT_SECONDS`, `APPEAL_TIMEOUT_SECONDS`, `ALBUM_DEBOUNCE_SECONDS`: conversation timing settings.
- `LOG_LEVEL`: bot log level.
- `MODULES_LOAD`, `MODULES_NO_LOAD`: optional module allowlist/denylist.
- `WEBHOOK_URL`: explicit webhook URL for production; overrides `REPLIT_DEV_DOMAIN`.
- `WEBHOOK_SECRET`: optional webhook secret for Telegram to sign update payloads.
- `CRON_SECRET`: bearer token for the serverless warn-expiry cron endpoint (required on Vercel).
- `REDIS_URL`: optional Redis connection URL for L2 cache.
- `WARN_LIMIT`: per-group warn threshold that triggers auto-ban; default `3`.
- `WARN_EXPIRY_DAYS`: days after which warn records expire; default `0` (disabled).
- `FED_WARN_LIMIT`: federation-wide warn threshold that triggers auto-ban; default `0` (disabled).
- `COMMUNITY_CHANNEL_URL`, `COMMUNITY_GROUP_URL`, `COMMUNITY_LOGS_URL`, `COMMUNITY_EXEC_URL`, `COMMUNITY_TRAVEL_URL`: optional community links shown in the additional menu (built-in defaults apply when empty).

Use `config.env.example` as the complete template.

## Code Style and Naming

Conventions live in the rules files; this section is only a pointer.
Follow [`.agents/rules/code-style.md`](.agents/rules/code-style.md) for
style, [`security-rules.md`](.agents/rules/security-rules.md) for
authorization, [`asyncio-gather-rules.md`](.agents/rules/asyncio-gather-rules.md)
for async work, and [`comment-style.md`](.agents/rules/comment-style.md) for
comments before editing source code.

## Architecture Rules

Ownership and runtime behavior live in the rules and architecture docs;
this section is only a pointer.

- Module, helper, workflow, database, and utility boundaries:
  [`.agents/rules/code-style.md`](.agents/rules/code-style.md) and
  [`docs/architecture/modules.md`](docs/architecture/modules.md).
- Startup, transport, scheduler, and cache behavior:
  [`docs/architecture/repository-map.md`](docs/architecture/repository-map.md)
  and [`docs/architecture/database.md`](docs/architecture/database.md).
- Moderation authorization and role safety:
  [`.agents/rules/security-rules.md`](.agents/rules/security-rules.md).

## Commit and Pull Request Guidance

For automated CI/CD and auto-PR workflows, see [`docs/operations/ci-cd.md`](docs/operations/ci-cd.md) for more details. Commit-specific instructions belong to the active repository workflow, not to the public `docs/` category.

Use focused commits and scoped conventional prefixes (see
[`CONTRIBUTING.md`](CONTRIBUTING.md#pull-requests)); keep one logical fix
per commit with its own `CHANGELOG.md` slice.

Pull requests should include:

- A short summary of the change.
- For a long or detailed or short description submit to [`CHANGELOG.md`](CHANGELOG.md).
- Validation commands run (e.g. Ruff format and lint, pyright).
- Any configuration, database, or deployment impact.
- Screenshots or log excerpts only when user-visible behavior changed.

## Security Requirements

Authorization boundaries, secrets, and compatibility live in
[`.agents/rules/security-rules.md`](.agents/rules/security-rules.md).
The non-negotiable summary: no secrets in code, logs, or commits; no
`config.env` edits during normal work; backward-compatible schemas unless
a migration plan ships with the change.
