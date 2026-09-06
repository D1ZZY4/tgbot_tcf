# CI/CD Workflows

This document describes all GitHub Actions workflows configured for the TCF Bot project.

For the user-facing CI/CD overview, see [`../../README.md`](../../README.md#cicd--automation).
For the changelog of CI/CD additions, see
[`../../CHANGELOG.md`](../../CHANGELOG.md).

## Overview

The project uses 5 automated workflows for continuous integration, code quality, and maintenance:

1. **Lint** - Lint, format, and import check
2. **Auto-Fix Code Quality** - Automatically fix linting issues
3. **Dependency Updates** - Weekly dependency updates with auto-PR
4. **CodeQL** - Security analysis
5. **Run Bot** - Long-running bot runner with handover and cron fallback

---

## 1. Lint (CI Gate)

**File:** `.github/workflows/lint.yml`

**Triggers:**
- Push to `main`, `feat/**`, `fix/**`
- Pull requests to `main`

**What it does:**
- Runs `uv run ruff format --check .` to verify formatting without modifying files
- Runs `uv run ruff check .` to catch all lint violations
- Runs `uv run python -c "import tcbot"` to verify all imports resolve cleanly
- **Fails the PR** if any step exits with a non-zero code

**Why this exists:**
`lint.yml` provides a repeatable CI result for formatting, lint, and import
checks. Whether it blocks merging depends on the repository's branch
protection settings.

---

## 2. Auto-Fix Code Quality

**File:** `.github/workflows/auto-fix.yml`

**Triggers:**
- Push to `main`, `feat/**`, `fix/**`
- Pull requests to `main`
- Weekly schedule (Monday 04:00 UTC)
- Manual dispatch

**What it does:**
- Runs `uv run ruff format .` to auto-format code
- Runs `uv run ruff check --fix .` to auto-fix linting issues
- Creates or updates an `auto-fix/ruff` branch and pull request when fixes are
  found outside a pull-request run
- **Comments on PR** with fix suggestions (if PR)
- Creates detailed summary of changes

**Benefits:**
- Reduces manual work for code style
- Consistent formatting across reviewed changes
- Catches common issues automatically

**Example generated commit:**
```
chore: Auto-fix code quality issues

- Ruff format: 3 files
- Ruff check --fix: 5 files

Auto-applied by GitHub Actions
```

---

## 3. Dependency Updates

**File:** `.github/workflows/dependency-update.yml`

**Triggers:**
- Weekly schedule (Monday 04:00 UTC)
- Manual dispatch

**What it does:**
- Runs `uv lock --upgrade` to update all dependencies
- Installs updated dependencies
- **Auto-creates PR** with dependency updates
- PR includes diff of changes
- **Sends Telegram notification** with result

**Benefits:**
- Regular dependency review
- Less manual work for routine updates
- Telegram status notifications when configured

**Example PR:**
```
Title: chore: Auto-update dependencies

Body:
## Automated Dependency Update

This PR updates project dependencies to their latest compatible versions.

### Changes
- python-telegram-bot: <old> → <new>
- motor: <old> → <new>

Review the dependency changes and CI results before merging.
```

---

## 4. CodeQL

**File:** `.github/workflows/codeql.yml`

**Triggers:**
- Push to `main`
- Pull requests to `main`
- Weekly schedule

**What it does:**
- Runs GitHub's security analysis
- Scans for vulnerabilities
- Checks for common security issues

---

## 5. Run Bot

**File:** `.github/workflows/run-bot.yml`

**Triggers:**
- Self-dispatch (`workflow_dispatch`) from the previous run, for seamless chaining
- Cron schedule every 15 minutes as a resurrection fallback if the chain breaks

**What it does:**
- Runs the bot for a ~5 hour window per run (GitHub caps a job at 6h). When `WEBHOOK_URL` is set the bot uses webhook mode; otherwise it falls back to polling. `WEBHOOK_SECRET` is optional because the runtime generates one when absent
- **Self-chains:** roughly 10 minutes before the window ends (`HANDOVER_LEAD=600`), it dispatches the next run. The dispatch is retried up to 3 times (10s apart). This requires a repository secret `BOT_PAT` (a Personal Access Token with the `workflow` scope), because the built-in `GITHUB_TOKEN` cannot trigger workflows
- The cron schedule (every 15 minutes) acts as a resurrection fallback if the chain breaks or no PAT is configured. The `concurrency` group (`cancel-in-progress: false`) serializes runs: a cron tick while a run is active queues behind it instead of being discarded, so ticks can pile up behind a long holder
- A `concurrency` group (`tcf-bot-runner`, `cancel-in-progress: false`) prevents overlapping bot instances. This avoids duplicate update processing in polling mode and keeps webhook ownership unambiguous
- Bot configuration comes from repository secrets (`BOT_TOKEN`, `MONGODB_URI`, `OWNER_ID`, `WEBHOOK_URL`, `WEBHOOK_SECRET`, etc.), plus the optional `BOT_PAT` for self-chaining

---

## Workflow Dependencies

```
Lint (CI Gate)
    ↓
Pass: PR can merge / Fail: PR is blocked

Auto-Fix Code Quality
    ↓
Auto-fix branch and PR OR PR Comment (PR)

Dependency Updates
    ↓
Auto-create PR
    ↓
Telegram Notification

Run Bot
    ↓
Self-dispatch next run (~10 min before window ends)
    ↓
Cron fallback restarts if the chain breaks
```

---

## Secrets Required

Configure these in GitHub repository settings → Secrets:

| Secret | Purpose | Required |
|--------|---------|----------|
| `BOT_TOKEN` | Telegram bot token (bot runtime + notifications) | Yes |
| `MONGODB_URI` | MongoDB connection string for the bot runtime | Yes |
| `OWNER_ID` | Your Telegram user ID (initial owner + notifications) | Yes |
| `WEBHOOK_URL` | Public HTTPS URL for Telegram webhook (e.g. `https://your-domain.com`). When set, bot runs in webhook mode; absent means polling fallback | Recommended |
| `WEBHOOK_SECRET` | Secret token for `set_webhook` and `X-Telegram-Bot-Api-Secret-Token` validation. Auto-generated when omitted | Recommended |
| `BOT_PAT` | Personal Access Token with `workflow` scope, used by Run Bot to self-chain into the next run for seamless 24/7 coverage | Optional (recommended) |
| `GITHUB_TOKEN` | Auto-provided by GitHub Actions | Auto |

Without `BOT_PAT`, the Run Bot workflow cannot dispatch its own next run; it falls back to the every-15-minute cron resurrection schedule.

---

## Notification Examples

### Dependency Update
```
Dependency Update - PASS

Dependencies updated
Result: PR created

View workflow
```

---

## Best Practices

### For Developers

1. Run the same checks locally before opening a pull request:
   `uv run ruff format --check .`, `uv run ruff check .`, and
   `uv run python -c "import tcbot"`.
2. Review auto-fix and dependency pull requests before merging them.
3. Treat Telegram notifications as status updates, not as a substitute for
   reviewing the workflow result.

### For Maintainers

1. **Monitor GitHub issues** - Auto-created issues need triage
2. **Review auto-fix pull requests** - Verify changes are correct
3. **Keep `BOT_PAT` valid** - An expired token breaks Run Bot self-chaining; the cron fallback still resurrects the bot, but with brief gaps
4. **Check workflow runs** - Weekly scheduled runs keep dependencies fresh

---

## Troubleshooting

### Telegram notifications not working
- Verify `BOT_TOKEN` and `OWNER_ID` secrets are set
- Verify bot can send messages to your user ID

### Auto-fix pull request not created
- Check branch protection rules allow bot commits
- Verify workflow has `contents: write` permission

### Dependency PR not created
- Verify `pull-requests: write` permission

### Bot not staying online (Run Bot)
- Verify `BOT_TOKEN`, `MONGODB_URI`, and `OWNER_ID` secrets are set
- For seamless 24/7, set `BOT_PAT` (a PAT with the `workflow` scope) so the run can dispatch its successor; otherwise only the 15-minute cron fallback restarts it
- A `409 Conflict` from Telegram means two instances are polling at once; the `tcf-bot-runner` concurrency group should prevent this, so check for a stray manual run

---

## Maintenance

### Weekly Tasks (Automated)
- Dependency updates (Monday 04:00 UTC)
- Code quality fixes (Monday 04:00 UTC)

### Manual Tasks
- Review and merge dependency update PRs
- Triage auto-created issues
- Keep `BOT_PAT` valid so Run Bot self-chaining stays seamless

---

The workflows documented here are the workflows currently present in
`.github/workflows/`. New automation should be documented here when it is
added.
