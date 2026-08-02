"""`game`'s durable events — A64-015.4 §13.

Domain layer, framework-free, and owned by the context that owns the fact
(architecture.md §8). Each is written to the outbox in the **same
transaction** as the row it describes (AD-16), so an event exists exactly
when the thing it announces did — and a rollback takes both.

## Five events for one aggregate, and why they are not one

`MatchRecord` has four states and five transitions into or between them,
and a consumer acts differently on every one:

    match_created            a pairing produced a contest. The event a
                             notification gateway wakes on to tell two
                             people they have a match to answer
    match_accepted_by_player one side answered yes and the other has not.
                             The event that tells the *opponent* somebody
                             is waiting on them
    match_activated          both answered. The event `rating`,
                             `statistics` and the live-game transport will
                             all key on, because it is the first moment a
                             game exists to play
    match_declined           a decision
    match_acceptance_expired an absence

The last two are deliberately separate for the reason
`QueueTicketCancelled` and `QueueTicketExpired` are: a fair-play signal
counts declines, a queue-health metric counts expiries, and a single
"match_ended_before_it_started" would make every consumer re-derive which
happened from a payload field.

## Naming follows the platform, not the brief

A64-015.4 §13 lists these as `match.created`, `match.accepted_by_player`
and so on. They are spelled `game.match_created` here, because
`DomainEvent.event_type` is namespaced **by owning context** everywhere
else on this platform — `friends.player_blocked`,
`matchmaking.queue_ticket_enqueued`, `users.presence_online` — and an
operator filtering the outbox by producer is filtering on that prefix. A
sixth spelling convention for one module would cost more than the literal
match with the brief is worth.

## Nothing consumes these yet, and they are published anyway

`OutboxRelay` marks an entry no handler wanted as published and counts it
separately, so an unsubscribed event costs one row and nothing else — the
same choice `matchmaking.domain.events` records. What makes it more than
bookkeeping here is that acceptance is the last step before *delivery*: the
notification gateway A64-015.5 and AD-09 build is a consumer of exactly
these five, and adding the producer later would mean the platform has no
record of any pairing answered before it shipped.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from app.modules.engine import PlayerSide
from app.modules.game.public.variants import ProductVariant
from app.platform.events import DomainEvent

#: The `aggregate_type` every event here carries — domain-model.md §10.4's
#: central aggregate. One constant rather than five literals, because an
#: operator querying the outbox "by subject" is querying this exact string,
#: and `matchmaking.PlayersPaired` already uses it for the same subject.
MATCH_AGGREGATE = "match"


@dataclass(frozen=True)
class _MatchEvent(DomainEvent):
    """The identity all five share.

    A base class rather than five copies, and the line it draws is
    CLAUDE.md §2.7's: these are not five events that happen to look alike,
    they are five transitions of one aggregate, so the match and the
    pairing that produced it are the same fact in each.

    `pairing_id` is on every payload because a consumer that sees an event
    twice — a relay redelivery — can then tell it is one pairing rather
    than two, which is the same argument `PlayersPaired` records.
    """

    aggregate_type: ClassVar[str] = MATCH_AGGREGATE

    match_id: UUID
    pairing_id: UUID
    light_player_id: UUID
    dark_player_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.match_id

    def _match_payload(self) -> dict[str, Any]:
        return {
            "match_id": str(self.match_id),
            "pairing_id": str(self.pairing_id),
            "light_player_id": str(self.light_player_id),
            "dark_player_id": str(self.dark_player_id),
        }


@dataclass(frozen=True)
class MatchCreated(_MatchEvent):
    """A pairing produced a durable match, awaiting two answers.

    Carries the whole offer — variant, rated, both sides, the deadline —
    because a consumer telling two people they have a match to accept
    cannot re-read a row that may already have expired by the time the
    relay delivers this.
    """

    event_type: ClassVar[str] = "game.match_created"

    variant: ProductVariant
    rated: bool
    acceptance_deadline: datetime

    def payload(self) -> dict[str, Any]:
        return {
            **self._match_payload(),
            "variant": self.variant.value,
            "rated": self.rated,
            "acceptance_deadline": self.acceptance_deadline.isoformat(),
        }


@dataclass(frozen=True)
class MatchAcceptedByPlayer(_MatchEvent):
    """One side answered yes, and the match is still waiting on the other.

    **Not published when the second acceptance arrives** — that one is
    `MatchActivated`, which says strictly more. Emitting both would make
    every consumer of the first check whether the second is about to
    follow.
    """

    event_type: ClassVar[str] = "game.match_accepted_by_player"

    side: PlayerSide
    player_id: UUID
    """Who accepted. Redundant with `side` and the two ids above, and
    carried anyway: a consumer routing a notification wants the player, and
    resolving a side to an id is exactly the kind of re-derivation a
    self-contained payload exists to avoid."""

    def payload(self) -> dict[str, Any]:
        return {
            **self._match_payload(),
            "side": self.side.value,
            "player_id": str(self.player_id),
        }


@dataclass(frozen=True)
class MatchActivated(_MatchEvent):
    """Both players accepted; the contest may be played.

    The event every downstream context keys on. `rating` and `statistics`
    care about matches that were actually played, and this is the first
    moment one could be.
    """

    event_type: ClassVar[str] = "game.match_activated"

    variant: ProductVariant
    rated: bool

    def payload(self) -> dict[str, Any]:
        return {**self._match_payload(), "variant": self.variant.value, "rated": self.rated}


@dataclass(frozen=True)
class MatchDeclined(_MatchEvent):
    """A participant refused the pairing.

    Carries who declined, because that is the whole content of the event:
    a fair-play signal in somebody who declines nine offers in an hour is
    the first consumer this will have, and it cannot be computed from a
    match id.
    """

    event_type: ClassVar[str] = "game.match_declined"

    side: PlayerSide
    player_id: UUID

    def payload(self) -> dict[str, Any]:
        return {
            **self._match_payload(),
            "side": self.side.value,
            "player_id": str(self.player_id),
        }


@dataclass(frozen=True)
class MatchAcceptanceExpired(_MatchEvent):
    """The acceptance window closed with at least one side silent.

    `occurred_at` is the match's own `acceptance_deadline`, **not** the
    instant the reconciler noticed — the fact became true when the window
    closed, and the job's interval is an implementation detail of who
    observed it. The same choice `QueueTicketExpired` makes, and for the
    same reason: the outbox orders by `occurred_at` (database.md §12.5).
    """

    event_type: ClassVar[str] = "game.match_acceptance_expired"

    light_accepted: bool
    dark_accepted: bool
    """Which sides had answered when the window closed.

    Two booleans rather than none, because "neither showed up" and "one
    player was left waiting" are different products of the same event, and
    only the second is a disappointment worth acting on.
    """

    def payload(self) -> dict[str, Any]:
        return {
            **self._match_payload(),
            "light_accepted": self.light_accepted,
            "dark_accepted": self.dark_accepted,
        }


__all__ = [
    "MATCH_AGGREGATE",
    "MatchAcceptanceExpired",
    "MatchAcceptedByPlayer",
    "MatchActivated",
    "MatchCreated",
    "MatchDeclined",
]
