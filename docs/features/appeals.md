# Appeals

This document describes the current ban appeal behavior implemented by `tcbot/modules/appeals.py` and `tcbot/modules/helper/workflows/appeal_flow.py`.

For the ban flow that triggers appeals, see
[`moderation/banning.md`](moderation/banning.md). For the check command often
used during appeals, see [`moderation/check.md`](moderation/check.md). For
shared helpers, see [`../architecture/helpers.md`](../architecture/helpers.md).
For the database layer, see [`../architecture/database.md`](../architecture/database.md).

```mermaid
flowchart TD
    DeepLink[/start appeal_BANID/] --> Validate{Valid ban_id?}
    Validate -->|no| Reject[Show error]
    Validate -->|yes| DM[Open private DM]
    DM --> Reason[WAITING_APPEAL]
    Reason --> Submit[Submit to APPEALS chat]
    Submit --> ReviewCard[Post review card<br/>in MAIN_GROUP topic]
    ReviewCard --> Decision{Staff decision}
    Decision -->|approve| Unban[Run /tcunban + notify]
    Decision -->|reject| Notify[Notify user rejected]
```

## Purpose

The appeal flow lets a user with an active federation ban submit one appeal through the bot in private messages. The appeal is forwarded to the configured appeal channel/topic, posted for staff review in the main group, logged in the federation logs channel, and then resolved through inline callback buttons.

## Entry points

A banned user can reach the appeal flow through a bot deep link:

- The `Submit Appeal` button attached to a ban log entry.
- The `Appeal` button shown by `/checkme` when the user has an active federation ban.
- A direct deep link in this format:
  - `https://t.me/<bot_username>?start=appeal_<ban_id>`

The registered `/start` entry filter only accepts private-chat messages matching:

```text
/start appeal_<10 lowercase letters or digits>
```

The flow itself is DM-only. If the link is opened outside a private chat, the bot asks the user to open it in private messages.

## Eligibility checks

When the appeal link is opened, the bot validates the ban record before starting the conversation:

1. The `ban_id` must exist in the `bans` collection.
2. The ban must still be active (`is_active: True`).
3. The Telegram user opening the link must match `banned_user_id` on the ban record.
4. The ban must not already have a stored `review_message_id` that is still fresh. If it does and was set within the last 72 hours, the user is told their appeal is still under review. If the `review_timestamp` is older than 72 hours (or missing), the stale review is cleared automatically and the user may submit a new appeal.

Invalid, expired, or wrong-account links end the conversation without changing the database.

## User submission format

After a valid deep link is opened, the bot sends instructions in DM and waits for one text message.

The message must start with `#appeal`. The check is case-insensitive and ignores leading/trailing whitespace, so these are accepted:

```text
#appeal
#APPEAL
#Appeal
   #appeal
```

Messages that do not start with `#appeal` are ignored and the bot remains in the appeal state.

The requested appeal body contains three sections:

```text
#appeal
Log link: https://t.me/TranssionCoreFederationLogs/123
Clarification: I understand what happened and why the ban was issued.
Agreement: I will follow the community rules going forward.
```

The current implementation does not parse the section labels semantically. It validates the `#appeal` prefix and, when the ban record has a `log_message_id`, checks that the submitted text contains that log message ID as a standalone number. This makes the log-link check tolerant of full Telegram links, bare message IDs, and links with query parameters, while rejecting partial numeric matches.

Examples:

- `https://t.me/c/12345/67?thread=10` matches log message ID `67`.
- `67` as a standalone token matches log message ID `67`.
- `670` does not match log message ID `67`.
- `6` does not match log message ID `67`.

If the log message ID is missing or mismatched, the bot replies with `Invalid log link. Please check and try again.` and keeps waiting.

Appeal messages have a **maximum length of 2000 characters**. Messages that exceed this limit are rejected with a trimming instruction; the user may shorten the message and try again without restarting the session.

## Submission side effects

When a valid appeal message is submitted, the bot first revalidates the
fresh ban record: the ban must still be active, must not have gained a fresh
pending review while the user was typing (a concurrent submit from another
session), and must not be inside the 24-hour rejection cooldown. Any of
those states ends the session with the matching reply instead of posting a
duplicate review card.

When the checks pass:

1. The user's appeal message is forwarded to the configured appeals destination (`cfg.appeals`).
2. A staff review card is posted to `cfg.main_group`, optionally inside `cfg.appeal_discussion_topic`.
3. A submitted-appeal log is posted to `cfg.logs`.
4. The pending-review slot is claimed atomically with `bans_db.set_review_if_absent`, so a concurrent duplicate submit cannot overwrite the winner and orphan its card. The loser deletes its own orphan review card best-effort and is told the appeal is already pending.
5. Appeal-log metadata is stored with `bans_db.set_appeal_log_msg` when the log post succeeded.
6. The original DM instruction message is edited to confirm submission.
7. The user is cached/updated in `users_cache`.

If both the review post and the appeal-log post fail, the instruction
message is edited to a delivery-failure reply and the conversation stays in
`WAITING_APPEAL` with its state intact, so the user can retry by sending
another `#appeal` message without reopening the deep link.

The review card contains two inline buttons:

| Button | Callback data |
|---|---|
| `Approve` | `appeal_approve_<ban_id>` |
| `Reject` | `appeal_reject_<ban_id>` |

The DM instruction message has a cancel button:

| Button | Callback data |
|---|---|
| `Cancel` | `cancel_appeal` |

Cancel clears the in-memory conversation keys and edits the instruction message to say that nothing was submitted.

## Database impact

Appeals reuse the existing `bans` collection. No separate appeal collection is used.

The following fields are read or updated on ban documents:

| Field | Used for |
|---|---|
| `ban_id` | Deep-link and callback identifier. |
| `banned_user_id` | Confirms the appeal belongs to the current user. |
| `is_active` | Blocks appeals for inactive/resolved bans. |
| `log_message_id` | Validates that the user referenced the correct ban log. |
| `review_message_id` | Marks a pending staff review and blocks duplicate appeals. |
| `review_timestamp` | Starts the 12-hour original-admin review priority window. |
| `appeal_log_msg_id` | Lets the bot edit the submitted-appeal log after approval/rejection. |
| `appeal_submitted_at` | Displayed in final appeal log edits. |
| `appeal_link` | Link to the forwarded appeal message. |

The update helpers involved are:

- `bans_db.set_review_if_absent(ban_id, review_msg_id)` (atomic claim; returns whether this submit won the review slot)
- `bans_db.set_review(ban_id, review_msg_id)` (unconditional write; kept for backward compatibility)
- `bans_db.set_appeal_log_msg(ban_id, appeal_log_msg_id, appeal_link=...)`
- `bans_db.deactivate_all_active_bans(user_id)` on approval (clears all active records atomically)
- `users_cache.upsert_user(...)` after submission

If the review post fails, `review_message_id` is not stored. If the appeal-log post fails, `appeal_log_msg_id` is not stored. The implementation logs those failures and continues where possible.

## Staff review rules

Appeal decisions are handled by `appeal.on_decision` and are registered outside the conversation handler. Only Founder or Admin reviewers (effective role `founder`/`admin` via `users_roles.get_effective_role`) may use the review buttons. In this codebase that matches `is_staff` semantics; Developer and Tester custom roles are not included.

A role-lookup outage fails closed with a retry alert instead of an authorization verdict, so a transient database blip is never misreported as "not authorized".

Taps that do not decide anything never edit the shared review card; they
answer the callback query with a popup alert so the card stays actionable
for staff:

- Taps from non-staff users.
- Taps inside the 12-hour banning-admin priority window by a different admin.
- Repeat taps after the appeal was already decided (the winner's verdict edit is preserved).
- Taps during a role-lookup outage.

The only resolved tap that edits the card is a stale one: an inactive ban
that still carries a live review marker (left behind by a manual `/tcunban`,
which never touches review state). That tap clears the marker best-effort
and updates the card to the already-resolved text.

The original banning admin gets a 12-hour priority window after the appeal review timestamp is set:

- During the first 12 hours, only the `admin_user_id` stored on the ban can approve or reject.
- The original banning admin is always allowed during that window.
- After 12 hours, any Founder/Admin reviewer can decide.
- If `review_timestamp` is missing, or `admin_user_id` is missing or zero (legacy record), the lock does not apply.

The callback handler always answers the callback query before continuing once it has parsed a valid decision action.

## Approval behavior

When a staff member approves an appeal:

1. Active connected groups are fetched from `groups_db.active_groups()`, with the primary groups (`cfg.main_group`, `cfg.exec_group`) appended when absent. If the fetch fails, approval aborts before deactivation with the review card untouched (a re-tap retries the full sequence); deactivating first would leave chats unbanned-nowhere with no record to re-drive.
2. All active bans for the user are deactivated with `bans_db.deactivate_all_active_bans(user_id)`, which clears any duplicate active records in one atomic operation. If this write fails, approval aborts before any group is touched (mirroring `execute_unban`): unbanning chats while the database still marks the user banned would split-brain and re-ban them on next join.
3. The review marker is cleared with `bans_db.clear_review(ban_id)` so a concurrent second decision sees no live review and stands down.
4. The user is unbanned from every group with `unban_chat_member(..., only_if_banned=True)` through bounded fan-out. Partial transient failures are logged at error level.
5. The user receives a DM telling them the appeal was approved.
6. The review message is edited to show who approved it and the inline keyboard is removed.
7. The submitted-appeal log message is edited to an approved version when possible.
8. A separate `Unban (via Appeal)` log is sent to the federation logs channel.

Each notification in steps 5-8 is inspected separately and logged (warning for the user DM, card edit, and appeal-log update; error for the unban log), so a silent Telegram failure is visible to operators.

Approval is a federation unban. It removes the Telegram ban across all connected groups and deactivates the persistent ban record. It runs its own inline deactivate-plus-fan-out sequence that mirrors `execute_unban`; it does not call `execute_unban` directly.

Approval is a federation unban. It removes the Telegram ban across all connected groups and deactivates the persistent ban record.

## Rejection behavior

When a staff member rejects an appeal:

1. `bans_db.set_rejected_by(ban_id, admin.id, admin.first_name)` records the rejector's identity (`rejected_by_id`, `rejected_by_name`, `rejected_at`) first and alone, so the 24-hour cooldown holds even if a later write fails.
2. The ban remains active.
3. The user receives a DM telling them the appeal was reviewed and not approved.
4. The review message is edited to show who rejected it and the inline keyboard is removed.
5. `bans_db.clear_review(ban_id)` clears `review_message_id` and `review_timestamp` so the user can submit a new appeal after the cooldown.
6. The submitted-appeal log message is edited to a rejected version when possible.

Steps 3-5 run in a single `asyncio.gather` (after the target display name
is resolved) so a DM failure does not block the review-message edit or the
DB writes. Each side effect is inspected: a failed DM logs at warning level,
a failed card edit at debug level, and a failed `clear_review` at error
level (the pending review would still be in the database).

Rejection does not deactivate the ban. The `review_message_id` and `review_timestamp` fields **are cleared** on rejection so the user may submit a subsequent appeal without being locked out.

## Logs

Appeal-related logs are built in `tcbot/modules/helper/parse_logmsg.py`:

| Template | Destination / use |
|---|---|
| `appeal_received_log` | Staff review card in the main group / appeal discussion topic. |
| `appeal_submitted_log` | Initial log in the federation logs channel. |
| `appeal_approved_edit` | Edited version of the submitted log after approval. |
| `appeal_rejected_edit` | Edited version of the submitted log after rejection. |
| `appeal_unban_log` | Separate unban log sent after an approved appeal. |

If editing the existing appeal log fails, the bot attempts to send a new log message as a fallback.

## Timeouts and fallbacks

- `cfg.appeal_timeout` (`APPEAL_TIMEOUT_SECONDS`, default `600`) is parsed from the environment but is not applied to the `ConversationHandler`; there is no `ConversationHandler.TIMEOUT` state. Conversations end only via escape commands, cancel, or successful submission.
- Any recognized command during the waiting state ends the session with `Appeal session ended.`
- Cancel ends the session without writing appeal metadata.
- If the user sends `#appeal` after the session state has expired or the ban ID is missing from `ctx.user_data`, the bot asks them to start the appeal again.

## Edge cases

- Non-banned users cannot submit because they cannot pass the active-ban lookup.
- A user cannot appeal someone else's ban; the `banned_user_id` must match the Telegram account opening the link.
- A user can have only one pending appeal when `review_message_id` was stored successfully.
- Case-insensitive `#appeal` tags are accepted.
- The log-link validation is number-based, not URL-domain-based; it checks for the stored log message ID as a standalone integer token.
- If the forwarded appeal message cannot be linked, the review/log text uses `N/A` for the appeal link but still posts the review/log when possible.
- If user DM notification fails during approval or rejection, the review/log operations still proceed because those calls use `asyncio.gather(..., return_exceptions=True)`.
- If a ban was already deactivated before a decision, a tap on a stale card (live review marker left by a manual unban) clears the marker and updates the card; any other already-decided tap only shows a popup so the recorded verdict is preserved.
- Two staff decisions racing each other are serialized only by the pre-decision guard (active ban, live review, no prior rejection); a true simultaneous double-tap can still double-notify. The database writes themselves stay idempotent.

## Rejection cooldown

After a staff member rejects an appeal, the banned user must wait **24 hours** before
submitting a new one. This prevents spam-appealing immediately after every rejection.

The cooldown is enforced in `BuildAppeal._start()`: when `ban.rejected_at` is present and
`utc_now() - to_utc(rejected_at) < timedelta(hours=24)`, the user receives a message
showing the hours remaining and `ConversationHandler.END` is returned.

The cooldown applies independently of the stale-review window. If a stale review
(≥ 72 h old) is cleared in `_start()`, the rejection cooldown check still follows
immediately after.

## Anonymous admin mode and appeal decisions

Federation staff in anonymous admin mode (GroupAnonymousBot, `user_id = 1087968824`)
**cannot use any bot command** because all command entry-point decorators check
`effective_user.id == 1087968824` and return an error.

However, staff **can still click Approve or Reject** on the appeal review card.
The callback path uses `effective_user` for the account associated with the
button press. In the callback behavior supported by this project, staff can
therefore use the appeal decision buttons even when their group messages are
anonymous; the bot does not replace the callback sender with
`GroupAnonymousBot`.

Practical consequence: an admin in anonymous mode cannot issue `/tcban` or `/tcwarn`,
but they can open the appeal review card in the main group and use the Approve/Reject
buttons. Their real account identity is recorded as the approver or rejector.

## Behavior reference

Important appeal behaviors to keep in mind:

1. `#appeal`, `#APPEAL`, and mixed-case tags are accepted.
2. Text without a leading hash tag is rejected.
3. A log message ID matches as a standalone number inside a Telegram link.
4. Partial numeric matches are rejected.
5. Appeal messages must not exceed 2000 characters; longer messages are rejected with a trim instruction and the user can retry without restarting.
6. The 12-hour reviewer lock blocks a different admin inside the window.
7. The original banning admin is allowed inside the 12-hour window.
8. Any staff reviewer is allowed after 12 hours.
9. Missing `review_timestamp` or missing/zero `admin_user_id` disables the reviewer lock.
10. Uppercase `#APPEAL` reaches the expired-session branch when conversation state is missing.
11. The module-level appeal builder uses the configured appeal log handle.
12. After rejection, a 24-hour cooldown applies before the user can start a new appeal.
13. Staff in anonymous admin mode can use appeal decision buttons but not bot commands.
14. Non-deciding taps (outsiders, locked-out reviewers, repeat taps, outage retries) never edit the review card.
15. Concurrent duplicate submits are resolved by an atomic review-slot claim; the loser is told the appeal is pending.
16. A total delivery failure keeps the conversation open for an in-place retry.
