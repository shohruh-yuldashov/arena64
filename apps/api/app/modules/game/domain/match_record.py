"""`MatchRecord` — the durable half of a match, and the acceptance
handshake that brings it to life (A64-015.4).

Framework-free (architecture.md §8): no ORM, no HTTP, no clock. Every
instant arrives as an argument (AD-07), so the whole acceptance window is a
unit test that runs in a microsecond.

## Why this is not `Match`, and why that is not a duplication

domain-model.md §10.4 describes one aggregate. This module and
`game.domain.match` are two halves of it, split by *what a state machine is
about* rather than by convenience — and A64-015.4 §4 asks for exactly that
distinction to be stated rather than resolved by widening one enum:

    Match         the rules. Position, whose turn, ply number, move log,
                  result. Its `MatchStatus` answers "has a move been
                  played, and has the game ended". Every transition is a
                  consequence of the rules kernel.

    MatchRecord   the platform. Who is playing whom, under which rule set,
                  for a rating or not, on which sides, from which two
                  queue tickets. Its `MatchRecordStatus` answers "does this
                  contest exist and may it be played". Every transition is
                  a consequence of a *person* — accepting, declining, or
                  failing to answer.

The two never model the same fact. `MatchStatus.CREATED` means "no move has
been played", which a match only reaches once acceptance has already
succeeded; `MatchRecordStatus.PENDING_ACCEPTANCE` is the state *before*
that, in which no rules-bearing `Match` exists at all. Collapsing them
would give the rules kernel a state it cannot reason about, and the
platform a state `TerminalStateEvaluator` would have to skip.

They meet when live gameplay ships: an `ACTIVE` record is a contest whose
`Match` is `CREATED` and waiting for its first move. That is one row
gaining a position, not one enum gaining four members.

## Why it is frozen, unlike `Match`

`Match` is an entity that is loaded, mutated and saved — its transitions
return `None`. This one is a value that is compare-and-set into storage,
exactly like `QueueTicket`, and for the identical reason: every write here
is a **compare-and-set on `status`**, which needs the before and the after
as two values. A mutating `accept()` would leave the caller holding only
the after, and the repository would have to trust that the row still says
what it said when it was read — which under two players tapping at once it
does not.

## The acceptance window is one instant, shared with the queue

`acceptance_deadline` is not computed here. It is handed in by
`matchmaking`, which derives it from the same instant and the same
`MATCHMAKING_RESERVATION_TTL_SECONDS` it writes onto both reserved queue
tickets as `reserved_until` — so the reservation deadline and the
acceptance deadline are *the same value in two rows* rather than two timers
somebody has to keep in step. See `PairingService._claim`.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.engine import EngineVersion, PlayerSide
from app.modules.game.domain.exceptions import (
    AcceptanceWindowClosed,
    MatchNotPending,
    NotAMatchParticipant,
)
from app.modules.game.domain.variants import ProductVariant


class MatchRecordStatus(StrEnum):
    """Where a paired contest is in the platform's half of its life.

    Four states, which is what A64-015.4 §4 asks for and no more. Each is
    reached by a different actor, which is why none of them collapses into
    another:

        pending_acceptance  a pairing produced it; two people must answer
        active              both answered yes; the match may be played
        cancelled           one answered no
        expired             nobody answered in time
    """

    PENDING_ACCEPTANCE = "pending_acceptance"
    """Created by a pairing and waiting on its two players.

    **The state a newly paired match starts in, always.** A64-015.4 §4: a
    match must not become `active` until acceptance succeeds, and starting
    here rather than at `active` is what makes that structural instead of
    remembered.
    """

    ACTIVE = "active"
    """Both players accepted. The contest exists and may be played."""

    CANCELLED = "cancelled"
    """A participant declined.

    `cancelled` rather than `aborted`, and the vocabulary is
    system-design.md §3's own: it reserves `Cancelled` for "a challenge or
    a queue ticket that never became a match at all", and `Aborted` — which
    `game.domain.match.MatchStatus` uses — for a match that *started* and
    ended with no result. A declined pairing never started.
    """

    EXPIRED = "expired"
    """The acceptance window closed with at least one side silent.

    Distinct from `cancelled` even though both end a pairing with no game,
    and the distinction is the one a consumer acts on: a decline is a
    decision and an expiry is an absence. A fair-play signal counts the
    first; a queue-health metric counts the second.
    """

    @property
    def is_pending(self) -> bool:
        return self is MatchRecordStatus.PENDING_ACCEPTANCE

    @property
    def is_settled(self) -> bool:
        """Whether the acceptance handshake is over, however it ended."""
        return not self.is_pending


@dataclass(frozen=True, slots=True)
class MatchSeat:
    """One side's player, the ticket that put them there, and whether they
    have answered.

    A record per seat rather than four parallel fields on the match,
    because the three facts belong together: pairing the wrong ticket with
    the wrong player by index is exactly the mistake
    `game.public.MatchParticipant` already refuses to make possible, and
    `accepted_at` is a property of the same person.
    """

    player_id: UUID
    """DM-06's opaque cross-context identifier. `game` cannot resolve it to
    a person and does not need to."""

    queue_ticket_id: UUID
    """Which queue ticket this player arrived on.

    Provenance, and the **durable link back to the pairing** A64-015.3
    recorded as missing: it is what lets a reconciler holding an orphaned
    reserved ticket find out whether its match was ever created, without
    having to know who the partner was.
    """

    accepted_at: datetime | None = None
    """When this side accepted, or `None` while they have not.

    An instant rather than a boolean, because "when" is the fact and "did"
    is derivable from it — and how long players take to accept is the first
    number anybody tuning the window will ask for.
    """

    @property
    def has_accepted(self) -> bool:
        return self.accepted_at is not None

    def accepting(self, at: datetime) -> "MatchSeat":
        return MatchSeat(
            player_id=self.player_id, queue_ticket_id=self.queue_ticket_id, accepted_at=at
        )


@dataclass(frozen=True, slots=True)
class MatchRecord:
    """One paired contest, as the platform records it."""

    pairing_id: UUID
    """The idempotency key this match was created under — A64-015.4 §3.

    Derived by `matchmaking` from the two claimed ticket ids and stable
    across retries, and carried here because a **unique index on this
    column** is what makes "one pairing, one match" hold under two workers.
    See `game.public.matches` for the contract and
    `game.infrastructure.models` for the constraint.
    """

    variant: ProductVariant
    rated: bool
    """Whether finishing this match moves a rating."""

    engine_version: EngineVersion
    """The rules build this match was created under — AD-15, and immutable
    after creation by MT-3."""

    light: MatchSeat
    """Moves first (`PlayerSide.LIGHT`)."""

    dark: MatchSeat

    created_at: datetime
    acceptance_deadline: datetime
    """When an unanswered pairing stops being offered.

    Absolute rather than a duration, for the reason `QueueTicket.expires_at`
    is: a match written under one reservation TTL must not be silently
    re-dated by a deploy that changes it. Equal by construction to both
    tickets' `reserved_until` — see this module's docstring.
    """

    status: MatchRecordStatus = MatchRecordStatus.PENDING_ACCEPTANCE

    declined_by: PlayerSide | None = None
    """Which side declined, or `None`. Set exactly when `status` is
    `cancelled` — a database CHECK enforces the same pairing (BE-06)."""

    settled_at: datetime | None = None
    """When the handshake ended, and `None` while it has not.

    One column for the three ways out rather than `activated_at`,
    `cancelled_at` and `expired_at`: `status` already says *which*, and
    three nullable instants of which at most one is ever set is three
    places for the pairing to be got wrong.
    """

    id: UUID = field(default_factory=generate_uuid7)
    """UUIDv7, application-generated (DB-07). Last so every other field can
    be passed positionally by the repository's rehydration."""

    def __post_init__(self) -> None:
        # Re-checked here rather than only at creation, because the
        # repository constructs instances directly when rehydrating — this
        # is what makes a corrupt row fail at the boundary rather than
        # reach a response. The database's own CHECK constraints are the
        # authoritative copies (BE-06).
        if self.light.player_id == self.dark.player_id:
            raise ValueError("a match needs two different players")
        if self.light.queue_ticket_id == self.dark.queue_ticket_id:
            raise ValueError("a match needs two different queue tickets")
        if self.acceptance_deadline <= self.created_at:
            raise ValueError("an acceptance window cannot close before it opens")
        if self.status.is_settled != (self.settled_at is not None):
            raise ValueError("settled_at is set exactly when the match is no longer pending")
        if (self.declined_by is not None) != (self.status is MatchRecordStatus.CANCELLED):
            raise ValueError("declined_by is set exactly when the match was cancelled")
        if self.status is MatchRecordStatus.ACTIVE and not (
            self.light.has_accepted and self.dark.has_accepted
        ):
            raise ValueError("an active match has been accepted by both players")

    @property
    def is_pending(self) -> bool:
        return self.status.is_pending

    def seat(self, side: PlayerSide) -> MatchSeat:
        return self.light if side is PlayerSide.LIGHT else self.dark

    def side_of(self, player_id: UUID) -> PlayerSide:
        """Which side `player_id` was assigned.

        Raises `NotAMatchParticipant` for anybody else, which is the whole
        of "a player cannot accept on behalf of the opponent" (§6): the
        side is *derived* from the caller's own identifier, so a request
        naming a side is not something this domain can express.
        """
        if player_id == self.light.player_id:
            return PlayerSide.LIGHT
        if player_id == self.dark.player_id:
            return PlayerSide.DARK
        raise NotAMatchParticipant("You are not a participant in that match.")

    def player_ids(self) -> tuple[UUID, UUID]:
        """Both players, light first."""
        return (self.light.player_id, self.dark.player_id)

    def ticket_ids(self) -> tuple[UUID, UUID]:
        """Both source queue tickets, light first."""
        return (self.light.queue_ticket_id, self.dark.queue_ticket_id)

    def opponent_of(self, player_id: UUID) -> UUID:
        """The other player. Raises `NotAMatchParticipant` for a stranger."""
        return self.seat(self.side_of(player_id).opponent()).player_id

    def accepted_by(self, side: PlayerSide, *, at: datetime) -> "MatchRecord":
        """This match with `side` recorded as accepting.

        **Idempotent for a repeat from the same side** (§6): a client that
        retries after a dropped response gets the match back unchanged
        rather than an error, because the outcome it asked for is already
        true. That is the same reasoning `DELETE /matchmaking/queue`
        applies, and it matters more here — a duplicate accept arriving
        after the *opponent* accepted must not be able to un-activate an
        active match.

        Activates when both sides have answered, in the same value: there
        is no window in which one side has accepted and the caller has yet
        to notice both have.

        Raises `MatchNotPending` once the handshake is over and
        `AcceptanceWindowClosed` past the deadline.
        """
        if self.seat(side).has_accepted and self.status is MatchRecordStatus.ACTIVE:
            return self
        self._require_open(at)
        if self.seat(side).has_accepted:
            return self

        seat = self.seat(side).accepting(at)
        accepted = self._with(light=seat) if side is PlayerSide.LIGHT else self._with(dark=seat)
        if not (accepted.light.has_accepted and accepted.dark.has_accepted):
            return accepted
        return accepted._with(status=MatchRecordStatus.ACTIVE, settled_at=at)

    def declined(self, side: PlayerSide, *, at: datetime) -> "MatchRecord":
        """This match, cancelled because `side` said no.

        **One decline ends it** (§6), whatever the other side did: a match
        two people were offered and one refused is not a match, and keeping
        the accepting player's `accepted_at` is deliberate — it is the
        record that says which of the two was left waiting.

        Raises `MatchNotPending` once the handshake is over and
        `AcceptanceWindowClosed` past the deadline, so a decline racing an
        expiry loses to whichever was recorded first.
        """
        self._require_open(at)
        return self._with(status=MatchRecordStatus.CANCELLED, declined_by=side, settled_at=at)

    def expired(self, at: datetime) -> "MatchRecord":
        """This match, abandoned because the window closed.

        No deadline check: this is the transition the deadline *causes*,
        and requiring `at` to be past it here would make the reconciler
        assert its own precondition twice. `MatchNotPending` still applies
        — an expiry cannot overwrite an activation.
        """
        if not self.is_pending:
            raise MatchNotPending("That match is no longer awaiting acceptance.")
        return self._with(status=MatchRecordStatus.EXPIRED, settled_at=at)

    def _require_open(self, at: datetime) -> None:
        if not self.is_pending:
            raise MatchNotPending("That match is no longer awaiting acceptance.")
        if at > self.acceptance_deadline:
            raise AcceptanceWindowClosed("The window to answer that match has closed.")

    def _with(
        self,
        *,
        light: MatchSeat | None = None,
        dark: MatchSeat | None = None,
        status: MatchRecordStatus | None = None,
        declined_by: PlayerSide | None = None,
        settled_at: datetime | None = None,
    ) -> "MatchRecord":
        """This match with some fields changed and the rest carried across.

        `None` means "unchanged" rather than "cleared", which is safe here
        because none of the five fields is ever *un*-set: a seat only gains
        an `accepted_at`, and `status`, `declined_by` and `settled_at` are
        written once when the handshake ends. A transition that had to
        clear one would be a transition this state machine does not have.

        `id`, `pairing_id` and the six creation-time facts are carried
        verbatim, which is what makes them immutable without a guard.
        """
        return MatchRecord(
            id=self.id,
            pairing_id=self.pairing_id,
            variant=self.variant,
            rated=self.rated,
            engine_version=self.engine_version,
            light=light if light is not None else self.light,
            dark=dark if dark is not None else self.dark,
            created_at=self.created_at,
            acceptance_deadline=self.acceptance_deadline,
            status=status if status is not None else self.status,
            declined_by=declined_by if declined_by is not None else self.declined_by,
            settled_at=settled_at if settled_at is not None else self.settled_at,
        )


__all__ = ["MatchRecord", "MatchRecordStatus", "MatchSeat"]
