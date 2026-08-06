"""The four social-graph events — A64-013.7.

Domain layer, framework-free, and owned by the context that owns the fact
(architecture.md §8). `notifications` learns about a block by importing
`friends.public`, never by importing a table or a service.

## What is in a payload, and what is deliberately not

Each payload carries **the two player ids and the instant**, and nothing
else. Not the usernames, not the display names, not the avatars — even
though a notification will eventually need all three.

The reason is A64-013.7's own rule, and it is the difference between a
notification system that respects privacy and one that only appears to: a
payload rendered at *enqueue* time is a payload rendered against the
relationship, the privacy settings and the block list as they were then. By
the time it is delivered, seconds or minutes later, the recipient may have
been blocked, the subject may have hidden their presence, and the friendship
may have ended. The consumer therefore re-reads and re-renders — see
`SocialNotificationDispatcher` — and anything this payload carried beyond
identity would be a stale copy competing with that.

## Why blocking emits events at all, given BL-1

`PlayerBlocked` and `PlayerUnblocked` exist and are made durable, and their
*notification* audience is empty: BL-1 keeps a block invisible to its
subject, so nobody is told. That is not a contradiction. The event log is
the platform's record of what happened (AD-17), and the consumers a block
already has — cache invalidation today, moderation and audit tomorrow — are
not notifications. Suppressing the event because one consumer must not act
on it would be deciding a subscriber's policy at the producer.
"""

from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from app.platform.events import DomainEvent


@dataclass(frozen=True)
class FriendRequestSent(DomainEvent):
    """A pending request was created — FR-1, and A64-021.1's first producer.

    The fact has existed since A64-013.2 and was recorded only in a log
    line, which meant the addressee was never told: the platform knew about
    every request and nothing on it could say so. Publishing it changes
    nothing about the request itself and gives the durable notification a
    source event whose recipient is unambiguous — the **addressee**, who did
    not perform the action and is the one who has to answer it.

    Carries the two ids and nothing else, for the reason every event in this
    file does: a payload rendered at enqueue time is rendered against a
    relationship, a privacy setting and a block list as they were *then*,
    and the consumer re-reads all three. See this module's docstring.
    """

    event_type: ClassVar[str] = "friends.friend_request_sent"
    aggregate_type: ClassVar[str] = "friend_request"

    request_id: UUID
    requester_id: UUID
    addressee_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.request_id

    def payload(self) -> dict[str, Any]:
        return {
            "request_id": str(self.request_id),
            "requester_id": str(self.requester_id),
            "addressee_id": str(self.addressee_id),
        }


@dataclass(frozen=True)
class FriendRequestAccepted(DomainEvent):
    """A pending request became a friendship — FR-4.

    The aggregate is the **request**, not the friendship, because the
    request is what transitioned: the friendship is a consequence written in
    the same transaction. An operator tracing "why does this friendship
    exist" follows `aggregate_id` to the request that produced it.
    """

    event_type: ClassVar[str] = "friends.friend_request_accepted"
    aggregate_type: ClassVar[str] = "friend_request"

    request_id: UUID
    requester_id: UUID
    addressee_id: UUID
    friendship_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.request_id

    def payload(self) -> dict[str, Any]:
        return {
            "request_id": str(self.request_id),
            "requester_id": str(self.requester_id),
            "addressee_id": str(self.addressee_id),
            "friendship_id": str(self.friendship_id),
        }


@dataclass(frozen=True)
class FriendRemoved(DomainEvent):
    """A friendship ended by one party — FS-2.

    Carries `removed_by` because the actor decides the audience: FS-2 makes
    removal unilateral and silent, so the person who was removed is not told,
    and a consumer cannot honour that without knowing which of the two acted.
    """

    event_type: ClassVar[str] = "friends.friend_removed"
    aggregate_type: ClassVar[str] = "friendship"

    friendship_id: UUID
    removed_by: UUID
    removed_player_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.friendship_id

    def payload(self) -> dict[str, Any]:
        return {
            "friendship_id": str(self.friendship_id),
            "removed_by": str(self.removed_by),
            "removed_player_id": str(self.removed_player_id),
        }


@dataclass(frozen=True)
class PlayerBlocked(DomainEvent):
    """One player blocked another — BL-1, and its cascade.

    `friendship_ended` and `requests_voided` record what the cascade did, so
    a consumer that cares about the friendship's end does not have to
    correlate this event with a `FriendRemoved` that is deliberately never
    emitted — blocking ends a friendship with `FriendshipEndReason.BLOCKED`,
    which is a different fact from a removal.
    """

    event_type: ClassVar[str] = "friends.player_blocked"
    aggregate_type: ClassVar[str] = "block"

    blocker_id: UUID
    blocked_id: UUID
    friendship_ended: bool
    requests_voided: int

    @property
    def aggregate_id(self) -> UUID:
        # The blocker: a block is one person's act, and the row is theirs.
        return self.blocker_id

    def payload(self) -> dict[str, Any]:
        return {
            "blocker_id": str(self.blocker_id),
            "blocked_id": str(self.blocked_id),
            "friendship_ended": self.friendship_ended,
            "requests_voided": self.requests_voided,
        }


@dataclass(frozen=True)
class PlayerUnblocked(DomainEvent):
    """A block was lifted — BL-3.

    Restores nothing, and a consumer must not act as though it did: the
    friendship the block ended stays ended and the requests it voided stay
    voided. What changes is only that contact is possible again.
    """

    event_type: ClassVar[str] = "friends.player_unblocked"
    aggregate_type: ClassVar[str] = "block"

    blocker_id: UUID
    blocked_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.blocker_id

    def payload(self) -> dict[str, Any]:
        return {"blocker_id": str(self.blocker_id), "blocked_id": str(self.blocked_id)}
