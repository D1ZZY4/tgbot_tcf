# Setup Guide

This guide explains how to run TCF Bot locally, with Docker, or in a hosted environment without committing secrets.

For the project overview, see [`../../README.md`](../../README.md). For
Replit-specific deployment, see [`../../replit.md`](../../replit.md).

## Prerequisites

- Python 3.14 or newer. The project metadata targets `>=3.13`.
- [`uv`](https://docs.astral.sh/uv/) for dependency and lockfile management.
- A Telegram bot token from BotFather.
- A MongoDB deployment, local or hosted.
- Telegram destination chats for logs, errors, proofs, and appeals.

## Local setup

```bash
git clone <repo-url>
cd tcbot
uv sync --frozen
cp config.env.example config.env
uv run python -m tcbot
```

The local command loads `config.env` through `python-dotenv`. Fill in the
required values before starting the bot.

Use `uv run python -m tcbot` so the locked `uv` environment is used regardless of whether your platform exposes Python as `python` or `python3`.

Format and lint after edits:

```bash
uv run ruff format .
uv run ruff check --fix .
uv run --with pyright pyright tcbot/
```

## Docker setup

The repository includes a Docker Compose stack with the bot, `mongo:7`, and Redis.
Compose reads `.env`, not `config.env`, and maps the bot's port as `5000:5000`.
Create a Docker-specific environment file before starting the stack:

```bash
cp config.env.example .env
# Edit .env and set at least:
# MONGODB_URI=mongodb://mongo:27017
# REDIS_URL=redis://redis:6379/0
docker compose up --build
```

Set `BOT_TOKEN`, `OWNER_ID`, and the destination variables required by the
features you enable in `.env`. The `bot` service waits for both MongoDB and
Redis health checks before startup. The image runs:

```bash
uv run --frozen python -m tcbot
```

## Hosted setup

For Replit or another hosting platform:

1. Store `BOT_TOKEN`, `MONGODB_URI`, and `OWNER_ID` in the platform secret
   manager or its environment configuration.
2. Store the required destination values (`LOGS`, plus `PROOFS`, `APPEALS`,
   and `APPEAL_DISCUSSION_TOPIC` when those features are enabled) as
   environment variables or secrets.
3. Start the bot with `uv run python -m tcbot`.
4. Make sure the Flask health endpoint port matches `PORT`.

Do not commit a filled `config.env` file.

### Community links (optional)

The Additional Links menu in the bot start screen shows buttons for community channels, groups, and the TRAVEL community. Each button is shown only when its corresponding env var is set to a non-empty URL. Leave any of these empty to hide that button:

| Variable | Button label |
|---|---|
| `COMMUNITY_CHANNEL_URL` | Main Channel |
| `COMMUNITY_GROUP_URL` | Discussion Group |
| `COMMUNITY_LOGS_URL` | Logs Channel |
| `COMMUNITY_EXEC_URL` | Exec Group |
| `COMMUNITY_TRAVEL_URL` | TRAVEL - Transsion Development (Community) |

## Environment variable format

The configuration loader reads environment variables in `tcbot/__init__.py`. Local development uses `python-dotenv` to load `config.env`.

Recommended `config.env` syntax:

```env
BOT_TOKEN="<telegram-bot-token>"
OWNER_ID="123456789"
MONGODB_URI="mongodb+srv://<user>:<password>@<cluster>/<options>"
DB_NAME="tcbot"
COMMUNITY_NAME="TCF - Transsion Core Federation"
PREFIXES='["/", "!", "."]'
PORT="5000"
```

Values that are parsed as `chat_id/thread_id` must use an integer chat ID and optional integer thread ID separated by `/`:

```env
PROOFS="-1001234567890/67"
LOGS="-1001234567890/42"
LOGS_ERRORS="-1001234567890/279"
APPEALS="-1001234567890/12"
```

Use a plain chat ID when no topic thread is needed:

```env
PROOFS="-1001234567890"
```

## Configuration reference

| Variable | Required | Format | Purpose |
|---|---:|---|---|
| `BOT_TOKEN` | Yes | string | Telegram bot token. Never commit it. |
| `OWNER_ID` | Yes | positive integer | Initial Founder user ID. Startup fails if missing or invalid. |
| `MONGODB_URI` | Yes | MongoDB URI | Motor connection string. Never commit it. |
| `DB_NAME` | No | string | MongoDB database name. Defaults to `tcbot`. |
| `COMMUNITY_NAME` | No | string | Display name in messages and logs. Defaults to `Bot`. |
| `PREFIXES` | No | Python-style list or CSV | Command prefixes. Default is `["/", "!", "."]`. |
| `PORT` | No | integer or `auto` | Flask keep-alive port. `auto`, invalid, or out-of-range values resolve to `5000`. |
| `MAIN_GROUP` | Usually | integer chat ID | Main community group or forum. |
| `MAIN_CHANNEL` | No | integer chat ID | Optional announcement channel reference. |
| `EXTEND_GROUP` | No | integer chat ID | Secondary group watched by selected handlers. |
| `PROOFS` | Yes for bans | `chat_id` or `chat_id/thread_id` | Destination for ban proof media. |
| `LOGS` | Yes | `chat_id` or `chat_id/thread_id` | Audit-log destination. |
| `LOGS_ERRORS` | Recommended | `chat_id` or `chat_id/thread_id` | Error-report destination. |
| `APPEALS` | Yes for appeals | `chat_id` or `chat_id/thread_id` | Submitted appeal record destination. |
| `APPEAL_LOG_HANDLE` | No | channel handle | Displayed in appeal instructions. Defaults to `@TranssionCoreFederationLogs`. |
| `APPEAL_DISCUSSION_TOPIC` | Yes for reviews | integer thread ID | Topic inside `MAIN_GROUP` where review cards are posted. |
| `PROOF_TIMEOUT_SECONDS` | No | integer seconds | Parsed into `cfg.proof_timeout`; **not currently enforced** (PTB `conversation_timeout` is not wired because the `[job-queue]` extra conflicts with this project's APScheduler setup). Conversations end via the command-fallback handler or the Cancel button. Default `100`; values below `1` fall back to default. |
| `APPEAL_TIMEOUT_SECONDS` | No | integer seconds | Parsed into `cfg.appeal_timeout`; **not currently enforced** (same reason as `PROOF_TIMEOUT_SECONDS`). Default `600`; values below `1` fall back to default. |
| `ALBUM_DEBOUNCE_SECONDS` | No | integer seconds | Album buffering window for ban proof media. Default `2`; values below `1` fall back to default. |
| `LOG_LEVEL` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | Runtime logging level. Default `INFO`. |
| `MODULES_LOAD` | No | comma-separated module names | Optional whitelist, e.g. `banning,appeals`. |
| `MODULES_NO_LOAD` | No | comma-separated module names | Optional blacklist, e.g. `maintenance,broadcasting`. |
| `REDIS_URL` | No | Redis URI | L2 cache connection string, e.g. `redis://localhost:6379/0`. When absent the bot uses in-process L1 cache only. |
| `WEBHOOK_URL` | No | URL | Base URL for webhook transport, e.g. `https://yourdomain.example.com`. An explicit value takes precedence over the automatically detected `REPLIT_DEV_DOMAIN`; when neither is available, the bot falls back to polling for local development. |
| `WEBHOOK_SECRET` | No | random string | Secret token sent in the `X-Telegram-Bot-Api-Secret-Token` header by Telegram on every webhook POST. Auto-generated with `secrets.token_hex(32)` if absent. Set it explicitly for a stable deployment token. |
| `CRON_SECRET` | Yes on Vercel | random string | Bearer token for the serverless warn-expiry cron endpoint. Requests without a matching token fail closed (`401`/`503`). |
| `WARN_LIMIT` | No | integer >= 1 | Per-group warning threshold that triggers automatic federation ban. Default `3`. When a user's warn count in one group reaches exactly this value, they are federation-banned and their group warns cleared. |
| `FED_WARN_LIMIT` | No | integer >= 0 | Federation-wide warning threshold: sum of warn counts across all groups triggers an automatic federation ban when >= this value. Default `0` (disabled). Set to a positive integer to enable cross-group warn aggregation. |
| `WARN_EXPIRY_DAYS` | No | positive integer | Days after which `warn_counts` records are deleted by the daily scheduler job. Default `0` (disabled). Set to a positive integer to enable automatic warn expiry. |

## Startup sequence

1. `tcbot.__init__` loads configuration into `cfg` and fails fast when `BOT_TOKEN`, `MONGODB_URI`, or `OWNER_ID` are missing.
2. `tcbot.__main__.main()` configures logging.
3. `tcbot.alive.start_keepalive()` starts Flask on `0.0.0.0:PORT`.
4. PTB `ApplicationBuilder` builds the bot application.
5. `tcbot.modules.get_handlers()` imports active modules and stops startup if an enabled module fails to import.
6. Signal handlers (`SIGTERM`, `SIGINT`) are registered immediately before the PTB lifecycle begins.
7. `_post_init()` connects MongoDB, ensures indexes, seeds the initial owner, and attaches the error reporter.
8. **Webhook mode** (when `WEBHOOK_URL` or `REPLIT_DEV_DOMAIN` resolves to a URL): `bot.set_webhook()` registers the URL, `register_webhook()` wires Flask's `POST /webhook` to PTB's update queue, and the bot waits for `SIGTERM`/`SIGINT`.
9. **Polling mode** (when no webhook URL is available): `run_polling()` starts with `drop_pending_updates=True`. A `WARNING` log identifies this local-development fallback.

## Troubleshooting

### `BOT_TOKEN is required but not set`

Set `BOT_TOKEN` in `config.env` or the host secret manager.

### `OWNER_ID is required and must be a positive integer`

Set `OWNER_ID` to your numeric Telegram user ID, not a username.

### `MONGODB_URI is required but not set`

Set `MONGODB_URI` in `config.env` or the host secret manager. Do not paste real connection strings into logs or documentation.

### MongoDB connection failure

Check `MONGODB_URI`, network access, Atlas IP allowlists, and database credentials.

### MongoDB `CERTIFICATE_VERIFY_FAILED` (`unable to get local issuer certificate`)

The host's system CA store is empty or outdated, so the Atlas TLS chain cannot be verified. The bot pins TLS trust to the `certifi` Mozilla bundle (`tlsCAFile` in `tcbot/database/mongos.py`), which fixes minimal sandboxes without one. If this error persists, the `certifi` install is stale (`uv sync --frozen` refreshes it) or `MONGODB_URI` carries a conflicting TLS option; an explicit `tlsCAFile` in the URI always wins over the bundled default. Never disable TLS verification to work around this.

### `Module import failed for: ...`

Check the named module's import traceback, missing dependencies, and top-level syntax before redeploying. Enabled modules should fail loudly rather than being skipped silently.

### A command does nothing

Check:

- The module is not blocked by `MODULES_NO_LOAD`.
- `MODULES_LOAD` is empty or includes the module filename without `.py`.
- The command prefix is included in `PREFIXES`.
- The module exposes a non-empty `__handlers__` list.

### Buttons stop responding

Check callback patterns in the registering module, then inspect `tcbot/modules/helper/keyboards.py` and the matching callback handler.
