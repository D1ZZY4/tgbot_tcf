# Contributing to TCF Bot

Thank you for helping improve TCF Bot. This guide is written for **human
contributors**. If you are an AI agent, start with [`AGENTS.md`](AGENTS.md)
and the six canonical rule files under [`.agents/rules/`](.agents/rules/)
instead: this file is the human-friendly summary, not the agent contract.

## Before You Start

1. Read [`README.md`](README.md) for the project overview.
2. Read [`AGENTS.md`](AGENTS.md) for repository architecture and conventions.
3. Skim [`CHANGELOG.md`](CHANGELOG.md) and the existing documentation for the
   area you plan to change.
4. If your change touches Python behavior, also read the relevant rule files:
   [Tooling and validation](.agents/rules/tooling-validation.md),
   [Code style and architecture](.agents/rules/code-style.md), and
   [Comment and documentation style](.agents/rules/comment-style.md).
   Authorization work additionally needs
   [Security](.agents/rules/security-rules.md); async work additionally needs
   [Asyncio](.agents/rules/asyncio-gather-rules.md); docs work additionally
   needs [Docs](.agents/rules/docs-rules.md).

For user-facing setup, use [`docs/getting-started/setup.md`](docs/getting-started/setup.md).
For deployment, use [`replit.md`](replit.md) and
[`docs/operations/ci-cd.md`](docs/operations/ci-cd.md).

## Local Setup

Requirements:

- Python 3.14
- `uv`
- MongoDB for runtime work
- Redis only when testing the optional L2 cache

Install the locked dependencies:

```bash
uv sync --frozen
```

Copy `config.env.example` to a local `config.env` when environment-based local
development is needed. Use placeholders or local values only.

> [!IMPORTANT]
> Never commit `config.env`, tokens, passwords, database URIs, webhook secrets, or private chat IDs.

Run the bot:

```bash
uv run python -m tcbot
```

## Development Workflow

1. Choose a focused change and inspect the current implementation before
   editing.
2. Keep command handlers in `tcbot/modules/`, database access in
   `tcbot/database/`, shared helpers in `tcbot/modules/helper/`, and runtime
   utilities in `tcbot/utils/`.
3. Use the existing workflow, role, formatting, and dispatch helpers instead of
   duplicating behavior.
4. Keep user-facing bot messages in English and HTML parse mode.
5. Update documentation and diagrams when behavior or structure changes.
6. Add a concise entry under `[Unreleased]` in `CHANGELOG.md`.

> [!CAUTION]
> Moderation, role, and database changes can affect every connected group. Verify success and failure paths, alternate entry points, and state transitions; never dismiss a moderation bypass as an edge case.

## Validation

Run the checks relevant to the change. For most code changes:

```bash
uv run ruff format --check .
uv run ruff check .
uv run --with pyright pyright .
uv run python -m compileall -q tcbot
uv run python -c "import tcbot"
git diff --check
```

For documentation-only changes, at minimum read the changed files, check
relative links and stale paths, and run:

```bash
git diff --check
```

If a validation command cannot run, include the exact command and error in the
pull request description.

> [!TIP]
> CI runs these same checks and fails the pull request on violations. Run them locally before pushing.

## Pull Requests

Keep pull requests focused and explain:

- What changed and why.
- Which user, operator, or contributor behavior is affected.
- Validation commands that were run and their results.
- Any configuration, database, migration, or deployment impact.
- Screenshots or log excerpts when they clarify a user-visible change.

Use a scoped Conventional Commit for the branch history when possible, such as
`feat(moderation): add ...`, `fix(cache): correct ...`, or
`docs(contributing): explain ...`. Keep commits focused and include a useful
body for non-trivial changes.

## Security

Do not report security-sensitive details in a public issue. Do not commit or
paste secrets, credentials, private chat IDs, production-only URLs, or raw
private user data. Describe the impact and reproduction safely, then contact a
maintainer privately.

## Review Checklist

- [ ] The change is limited to the intended scope.
- [ ] Existing behavior and backward compatibility are preserved unless the
      change intentionally modifies them.
- [ ] Documentation and `CHANGELOG.md` are updated where needed.
- [ ] No secrets or private deployment data are included.
- [ ] Relevant validation commands pass.
- [ ] The pull request explains configuration, database, or deployment impact.
