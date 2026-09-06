# Documentation Style and Maintenance Rules

This file defines documentation scope, style standards, project facts to keep
current, and the maintenance workflow for TCF Bot docs. Code conventions live
in [`code-style.md`](code-style.md), and comment and Markdown conventions live
in [`comment-style.md`](comment-style.md). Validation commands live in
[`tooling-validation.md`](tooling-validation.md).

---

## Read Before Work and Update After Work

Before editing documentation, read this file,
[`tooling-validation.md`](tooling-validation.md),
[`code-style.md`](code-style.md), [`comment-style.md`](comment-style.md),
[`AGENTS.md`](../../AGENTS.md), and [`CHANGELOG.md`](../../CHANGELOG.md).

When you change *any* documentation file, update the related markdown in the
same turn:

- [`CHANGELOG.md`](../../CHANGELOG.md): entry under `[Unreleased]`
  describing the doc change.
- [`docs/README.md`](../../docs/README.md): if a new doc was added or the
  documentation structure changed, update the category index.
- [`docs/architecture/repository-map.md`](../../docs/architecture/repository-map.md):
  if the repository tree changed.
- Any related docs whose content is now stale or whose cross-references would
  break.

## Scope

Documentation normally lives in:

- root docs: `README.md`, `AGENTS.md`, `replit.md`
- agent/contributor rules: `.agents/rules/*.md`
- developer docs: `docs/**/*.md`, grouped by category under `docs/`

Rules:

- Do not edit `config.env` while doing documentation maintenance unless
  explicitly asked.
- Keep public contributor and operator guidance separate from agent-only
  rules.

## Documentation Standards

Write in clear English with a professional, friendly tone. Be practical and
direct, not overly formal.

Good documentation should:

- describe current behavior, not planned behavior unless clearly marked,
- link to related docs using relative paths,
- avoid real tokens, MongoDB URIs, private credentials, or sensitive chat
  IDs,
- use code blocks for commands, env examples, and file-layout examples,
- keep user-facing setup steps separate from agent-only rules,
- mention validation commands when they matter,
- avoid stale references to files that do not exist.

## Project Facts To Keep Current

As of 2026-06-02, TCF Bot uses:

- Python 3.14 project target
- `python-telegram-bot` (with the `[rate-limiter]` extra, no `[job-queue]`
  extra), tracking the latest compatible release
- Motor/MongoDB
- Flask keep-alive server
- `uv` and `uv.lock`
- Ruff

Recent project additions to keep accurate when editing docs:

- Smart mention system in `tcbot/modules/helper/formatter.py`
  (`mention(user_id, name, username=None)`) with global `t.me/username`
  link fallback to plain text + ID.
- Batch query helpers in `tcbot/database/users_cache.py`
  (`get_user_mention_data`, `get_mention_data_batch`,
  `get_first_names_batch`).
- Partial-name search in `tcbot.modules.helper.extraction.extract_target`;
  resolution order is reply → args (full ID/username) → args (partial DB
  search) → text mention → @mention.
- Username field on `Identity` and `member_cache` indexes on `username` and
  `first_name`.
- CI/CD workflows: `.github/workflows/auto-fix.yml` (auto-PR for Ruff fixes
  on the fixed `auto-fix/ruff` branch),
  `.github/workflows/dependency-update.yml` (weekly auto-PR like dependabot),
  `.github/workflows/run-bot.yml` (self-chaining 24/7 runner with
  webhook-first transport), `.github/workflows/codeql.yml` (security
  scanning). All workflows are documented in
  [`docs/operations/ci-cd.md`](../../docs/operations/ci-cd.md).

Core commands:

```bash
uv run ruff check .
uv run ruff format .
uv run python -m tcbot
```

## Update Workflow

1. Inventory the requested Markdown files.
2. Check current repository structure before documenting paths.
3. Inspect relevant code or config templates only as needed.
4. Update docs with minimal but complete edits.
5. Update indexes when adding a new documentation file.
6. Search for stale links or old filenames.
7. Run lightweight validation.

Suggested validation:

```bash
git --no-pager diff --check
```

For docs-only changes, runtime validation is usually not required unless
documentation examples depend on generated code.

## Detailed Guides

When updating a detailed feature guide under `docs/features/`, include:

- purpose and ownership,
- commands/callbacks involved,
- user flow and staff flow,
- database impact,
- logging behavior,
- edge cases,
- validation hints.

For current detailed feature docs, keep these topics accurate:

- appeal submissions happen in bot DM through deep links,
- `#appeal` matching is case-insensitive,
- ban proof is required,
- warnings are per-group,
- warn-limit auto-ban clears warnings only after successful ban,
- role checks use canonical role helpers.
