# Database Layer

The database layer lives in `tcbot/database/` and is the only place that should perform MongoDB reads and writes. Command modules and workflows should call helper functions instead of calling `mongos.col()` directly.

For modules that consume these database helpers, see [`modules.md`](modules.md).
For shared helpers, see [`helpers.md`](helpers.md). For conversation flows, see
[`workflows.md`](workflows.md).

```mermaid
flowchart TD
    Modules[tcbot/modules/*.py] --> DBHelpers[tcbot/database/*_db.py]
    Helpers[tcbot/modules/helper/] --> DBHelpers
    Workflows[tcbot/modules/helper/workflows/] --> DBHelpers
    DBHelpers --> Mongos[mongos.py<br/>connection + col]
    DBHelpers --> Cache[cache.py<br/>TwoLevelCache]
    DBHelpers --> Documents[documents.py<br/>TypedDicts]
    Mongos --> Motor[Motor AsyncIOMotorClient]
    Motor --> MongoDB[(MongoDB)]
    DBHelpers --> Indexes[Index setup<br/>on startup]
    Indexes --> MongoDB
    Cache --> L1[TTLCache L1<br/>in-memory]
    Cache --> L2[Redis L2<br/>optional]
    L2 --> Redis[(Redis)]
    Scheduler[scheduler.py<br/>APScheduler 3.11.3] --> MongoDB
    Scheduler --> DBHelpers
```

## Connection manager

`mongos.py` owns the Motor client lifecycle.

| Export | Purpose |
|---|---|
| `connect()` | Creates the `AsyncIOMotorClient`, selects `cfg.db_name`, and pings MongoDB through the circuit breaker. A successful ping records a CLOSED success on the `mongodb` circuit; repeated failures trip it to OPEN. |
| `ensure_indexes()` | Creates all required indexes on startup. Safe to call repeatedly. |
| `db()` | Returns the active database or raises if `connect()` has not run. |
| `col(name)` | Returns a collection from `db()`. Use only inside database helper modules. |
| `db_call(coro)` | Executes a Motor coroutine through the `mongodb` circuit breaker. Raises `CircuitOpenError` when the circuit is OPEN so callers fast-fail instead of waiting the 45-second socket timeout. All eight DB helper modules (`bans_db`, `groups_db`, `users_roles`, `users_cache`, `warns_db`, `mutes_db`, `kicks_db`, `queues_db`) wrap every Motor operation with `db_call()`. Five consecutive failures open the circuit; a half-open probe re-closes it when MongoDB recovers. |
| `make_short_id(length=10)` | Generates lowercase alphanumeric IDs for records such as bans and promotion requests. |

## Collections and helpers

| Helper | Collection(s) | Main responsibilities |
|---|---|---|
| `users_cache.py` | `member_cache` | Member profile cache operations: upsert, change-detection upsert, get, batch queries, mention formatting, total count, all users list. |
| `users_roles.py` | `tc_owners`, `tc_admins`, `tc_roles` | Owner CRUD, admin CRUD, developer/tester role CRUD, effective-role resolution, can_act_on checks. |
| `bans_db.py` | `bans` | Active ban lookup, ban creation/update, unban deactivation, appeal/review metadata, active ban lists. |
| `warns_db.py` | `warns`, `warn_counts` | Warning history, warning counters, backfill/sync, remove latest warning, clear warnings. |
| `kicks_db.py` | `kicks` | Kick audit records. |
| `mutes_db.py` | `mutes`, `active_mutes` | Mute audit records (`mutes`). Active-mute store (`active_mutes`): one document per muted user, used to re-apply restrictions on join and on group connect. `set_active_mute` / `clear_active_mute` / `get_active_mute` / `active_mute_docs`. |
| `queues_db.py` | `promotion_requests` | Queued Admin promotion requests and resolution status. |
| `cache.py` | in-process + Redis | `TTLCache[T]` (L1 in-process) and `TwoLevelCache[T]` (L1 in-process + L2 Redis). Five public singletons: `effective_role_cache`, `connected_cache`, `active_groups_cache`, `owner_id_cache`, `user_mention_cache`. Key methods: `get`/`put`/`invalidate` (sync, L1 plus FIFO Redis mutation queue shared by each Redis prefix), `get_or_fetch` (async, L1 -> L2 -> DB, primary hot-path), `clear` (sync, L1 only; does **not** touch Redis), `clear_all` (async, L1 + Redis SCAN+UNLINK; use when the invalidation key is unknown). Redis keys use the `v2` namespace and tagged JSON values restore `datetime` and `ObjectId` types on cache hits. |
| `redis_client.py` | Redis (optional) | Async Redis client singleton via `redis.asyncio.ConnectionPool`. `connect(url)` creates the pool and runs `PING`. `client()` returns the active client or `None` when Redis is not configured. `hiredis` C extension is optional and only used when `REDIS_URL` is set. |
| `scheduler.py` | MongoDB (APScheduler) | APScheduler 3.11.3 `AsyncIOScheduler` backed by `MongoDBJobStore`. The scheduler supports persistent one-off unban jobs and the optional recurring warn-expiry job; the current ban command does not create timed-ban schedules. Member-cache cleanup is handled by a MongoDB TTL index, not a scheduler job. Background asyncio task owns the stop event and shutdown sequence (`_sched_stop` + `scheduler.shutdown(wait=False)` with a 10 s join). `start()` reports readiness only after recurring schedules are registered and background execution starts; initialization failures propagate instead of leaving a dead scheduler. `is_ready()` returns `True` when that startup sequence has completed. |
| `documents.py` | type-only | `TypedDict` document shapes and `Literal` aliases. |
| `types.py` | type-only | `NewType` primitives such as `UserId`, `GroupId`, `ChatId`, and `BanId`. |
| `groups_db.py` | `federated_groups`, `pending_joins` | Connected group state, pending connection requests, group cache invalidation. |

## Member cache optimization

The `member_cache` collection stores user profile data. For performance, use the appropriate query function:

| Function | Fields fetched | Use case |
|---|---|---|
| `get_user(user_id)` | All fields | When you need a complete user profile |
| `get_first_name(user_id, fallback)` | `first_name` only | When you only need the display name for one user |
| `get_user_mention_data(user_id)` | `first_name`, `username` | Single-user mention formatting (returns tuple) |
| `get_first_names_batch(user_ids)` | `first_name` only | Display names for many users in one query (returns `dict[int, str]`) |
| `get_mention_data_batch(user_ids)` | `first_name`, `username` | Mention data for many users in one query (returns `dict[int, tuple]`) |
| `search_by_name(needle, limit)` | `user_id`, `first_name`, `username` | Partial name or username search: server-side regex, max `limit` results (default 5). Used by target resolution in `extraction.py` to avoid loading the full user cache. |

For group title lookups across multiple chat IDs, use `groups_db.get_group_titles(chat_ids)` which returns `dict[int, str]` in a single query.

| `upsert_user(user_id, username, first_name, last_name)` | All fields | Unconditional DB write (used on first-seen and forced refresh). `None` means "unknown, preserve stored value"; pass `""` to clear a field. |
| `upsert_user_if_changed(user_id, username, first_name, last_name)` | All fields | Change-detection write: compares the `(first_name, username, last_name)` triple against the L1 entry; skips the MongoDB write on full match. Returns `True` when a write occurred. |
| `harvest_user_identity(user_id, username, first_name, last_name)` | All fields | Snapshot write for data taken from a live Telegram `User` object: `None` means "absent", so missing fields are cleared via `""` and removals propagate. Use on every hot-path update (e.g. per-message member cache harvesting). Never use with partial data from ban/promote/check-by-ID paths (those keep `None`). |
| `sync_user_identity(bot, user_id)` | Triple | Shared resolver in `extraction.py`: cached read, live verify against Telegram, update on mismatch; persists what it finds. Profile views (`/check`, `/tcstats` detail) use it. The older gap-fill-only `resolve_user_identity` was removed once every consumer used `sync`. |
| `has_recent_identity_attempt(user_id)` / `remember_identity(...)` | L1 only | Bounds repeat Telegram lookups by the L1 TTL without touching MongoDB. |

**Performance tip:** Use batch functions whenever you need data for more than one user in a list view or fan-out result. Calling single-user functions inside a loop is an N+1 anti-pattern. Both batch functions use the `(user_id, first_name, username)` index in `member_cache`. For partial-name target resolution, use `search_by_name` instead of loading users into Python. For paginated list views, use the server-side page helpers (`active_bans_page`, `all_users_page`) instead of fetching full collections.

**Hot-path harvest pattern:** On every observed Telegram update, call
`harvest_user_identity` (not `upsert_user`). When the cached identity has not
changed, the helper skips the MongoDB write. The update handlers schedule this
harvest in the background so the main handler is not held up by an unnecessary
write.

## Startup indexes

`ensure_indexes()` creates:

| Collection | Index |
|---|---|
| `bans` | `(banned_user_id, is_active, timestamp desc, ban_id desc)` compound (serves get_active_ban filter+sort), unique `(ban_id)`, `(banned_user_id, appeal_log_msg_id)` sparse, `(is_active, timestamp desc, ban_id desc)` (serves active_bans / active_ban_count), `(banned_user_id, timestamp desc, ban_id desc)` (serves /check ban history) |
| `tc_owners` | unique `(user_id)` |
| `tc_admins` | unique `(user_id)` |
| `tc_roles` | unique `(user_id)`, `(role)` (serves `all_by_role` filter) |
| `federated_groups` | `(chat_id, is_active)`, unique `(chat_id)`, `(is_active)` |
| `pending_joins` | unique `(chat_id)` (one pending request per chat) |
| `member_cache` | unique `(user_id)`, `(user_id, first_name, username)` covered-query index for batch `$in` projections, `(username)`, `(first_name)`, `(last_updated)` TTL auto-expiry |
| `warns` | `(user_id, chat_id, timestamp desc)`, `(user_id, timestamp desc)` (per-user history), `(user_id, chat_id, timestamp asc)` (oldest-first `get_warns` sort), `(timestamp)` (warn expiry sweep) |
| `warn_counts` | unique `(user_id, chat_id)`, `(updated_at)` (counter expiry sweep), `(user_id, count, updated_at desc)` (`user_warn_groups` / `federation_warn_count`) |
| `kicks` | `(user_id, timestamp desc)`, `(chat_id)` |
| `mutes` | `(user_id, timestamp desc)`, `(chat_id)` |
| `active_mutes` | unique `(user_id)`, `(user_id, until_date)`, TTL on `(until_date)` serving the expiry-filtered fetch and pruning expired timed mutes (permanent `None` rows never expire; the retired plain single-field index shared its auto-name and is dropped on startup when found without `expireAfterSeconds`) |
| `promotion_requests` | unique `(request_id)`, `(target_id, status)`, unique `(target_id)` partial on `status == "pending"` (one pending request per user), `(status, requested_date)` (serves `all_pending` filter plus oldest-first sort) |

If a new query depends on a new access pattern, add the matching index in `ensure_indexes()` together with the helper change.

## Role model

Effective roles are resolved in `users_roles.get_effective_role()`:

1. Founder from `tc_owners` returns `"founder"`.
2. Admin from `tc_admins` returns `"admin"`.
3. Custom role from `tc_roles` returns `"developer"` or `"tester"`.
4. No role returns `None`.

Rank ordering:

```text
founder = 4 > admin = 3 > developer = 2 > tester = 1 > none = 0
```

Use `users_roles.role_rank()` and `users_roles.can_act_on()` instead of hand-written comparisons.

Hot auth paths are cache-backed: `is_owner` resolves via the cached owner ID
(300 s TTL, invalidated on transfer) and `is_staff` via the cached effective
role (60 s TTL), so repeated checks cost no MongoDB round trip on hits.
`owner_only` uses the cached owner ID and `staff_only` uses the cached
effective role directly (fail-closed with a retry reply on outage, mirroring
the appeal-review path); `is_staff` keeps its historical coerce-to-`False`
contract for its remaining callers.

## Ban model

`bans` documents are represented by `BanDoc` and may contain:

| Field | Meaning |
|---|---|
| `ban_id` | Short unique ban identifier. |
| `banned_user_id` | Target Telegram user ID. |
| `reason` | Moderation reason. |
| `admin_user_id` | Admin who created or updated the ban. |
| `proof_message_id` | Uploaded proof message ID in the proof destination. |
| `log_message_id` | Audit log message ID. |
| `previous_proof_message_id` / `previous_log_message_id` | Prior records when an active ban is updated. |
| `until_date` / `duration_str` | Reserved for future timed-ban support; both currently `None`. |
| `timestamp` | Initial creation time. |
| `updated_timestamp` | Last update time when applicable. |
| `is_active` | Whether the federation ban is active. |
| `update_count` | Number of updates to the ban. |
| `review_message_id` / `review_timestamp` | Appeal review card metadata. |
| `appeal_log_msg_id` / `appeal_submitted_at` / `appeal_link` | Submitted appeal metadata. |
| `rejected_by_id` / `rejected_by_name` / `rejected_at` | Rejector identity and timestamp (set by `bans_db.set_rejected_by` on appeal rejection). |

Key helper functions:

- `bans_db.get_active_ban(user_id)`: returns the currently active ban for a user, or `None`.
- `bans_db.get_ban(ban_id)`: fetches a single ban record by its short ID.
- `bans_db.create_ban(...)` / `bans_db.update_ban(...)`: write a new ban or update an existing one.
- `bans_db.deactivate_ban(ban_id)`: marks a single ban record inactive by its `ban_id`.
- `bans_db.deactivate_all_active_bans(user_id)`: marks every active ban for a user inactive in one atomic update. Returns the count of deactivated records. Used by unban and appeal-approval flows to clear all active duplicates at once.
- `bans_db.deactivate_extra_active_bans(user_id, keep_ban_id)`: marks all active bans for a user inactive except the one matching `keep_ban_id`. Used by the ban-update path to clean up duplicate active records before writing the canonical update.
- `bans_db.set_review(...)` / `bans_db.set_appeal_log_msg(...)`: store appeal/review metadata on an existing ban.
- `bans_db.set_review_if_absent(...)`: atomic variant used by appeal submission; claims the pending-review slot only when none is stored and returns whether this submit won, so concurrent duplicate submits cannot overwrite each other.
- `bans_db.active_bans()` / `bans_db.active_ban_count()` / `bans_db.active_ban_user_ids()`: federation-wide active ban queries.
- `bans_db.active_bans_page(skip, limit)` / `bans_db.active_bans_for_users(ids)` / `bans_db.user_appealable_bans(user_id)`: server-side paged/filtered variants for list views, name search, and the appeals drill-down.
- `bans_db.user_bans(user_id)` / `bans_db.user_ban_count(user_id)`: per-user ban history (all records, active and inactive).
- `bans_db.user_appeal_count(user_id)`: count of submitted appeals for a user.

## Warning model

Warnings are stored per user and chat:

- `warns` stores each warning event.
- `warn_counts` stores a counter document for fast limit checks with `unique (user_id, chat_id)` index.
- `cfg.warn_limit` (env var `WARN_LIMIT`, default 3, minimum 1) is the per-group
  threshold. A second threshold `FED_WARN_LIMIT` (env var `FED_WARN_LIMIT`,
  default 0 = disabled) triggers
  auto-ban when a user's total warns across all groups reaches or exceeds the
  configured value. See
  [`../features/moderation/warnings.md`](../features/moderation/warnings.md).

Key helper functions:

- `warns_db.add_warn(user_id, reason, admin_id, chat_id)`: records a warning and returns the new warn count.
- `warns_db.warn_count(user_id, chat_id)` / `warns_db.get_warns(user_id, chat_id)`: current count and full list for a user in a group.
- `warns_db.remove_last_warn(user_id, chat_id)` / `warns_db.clear_warns(user_id, chat_id)`: undo latest warning or reset all. `clear_warns` and `clear_all_warns` raise the warns-delete failure instead of returning 0, so a database outage is never misreported as an empty warn state; counter-delete failures stay error-logged but non-fatal.
- `warns_db.user_total_warns(user_id)` / `warns_db.user_warn_groups(user_id)`: federation-wide warn aggregates used by `/check`.
- `warns_db.federation_warn_count(user_id)`: total warn count across all connected groups; used by `warning_flow.execute_warn` to evaluate the `FED_WARN_LIMIT` threshold.
- `warns_db.migrate_records(old_chat_id, new_chat_id)`: repoints `warns` and `warn_counts` documents from a legacy basic-group `chat_id` to its new supergroup `chat_id`; called by `greeting.on_chat_migration` alongside `groups_db.migrate_group` so warning history and thresholds survive a chat migration.

## Kick model

Kicks are append-only audit records:

- `kicks` stores one document per kick event with fields `user_id`, `chat_id`, `reason`, `admin_id`, and `timestamp`.
- `kicks_db.user_kicks(user_id)` returns all kick records for a user, newest first.
- `kicks_db.user_kick_count(user_id)` returns the total count.
- Records are never deleted; the collection is a permanent audit trail.

## Mute model

Mutes use an append-only audit trail (`mutes`) plus a live-state store (`active_mutes`):

- `mutes` stores one document per mute event with fields `user_id`, `chat_id`, `reason`, `admin_id`, and `timestamp`. The optional `duration_secs` field is present for timed mutes (absent or `None` for permanent mutes). Records are never deleted; the collection is a permanent audit trail.
- `active_mutes` stores one document per currently-muted user. `mutes_db.set_active_mute(user_id, ...)` upserts on each `/tcmute`; `mutes_db.clear_active_mute(user_id)` deletes on `/tcunmute`. This powers two re-application paths: (1) `greeting._handle_member` calls `get_active_mute` on every join event and calls `restrict_chat_member` when an active mute is found; (2) `connected_flow.complete_join` calls `active_mute_docs()` and fans out `restrict_chat_member` for every active mute when a new group connects.
- `mutes_db.user_mutes(user_id)` returns all mute records for a user, newest first.
- `mutes_db.user_mute_count(user_id)` returns the total count.

## Group model

`federated_groups` stores active and inactive group records. Disconnecting marks a group inactive instead of deleting it. `pending_joins` stores temporary connection prompts until the owner accepts or cancels.

## Caches

`cache.py` provides two cache types and five public singletons.

### Cache types

`TTLCache[T]`: pure in-process TTL cache. Operations are synchronous and do
not perform network I/O. Suitable for caches that do not need Redis
distribution.

`TwoLevelCache[T]`: wraps `TTLCache[T]` and adds an optional Redis L2 layer.
When Redis is available, `get_or_fetch` checks L1, then L2 (bounded by a
1.0 s timeout so a stalled Redis falls through to the DB fetch), then calls
the DB fetch coroutine and populates both layers. `put` and `invalidate` operate on
L1 synchronously and enqueue FIFO Redis writes/deletes shared by cache objects
using the same prefix. `clear_all` enqueues a prefix-wide `SCAN` and `UNLINK`
after earlier mutations. Redis keys use the `v2` namespace, and tagged JSON
restores `datetime` and `ObjectId` values while untagged legacy JSON remains
readable. When Redis is not configured, the cache uses in-process behavior.

`CACHE_MISS` sentinel: compare with `is CACHE_MISS` to detect a miss. Distinct from `None` because `None` is a valid cached value (for example, a user with no role).

### Public singletons

| Cache | Type | L1 TTL | L2 TTL | Typical key | Populated by |
|---|---|---|---|---|---|
| `effective_role_cache` | `TwoLevelCache[str \| None]` | 60 s | 90 s | `user_id` | `users_roles.get_effective_role()` |
| `connected_cache` | `TwoLevelCache[bool]` | 120 s | 180 s | `chat_id` | `groups_db.is_connected()` |
| `active_groups_cache` | `TwoLevelCache[list[GroupDoc]]` | 30 s | 45 s | fixed key | `groups_db.active_groups()` |
| `owner_id_cache` | `TwoLevelCache[int \| None]` | 300 s | 360 s | fixed key | `users_roles.get_owner_id()` |
| `user_mention_cache` | `TwoLevelCache[list[str \| None]]` | 300 s | 600 s | `user_id` | `users_cache.get_user_mention_data()` / `upsert_user()` |

Write helpers must invalidate or refresh related cache entries. Role writes invalidate the target user's effective role cache; group writes clear or update the groups and connected caches.

## Scheduler

`scheduler.py` owns the APScheduler 3.11.3 lifecycle.

| Export | Purpose |
|---|---|
| `start(mongodb_uri, db_name, warn_expiry_days)` | Spawns the background asyncio task, waits until the scheduler is ready. |
| `stop()` | Sets the stop event; waits up to 10 s for graceful shutdown. |
| `schedule_unban(ban_id, user_id, run_at)` | Registers a persistent one-off `DateTrigger` unban job. Returns the schedule ID. |
| `cancel_schedule(schedule_id)` | Removes a schedule by ID. Returns `True` if found, `False` if already fired or never created. |

Recurring jobs registered on every startup (idempotent via `replace_existing=True` on each `add_job` call):

| Job | Trigger | Purpose |
|---|---|---|
| `expire_old_warns` | every 24 h | Deletes `warn_count` records older than `WARN_EXPIRY_DAYS` days via `db_call()` (circuit-breaker protected). Only registered when `WARN_EXPIRY_DAYS > 0`. On Vercel the same function runs on demand through `GET /api/cron` (see [`../operations/vercel.md`](../operations/vercel.md)) because no persistent scheduler exists there. |

`member_cache` cleanup is now handled automatically by a MongoDB TTL index on `last_updated` (`expireAfterSeconds=7776000`, equivalent to 90 days), created in `mongos.ensure_indexes()`. The former `_cleanup_old_records` weekly job has been retired; any persisted schedule from a previous run is removed from the APScheduler datastore on first startup after the upgrade.

## Document typing

Use `documents.py` for MongoDB shapes and `types.py` for nominal ID types in new helpers. These are typing aids; stored MongoDB values remain plain strings, integers, booleans, and datetimes.

All `*_db.py` modules use a TypedDict from `documents.py` when inserting records:

| Collection | TypedDict |
|---|---|
| `bans` | `BanDoc` |
| `kicks` | `KickDoc` |
| `mutes` | `MuteDoc` |
| `warns` | `WarnDoc` |
| `warn_counts` | `WarnCountDoc` |
| `member_cache` | `UserDoc` |
| `federated_groups` | `GroupDoc` |
| `pending_joins` | `PendingGroupDoc` |
| `tc_admins` | `AdminDoc` |
| `tc_roles` | `RoleDoc` |
| `tc_roles` (index reference) | `RoleRefDoc` |
| `active_mutes` | `ActiveMuteDoc` |
| `promotion_requests` | `PromotionRequestDoc` |

## Safety rules

- Do not call `col()` from command modules or workflow files.
- Keep new collection helpers in `*_db.py` files.
- Keep stored schema changes backward-compatible unless a migration plan exists.
- Use `utc_now()` from `tcbot.utils.time_and_date` for stored timestamps.
- Never log secrets or connection strings.
