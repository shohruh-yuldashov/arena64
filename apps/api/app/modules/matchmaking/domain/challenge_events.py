"""What happens to a challenge, as durable facts — A64-022.1 §11.

Domain layer, framework-free, written to the outbox in the same transaction
as the row they describe (AD-16) — so an event exists exactly when the thing
it announces did.

Their own module rather than members of `events.py`, and the line is the one
that file already draws for itself: it holds the *queue ticket's* three
transitions plus pairing, and a challenge is a different aggregate with a
different identity and a different lifetime. Two aggregates in one events
file would make `aggregate_type` a thing a reader has to check per class.

## Nothing consumes these yet, and they are published anyway

The reason `events.py` gives for the queue's three, unchanged: `OutboxRelay`
marks an entry no handler wanted as published and counts it separately, so
an unsubscribed event costs one row.

What makes it more than bookkeeping is that every consumer this epic needs
is already built and is waiting for exactly these. A64-022.2's realtime
frame, the notification that somebody has been challenged, and A64-022.3's
match creation are all subscribers to events on this list — and the producer
has to exist first, because a consumer added later has no record of the
challenges that happened before it.

## What a payload carries

The challenge's own durable facts: who, what settings, when. **No prose, no
usernames, no display names, no ratings.** A consumer that wants to say
"Aziz challenged you" composes that from `challenger_id` through
`profiles`, which is the module that owns names and the module that knows
whether the viewer may see one.

Self-contained, per `DomainEvent`: a consumer acting on
`matchmaking.challenge_declined` does not have to re-read a row, which
matters because the row is allowed to have been swept by then.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from app.modules.game.public import ProductVariant
from app.modules.reference.public import TimeControlId
from app.platform.events import DomainEvent

#: `database.md` §10.5's `aggregate_type` for every event below.
CHALLENGE_AGGREGATE = "challenge"


@dataclass(frozen=True)
class _ChallengeEvent(DomainEvent):
    """The identity all four share.

    A base class rather than four copies, for the reason `_QueueTicketEvent`
    gives: these are not four events that look alike, they are four
    transitions of one aggregate, so the identity and the two parties are the
    same fact in each.

    **Both player ids are on every payload.** A consumer's first act is to
    decide whom to tell, and re-deriving the other party would mean a lookup
    against a row a retention sweep is allowed to have removed.
    """

    aggregate_type: ClassVar[str] = CHALLENGE_AGGREGATE

    challenge_id: UUID
    challenger_id: UUID
    recipient_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.challenge_id

    def _parties(self) -> dict[str, Any]:
        return {
            "challenge_id": str(self.challenge_id),
            "challenger_id": str(self.challenger_id),
            "recipient_id": str(self.recipient_id),
        }


@dataclass(frozen=True)
class FriendChallengeCreated(_ChallengeEvent):
    """One player invited a friend to play.

    Carries the **settings** as well as the parties, because its consumers
    need them and a lookup would not do: A64-022.2's realtime frame shows the
    time control on the invitation, and a notification that said only
    "somebody challenged you" would make the recipient open the app to learn
    what they were being asked to play.

    `expires_at` is on the payload for the same reason `QueueTicketEnqueued`
    carries it — a consumer delivering this late needs to know whether the
    thing it is announcing is still answerable.
    """

    event_type: ClassVar[str] = "matchmaking.friend_challenge_created"

    time_control_id: TimeControlId
    variant: ProductVariant
    rated: bool
    expires_at: datetime

    def payload(self) -> dict[str, Any]:
        return {
            **self._parties(),
            "time_control_id": self.time_control_id.value,
            "variant": self.variant.value,
            "rated": self.rated,
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class FriendChallengeAccepted(_ChallengeEvent):
    """The recipient agreed, and a match exists — A64-022.3 §13.

    Carries `match_id`, which is the fact everything downstream needs and
    the one this event exists to publish: a consumer that only learned "a
    challenge was accepted" would have to go looking for the game.

    Carries the settings too, for the reason `FriendChallengeCreated` does —
    a notification saying "your challenge was accepted" is more useful for
    naming what will be played, and a consumer that had to read the row for
    it would be reading a row a retention sweep may remove.

    ## Ordering against `match.created`

    Both are staged in the same transaction and both are published by the
    same relay pass, so a consumer sees both or neither. Their **order
    within** that pass is the outbox's `occurred_at`, and `game` stamps the
    match a moment before this is built — so `match.created` arrives first.

    That ordering is not depended on by anything today and nothing here
    requires it. It is recorded because it is the ordering that exists, and
    a future consumer that does depend on it should find it written down
    rather than discovered.
    """

    event_type: ClassVar[str] = "matchmaking.friend_challenge_accepted"

    match_id: UUID
    time_control_id: TimeControlId
    variant: ProductVariant
    rated: bool

    def payload(self) -> dict[str, Any]:
        return {
            **self._parties(),
            "match_id": str(self.match_id),
            "time_control_id": self.time_control_id.value,
            "variant": self.variant.value,
            "rated": self.rated,
        }


@dataclass(frozen=True)
class FriendChallengeDeclined(_ChallengeEvent):
    """The recipient said no.

    No settings and no reason. The challenger is told their invitation was
    answered; what it was for is something they already know, and *why* is
    not something a decline carries — see `ChallengeStatus.DECLINED`.
    """

    event_type: ClassVar[str] = "matchmaking.friend_challenge_declined"

    def payload(self) -> dict[str, Any]:
        return self._parties()


@dataclass(frozen=True)
class FriendChallengeCancelled(_ChallengeEvent):
    """The challenger withdrew it.

    Its consumer is a *retraction*: a recipient looking at an invitation that
    no longer exists must stop seeing it, which is why this is published even
    though nobody is being told good news.
    """

    event_type: ClassVar[str] = "matchmaking.friend_challenge_cancelled"

    def payload(self) -> dict[str, Any]:
        return self._parties()


@dataclass(frozen=True)
class FriendChallengeExpired(_ChallengeEvent):
    """The window closed with no answer.

    Emitted by the sweep that writes the terminal row (A64-022.6), never by a
    read. That is the whole argument for having a sweep at all: a challenge
    that expired without an event is one no consumer can react to, so a stale
    invitation would sit on a recipient's screen until they reloaded.
    """

    event_type: ClassVar[str] = "matchmaking.friend_challenge_expired"

    def payload(self) -> dict[str, Any]:
        return self._parties()


__all__ = [
    "CHALLENGE_AGGREGATE",
    "FriendChallengeAccepted",
    "FriendChallengeCancelled",
    "FriendChallengeCreated",
    "FriendChallengeDeclined",
    "FriendChallengeExpired",
]
