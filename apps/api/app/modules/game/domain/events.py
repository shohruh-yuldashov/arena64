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
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.domain.variants import MatchOrigin, ProductVariant
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

    ## The two queue ticket ids, added by A64-015.5

    Provenance, and the reason it has to be *on the event* rather than
    looked up: A64-015.5 §1's acceptance-failure policy is enforced by a
    `matchmaking` consumer that reacts to a decline or an expiry by putting
    the player who accepted back in the queue, **with the `entered_at` they
    always had**. That means finding the original ticket, and the only
    durable link from a match to its tickets lives in `game`'s own table —
    which a consumer in another module must not read (R-1).

    Carrying them is additive and needs no `event_version` bump
    (`DomainEvent`): a consumer written before this change ignores two
    fields it does not know about.
    """

    aggregate_type: ClassVar[str] = MATCH_AGGREGATE

    match_id: UUID
    pairing_id: UUID
    light_player_id: UUID
    dark_player_id: UUID
    light_ticket_id: UUID
    dark_ticket_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.match_id

    def _match_payload(self) -> dict[str, Any]:
        return {
            "match_id": str(self.match_id),
            "pairing_id": str(self.pairing_id),
            "light_player_id": str(self.light_player_id),
            "dark_player_id": str(self.dark_player_id),
            "light_ticket_id": str(self.light_ticket_id),
            "dark_ticket_id": str(self.dark_ticket_id),
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

    It also carries **who had already accepted**, added by A64-015.5. That
    is what A64-015.5 §1's policy turns on: the participant who said yes and
    lost the match to somebody else's refusal is requeued with their
    original place in line, and the one who refused is not. A consumer that
    had to re-read the match to learn which was which would be reading
    `game`'s table from another module.
    """

    event_type: ClassVar[str] = "game.match_declined"

    side: PlayerSide
    player_id: UUID

    light_accepted: bool
    dark_accepted: bool
    """Which sides had answered when the decline landed.

    The decliner's own flag is `False` by construction — a player who
    accepted and then declined is refused by `MatchRecord.declined`, which
    only leaves `pending_acceptance` once. So at most one of these is
    `True`, and it names the player the policy owes a requeue.
    """

    def payload(self) -> dict[str, Any]:
        return {
            **self._match_payload(),
            "side": self.side.value,
            "player_id": str(self.player_id),
            "light_accepted": self.light_accepted,
            "dark_accepted": self.dark_accepted,
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


@dataclass(frozen=True)
class MoveApplied(DomainEvent):
    """One move was played and durably logged — A64-016.4 §10.

    Published per ply, which makes it the highest-volume event on the
    platform by a wide margin: one row per move of every game, where
    `MatchCreated` is one per pairing. That is deliberate and is what the
    outbox is for — a consumer rebuilding a projection, a fair-play
    analyser, or the spectator feed all need the sequence rather than a
    summary, and none of them can reconstruct it from a completion event.

    **Carries no board.** The path and the resulting fingerprint are enough
    for a consumer to follow along or to detect divergence; a position per
    move would multiply the outbox's largest stream by the size of a board.
    """

    event_type: ClassVar[str] = "game.move_applied"
    aggregate_type: ClassVar[str] = MATCH_AGGREGATE

    match_id: UUID
    ply_number: int
    side: PlayerSide
    path: tuple[str, ...]
    """The squares the piece occupied, in order — never an origin and a
    destination. Two capture routes can share endpoints, so the pair is
    lossy and a consumer replaying from it would reconstruct a different
    game."""

    resulting_position_hash: str

    @property
    def aggregate_id(self) -> UUID:
        return self.match_id

    def payload(self) -> dict[str, Any]:
        return {
            "match_id": str(self.match_id),
            "ply_number": self.ply_number,
            "side": self.side.value,
            "path": list(self.path),
            "resulting_position_hash": self.resulting_position_hash,
        }


@dataclass(frozen=True)
class SeatSummary:
    """One seat on the completion event — who played, and what they rated.

    Primitive-only, because it is serialised into an outbox row and read
    back by a different process. The rating is the **snapshot captured at
    match creation** (SPEC-RATING §7.6), never a current value: carrying
    the current one would make the consumer's arithmetic depend on when the
    relay happened to run.
    """

    player_id: UUID
    rating_value: float
    rating_deviation: float
    rating_volatility: float
    games_played: int
    is_provisional: bool


def _seat_payload(seat: "SeatSummary | None") -> dict[str, Any] | None:
    if seat is None:
        return None
    return {
        "player_id": str(seat.player_id),
        "rating_value": seat.rating_value,
        "rating_deviation": seat.rating_deviation,
        "rating_volatility": seat.rating_volatility,
        "games_played": seat.games_played,
        "is_provisional": seat.is_provisional,
    }


@dataclass(frozen=True)
class MatchCompleted(DomainEvent):
    """A match was played to an end — A64-016.4 §6.

    The event `rating` and `statistics` key on, and the first on this
    platform that says a *game* happened rather than a pairing. `rated`
    travels with it for exactly that reason: a consumer deciding whether to
    move a rating must not have to read the match back, because by the time
    a relay delivers this the row may have been archived.

    Emitted on the ply that ends the game and on no other, so a consumer
    counting completions counts games.
    """

    event_type: ClassVar[str] = "game.match_completed"
    aggregate_type: ClassVar[str] = MATCH_AGGREGATE

    match_id: UUID
    variant: ProductVariant
    rated: bool
    outcome: MatchOutcome
    termination_reason: TerminationReason
    winner: PlayerSide | None
    """`None` for a draw and for an aborted match. Never `None` for a win —
    the pairing is `MatchResult`'s invariant and a database `CHECK`."""

    ply_number: int
    """How long the game was. Carried because "games are ending in four
    plies" is an incident and reading it from a completion event is the
    only way to see it without joining the move log."""

    # --- A64-017.3: everything `rating` needs, so it reads nothing back ---
    #
    # Added **additively**: every field above is unchanged and every
    # existing consumer keeps working (`services.md` §10.2 — payloads are
    # bounded and self-contained). What they buy is that the rating
    # consumer never re-reads the match, which matters because by the time
    # a relay delivers this the row may have been archived — and because
    # re-reading is how a calculation ends up using a *current* rating
    # instead of the seat snapshot PR-3 requires.
    engine_version: int = 0
    """The rules build this match was played under — AD-15.

    Carried rather than inferred: a consumer that assumed the current build
    would mis-explain any match played across an upgrade."""

    light: "SeatSummary | None" = None
    dark: "SeatSummary | None" = None
    """The two seats, each with the rating snapshot captured at creation.

    `None` for a match created before A64-017.2, which has no snapshot and
    therefore cannot be rated — see `SeatSummary`."""

    speed_class: str | None = None
    """The rating key's second component. `None` for the same reason."""

    # --- A64-019.5: the round trip R-25 promised, completed ---
    #
    # A64-019.0 gave `game.match` an `origin` and an opaque `origin_ref` so a
    # tournament could recognise its own matches. It handed neither back:
    # the columns were written and the completion event did not carry them,
    # so the originating context saw a match end and could not tell it was
    # one of its own. The mechanism `services.md` §11.3 assumed existed was
    # therefore still half absent — this is the other half.
    #
    # Additive with defaults, like the block above: every consumer written
    # before this ignores two fields it does not know about, and a match
    # recorded before A64-019.0 correctly reads as having come from the
    # queue.
    origin: MatchOrigin = MatchOrigin.QUEUE
    origin_ref: UUID | None = None
    """The originating context's own identifier. **Opaque to `game`** — it
    is stored, echoed here, and never dereferenced."""

    @property
    def aggregate_id(self) -> UUID:
        return self.match_id

    def payload(self) -> dict[str, Any]:
        return {
            "match_id": str(self.match_id),
            "variant": self.variant.value,
            "rated": self.rated,
            "outcome": self.outcome.value,
            "termination_reason": self.termination_reason.value,
            "winner": self.winner.value if self.winner is not None else None,
            "ply_number": self.ply_number,
            "engine_version": self.engine_version,
            "speed_class": self.speed_class,
            "light": _seat_payload(self.light),
            "dark": _seat_payload(self.dark),
            "origin": self.origin.value,
            "origin_ref": str(self.origin_ref) if self.origin_ref is not None else None,
        }


__all__ = [
    "MATCH_AGGREGATE",
    "SeatSummary",
    "MatchAcceptanceExpired",
    "MatchAcceptedByPlayer",
    "MatchActivated",
    "MatchCompleted",
    "MatchCreated",
    "MatchDeclined",
    "MoveApplied",
]
