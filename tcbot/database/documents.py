# © Copyright 2024 - 2026 Transsion Core
# © Copyright 2024 - 2026 Dizzy
# © Copyright 2026 Ave Labs

"""TypedDict document schemas for MongoDB collections."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

from tcbot.database.types import BanId, ChatId, GroupId, UserId

if TYPE_CHECKING:
    from datetime import datetime

RoleName = Literal["founder", "admin", "developer", "tester"]
RequestStatus = Literal["pending", "approved", "rejected"]


class AdminDoc(TypedDict, total=False):
    """MongoDB document for a federation admin record."""

    user_id: UserId
    promoted_by: UserId
    promoted_date: datetime


class BanDoc(TypedDict, total=False):
    """MongoDB document for a federation ban record."""

    ban_id: BanId
    banned_user_id: UserId
    reason: str
    admin_user_id: UserId
    proof_message_id: int
    log_message_id: int
    previous_proof_message_id: int | None
    previous_log_message_id: int | None
    timestamp: datetime
    updated_timestamp: datetime | None
    until_date: datetime | None
    duration_str: str | None
    is_active: bool
    update_count: int
    review_message_id: int | None
    review_timestamp: datetime | None
    appeal_log_msg_id: int | None
    appeal_submitted_at: datetime | None
    appeal_link: str | None
    rejected_by_id: int | None
    rejected_by_name: str | None
    rejected_at: datetime | None


class GroupDoc(TypedDict, total=False):
    """MongoDB document for a connected group."""

    chat_id: GroupId
    title: str
    added_by: UserId
    added_date: datetime
    is_active: bool


class PendingGroupDoc(TypedDict, total=False):
    """MongoDB document for a group awaiting connection approval."""

    chat_id: ChatId
    title: str
    owner_id: UserId
    message_id: int
    added_date: datetime


class RoleDoc(TypedDict, total=False):
    """MongoDB document for a user's federation role assignment."""

    user_id: UserId
    role: RoleName
    assigned_by: UserId
    assigned_at: datetime


class RoleRefDoc(TypedDict, total=False):
    """Minimal reference document storing only a user ID for role index lookups."""

    user_id: UserId


class UserDoc(TypedDict, total=False):
    """MongoDB document for a cached user profile."""

    user_id: UserId
    username: str | None
    first_name: str | None
    last_name: str | None
    commit_date: datetime
    last_updated: datetime


class KickDoc(TypedDict, total=False):
    """MongoDB document for a single kick event (append-only audit record)."""

    user_id: UserId
    chat_id: ChatId
    reason: str
    admin_id: UserId
    timestamp: datetime


class MuteDoc(TypedDict, total=False):
    """MongoDB document for a single mute event (append-only audit record).

    ``duration_secs`` is absent for permanent mutes and present (int) for
    timed mutes, reflecting how long the restriction was intended to last.
    """

    user_id: UserId
    chat_id: ChatId
    reason: str
    admin_id: UserId
    timestamp: datetime
    duration_secs: int | None


class ActiveMuteDoc(TypedDict, total=False):
    """MongoDB document tracking a currently active federation mute.

    Stored in the ``active_mutes`` collection: one document per muted user.
    ``until_date`` is ``None`` for a permanent mute or a UTC datetime for a
    timed mute.  Documents for timed mutes are auto-filtered by ``until_date``
    at query time (documents with ``until_date`` in the past are treated as
    expired and ignored).  A TTL index additionally prunes expired timed rows
    from storage; permanent mutes are never TTL-expired.  Use
    ``set_active_mute`` / ``clear_active_mute`` from ``mutes_db`` to manage
    this collection; do not write directly.
    """

    user_id: UserId
    until_date: datetime | None
    timestamp: datetime


class WarnDoc(TypedDict, total=False):
    """MongoDB document for a single warning issued to a user."""

    _id: object
    user_id: UserId
    reason: str
    admin_id: UserId
    chat_id: ChatId
    timestamp: datetime


class WarnCountDoc(TypedDict, total=False):
    """MongoDB document tracking the running warning total for a user in a chat."""

    user_id: UserId
    chat_id: ChatId
    count: int
    updated_at: datetime


class PromotionRequestDoc(TypedDict, total=False):
    """MongoDB document for a pending or resolved staff promotion request."""

    request_id: str
    target_id: UserId
    username: str | None
    first_name: str
    promoted_by: UserId
    status: RequestStatus
    requested_date: datetime
    resolved_date: datetime | None
    resolved_by: UserId | None
