# Workflow Overview

This page describes the user-visible flows in TCF Bot. For state constants,
factories, and callback details, see
[Workflow internals](../architecture/workflows.md).

## Moderation flows

| Flow | Entry commands | Permission | Scope | Notes |
|---|---|---|---|---|
| Ban | `/tcban`, `/tcb` | Developer+ | All connected groups | Requires inline reason and proof media; updates existing active ban instead of duplicating. |
| Unban | `/tcunban`, `/tcunb` | Developer+ | All connected groups | Direct command; resolves an active ban and removes it federation-wide. |
| Kick | `/tckick`, `/tck` | Tester+ | Current group only | Conversation asks for reason/proof; auto-demotes role holders before kicking. |
| Mute | `/tcmute`, `/tcm` | Tester+ | All connected groups | Optional duration token before reason, for example `7d`. |
| Unmute | `/tcunmute`, `/tcunm`, `/tcum` | Tester+ | All connected groups | Direct command; restores send permissions. |
| Warn | `/tcwarn`, `/tcw` | Tester+ | Current group warning history | Reason required; auto-ban at `WARN_LIMIT` (default 3) per group or when `FED_WARN_LIMIT` (env, default disabled) is crossed across all groups. |
| Unwarn | `/tcunwarn`, `/tcunw` | Tester+ | Current group warning history | Removes the newest warning. |
| Warn list | `/warns`, `/warnlist` | Tester+ | Current group warning history | Shows a user's warnings. |
| Reset warns | `/resetwarns`, `/clearwarns` | Tester+ | Current group warning history | Clears all warnings for a user in the chat. |

## Ban flow

```mermaid
flowchart TD
    A[Admin sends /tcban target reason] --> B{Permission and target valid?}
    B -- No --> X[Reply with error and end]
    B -- Yes --> C{Target has federation role?}
    C -- Yes --> D[Auto-demote target and notify/log]
    C -- No --> E[Prompt for proof]
    D --> E
    E --> F[Admin sends photo/video proof]
    F --> G[Upload proof to proof destination]
    G --> H[Create or update ban record + post log in parallel]
    H --> I[fan_out ban across connected groups]
    I --> J[Edit prompt summary + DM appeal link]
```

Ban proof supports Telegram media albums. Album items are buffered for `ALBUM_DEBOUNCE_SECONDS` before processing.

## Reason + proof flows

Kick, mute, and warn use the shared `reason_flow.build_modaction_conv()` factory.

```mermaid
flowchart TD
    A[Command entry] --> B{Inline reason exists?}
    B -- Yes --> C[Prompt for proof]
    B -- No --> D[Prompt for reason]
    D --> E{Reason action}
    E -- Text --> C
    E -- Skip, if allowed --> C
    E -- Cancel --> Z[End]
    C --> F{Proof action}
    F -- Photo/video --> G[Record proof description and execute]
    F -- Skip, if allowed --> G
    F -- Cancel --> Z
    G --> Y[Reply/log result and end]
```

Warns configure `BuildReason("warn", skip_allowed=False)`, so a reason cannot be skipped.

## Appeal flow

Appeals start from a deep link: `/start appeal_<ban_id>` in bot PM.

```mermaid
flowchart TD
    A[User opens appeal deep link in PM] --> B{Active ban and ban ID match?}
    B -- No --> X[Explain why appeal cannot start]
    B -- Yes --> C[Show appeal instructions]
    C --> D[User sends #appeal text]
    D --> E{Prefix + log link valid?}
    E -- No --> C
    E -- Yes --> F[Forward appeal and post review card]
    F --> G[Staff taps Approve or Reject]
    G --> H{Decision}
    H -- Approve --> I[Unban across connected groups and notify user]
    H -- Reject --> J[Mark reviewed and notify user]
```

Required appeal sections:

```text
#appeal
Log link: https://t.me/...
Clarification: explanation of the situation
Agreement: commitment to follow community rules
```

The original banning admin has a 12-hour priority review window. During that window, only the banning admin (`admin_user_id` on the ban) can review. After the window, any staff reviewer accepted by `is_staff` (Founder or Admin) can act. When the ban record carries no usable banning admin (missing or zero `admin_user_id`), the window does not apply.

## Group connection flow

```mermaid
flowchart TD
    A[Bot added or /tcconnect used in group] --> B[Show Connect / Cancel prompt]
    B --> C{Group owner chooses Connect?}
    C -- Cancel --> X[Remove pending request and log rejection]
    C -- Connect --> D{Bot has required admin permissions?}
    D -- No --> E[Show required permissions]
    D -- Yes --> F[Add group as active]
    F --> G[Apply existing federation bans and active mutes]
    G --> H[Log connection]
```

Disconnected groups are marked inactive rather than deleted, preserving historical data.

## Staff role flows

- `/tcpromote` assigns `admin`, `developer`, or `tester` based on executor rank.
- Founder can assign Admin, Developer, or Tester.
- Admin can assign Developer or Tester directly.
- Admin-to-Admin promotion creates a queued request for Founder approval.
- `/tcdemote` uses confirm/cancel buttons before removing a role.
- `/transferowner` transfers Founder ownership.

## Statistics and lookup flows

- `/checkme` shows the caller's active ban status and appeal/proof buttons when applicable.
- `/check` / `/c` lets anyone inspect a user's full federation profile (identity, role, bans, warnings, kicks, mutes, appeals).
- `/tcgroups` lists connected groups with a details toggle.
- `/tcstats` shows summary cards, active bans, connected chats, and search/detail views.

## Maintenance and broadcast flows

- `/tcbroadcast` sends a message to every active connected group through bounded fan-out and logs success/failure counts.
- Staff-only `/cleanup` (aliases `/tcclean`, `/tcc`) checks active groups and
  deactivates groups the bot can no longer access.
- `/leaveall`, `/exitall`, and `/tcleave` are Founder-only emergency commands that make the bot leave connected groups and mark them inactive.
