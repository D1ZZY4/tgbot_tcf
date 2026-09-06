# Comment and Documentation Style Rules

This file defines comments, docstrings, section dividers, and Markdown
conventions for TCF Bot. Code conventions live in
[`code-style.md`](code-style.md), and tooling and validation requirements live
in [`tooling-validation.md`](tooling-validation.md).

---

## Goals

Comments should explain intent, constraints, risks, or non-obvious behavior.
They should not restate the code. Prefer clear names and small functions over
large explanatory comments.

## Better Comments Prefixes

Use these prefixes in inline `#` comments and, when useful, inside docstrings:

| Prefix | Use for |
|---|---|
| `# ! WARNING:` | Dangerous behavior or security-sensitive constraints |
| `# ! CRITICAL:` | Must-not-ignore correctness or safety rules |
| `# ?` | Questions, uncertainty, or follow-up to verify |
| `# TODO:` | Deferred work with enough context to act |
| `# *` | Important notes, highlights, or design intent |
| `# //` | Dead/commented-out code marker; remove instead of keeping |

Examples:

```python
# ! CRITICAL: This check prevents non-staff users from approving appeals.
# ? Should this use fan_out() after group count grows further?
# TODO: Add a migration once old ban documents no longer omit proof_link.
# * Telegram albums arrive as separate updates; debounce before final upload.
```

Rules:

- Use one space after `#`: `# !`, `# ?`, `# TODO:`, or `# *`.
- `# !` must include `WARNING:` or `CRITICAL:`.
- TODOs must include enough context for a future maintainer.
- Do not use `# //` to temporarily disable code. Delete dead code.
- Do not use double-hash comments such as `## note` in Python modules.

## Docstrings

Module docstrings are required and single-line:

```python
"""Federation ban commands and handler registration."""
```

Use function docstrings when the purpose, constraints, or return shape is not
obvious from the name and signature. Use multi-line docstrings only when the
extra detail adds real value.

Rules:

- Do not use Sphinx `:param:` or `:returns:` tags.
- Do not use Markdown tables inside Python docstrings.
- Do not add docstrings to trivial one-line helpers whose names are clear.
- Keep docstrings accurate when behavior changes.

## Section Dividers

Use section divider comments for top-level organization in medium and large
modules. Prefer the canonical divider forms:

```python
# ────────────────────────────────── H1 ───────────────────────────────── #
# ────────────────────────── H2 ────────────────────────── #
# ~~~~~~~~~~~~~~~~~~~ H3 ~~~~~~~~~~~~~~~~~~~~ #
# ~~~~~~~~~~~ H4 ~~~~~~~~~~~ #
```

Use H1 for module-level sections, H2 for major blocks, H3 for sub-groups, and
H4 only for rare minor groupings. Do not add dividers to very small modules.

Common section names include `Handlers`, `Commands`, `Retrieval`, `Mutations`,
`Role CRUD`, `Role Resolution`, `Collection Helpers`, and `Utilities`.

## Inline Comments and Constants

- Keep comments short and close to the code they explain.
- Explain why, not what an obvious assignment does.
- Update or remove comments when code changes.
- Prefer a helper with a clear name over a long inline comment.
- Add a `# *` comment above a non-obvious module constant.
- Do not comment obvious constants.
- Use handler labels only when they improve readability.

## Em Dashes

The em dash character (U+2014, `—`) is forbidden in every tracked file:
Python code, comments, docstrings, string literals (including bot replies,
audit logs, and error messages), Markdown, YAML, INI snippets, and skill
docs. Use a hyphen (`-`), comma, colon, or restructure the sentence instead.

The canonical section dividers in this file use the box-drawing character
U+2500 (`─`), which is a different character and stays allowed.

Scan before committing; the result must be empty:

```bash
rg -n "—" .
```

## Markdown Documentation

For files under `.agents/` and `docs/`:

- Write in English only.
- Prefer concise headings, paragraphs, and bullets.
- Use fenced code blocks with language tags when helpful.
- Use project-relative paths in backticks and validate relative links.
- Do not include real credentials, private chat IDs, or production-only links.
- Keep public docs separate from agent-only rules.
- Describe current behavior and clearly mark anything aspirational.
- Update related documentation when paths, behavior, or repository structure
  changes.

Do not:

- Comment out dead code.
- Add vague TODOs such as `# TODO: fix later`.
- Use Sphinx-style docstring tags.
- Explain obvious code.
- Hand-type malformed section dividers.
- Add comments that contradict the category rules in this directory.