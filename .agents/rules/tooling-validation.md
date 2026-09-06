# Tooling and Validation Rules

This file defines the project workflow, dependency, documentation-maintenance,
and validation requirements for TCF Bot. Code conventions live in
[`code-style.md`](code-style.md), and comment and Markdown conventions live in
[`comment-style.md`](comment-style.md). Documentation maintenance workflow
lives in [`docs-rules.md`](docs-rules.md).

---

## Read Before Work and Update After Work

These rules apply to every task:

1. Before changing the repository, read this file, [`code-style.md`](code-style.md),
   [`comment-style.md`](comment-style.md), [`docs-rules.md`](docs-rules.md),
   [`AGENTS.md`](../../AGENTS.md), and [`CHANGELOG.md`](../../CHANGELOG.md).
2. Read the rules file matching the scope ([`security-rules.md`](security-rules.md)
   for authorization and secrets, [`asyncio-gather-rules.md`](asyncio-gather-rules.md)
   for async and fan-out), the relevant skill in [`.agents/skills/`](../skills/),
   source files, configuration, and documentation for the requested scope.
3. After a change, add an entry under `[Unreleased]` in
   [`CHANGELOG.md`](../../CHANGELOG.md).
4. Update related documentation, repository maps, skills, and guidance whose
   content or paths became stale.
5. Search for old paths and broken links before finalizing.

The six files in this directory are the canonical engineering rules. Public
contributor guidance belongs in [`CONTRIBUTING.md`](../../CONTRIBUTING.md);
deployment and feature documentation belongs under `docs/`.

## Read Fully Before Editing

This project is complex and modular: handlers, workflows, helpers, database
helpers, caches, and docs reference each other across dozens of files. A
partial read causes failed edits and wrong assumptions. Therefore:

- Always read the full file with the Read tool before editing it. Never
  read with line limits or offsets and never guess the surrounding content.
- Never use `sed`, `cat`, `head`, `tail`, `rg`, or `awk` to read file content;
  use the Read tool. Shell search finds locations; only a full Read gives
  the content to edit against.
- Read every connected file, not just the target: importers and callers of
  every changed function, the modules it imports, and the shared helpers
  it uses.
- Read every affected file: handler registrations, callback patterns,
  documentation, and examples that describe the changed behavior.
- Re-read after the formatter runs: formatting reflows text, so text
  captured before a format pass may no longer match.
- Verify every edit after applying it: re-read the edited region or check
  `git diff` before moving on. A tool reporting success is not proof the
  result is correct.

## Skills and Sub-Agents

Skills in `.agents/skills/` apply automatically when their trigger matches.
Use the relevant skill before editing code, documentation, database helpers,
workflows, diagrams, or other specialized areas. Compose skills when a task
spans more than one area.

Remaining skill files use YAML frontmatter with `name` matching the skill
directory and an actionable `description` explaining when to use it. Keep
skill content project-specific and current.

## Autonomous Improvement Loop

For each requested improvement, update, fix, or audit:

1. Scope one concern and identify its affected files and validation surfaces.
2. Inspect current code, existing helpers, repository status, and duplicate or
   dead paths before editing.
3. Verify version-sensitive library behavior with `npx ctx7@latest`: resolve
   the library first, query one concept at a time, and never send credentials
   or private project data.
4. Design and implement the smallest modular change using the owning helper or
   domain module as the single source of truth.
5. Run targeted checks, then the relevant full validation and runtime logs.
6. Review requirements, docs, stale paths, dead code, and duplicate logic.
7. Repeat only for a concrete remaining defect; stop after bounded attempts and
   report blockers precisely.

Performance claims require measurements. Prefer bounded concurrency and explicit
failure behavior.

## Todo and Plan Discipline

Work is tracked with a todo list, never with memory or good intentions.
Diligence here is mandatory, not optional:

- Create a todo list for any task with 3 or more steps; update it in real
  time as work proceeds. Never leave the list stale and never ignore it.
- Exactly one item is `in_progress` at any moment.
- Mark an item `completed` the moment its work is actually done, including
  its verification: diligently, every time, without being reminded.
  Never mark complete on intent, and never batch several completions into
  one update.
- After completing an item, immediately continue to the next pending item.
  Do not stall, do not end the turn with work left unstarted.
- New instructions arriving mid-task become new todos first, then work.
- If blocked or partial, keep the item `in_progress` and add a follow-up
  todo describing the blocker.
- Do not start the next item while the current one is unverified.
- Never silently drop an item: cancel explicitly with a stated reason
  instead of abandoning it.

## Pre-Edit Checklist

Before editing TCF Bot code, verify:

- The change belongs in the selected file and not in an existing helper,
  workflow, or domain module.
- Handlers stay in `tcbot/modules/`, workflows stay in `*_flow.py`, and
  database access stays in `tcbot/database/`.
- Messages are HTML-only and user content is escaped.
- Role checks use canonical helpers and destructive actions preserve
  auto-demotion behavior.
- Multi-group actions use `fan_out()`.
- Datetimes use project datetime helpers.
- No secrets, credentials, or private IDs are introduced.
- The validation plan fits the scope of the change.

## Dependency and Tooling Policy

- Target Python 3.14.
- Use `uv` for dependency installation, locking, and tool execution.
- Keep `pyproject.toml` and `uv.lock` synchronized.
- Do not add dependencies to `requirements.txt`.
- Do not change pinned dependencies blindly, especially the accepted
  APScheduler `3.11.3` integration risk.

Install dependencies from the lockfile (Replit only):

```bash
uv sync --frozen
```

## Ruff and Validation

Format source files:

```bash
uv run ruff format .
```

Apply safe lint fixes:

```bash
uv run ruff check --fix .
```

Check without modifying files:

```bash
uv run ruff format --check .
uv run ruff check .
```

The `uv run` prefix is primary so no venv activation is needed.

Common diagnostics:

- `F401`: unused import. Remove it unless it is part of a documented public
  API.
- `F841`: unused local variable. Remove it or use the value meaningfully.
- `I001`: imports are unsorted. Let `ruff check --fix` correct it.
- `E4`, `E7`, `E9`: syntax, indentation, or parse problems. Fix manually.

Safe to auto-fix: import sorting, unused imports, formatting, and simple
unused-variable cleanup. Review manually before deleting unused functions or
modules, changing database fields, handler registration, exception handling,
or moderation behavior.

Recommended minimum validation by change type:

| Change type | Minimum validation |
|---|---|
| Documentation-only | Read changed docs, scan links and stale paths, then run `git diff --check`. |
| Formatter or comment-only code change | `uv run ruff format --check .` and `uv run ruff check .` |
| Command handler change | Ruff checks, then start the bot and inspect startup logs. |
| Database helper change | Ruff checks and an import check of the changed module. |
| Workflow change | Ruff checks and an import check of the changed flow. |
| Dependency or configuration change | `uv sync --frozen` (Replit), Ruff checks, and an import check. |
| Runtime change | Ruff checks, compileall, import check, and a clean application startup. |

For the full runtime check:

```bash
uv run ruff format --check .
uv run ruff check .
uv run --with pyright pyright .
uv run python -m compileall -q tcbot
uv run python -c "import tcbot"
git diff --check
```

Do not claim a validation command passed unless it actually ran and exited
successfully. If a check cannot run, report the exact command and error.

## Security and Scope

Authorization boundaries, secret handling, and compatibility guarantees live
in [`security-rules.md`](security-rules.md). For this workflow:

- Do not edit unrelated files or refactor outside the requested scope.

## Final Review

Before declaring work complete:

- Review the complete diff, including renames and deletions.
- Confirm all changed documentation links resolve.
- Confirm no ignored files or secrets are staged.
- Run the relevant validation commands.
- Leave the working tree clean after committing, when a commit was requested.