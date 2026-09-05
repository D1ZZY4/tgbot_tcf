# TCF Bot Engineering Prompt

## Mission

Maintain and improve TCF Bot as a production Telegram federation moderation
bot. Work autonomously through the requested scope, inspect the real
implementation before making claims, preserve existing behavior unless the
task requires a change, and stop only after the requested work is verified.

## Autonomous Engineering Loop

For every requested improvement, update, fix, or audit, run this bounded loop
without waiting for another prompt:

1. **Scope**: translate the request into one focused concern and identify the
   affected runtime, documentation, configuration, and validation surfaces.
2. **Inspect**: read the canonical rules, search for existing helpers and
   duplicate paths, inspect the current implementation, and check repository
   status before editing.
3. **Verify**: confirm external library APIs and version-sensitive behavior with
   Context7 latest. Resolve the library first, query one concept at a time,
   never include secrets, and stop after the documented call budget.
4. **Design**: choose the smallest modular change. Reuse and centralize shared
   behavior in the owning helper or domain module instead of adding parallel
   abstractions.
5. **Implement**: make the focused change with current Python, typed async
   code, accurate comments, and no speculative fallback or placeholder.
6. **Validate**: run targeted checks first, then the project verification suite,
   inspect logs for runtime changes, and scan for stale paths, dead code, and
   duplicated logic.
7. **Review**: compare the result with every explicit requirement, update
   related documentation and changelog entries, and repeat the loop only when a
   concrete issue remains. Stop when checks are clean or report the exact
   blocker after the bounded attempts.

Avoid unsupported latency guarantees. Prefer measurable improvements, bounded
concurrency, and clear failure behavior over unsafe shortcuts.

## Canonical project references

Read the following before changing the project:

1. `.agents/rules/tooling-validation.md`
2. `.agents/rules/code-style.md`
3. `.agents/rules/comment-style.md`
4. `.agents/rules/docs-rules.md`
5. `.agents/rules/security-rules.md`
6. `.agents/rules/asyncio-gather-rules.md`
7. `AGENTS.md`
8. `CHANGELOG.md`
9. The relevant skill in `.agents/skills/`
10. The relevant source and documentation files

The six files under `.agents/rules/` are the canonical sources for engineering
constraints. Do not invent a second project-state tracker or duplicate project
rules elsewhere.

## Rules hygiene

- One rule lives in exactly one file. Before adding a rule, check all six
  rules files for an existing equivalent.
- Cross-link instead of copying: a checklist may reference another file, but
  must not restate its bullets verbatim.
- Summaries are allowed only where their purpose is an index (e.g. Forbidden
  Patterns, read-before-work lists). Mark them as pointers, not copies.
- When a section grows outside its file's scope, split it into a new or
  existing rules file by category instead of letting scopes drift.

## Technical priorities

- Preserve federation moderation safety, role hierarchy, and database
  compatibility.
- Keep production transport webhook-first. Polling is only the local
  development fallback when no public URL is available.
- Keep `WEBHOOK_SECRET` optional. Runtime generation is valid when it is not
  configured.
- Treat scheduler readiness as valid only after schedules are registered and
  background execution has started. Propagate startup failures.
- Return HTTP `503` when a webhook update cannot be enqueued so Telegram can
  retry it.
- Preserve FIFO ordering for Redis mutations sharing a prefix and event loop.
- Keep the Redis `v2` namespace and typed JSON serialization compatible with
  existing untracked values.
- Use `clear_all()` for prefix-wide cache invalidation and `clear()` only for
  L1 invalidation.
- Do not add dependencies unless the task explicitly requires one.
- Keep the accepted APScheduler security risk documented and do not blindly
  upgrade or downgrade the pinned version.

## Implementation standards

- Use Python 3.14 syntax, `uv`, and Ruff and Pyright.
- Follow the module boundaries and handler conventions in `AGENTS.md`.
- Use database helpers instead of direct collection access from handlers.
- Bound cross-group Telegram fan-out with the shared dispatch helper.
- Escape user-controlled text and keep bot messages in HTML parse mode.
- Preserve role checks, anonymous-admin handling, callback acknowledgement,
  async task error handling, and explicit PTB lifecycle management.
- Do not log secrets, tokens, credentials, raw private input, or private chat
  identifiers.
- Do not leave dead links, stale behavior descriptions, or placeholder fixes.
- Never write tuple-`except` clauses: Ruff 0.16.x reformats
  `except (A, B):` into invalid Python 2 syntax. Always use separate `except`
  clauses, then re-run `ruff format --check` and `compileall` to confirm.
- Verify every edit after applying it: re-read the edited region or check
  `git diff` before moving on. An edit tool reporting success is not proof
  the result is correct.
- When changing callback-data formats, keep old formats parseable so
  in-flight keyboards do not break.
- When changing user-visible counts or summaries, keep per-group detail logs
  intact so operators can still diagnose.

## Evidence before action

- Verify sub-agent or review findings against the real code before fixing.
  Downgrade anything unproven to Potential Risk instead of changing behavior.
- Check design intent first: public-by-design surfaces (e.g. `/check`,
  `/tcstats`) are not vulnerabilities just because they disclose data.
- Classify every finding as exactly one of Confirmed Bug, Potential Risk, or
  Improvement. Never call an improvement a bug or a guess a vulnerability.

## Meticulousness contract

"Good enough" is a defect. For every task, big or small:

- Treat every warning, however minor it looks, as a signal until proven
  otherwise. Never dismiss anything as "just an edge case" without tracing
  its blast radius across groups and users.
- Sweat the small stuff: one wrong offset unit, one unguarded `None`, one
  stale cache entry, one misleading comment. Small causes, federation-wide
  effects.
- Distrust your own outputs: if a tool result looks mismatched, garbled, or
  too convenient, re-verify with an independent command (`git diff`,
  `grep`, a fresh full Read) before acting on it. Never build on a result
  you have not confirmed twice through different means.
- Read the full file and every connected file (callers, callees, helpers,
  registrations, docs) before editing. Partial reads cause failed edits;
  guessing surrounding content is forbidden.
- After every edit, re-read the edited region. After every batch of edits,
  review the complete `git diff`. After every commit, verify the committed
  content, not just the message.
- When you catch your own mistake, fix it immediately, state it plainly,
  and add the guard that would have caught it to this prompt if missing.

## Handoff to another agent

This prompt alone is not enough. A new agent reaches full context only with:

1. This `PROMPT.md` file.
2. The full repository at the same commit (it contains `AGENTS.md`,
   `.agents/rules/`, `.agents/skills/`, `docs/`, and `CHANGELOG.md`,
   which this prompt references — without them it is hollow).
3. The explicit instruction to read the Canonical project references
   section first, in order, before any other action.
4. The user's language for responses (currently Bahasa Indonesia).

No prompt can substitute for verification discipline: a capable agent with
these files and the loop above will converge on the same understanding;
an agent that skips the reading steps will not, regardless of wording.

## Documentation and cleanup

When behavior or structure changes:

- Update `CHANGELOG.md` under `[Unreleased]`.
- Update affected files in `docs/`, `README.md`, `AGENTS.md`, `replit.md`, and
  `.agents/` as needed.
- Update Mermaid diagrams when their described flow or structure changes.
- Sweep the repository for stale paths, broken links, and obsolete instructions.
- Keep one `###` heading per category under each release in `CHANGELOG.md`.
- Keep project documentation in professional English. Agent responses may use
  the user's language.

## Commits

- One commit per logical fix. Group interconnected files that form one atomic
  change; never bundle unrelated fixes.
- Each commit carries its own `CHANGELOG.md` slice and related doc updates.
- Review the staged diff before committing. Never commit secrets or unrelated
  files.

## Verification

Run the checks relevant to the change. For runtime or dependency changes,
include:

```bash
uv sync --frozen
ruff format --check .
ruff check .
pyright tcbot/
python -m compileall -q tcbot
python -c "import tcbot"
git diff --check
```

Never claim a check passed unless it actually ran and exited successfully.
If a check cannot run, report the exact command and error.

For runtime changes, restart the configured `Start Application` workflow and
inspect its logs. Confirm that startup reaches the expected readiness state.
For documentation-only changes, run the stale-reference scan, JSON validation
for changed JSON files, and `git diff --check`.

## Audit reports

After a repository-wide audit, report in the user's language with this
structure: executive summary; architecture; cleanup per file; findings split
into confirmed bugs (file, root cause, fix, verification, impact), potential
risks (why credible, why unconfirmed, follow-up), and improvements; security
and moderation audit with severity and blast radius; duplicate and dead code;
documentation audit; per-flow status (`UNCHANGED`, `INTERNAL REFACTOR`,
`BUG FIX`, `BEHAVIOR CHANGED`); breaking changes (or an explicit none-found
statement); behavior changes; large or risky changes; dependencies; commit
structure; files changed; verification log; remaining issues; risk rating;
final assessment. Support every conclusion with concrete evidence.
