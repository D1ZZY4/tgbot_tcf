---
name: mongodb-query-optimizer
description: Optimize MongoDB query and index performance for the TCF Bot project. Use only when the user asks about slow MongoDB queries, indexes, query plans, Motor async database helper performance, mongos.ensure_indexes(), Atlas Performance Advisor, or database access patterns that affect performance.
---

# MongoDB Query Optimizer for TCF Bot

Before invoking this skill, confirm the read/update rules in
[`tooling-validation.md`](../../rules/tooling-validation.md#read-before-work-and-update-after-work),
[`code-style.md`](../../rules/code-style.md), and
[`comment-style.md`](../../rules/comment-style.md). After any change in
`tcbot/database/`, update [`CHANGELOG.md`](../../../CHANGELOG.md) and
[`docs/architecture/database.md`](../../../docs/architecture/database.md) in
the same turn.

Use this skill only for MongoDB performance, query-plan, or indexing work. Keep guidance aligned with TCF Bot's async Motor database layer and project architecture.

Last refreshed for this project: 2026-05-29.

## When to Use This Skill

Invoke this skill when the user asks about:

- Slow MongoDB queries or collections.
- Index design, index review, index bloat, or query coverage.
- `explain()` output, `COLLSCAN`, in-memory sort, high `docsExamined`, or poor selectivity.
- Motor async helper performance in `tcbot/database/*_db.py`.
- Updating or reviewing indexes in `tcbot/database/mongos.py` `ensure_indexes()`.
- Atlas Performance Advisor or slow query log analysis.

Do **not** invoke this skill for routine MongoDB query writing unless the user asks for performance or indexing help.

## Project Rules

TCF Bot stores all MongoDB access behind domain helper modules:

- `tcbot/database/mongos.py` owns the `AsyncIOMotorClient`, database accessor, collection accessor, connection pool settings, and `ensure_indexes()`.
- `tcbot/database/*_db.py` files contain async helper functions for each collection/domain.
- `tcbot/modules/` and workflow modules should call database helpers instead of calling `mongos.col()` or Motor collections directly.
- Index changes should be made in `tcbot/database/mongos.py` inside `ensure_indexes()` so startup creates required indexes consistently.
- Keep index creation idempotent. `ensure_indexes()` is called during application post-init after `connect()` and before handlers run.
- Do not add direct collection calls to command modules as an optimization shortcut.

Current critical indexes in `ensure_indexes()` (verify against `tcbot/database/mongos.py` before recommending):

- `bans`: `(banned_user_id, is_active, timestamp desc, ban_id desc)`, unique `ban_id`, sparse `(banned_user_id, appeal_log_msg_id)`, `(is_active, timestamp desc, ban_id desc)`, `(banned_user_id, timestamp desc, ban_id desc)`.
- `tc_owners`: unique `user_id`.
- `tc_admins`: unique `user_id`.
- `tc_roles`: unique `user_id`, plus `role` for staff roster lookups.
- `federated_groups`: `(chat_id, is_active)`, unique `(chat_id)`, plus `(is_active)`.
- `pending_joins`: unique `chat_id`.
- `member_cache`: unique `user_id`, `(user_id, first_name, username)` for batch projections, `username`, `first_name`, and a TTL on `last_updated`.
- `warns`: `(user_id, chat_id, timestamp desc)`, `(user_id, timestamp desc)`, `(user_id, chat_id, timestamp asc)`, `(timestamp)`.
- `warn_counts`: unique `(user_id, chat_id)`, `(updated_at)`, `(user_id, count, updated_at desc)`.
- `kicks`: `(user_id, timestamp desc)` plus `(chat_id)`.
- `mutes`: `(user_id, timestamp desc)` plus `(chat_id)`.
- `active_mutes`: unique `(user_id)`, `(until_date)`, `(user_id, until_date)`.
- `promotion_requests`: unique `(request_id)`, `(target_id, status)`, `(status, requested_date)`, and partial-unique `(target_id)` where `status` is pending.

Verify these before recommending duplicates.

## Optimization Workflow

### 1. Identify the Query Shape

Start from the exact helper or query path when possible:

- Collection name.
- Filter fields and equality/range predicates.
- Sort order.
- Projection fields.
- Limit or pagination behavior.
- Update predicate for write operations.
- Expected cardinality and hot path frequency.

Prefer inspecting `tcbot/database/*_db.py` helper code before suggesting changes. If the user only provides a query snippet, ask for or infer the collection and call site carefully.

### 2. Review Existing Indexes

Check `tcbot/database/mongos.py` `ensure_indexes()` first. If a live database tool is available, also inspect actual collection indexes because deployed indexes may differ from the repository.

Look for:

- Existing compound indexes that already satisfy the filter and sort.
- Duplicate or prefix-redundant indexes.
- Unique indexes used for data integrity.
- Missing indexes for frequent active-record lookups.
- Sorts that are not supported by index order.

### 3. Use Evidence When Available

If MongoDB MCP or another safe database inspection tool is available, prefer evidence over guessing:

- `collection-indexes` to list actual indexes.
- `explain` with `queryPlanner` and, when safe, `executionStats`.
- `find` with a small `limit` only when a sample document is needed.
- Atlas Performance Advisor for slow query logs, suggested indexes, schema suggestions, and drop-index suggestions.

Never expose secrets or connection strings. Do not run destructive operations.

### 4. Diagnose the Bottleneck

Common causes in this project:

- A helper filters by one field but sorts by another without a compound index.
- Active-record queries filter by `is_active` plus a user or chat field but use only a partial prefix.
- Review or queue lookups filter by status without matching a request or target field.
- Warning history queries need `user_id`, `chat_id`, and descending `timestamp` together.
- Count/update helpers use predicates that are not backed by unique or selective indexes.
- A suggested index duplicates an existing unique or compound index.

For reads, compare `nReturned`, `totalDocsExamined`, `totalKeysExamined`, stage names, and whether a blocking sort appears. For writes, index the update predicate, not the updated fields unless they are also queried.

### 5. Recommend or Implement the Minimal Fix

Prefer one focused fix:

- Adjust an existing helper query only when the query shape is the real problem.
- Add or refine an index in `mongos.ensure_indexes()` when the query shape is valid but unsupported.
- Prefer compound indexes following ESR: equality fields first, then sort fields, then range fields when applicable.
- Preserve unique constraints and data integrity indexes.
- Avoid adding many speculative indexes. Each index slows writes and consumes memory/storage.
- Suggest dropping indexes only with strong evidence, such as Atlas drop-index suggestions or verified redundancy.

When editing code, keep changes scoped to `tcbot/database/mongos.py` and related `tcbot/database/*_db.py` helpers unless the user explicitly asks for broader refactoring.

## TCF Bot Index Design Guidelines

Use these rules when reviewing or proposing indexes:

- Index helper predicates used by hot command paths and scheduled maintenance paths.
- Match compound index field order to the helper's filter and sort.
- Include `is_active` in indexes only when active/inactive filtering is a common predicate with another selective field.
- Keep unique indexes for identity fields such as `user_id`, `chat_id`, `ban_id`, and request IDs when uniqueness is required.
- For descending history views, include timestamp as `-1` after equality predicates.
- Do not index low-cardinality fields alone, such as `status` or `is_active`, unless combined with selective fields.
- Avoid direct module-level database calls to bypass helper overhead; the architecture cost is negligible compared with MongoDB I/O and keeps behavior predictable.
- Consider cache invalidation and role-cache behavior when optimizing role, owner, admin, or group helpers.

## Motor Async Patterns

Keep recommendations compatible with Motor async code:

- Always `await` Motor calls such as `find_one`, `insert_one`, `update_one`, `delete_one`, `count_documents`, and `create_index`.
- Use `.to_list(None)` or a bounded limit intentionally; avoid unbounded reads on large collections unless the helper already represents an admin-only complete export.
- Prefer projections for existence checks and lightweight reads, for example `{"_id": 1}` or `{"_id": 0, "user_id": 1}`.
- Keep startup index creation parallel in `asyncio.gather()` when adding independent `create_index()` calls.
- Keep helper APIs async and domain-focused.

## MCP and Atlas Guidance

If MongoDB MCP tools are available, use them when the user asks for live performance analysis.

Useful database tools:

| Tool | Use |
| --- | --- |
| `collection-indexes` | Inspect actual collection indexes. |
| `explain` | Check query plan, index use, blocking sort, and execution stats. |
| `find` | Fetch a tiny sample when schema shape is unclear. |

Useful Atlas tools:

| Tool | Use |
| --- | --- |
| `atlas-list-projects` | Find the Atlas project ID when needed. |
| `atlas-get-performance-advisor` | Fetch slow query logs, suggested indexes, drop-index suggestions, and schema suggestions. |

If Atlas or MCP is unavailable, explain that recommendations are based on repository query shape rather than live workload metrics.

Do not create indexes directly through live tooling unless the user explicitly approves. For this project, prefer committing index definitions in `mongos.ensure_indexes()`.

## Reference Files

Load these project-local references when useful:

Always useful for index work:

- `references/core-indexing-principles.md`
- `references/antipattern-examples.md`

Conditional references:

- `references/aggregation-optimization.md` when diagnosing aggregation pipelines.
- `references/update-query-examples.md` when diagnosing `update_one`, `find_one_and_update`, replacement, or write-heavy paths.

## Response Style

When returning recommendations:

- Be concise and evidence-based.
- Name the collection, helper/query shape, and proposed index.
- Explain why the index order matches the query.
- Mention tradeoffs such as write overhead or redundant indexes.
- Distinguish verified findings from assumptions.
- If code was changed, summarize the exact files changed and validation run.

Example recommendation:

> For `bans.find_one({"banned_user_id": user_id, "is_active": True}, sort=[("timestamp", -1), ("ban_id", -1)])`, the existing `banned_user_id + is_active` index supports filtering but not the sort. If this lookup is hot and users can have many ban records, consider `[("banned_user_id", 1), ("is_active", 1), ("timestamp", -1), ("ban_id", -1)]`. This improves newest-active-ban lookup at the cost of one wider write-maintained index.
