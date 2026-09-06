# Security and Authorization Rules

This file defines authorization boundaries, role and identity safety, secret
handling, and compatibility guarantees for TCF Bot. Moderation authorization
is security-sensitive: a bypass can affect every connected group. Python style
and module boundaries live in [`code-style.md`](code-style.md), async patterns
live in [`asyncio-gather-rules.md`](asyncio-gather-rules.md), and validation
commands live in [`tooling-validation.md`](tooling-validation.md).

---

## Role Resolution and Authorization

- Resolve roles with `users_roles.get_effective_role(user_id)`.
- Use `users_roles.can_act_on()` or
  `decorators.resolve_and_check()` for executor-versus-target checks.
- Do not chain owner, admin, and role checks manually in handlers.
- Do not compare role ranks inline in command modules.
- Use `ROLE_LABEL` for user-facing role labels.
- Use `identity.classify`, `identity.refuse_message`, and
  `identity.staff_notice` for self, bot, Telegram, Founder, and staff branches.
- Call `Demote.execute(..., trigger="ban"|"kick"|"mute")` before banning, kicking,
  or muting a target who holds a federation role.
- Developer and Tester roles live in `tc_roles`.
- Admin promotion requests use `queues_db` and the existing promotion workflow.
- Role lookup failures must reject the action (fail closed); never treat a
  failed lookup as a user with no role.
- Preserve `asyncio.CancelledError` in role lookups; re-raise instead of
  converting it into a role result.
- Guard primary groups with `cfg.is_primary_group()`; never inline the
  main/exec tuple check.

## Secrets and Credentials

- Never hardcode bot tokens, MongoDB URIs, API keys, passwords, webhook
  secrets, deployment chat IDs, or other credentials.
- Do not log or document secrets, tokens, credentials, raw private input, or
  private chat identifiers.
- Do not print or log secrets from handlers, jobs, or database helpers.
- Keep runtime secrets in environment variables or the platform secret manager.
- Never edit or commit `config.env` during normal work.
- Do not commit tokens, MongoDB URIs, API keys, passwords, or private chat IDs
  that should remain secret.

## Compatibility

- Preserve backward compatibility for production moderation, role, and database
  behavior.
- Do not remove meaningful behavior merely to silence a warning.
- Treat every moderation flow change as potentially high impact: verify success
  and failure paths, alternate entry points, and state transitions.
- The webhook route in `alive.py` rejects non-JSON content types at the parser
  level; do not weaken this without a security review.
