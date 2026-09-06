# TCF Bot - Replit Deployment Guide

Deploying TCF Bot on Replit requires specific configuration for secrets,
keep-alive, and webhook transport. This guide covers Replit-only setup.

For general project documentation, see [`README.md`](README.md).
For contributor rules and code style, see [`AGENTS.md`](AGENTS.md).

---

## Replit Secrets

Do not use `config.env` on Replit. Store all secrets in the Replit Secrets
manager (the lock icon in the sidebar).

Required secrets:

- `BOT_TOKEN`: Telegram bot token from BotFather.
- `MONGODB_URI`: MongoDB connection string.
- `OWNER_ID`: Telegram user ID for the initial federation founder.

Optional secrets:

- `REDIS_URL`: Redis connection URL for L2 cache.
- `WEBHOOK_SECRET`: Secret token for Telegram webhook validation.
- `PORT`: Flask keep-alive port, defaults to `5000`.

Replit Secrets are injected as environment variables at runtime. They are
never committed to the repository and do not require a `.env` file.

---

## Run Command

The Replit run command is:

```bash
uv run python -m tcbot
```

This starts the bot in webhook mode when `WEBHOOK_URL` is set, or falls back
to polling for local development.

---

## Replit-Specific Configuration

### Keep-Alive Server

The Flask keep-alive server binds to `0.0.0.0:${PORT}`. Replit's proxy
requires an HTTP server listening on the assigned port. The bot starts this
server automatically before connecting to Telegram.

If `PORT` is unset, invalid, or outside `1..65535`, the application defaults
to `5000`.

### Webhook Transport

Production Replit deployments should use webhook mode:

1. Set `WEBHOOK_URL` to your Replit public domain (e.g., `https://your-project.username.repl.co`). When `WEBHOOK_URL` is unset, the bot auto-detects `REPLIT_DEV_DOMAIN`, so an explicit URL is optional on Replit.
2. Set `WEBHOOK_SECRET` to a random string for request validation.
3. The bot registers the webhook with Telegram on startup.

Local development without a public URL falls back to polling automatically.

### Nix Configuration

Ensure the `.replit` file contains:

```ini
run = "uv run python -m tcbot"
```

For Python 3.14, the Replit Nix environment should provide a compatible
runtime. If using a custom `pyproject.toml`, ensure the Replit Python version
meets `requires-python = ">=3.13"`.

---

## Dependencies

Install dependencies with:

```bash
uv sync --frozen
```

The `uv.lock` file ensures reproducible installs on Replit. Do not commit
`config.env` with real credentials.

---

## Deployment Checklist

- [ ] `BOT_TOKEN` set in Replit Secrets.
- [ ] `MONGODB_URI` set in Replit Secrets.
- [ ] `OWNER_ID` set in Replit Secrets.
- [ ] `WEBHOOK_URL` set for production webhook mode (optional on Replit: `REPLIT_DEV_DOMAIN` is auto-detected).
- [ ] `WEBHOOK_SECRET` set for webhook validation.
- [ ] Run command is `uv run python -m tcbot`.
- [ ] `uv sync --frozen` has been run after dependency changes.
- [ ] Flask keep-alive is running on the assigned `PORT`.
- [ ] Bot status shows connected in Telegram.

---

## Troubleshooting

### Bot does not start

Check the Replit console output for missing environment variables. Ensure all
required secrets are set in the Replit Secrets manager.

### Webhook not receiving updates

Verify `WEBHOOK_URL` matches the public Replit domain and that Telegram can
reach it. Check the webhook secret matches between Telegram and Replit Secrets.

### Keep-alive not responding

Ensure the Flask server is listening on `0.0.0.0:${PORT}` and that the Replit
proxy is forwarding to the correct port. The `/health` endpoint should return
JSON with subsystem status.

### MongoDB connection errors

Verify `MONGODB_URI` is correct and that the MongoDB instance is accessible
from the Replit environment. For local MongoDB, use a service with a public
connection string or a MongoDB Atlas cluster.

---

## Local Development vs Replit

| Aspect | Local Development | Replit Deployment |
|---|---|---|
| Secrets | `config.env` file | Replit Secrets manager |
| Transport | Polling (no public URL) | Webhook (with `WEBHOOK_URL`) |
| Dependencies | `uv sync --frozen` | `uv sync --frozen` |
| Run command | `uv run python -m tcbot` | `uv run python -m tcbot` |
| MongoDB | Local or Atlas | Atlas or external |
| Redis | Optional local | Optional external |
