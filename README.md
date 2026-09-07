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

## Commands

| Command | Purpose |
|---|---|
| `/tcban` (alias `/tcb`) | Federation-wide ban with required photo/video proof. |
| `/tcunban` (alias `/tcunb`) | Lift an active federation ban across all groups. |
| `/tckick` (alias `/tck`) | Remove a user from the current group only. |
| `/tcmute` (alias `/tcm`), `/tcunmute` | Restrict or restore messaging across all groups, optionally timed (`30s`, `15m`, `2h`, `7d`, `1w`, `3mo`, `2ye`). |
| `/tcwarn` (alias `/tcw`), `/tcunwarn`, `/warns`, `/resetwarns` | Per-group warnings; hitting the limit triggers an automatic federation ban. |
| `/check`, `/checkme` | Ban-history lookup for a target or for yourself, with appeal links. |
| `/tcconnect`, `/tcdisconnect`, `/rmtc`, `/tcgroups` | Connect or remove groups and list the federation roster. |
| `/tcpromote`, `/tcdemote`, `/transferowner` | Manage Founder, Admin, Developer, and Tester roles. |
| `/tcstats` | Federation statistics with drill-down views. |

Appeals are submitted by the banned user in bot DM through a deep link. Command prefixes are configurable (default `/`, `!`, `.`).

## Configuration

Only three values are required. Everything else has a working default.

| Variable | Required | Description |
|---|---:|---|
| `BOT_TOKEN` | Yes | Bot token from BotFather. |
| `OWNER_ID` | Yes | Telegram user ID seeded as the initial Founder. |
| `MONGODB_URI` | Yes | MongoDB connection string. |

Common optional values: `REDIS_URL` (enables the shared cache), `WEBHOOK_URL` (enables webhook transport, otherwise the bot polls), `PORT` (health server port, default `5000`), and the `PROOFS`, `LOGS`, `LOGS_ERRORS`, `APPEALS` log destinations, which each accept a chat ID or a `chat_id/thread_id` pair.

> [!IMPORTANT]
> Never commit real secrets. Keep `BOT_TOKEN`, `MONGODB_URI`, passwords, webhook secrets, and private chat IDs out of the repository; use the platform secret manager on hosted deployments.

<details>
<summary>Full variable reference (all settings in <code>config.env.example</code>)</summary>

### Core

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | Yes | - | Bot token from BotFather. |
| `OWNER_ID` | Yes | - | Telegram user ID seeded as the initial Founder. |
| `MONGODB_URI` | Yes | - | MongoDB connection string. |
| `DB_NAME` | No | `tcbot` | Database name, created automatically. |
| `COMMUNITY_NAME` | No | `Bot` | Display name used in bot messages and logs. |
| `PREFIXES` | No | `["/", "!", "."]` | Python-style list of command prefixes. |
| `PORT` | No | `5000` | Flask health-server port (`1`-`65535`); `auto` or invalid values fall back to `5000`. |
| `LOG_LEVEL` | No | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |

### Federation chats

| Variable | Required | Description |
|---|---|---|
| `MAIN_GROUP` | Usually | Main community group or forum (negative chat ID). |
| `MAIN_CHANNEL` | No | Announcement channel reference for some log messages. |
| `EXTEND_GROUP` | No | Secondary group watched alongside `MAIN_GROUP`. |

### Logs, proofs, and appeals

Chat destinations accept a chat ID or a `chat_id/thread_id` pair.

| Variable | Required | Description |
|---|---|---|
| `PROOFS` | For bans | Ban proof media destination. |
| `LOGS` | Yes | Audit-log destination for bans, unbans, appeals, and admin actions. |
| `LOGS_ERRORS` | Recommended | Error-report destination; empty disables error shipping (errors are not forwarded to `LOGS`). |
| `APPEALS` | For appeals | Submitted-appeal record destination. |
| `APPEAL_LOG_HANDLE` | No | Display handle shown with appeal logs (default `@TranssionCoreFederationLogs`). |
| `APPEAL_DISCUSSION_TOPIC` | For reviews | Thread ID inside `MAIN_GROUP` where review cards are posted. |

### Moderation thresholds

| Variable | Default | Description |
|---|---|---|
| `WARN_LIMIT` | `3` | Per-group warns triggering auto-ban (minimum `1`, exact match). |
| `FED_WARN_LIMIT` | `0` (disabled) | Federation-wide warns triggering auto-ban. |
| `WARN_EXPIRY_DAYS` | `0` (disabled) | Days after which warn counts expire via a daily job. |

### Timeouts and modules

| Variable | Default | Description |
|---|---|---|
| `PROOF_TIMEOUT_SECONDS` | `100` | Parsed but not currently enforced (reserved). |
| `APPEAL_TIMEOUT_SECONDS` | `600` | Parsed but not currently enforced (reserved). |
| `ALBUM_DEBOUNCE_SECONDS` | `2` | Album buffering window before the ban executor runs. |
| `MODULES_LOAD` | empty (all) | Whitelist of modules to load exclusively. |
| `MODULES_NO_LOAD` | empty | Blacklist of modules to skip. |

### Transport

| Variable | Description |
|---|---|
| `WEBHOOK_URL` | Public HTTPS base URL for webhook transport; empty falls back to polling. Auto-detected from `REPLIT_DEV_DOMAIN` on Replit. |
| `WEBHOOK_SECRET` | Used as-is when set, auto-generated per restart when empty; required on Vercel. |
| `CRON_SECRET` | Bearer token for `/api/cron`; empty refuses every request (fail closed). |

### Community links

`COMMUNITY_CHANNEL_URL`, `COMMUNITY_GROUP_URL`, `COMMUNITY_LOGS_URL`, `COMMUNITY_EXEC_URL`, `COMMUNITY_TRAVEL_URL`: each shows one button in the Additional Links menu; a non-empty value overrides the built-in default, empty falls back to it.

</details>

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

> [!CAUTION]
> Never run two live bot instances with the same token (for example polling locally while webhook mode is deployed). The transports fight over updates and Telegram reports `Conflict` errors.

One pitfall to avoid: committing `config.env` with real values because local testing worked. Keep it untracked and put production values in the host secret manager instead.

## Deployment

Run exactly one live instance per token on exactly one target below. Every target runs the same command (`uv run python -m tcbot`) except Vercel serverless, which uses `/api/webhook` + `/api/cron` instead of a process.

<details>
<summary>GitHub Actions automation (lint, auto-fix, dependencies, CodeQL)</summary>

Four CI workflows run automatically; no secrets needed (validation steps use dummy values, so fork PRs work too). All use Python 3.14 with `uv sync --frozen` and cached `uv` setup.

- **Lint** (`lint.yml`): on push to `main`/`feat/**`/`fix/**` and PRs to `main`. Runs `ruff format --check`, `ruff check`, and the `import tcbot` check. Fails the PR subject to branch protection.
- **Auto-Fix** (`auto-fix.yml`): same push/PR triggers plus weekly Monday 04:00 UTC and manual dispatch. Runs `ruff format` + `ruff check --fix`; outside a PR it force-pushes the fixed `auto-fix/ruff` branch and opens a PR, on a PR it comments the local fix commands instead. Needs `contents: write` + `pull-requests: write`.
- **Dependency Updates** (`dependency-update.yml`): weekly Monday 04:00 UTC and manual dispatch. Runs `uv lock --upgrade`, validates with Ruff plus the import check, then opens a `deps/auto-update-YYYYMMDD` PR labeled `dependencies` against `main` (no-op when the lockfile is unchanged). Sends the owner a Telegram status DM when `BOT_TOKEN`/`OWNER_ID` secrets exist, silently skips otherwise.
- **CodeQL** (`codeql.yml`): on push/PR to `main` plus weekly Tuesday 15:38 UTC. Scans the `actions` and `python` languages with `build-mode: none`; findings land under the repository Security tab.

Mirror the gate locally before pushing: `uv run ruff format --check .`, `uv run ruff check .`, `uv run python -c "import tcbot"`. Full reference: [`docs/operations/ci-cd.md`](docs/operations/ci-cd.md).
</details>

<details>
<summary>GitHub Actions 24/7 runner (self-chaining)</summary>

`.github/workflows/run-bot.yml` runs the bot in 5-hour windows (GitHub hard-caps a job at 6 h) with a crash watchdog, a handover dispatch about 10 minutes before the window ends, and a `*/15` cron resurrection fallback.

1. Repository → Settings → Secrets and variables → Actions: set `BOT_TOKEN`, `MONGODB_URI`, `OWNER_ID` (plus `WEBHOOK_URL`/`WEBHOOK_SECRET` for webhook mode, `REDIS_URL` optional).
2. For gap-free chaining add `BOT_PAT`: a Personal Access Token with the `workflow` scope. Without it only the 15-minute cron restarts the bot.
3. Actions → "TCF Bot - 24/7 Runner" → Run workflow (or wait for the schedule).
4. Verify in the run logs (`Bot started`, `subsystems ready`); crash tails ship as credential-scrubbed artifacts kept 7 days.

Fail-fast guard: 5 deaths within 10 minutes aborts the run (usually bad config); the cron recovers after you fix secrets. Full reference: [`docs/operations/ci-cd.md`](docs/operations/ci-cd.md). Respect GitHub Actions usage limits and Terms of Service; this runner is a community convenience, not a hosting SLA.
</details>

<details>
<summary>Vercel native serverless</summary>

Telegram delivers each update to `/api/webhook`; Vercel Cron drives warn expiry through `/api/cron`. No process, no polling, no scheduler.

1. Vercel → Project → Settings → Environment Variables: set `BOT_TOKEN`, `MONGODB_URI`, `OWNER_ID`, `WEBHOOK_SECRET` (required, fail-closed `503` without it), `CRON_SECRET` (required, fail-closed without it).
2. Deploy with `vercel --prod` (dependencies from `pyproject.toml` + `uv.lock`, Python `3.14`, two functions with `maxDuration: 60` plus a daily `02:00 UTC` cron per `vercel.json`).
3. Stop every other instance for this token, then register the webhook once per URL: `curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" -d "url=https://<project>.vercel.app/api/webhook" -d "secret_token=<WEBHOOK_SECRET>" -d "drop_pending_updates=true"`.
4. Validate: `GET /api/webhook` → `200 OK`; `/checkme` in bot DM replies; `GET /api/cron` with `Authorization: Bearer <CRON_SECRET>` → `200`.

Limits: multi-step conversations are best-effort (cold starts drop instance-memory state), timed unbans never fire, large fan-outs can approach the function timeout. Proof-gated moderation belongs on a long-lived transport. Full reference: [`docs/operations/vercel.md`](docs/operations/vercel.md).
</details>

<details>
<summary>Docker and Docker Compose</summary>

1. `cp config.env.example .env` and set at least `BOT_TOKEN`, `OWNER_ID`, `MONGODB_URI=mongodb://mongo:27017`, `REDIS_URL=redis://redis:6379/0`.
2. `docker compose up --build` (bot plus `mongo:7` plus `redis:7-alpine`, each with health checks; the bot waits for both databases).
3. Verify: `curl localhost:5000/health` returns subsystem JSON with HTTP 200.

Single image without compose: `docker build -t tcf-bot .` then `docker run --env-file .env -p 5000:5000 tcf-bot` (image command is `uv run --frozen python -m tcbot`). Point MongoDB/Redis at reachable hosts.
</details>

<details>
<summary>Heroku (container stack, no Procfile needed)</summary>

The repository ships no `Procfile`; deploy the existing `Dockerfile` through the container registry.

1. `heroku create <app> && heroku stack:set container --app <app>` (or set the stack in Dashboard → Settings).
2. `heroku config:set BOT_TOKEN=... MONGODB_URI=... OWNER_ID=... WEBHOOK_URL=https://<app>.herokuapp.com WEBHOOK_SECRET=... --app <app>` (plus `REDIS_URL` from Heroku Data for Redis when needed; never commit secrets).
3. `heroku container:push worker --app <app> && heroku container:release worker --app <app>`.
4. `heroku ps:scale worker=1 --app <app>` and verify with `heroku logs --tail --app <app>`.

Heroku assigns `PORT` automatically and the bot reads it from the environment. Keep exactly one `worker` dyno per token.
</details>

<details>
<summary>VPS with systemd (Ubuntu/Debian)</summary>

1. Install Python 3.14 and `uv`, then `git clone <repo-url> /opt/tcf-bot && cd /opt/tcf-bot && uv sync --frozen`.
2. Create `/opt/tcf-bot/config.env` with `BOT_TOKEN`, `OWNER_ID`, `MONGODB_URI` (Atlas with the VPS IP allowlisted, or self-hosted MongoDB), mode `0600`.
3. Install this unit as `/etc/systemd/system/tcf-bot.service` (adjust the `uv` path from `which uv`):

```ini
[Unit]
Description=TCF Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tcfbot
WorkingDirectory=/opt/tcf-bot
EnvironmentFile=/opt/tcf-bot/config.env
ExecStart=/usr/local/bin/uv run --frozen python -m tcbot
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

4. `systemctl daemon-reload && systemctl enable --now tcf-bot && journalctl -u tcf-bot -f`.
5. Webhook mode needs a public HTTPS URL pointing at `PORT` (reverse proxy or tunnel) with `WEBHOOK_URL` set; without one the bot polls, which is fine for one instance. Open the firewall only for what the proxy needs and point the uptime monitor at `GET /health`.
</details>

<details>
<summary>Windows Server over RDP</summary>

1. RDP in, install Python 3.14 (add to PATH) and `uv` (`irm https://astral.sh/uv/install.ps1 | iex`), then `git clone <repo-url> C:\tcf-bot` and `uv sync --frozen` inside it.
2. Create `C:\tcf-bot\config.env` with `BOT_TOKEN`, `OWNER_ID`, `MONGODB_URI`; restrict it to the service account via file ACLs.
3. Verify once interactively: `uv run python -m tcbot` (expect `subsystems ready`, `Ctrl+C` to stop).
4. Persist with Task Scheduler: trigger "At startup", action `uv.exe run --frozen python -m tcbot` with start-in `C:\tcf-bot`, "Run whether user is logged on or not", "If the task fails, restart every 1 minute". Prefer a dedicated service account, never an admin's personal session.
5. Webhook mode needs public HTTPS reaching the host `PORT` (reverse proxy or tunnel) plus `WEBHOOK_URL`; otherwise the bot polls. Monitor `GET /health` and keep exactly one running instance per token.
</details>

## Health Check

The Flask keep-alive server binds to `0.0.0.0:${PORT}` (defaults to `5000` on unset, invalid, or out-of-range values).

- `GET /` returns `OK` for uptime monitors.
- `GET /health` returns subsystem status (MongoDB, Redis, scheduler, circuit breakers) as JSON, with HTTP 503 while degraded.

> [!TIP]
> Point uptime monitors at `GET /health`: the 503 status tells load balancers and watchdogs the instance needs attention without reading the body.

## Troubleshooting

- `BOT_TOKEN`, `OWNER_ID`, or `MONGODB_URI` errors on startup: the bot fails fast on missing identity or database settings. Set them in `config.env` or the host secret manager.
- MongoDB connection failures: check the URI, network access, Atlas IP allowlists, and credentials.
- `GET /health` returns 503: one subsystem (MongoDB, Redis, scheduler, or a circuit breaker) is degraded; read the JSON body to see which one.

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

<details>
<summary>Contribution activity graph</summary>

Contribution, issue, and pull request activity, rendered by Repobeats.

![Repobeats analytics image](https://repobeats.axiom.co/api/embed/1a7b88805412c20305e8076a29a737330bae83ec.svg "Repobeats analytics image")

</details>

## License

Copyright © 2024-2026 Transsion Core, Dizzy, Ave Labs. All rights reserved.

See `LICENSE` for details.
