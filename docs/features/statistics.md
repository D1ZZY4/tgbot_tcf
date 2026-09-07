# Statistics

This document describes the unified federation statistics command implemented by `tcbot/modules/stats.py` and `tcbot/modules/helper/workflows/stats_flow.py`.

For module structure, see [`../architecture/modules.md`](../architecture/modules.md).
For shared helpers and decorators, see [`../architecture/helpers.md`](../architecture/helpers.md).
For database access patterns, see [`../architecture/database.md`](../architecture/database.md).
For the check command, see [`moderation/check.md`](moderation/check.md).

```mermaid
flowchart TD
    Cmd[/tcstats command/] --> Summary[Summary view]
    Summary --> Buttons{Drill-down}
    Buttons --> Staff[Staff list<br/>batch query]
    Buttons --> Users[Users list<br/>batch query]
    Buttons --> Chats[Connected chats]
    Buttons --> Bans[Active bans<br/>batch query]
    Bans --> SearchPanel[Search panel]
    Staff & Users & Chats & Bans --> Detail[Detail callback]
    Detail --> Profile[Check hint text]
```

## Purpose

`/tcstats` is the federation's read-only overview. A single inline keyboard
opens drill-down panes (Staff Roster, Connected Chats, User Bans for everyone,
plus Users for the Owner/Founder only) plus a search panel for active bans.
Every pane returns to the same overview through a `« Back` button.

Aliases:

- `/tcstats`
- `/tcs`

## Command surface

| Command | Aliases | Who can use | Where |
|---|---|---|---|
| `/tcstats` | `/tcs` | Anyone | Bot PM, exec group, or supported federation group context |

## Top-level overview

`Stats.main(*, viewer_id=None)` returns the overview card:

```text
<community> Stats

Founder: <mention>
Staff: <total> (Admins n, Devs n, Testers n)
Users tracked: <n>
Active bans: <n>
Connected chats: <n>
```

Inline keyboard:

```text
[ Staff Roster ] [ User Bans ]
[ Connected Chats ]
[ Users ]                     <- Owner/Founder only (row 3)
```

The `Users` button is rendered only when `viewer_id` belongs to the Owner or
holds the Founder role (`is_owner` plus `get_effective_role`, both cached, in
the same parallel batch; a failed lookup hides the button). The
`stats_users` / `stats_user_item` callbacks enforce the same gate with a
`Founder only.` alert, so a stale or crafted tap without the button still
cannot open the list. Lookup outages fail closed with a retry alert.

Independent counters and the Founder mention are fetched concurrently with
`asyncio.gather`; the Telegram response still depends on database and network
latency.

## Drill-downs

### Staff Roster (`stats_admins`)

`Stats.staff_roster()` lists every staff member, grouped by role. Names are resolved in a single parallel pass for the whole roster: owner first, then each role section.

```text
Staff Roster - <community>

Founder
- <mention>

Admins (n)
- <mention>

Developers (n)
- <mention>
- <mention>

Testers (n)
- No staff assigned
```

### Users (`stats_users:<page>` and `stats_user_item:<page>:<idx>[:stable]`)

Owner/Founder only, at both layers: the menu button renders only for them
and both callbacks alert-deny everyone else. `Stats.users_list(page)` paginates `users_cache.all_users_page()` (server-side skip/limit, sorted by `first_name`). Each row shows the cached display name, ID, and `@username` when present. Numbered buttons open `Stats.user_detail(bot, page, idx, stable)`, which renders instantly from cache and refreshes stale/sparse documents in the background via `extraction.launch_identity_refresh` (zero added latency):

```text
User Details

Name: <mention>
ID: <id>
Username: @username or -
Last name: <last_name or ->

First seen: <utc>
Last seen: <utc>

Use `/check <id>` for the full profile.
```

The detail view shows a text hint with the equivalent `/check <id>`
command (no button), so a user can move from identity details to
federation history.

### Connected Chats (`stats_chats:<page>` and `stats_chat_item:<page>:<idx>[:stable]`)

`Stats.chats_list(page)` paginates `groups_db.active_groups()`. Each row shows the chat title and ID. Numbered buttons open `Stats.chat_detail(bot, page, idx, stable)`, which renders the cached title instantly and re-verifies renames in the background (persisted via `groups_db.refresh_group_title`):

```text
Group Details

Name: <title>
Chat ID: <chat_id>

Connected by: <mention>
Date: <utc>
```

### User Bans (`stats_bans:<page>` and `stats_ban_item:<page>:<idx>[:stable]`)

`Stats.bans_list(page)` pages `bans_db.active_bans_page()` (server-side count via `active_ban_count()` plus one skip/limit fetch, newest first). The list is ordered newest first via the existing index. The page footer adds a `[ Search ]` button that opens the search panel. Numbered buttons open `Stats.ban_detail(page, idx, stable)`, which reuses `helper/ban_info.build_ban_detail` and exposes a `View Proof` URL when proof exists. When the button carries the stable ban ID, the detail view fetches the record directly via `bans_db.get_ban` (one indexed read, immune to list shifts); missing or inactive records report not-found, matching the list-derived path.

### Search panel (`stats_bans_search`, `stats_search_*`)

`Stats.open_search` records `(chat_id, message_id)` in `ctx.user_data` so the user's free-text query message can be deleted and the original card edited in place. Search runs through `Stats.search_run`:

- Numeric query → `bans_db.get_active_ban(int(query))`.
- Non-numeric query → anchored prefix lookup in the member cache via `users_cache.search_by_name` (same semantics as command target resolution, capped at 30 hits), then a single `$in` fetch of their active bans via `bans_db.active_bans_for_users`. Only matching rows travel over the wire.

Results are rendered with a numbered keyboard. Each hit opens `Stats.search_detail`, which reuses `build_ban_detail` and offers `View Proof` plus `Back to Results`.

The free-text input handler is scoped to private chats and only fires while the search panel is active; it ignores every other text message.

## Class architecture

`Stats` is a stateless container. Every method is a `@classmethod` returning `(text, InlineKeyboardMarkup)`. Callbacks pair an `await q.answer()` with `safe_edit_cb()` so the same content can be re-tapped without raising `Message is not modified`.

```python
class Stats:
    PAGE_SIZE = 6

    @classmethod async def main(*, viewer_id=None) -> tuple[str, InlineKeyboardMarkup]
    @classmethod async def staff_roster() -> tuple[str, InlineKeyboardMarkup]
    @classmethod async def users_list(page) -> tuple[str, InlineKeyboardMarkup]
    @classmethod async def user_detail(bot, page, idx, stable=None) -> tuple[str, InlineKeyboardMarkup]
    @classmethod async def chats_list(page) -> tuple[str, InlineKeyboardMarkup]
    @classmethod async def chat_detail(bot, page, idx, stable=None) -> tuple[str, InlineKeyboardMarkup]
    @classmethod async def bans_list(page) -> tuple[str, InlineKeyboardMarkup]
    @classmethod async def ban_detail(page, idx, stable=None) -> tuple[str, InlineKeyboardMarkup]
    @classmethod def    open_search(ctx, q) -> tuple[str, InlineKeyboardMarkup]
    @staticmethod      clear_search(ctx) -> None
    @classmethod async def search_run(query) -> list[dict]
    @classmethod async def search_results(query, results) -> tuple[str, InlineKeyboardMarkup]
    @classmethod async def search_detail(results, idx) -> tuple[str, InlineKeyboardMarkup]
```

The previous `stats_chats_flow.py` has been removed; its responsibilities live entirely inside `Stats`.

## Database helpers used

| Helper | Purpose |
|---|---|
| `users_roles.get_owner_id()` | Founder's user ID for the overview line. |
| `users_roles.admin_count()` | Total Admins for the overview. |
| `users_roles.all_admins()` | Full Admin list for the staff roster. |
| `users_roles.all_by_role("developer" \| "tester")` | Per-role lists for the staff roster. |
| `users_cache.total_users()` | Cached-user count for the overview. |
| `users_cache.all_users_page()` | Paginated user list (server-side skip/limit, server-sorted by `first_name`). |
| `users_cache.get_first_name(uid, fallback)` | Display-name lookups. |
| `bans_db.active_ban_count()` | Active-ban count for the overview. |
| `bans_db.active_bans_page()` / `active_ban_count()` | Paginated ban list (server-side skip/limit). |
| `bans_db.active_bans_for_users()` | Name-search hits (single `$in` fetch). |
| `bans_db.get_active_ban(uid)` | Direct ID search hit. |
| `groups_db.active_group_count()` | Connected-group count for the overview. |
| `groups_db.active_groups()` | Paginated chat list and detail lookup. |

## Async behaviour

Every list view that needs more than one independent read uses
`asyncio.gather`. Per-item lookups are resolved before the formatting loop, so
string construction remains synchronous. The search input handler also runs
the search and message deletion concurrently.

## Edge cases

- A user with no cached profile renders as their numeric user ID (e.g. `123456789`) in every list when no cached name is available. This is the `str(uid)` return from `_best_name()` in `extraction.py`, not the earlier `"User <id>"` pattern.
- An empty roster ("- No staff assigned") never crashes pagination because the user/chat/ban lists have their own empty-state branch.
- Re-tapping the same drill-down does not raise; `safe_edit_cb` swallows the `Message is not modified` `BadRequest`.
- The search input handler is private-chat only and gated by `SEARCH_KEY`; it never absorbs unrelated group messages.
- `Stats.clear_search(ctx)` is called whenever the user navigates away from the bans panel so stale results never leak into a new search.

## Behavior reference

- `/tcstats` and `/tcs` both reach `cmd_stats` regardless of prefix (`/`, `!`, `.`).
- The `Users` button and both Users callbacks are Owner/Founder only; all other panes stay public.
- `Staff Roster` page shows the Founder mention exactly once, then each role section, even when a role list is empty.
- `Users` pagination clamps to the last page when the requested page exceeds `total_pages`.
- `Connected Chats` detail card matches the format produced by the previous `stats_chats_flow.build_chat_detail`.
- `User Bans` Search panel handles numeric IDs and free-text queries; each hit drills into the same detail card as the regular bans list.
- All callbacks ack the query before editing, so Telegram never marks them as unanswered.
