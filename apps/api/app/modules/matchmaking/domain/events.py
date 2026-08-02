"""The three queue events — A64-014.1.

Domain layer, framework-free, and owned by the context that owns the fact
(architecture.md §8). Each is written to the outbox in the same transaction
as the ticket it describes (AD-16), so an event exists exactly when the
thing it announces did.

## Nothing consumes these yet, and they are published anyway

`OutboxRelay` marks an entry no handler wanted as published and counts it
separately, so an unsubscribed event costs one row and nothing else. The
same choice `friends` made for `PlayerBlocked`, whose notification audience
is empty by rule, and for the same reason: "suppressing the event because
one consumer must not act on it would be deciding a subscriber's policy at
the producer."

What makes it more than bookkeeping here is that matchmaking's whole future
is event-driven. A64-014.2's pairing worker, the notification that a queue
is taking unusually long, and the fair-play signal in a player who queues
and cancels forty times an hour are all consumers of exactly these three.
Adding the producer later would mean the platform has no record of any
queueing that happened before it.

## What a payload carries, and what it deliberately does not

The ticket's own facts — who, which pool, what rating, when — and no
usernames, no display names, no ratings *as they are now*. Two rules, both
already established:

  - A payload is self-contained (`DomainEvent`), so a consumer acting on
    `matchmaking.queue_ticket_expired` does not have to re-read a row that
    has since been pruned.
  - `rating_snapshot` is the number the ticket recorded at entry (QT-2),
    not a live reading. An event carrying "their rating" would be a second
    place that decision could be got wrong.

`region` and `queue_type` are on every payload because a consumer's first
act is almost always to route by pool — a pairing worker subscribes to one,
a metric is per-pool — and re-deriving it would mean a lookup against a row
that is allowed to be gone.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

from app.modules.game.public import ProductVariant
from app.modules.matchmaking.domain.queue_pool import QueueType, Region
from app.platform.events import DomainEvent

#: The `aggregate_type` every event here carries — domain-model.md §10.2's
#: aggregate root. One constant rather than three literals, because an
#: operator querying the outbox "by subject" is querying this exact string.
QUEUE_TICKET_AGGREGATE = "queue_ticket"


@dataclass(frozen=True)
class _QueueTicketEvent(DomainEvent):
    """The fields all three share, and the `aggregate_id` all three answer
    the same way.

    A base class rather than three copies, and the line it draws is the one
    CLAUDE.md §2.7 asks for: these are not three events that happen to look
    alike, they are three transitions of one aggregate, so the identity and
    the pool are the same fact in each. What is *not* hoisted is
    `payload()` — see `QueueTicketCancelled`, whose body differs.
    """

    aggregate_type: ClassVar[str] = QUEUE_TICKET_AGGREGATE

    ticket_id: UUID
    player_id: UUID
    variant: ProductVariant
    """Which rule set the pool plays — A64-015.2.

    On the payload for the same reason `queue_type` and `region` are: a
    consumer routes by pool, and a pairing worker for one variant must be
    able to ignore an event for another without a lookup.
    """

    queue_type: QueueType
    region: Region

    @property
    def aggregate_id(self) -> UUID:
        return self.ticket_id

    def _ticket_payload(self) -> dict[str, Any]:
        return {
            "ticket_id": str(self.ticket_id),
            "player_id": str(self.player_id),
            "variant": self.variant.value,
            "queue_type": self.queue_type.value,
            "region": self.region.value,
        }


@dataclass(frozen=True)
class QueueTicketEnqueued(_QueueTicketEvent):
    """A player entered a pool.

    The event A64-014.2's pairing worker wakes on. It carries
    `rating_snapshot` and `expires_at` because a pairing pass needs both to
    decide anything — the rating to find a neighbour, the deadline to know
    whether the ticket is still worth pairing by the time the event is
    delivered.
    """

    event_type: ClassVar[str] = "matchmaking.queue_ticket_enqueued"

    rating_snapshot: int
    expires_at: datetime

    def payload(self) -> dict[str, Any]:
        return {
            **self._ticket_payload(),
            "rating_snapshot": self.rating_snapshot,
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class QueueTicketCancelled(_QueueTicketEvent):
    """A player left a pool of their own accord.

    Distinct from `QueueTicketExpired` even though both end a wait with no
    match, and the distinction is the one a consumer acts on: a cancellation
    is a decision, an expiry is an absence. A future "your search is taking
    a while" notification must fire on neither, a fair-play signal counts
    only the first, and a queue-health metric counts only the second.

    Carries `waited_for_seconds` because how long somebody was prepared to
    wait before giving up is the number that says whether a pool is too
    thin — and it is derivable only from two timestamps a consumer would
    otherwise have to hold both of.
    """

    event_type: ClassVar[str] = "matchmaking.queue_ticket_cancelled"

    waited_for_seconds: float

    def payload(self) -> dict[str, Any]:
        return {**self._ticket_payload(), "waited_for_seconds": self.waited_for_seconds}


@dataclass(frozen=True)
class QueueTicketExpired(_QueueTicketEvent):
    """A ticket's window closed with no match.

    `occurred_at` is the ticket's `expires_at`, **not** the instant the
    sweep noticed — the fact became true when the window closed, and the
    sweeper's interval is an implementation detail of who observed it. The
    same choice `PresenceSweeper` makes for a lapsed presence record, and
    for the same reason: the outbox orders by `occurred_at` (database.md
    §12.5), so a batch of expiries drains in the order the tickets actually
    lapsed rather than in the order one query returned them.
    """

    event_type: ClassVar[str] = "matchmaking.queue_ticket_expired"

    waited_for_seconds: float

    def payload(self) -> dict[str, Any]:
        return {**self._ticket_payload(), "waited_for_seconds": self.waited_for_seconds}


@dataclass(frozen=True)
class PlayersPaired(DomainEvent):
    """A scan turned two tickets into a match — A64-015.3.

    ## Why one event and not two `queue_ticket_matched`

    A pairing is one fact about two tickets, and every consumer of it needs
    both halves: a notification tells two players, a metric records one
    match, a fair-play signal looks at who was paired with whom. Two
    per-ticket events would make every consumer join them back together,
    and the first one to act on a half-delivered pair would announce a
    match to one player.

    That is the opposite of the argument the three events above make, and
    the difference is real: enqueued, cancelled and expired are each one
    ticket's whole story. This one is not.

    ## Its aggregate is the match, not a ticket

    `aggregate_id` is the `match_id`, because that is the subject an
    operator querying the outbox is looking for and the identifier every
    downstream context (rating, statistics, replay) will key on. The two
    ticket ids are payload — provenance rather than identity.

    Published **after** `game` accepted the match request, in the same
    transaction as the two `matched` transitions (AD-16). A pairing that
    was compensated emits nothing: nothing durable happened, and an event
    announcing a match that was rolled back is worse than silence.
    """

    event_type: ClassVar[str] = "matchmaking.players_paired"
    aggregate_type: ClassVar[str] = "match"

    match_id: UUID
    pairing_id: UUID
    """The idempotency key the match was created under. On the payload so a
    consumer that sees this event twice — a relay redelivery — can tell it
    is one pairing rather than two."""

    variant: ProductVariant
    queue_type: QueueType
    region: Region

    light_player_id: UUID
    dark_player_id: UUID
    light_ticket_id: UUID
    dark_ticket_id: UUID

    waited_for_seconds: float
    """How long the **longer-waiting** of the two had been in the pool.

    One number rather than two, because the question it answers is about
    the pool rather than about a player: how long did this pool take to
    produce a match. The longer of the pair is the honest figure — the
    match could not have happened before it.
    """

    @property
    def aggregate_id(self) -> UUID:
        return self.match_id

    def payload(self) -> dict[str, Any]:
        return {
            "match_id": str(self.match_id),
            "pairing_id": str(self.pairing_id),
            "variant": self.variant.value,
            "queue_type": self.queue_type.value,
            "region": self.region.value,
            "light_player_id": str(self.light_player_id),
            "dark_player_id": str(self.dark_player_id),
            "light_ticket_id": str(self.light_ticket_id),
            "dark_ticket_id": str(self.dark_ticket_id),
            "waited_for_seconds": self.waited_for_seconds,
        }


class ReconciliationAction(StrEnum):
    """What a reconciler did with one stranded reservation — A64-015.4 §9.

    Three actions, and they are the three durable states a lapsed
    reservation can be in. An operator reading this enum in the outbox is
    reading a direct account of which failure happened:

        settled     a match exists and the ticket had not caught up. The
                    ordinary crash: `game` committed and the worker died
                    before it could mark the tickets matched.
        released    no match was created and the ticket's own window is
                    still open, so the player goes back in line with the
                    `entered_at` they always had.
        expired     no match, and the ticket's window closed while it was
                    reserved. Releasing it would put a ticket back into
                    `waiting` past its own deadline.
    """

    SETTLED = "settled"
    RELEASED = "released"
    EXPIRED = "expired"


@dataclass(frozen=True)
class PairingReconciled(DomainEvent):
    """A stranded reservation was resolved without a human — A64-015.4 §9.

    ## Why this is published at all

    Nothing subscribes to it, and it is the one event on this platform
    whose primary reader is an **operator** rather than a consumer. A
    reconciliation is by definition evidence that something else failed
    halfway — a worker died between two transactions, a pairing lost a race
    with the expiry sweep — and the durable record of how often that
    happens is what turns "we think the recovery path works" into a number.

    A64-015.3 shipped the same information as a `pairing_settle_failed` log
    line at `ERROR`, with a human on the end of it. This is the replacement,
    and it is deliberately an event rather than a metric: the *reason* a
    given pairing was reconciled is only reconstructible from the ticket,
    the match and the action together, and a counter loses all three.

    ## Its aggregate is the ticket

    One event per **reservation**, not per pairing. The reconciler claims
    whatever bounded batch it locks and may well see one half of a pair
    without the other, so a per-pairing event would either be published
    twice or wait for a partner that another worker has already handled.
    `PlayersPaired` makes the opposite choice for the opposite reason —
    there, both halves are always in hand.
    """

    event_type: ClassVar[str] = "matchmaking.pairing_reconciled"
    aggregate_type: ClassVar[str] = QUEUE_TICKET_AGGREGATE

    ticket_id: UUID
    player_id: UUID
    action: ReconciliationAction

    match_id: UUID | None
    """The match this ticket turned out to have, or `None` when it had
    none. Non-null exactly when `action` is `settled`."""

    reserved_until: datetime
    """The deadline the reservation overran. Carried because "how far past
    its window did this sit" is the number that says whether the reconciler
    is running often enough."""

    @property
    def aggregate_id(self) -> UUID:
        return self.ticket_id

    def payload(self) -> dict[str, Any]:
        return {
            "ticket_id": str(self.ticket_id),
            "player_id": str(self.player_id),
            "action": self.action.value,
            "match_id": None if self.match_id is None else str(self.match_id),
            "reserved_until": self.reserved_until.isoformat(),
        }
