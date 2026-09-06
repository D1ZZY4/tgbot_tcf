# TCF Bot

Telegram federation moderation bot for the Transsion Core Federation community. One bot enforces moderation decisions across all connected groups: federation bans with proof, appeals, staff roles, and audit logging.

## Requirements

- Python 3.14 and `uv`
- A Telegram bot token from BotFather
- MongoDB (Atlas or self-hosted)
- Redis is optional. Without it the bot uses in-process caching.

## Quick Start

```bash
cp config.env.example config.env
uv sync --frozen
uv run python -m tcbot
```

Fill in at least `BOT_TOKEN`, `OWNER_ID`, and `MONGODB_URI` in `config.env` before running. Never commit that file. The full variable list is in [`config.env.example`](config.env.example) and [`docs/getting-started/setup.md`](docs/getting-started/setup.md).

## What It Does

- **Federation bans with proof.** `/tcban` collects proof media or text first, then enforces across every connected group and stores the proof reference for review.
- **Appeals.** Banned users submit through a deep link in bot DM. Staff approve or reject from review cards posted in the main group.
- **Standard moderation.** `/tckick`, `/tcmute`, `/tcwarn`, `/tcunban`, and `/check` cover kick, mute, warn, unban, and ban-history lookup. Warn thresholds can trigger an automatic federation ban.
- **Staff roles.** Founder, Admin, Developer, and Tester hierarchy with promotion requests, demotion, and automatic demotion before a staff member is banned, kicked, or muted.
- **Connected groups.** Groups opt in with `/tcconnect`. Multi-group actions fan out with bounded concurrency so one failing group does not abort the rest.
- **Audit trail.** Moderation, appeal, role, and error reports go to the configured log destinations.

## Configuration

Only three values are required. Everything else has a working default.

| Variable | Required | Description |
|---|---:|---|
| `BOT_TOKEN` | Yes | Bot token from BotFather. |
| `OWNER_ID` | Yes | Telegram user ID seeded as the initial Founder. |
| `MONGODB_URI` | Yes | MongoDB connection string. |

Common optional values: `REDIS_URL` (enables the shared cache), `WEBHOOK_URL` (enables webhook transport, otherwise the bot polls), `PORT` (health server port, default `5000`), and the `PROOFS`, `LOGS`, `LOGS_ERRORS`, `APPEALS` log destinations, which each accept a chat ID or a `chat_id/thread_id` pair.

Do not put tokens, connection strings, or private chat IDs in committed files. Use the platform secret manager on hosted deployments.

## Running

Local development uses polling when no public URL is configured:

```bash
uv run python -m tcbot
```

With Docker Compose (bot plus local MongoDB and Redis):

```bash
docker compose up --build
```

The compose setup reads `.env`, so copy `config.env.example` to `.env` first. Details are in [`docs/getting-started/setup.md`](docs/getting-started/setup.md).

In production set `WEBHOOK_URL` to the public HTTPS base URL so Telegram delivers updates to `POST /webhook`. Requests are validated against `WEBHOOK_SECRET`. On Vercel the webhook and the daily warn-expiry cron run as serverless functions; `WEBHOOK_SECRET` and `CRON_SECRET` are required there. See [`docs/operations/vercel.md`](docs/operations/vercel.md). On Replit keep secrets in Replit Secrets and use the same run command; see [`replit.md`](replit.md).

One pitfall to avoid: committing `config.env` with real values because local testing worked. Keep it untracked and put production values in the host secret manager instead.

## Health Check

The Flask keep-alive server binds to `0.0.0.0:${PORT}` (defaults to `5000` on unset, invalid, or out-of-range values).

- `GET /` returns `OK` for uptime monitors.
- `GET /health` returns subsystem status (MongoDB, Redis, scheduler, circuit breakers) as JSON, with HTTP 503 while degraded.

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run --with pyright pyright .
uv run python -m compileall -q tcbot
uv run python -c "import tcbot"
git diff --check
```

`pyproject.toml` targets Python 3.14 with line length 88. Dependencies install from `uv.lock` via `uv sync --frozen`. CI runs lint, auto-fix PRs, weekly dependency updates, CodeQL, and a long-running bot runner; see [`docs/operations/ci-cd.md`](docs/operations/ci-cd.md). Contribution workflow and review checklist are in [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Documentation

- [Setup guide](docs/getting-started/setup.md): local, Docker, and hosted setup with the full environment reference.
- [Architecture map](docs/architecture/repository-map.md): startup flow, package ownership, and service boundaries.
- [Module breakdown](docs/architecture/modules.md): command ownership and handler registration.
- [Database layer](docs/architecture/database.md): collections, helpers, and indexes.
- [Workflow overview](docs/features/workflow-overview.md): user-visible moderation, appeal, and role flows.
- [Project guide](AGENTS.md): contributor and maintainer rules.

## Repo Activity

Contribution, issue, and pull request activity, rendered by Repobeats.

![Repobeats analytics image](https://repobeats.axiom.co/api/embed/1a7b88805412c20305e8076a29a737330bae83ec.svg "Repobeats analytics image")

## License

Copyright © 2024-2026 Transsion Core, Dizzy, Ave Labs. All rights reserved.

See `LICENSE` for details.
