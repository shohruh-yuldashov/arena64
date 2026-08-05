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

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.engine import EngineVersion, PlayerSide
from app.modules.game.domain.clock import ClockState, TimeControl
from app.modules.game.domain.draw_agreement import DrawAgreement, DrawOffer
from app.modules.game.domain.exceptions import (
    AcceptanceWindowClosed,
    DrawOfferAlreadyPending,
    DrawOfferNotAllowedYet,
    DrawOfferNotPending,
    DrawOfferNotRecipient,
    InvalidMatchTransition,
    MatchNotActive,
    MatchNotPending,
    NotAMatchParticipant,
)
from app.modules.game.domain.match import agreed_draw_result, resignation_result
from app.modules.game.domain.result import MatchOutcome, MatchResult, TerminationReason
from app.modules.game.domain.variants import MatchOrigin, ProductVariant


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

    COMPLETED = "completed"
    """The contest was played to an end — A64-016.4 §6.

    Reached from `active` alone, by a move whose resulting position the
    terminal evaluator or the draw rules declared final. Terminal, and
    permanently so: MT-10 makes a completed match a permanent record, and
    §6's "reject all later move submissions" is that rule applied to the
    live path.

    Distinct from `cancelled` and `expired`, which are the two ways a
    pairing ends *without being played*. Collapsing them would make
    "matches that were games" unanswerable, and it is the exact predicate
    retention uses to decide what it may delete — `ix_match__abandoned`
    excludes this status, so a completed match with its move log is
    structurally out of reach of the queue's sweep.
    """

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
class SeatRating:
    """The Glicko-2 triple a seat carries — the domain's own copy.

    `game.public.SeatRating` restated inside the domain for the reason every
    published type is: the port is a contract with `matchmaking`, and the
    aggregate must not hold a type whose shape another module's callers
    decide.

    Frozen, and never refreshed. It is a fact about the past.
    """

    value: float
    deviation: float
    volatility: float
    games_played: int
    is_provisional: bool
    speed_class: str


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

    queue_ticket_id: UUID | None = None
    """Which queue ticket this player arrived on, or `None` — R-25.

    Provenance, and the **durable link back to the pairing** A64-015.3
    recorded as missing: it is what lets a reconciler holding an orphaned
    reserved ticket find out whether its match was ever created, without
    having to know who the partner was.

    **`None` when the match did not come from the queue.** A tournament
    pairing, a challenge and a rematch each produce a match and none of them
    produces a ticket. A64-019.5 wrote a derived uuid5 here to satisfy a
    `NOT NULL`, which made the column assert a ticket existed when none did
    — a fabricated fact in a permanent record, and one `settlements_for`
    would happily answer questions about.

    Where a match came from is `origin` and `origin_ref`; this is only the
    queue's provenance. A consumer that needs a ticket asks for
    `MatchOrigin.QUEUE`, which is what guarantees one.
    """

    rating: SeatRating | None = None
    """What this player rated when the match was created — MT-4.

    Written once, at creation, and never updated: PR-3 requires the rating
    calculation to run on the values captured before the game was played.
    `game` stores it and hands it back on completion; it never computes one.

    `None` for a match created before A64-017.2, and for any created without
    a snapshot. Such a match cannot be rated, which is correct — nothing
    rated it.
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
        """This seat, having answered yes.

        `replace` rather than a fresh `MatchSeat`, and the difference is a
        rating outage. Naming three of four fields silently dropped
        `rating` — the snapshot captured at creation (MT-4, PR-3) — and
        `MatchRecordRepository.settle` writes every rating column from the
        record it is given, so accepting a match **nulled its own seat
        snapshots in the database**. `MatchCompleted` then carried
        `light=None, dark=None`, the rating consumer correctly read that as
        "not rateable", and no rating on this platform would ever have
        moved.

        Constructing by name is what let a field be forgotten. `replace`
        cannot forget one, so a fifth field added later inherits this
        instead of needing to be remembered here.
        """
        return replace(self, accepted_at=at)


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

    origin: MatchOrigin = MatchOrigin.QUEUE
    """Where this match came from — R-25. Defaults to `QUEUE`, which is what
    every match created before A64-019.0 was."""

    origin_ref: UUID | None = None
    """The originating context's own identifier, **opaque**.

    A tournament pairing, a challenge, a rematch offer. `game` never
    dereferences it and there is no foreign key (DB-03): a constraint here
    would make `game` and `tournaments` undeployable apart, which is the one
    seam `architecture.md` §16 exists to keep open.
    """

    status: MatchRecordStatus = MatchRecordStatus.PENDING_ACCEPTANCE

    declined_by: PlayerSide | None = None
    """Which side declined, or `None`. Set exactly when `status` is
    `cancelled` — a database CHECK enforces the same pairing (BE-06)."""

    ply_number: int = 0
    """How many moves have been played — A64-016.4 §3.

    The **authoritative sequence**, and the column two concurrent moves
    contend on. `0` for a match nobody has moved in, which is also every
    match this platform created before A64-016.4.

    Kept here as well as being derivable from the move log because it is
    the value the transaction reads under a row lock to decide which ply a
    submission becomes — deriving it would mean a `COUNT(*)` or a `MAX()`
    over the log inside every move's critical section.
    """

    time_control: TimeControl | None = None
    """How much time each side gets, or `None` for an untimed match.

    `None` today for every match, because `reference.time_control` does not
    exist and `matchmaking` therefore cannot supply one — see
    `game.domain.clock`. An untimed match runs the whole clock machinery not
    at all: no deadline, no flag, and null clock columns on its moves.
    """

    clock: ClockState | None = None
    """The clock as of the last move, or `None` for an untimed match.

    Set exactly when `time_control` is, which a database `CHECK` enforces as
    well as this record (BE-06): a match with a budget and no clock would be
    one nothing could adjudicate, and a clock with no budget would be one
    nothing could credit.

    The **version is `ply_number`**, not a field here — see
    `game.domain.clock` on why a second sequence would be a second thing to
    keep in step.
    """

    outcome: MatchOutcome | None = None
    termination_reason: TerminationReason | None = None
    winner: PlayerSide | None = None
    """The result, or `None` while the match is not completed — §6.

    Three columns rather than one, because the domain's `MatchResult` is a
    value with an invariant (`winner` is set exactly for a win) and storing
    it as a blob would put that invariant beyond a `CHECK`. Rehydrated into
    a `MatchResult` by `result`, so nothing above this layer handles the
    three separately.

    DM-08's reasoning applies unchanged: there is no "pending" sentinel,
    because a sentinel is what the code computing a rating forgets to
    check.
    """

    ended_at: datetime | None = None
    """When the contest ended, and `None` while it has not — §6.

    Distinct from `settled_at`, which is when the *handshake* ended. For a
    played match `settled_at` is when both players accepted and `ended_at`
    is when the last move was made; collapsing them would make "how long
    did this game take" unanswerable.
    """

    draw_agreement: DrawAgreement = field(default_factory=DrawAgreement)
    """The standing draw offer and the re-offer restrictions —
    A64-020.5C-pre §2.

    Durable, because §1 requires an offer to survive a process restart, a
    socket reconnect and a page refresh. Here rather than on `Match`
    because `Match` is replayed from the move log and no move records an
    offer — see `game.domain.draw_agreement` for the full argument.

    Defaulted rather than nullable: "no offer, no restriction" is a total
    state, so every match ever played already has the right value and no
    backfill has to guess one.
    """

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
        # Two *present* tickets must differ. Two absent ones are the
        # ordinary shape of a match that did not come from the queue, and
        # comparing `None` to `None` would refuse every one of them.
        if (
            self.light.queue_ticket_id is not None
            and self.light.queue_ticket_id == self.dark.queue_ticket_id
        ):
            raise ValueError("a match needs two different queue tickets")
        if self.origin is MatchOrigin.QUEUE and None in self.ticket_ids():
            raise ValueError("a queue match records the ticket each player arrived on")
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
        if self.ply_number < 0:
            raise ValueError("a ply count cannot be negative")
        if (self.time_control is None) != (self.clock is None):
            raise ValueError("a clock is present exactly when a time control is")
        if (self.status is MatchRecordStatus.COMPLETED) != (self.outcome is not None):
            raise ValueError("an outcome is recorded exactly when the match completed")
        if (self.outcome is not None) != (self.ended_at is not None):
            raise ValueError("ended_at is set exactly when there is an outcome")
        if (self.winner is not None) != (self.outcome is MatchOutcome.WIN):
            raise ValueError("a winner is recorded exactly for a decisive result")
        # A64-020.5C-pre §4. An offer only means something on a match that
        # can still be played, and a terminal row carrying one would be
        # rendered as answerable by every reconnecting client. The database
        # keeps the authoritative copy (BE-06).
        if self.draw_agreement.is_pending and self.status is not MatchRecordStatus.ACTIVE:
            raise ValueError("a draw offer stands only on an active match")

    @property
    def is_pending(self) -> bool:
        return self.status.is_pending

    @property
    def result(self) -> MatchResult | None:
        """The result as the domain models it, or `None` if unfinished.

        Assembled from the three columns rather than stored as one, so the
        pairing between an outcome and its winner is enforced by a `CHECK`
        as well as by `MatchResult`'s own constructor (BE-06).
        """
        if self.outcome is None or self.termination_reason is None:
            return None
        return MatchResult(outcome=self.outcome, reason=self.termination_reason, winner=self.winner)

    def completed(
        self,
        result: MatchResult,
        *,
        ply_number: int,
        at: datetime,
        clock: ClockState | None = None,
    ) -> "MatchRecord":
        """This match, played to an end — §6.

        A new value rather than a mutation, like every other transition on
        this record: the repository writes what it is given, and a caller
        holding the previous value still holds what it read.

        **Idempotent by construction at the caller.** A second settlement
        of an already-completed match is refused by the same row lock and
        status check that refuses a move — see `LiveMoveService`. This
        method itself refuses anything that is not `active`, so a repair
        script cannot resurrect a cancelled pairing as a played game.
        """
        if self.status is not MatchRecordStatus.ACTIVE:
            raise InvalidMatchTransition(f"A {self.status.value} match cannot be completed.")

        return replace(
            self,
            status=MatchRecordStatus.COMPLETED,
            ply_number=ply_number,
            clock=clock or self.clock,
            outcome=result.outcome,
            termination_reason=result.reason,
            winner=result.winner,
            ended_at=at,
            # A64-020.5C-pre §2. **Every** completion clears the agreement,
            # not only the two this phase adds — here rather than at each
            # call site because there are four of them (a move, a flag, a
            # resignation, an accepted draw) and the one that forgot would
            # leave a finished game showing an answerable offer. The
            # invariant in `__post_init__` then cannot be violated by any
            # settlement path, which is what makes it worth stating.
            draw_agreement=self.draw_agreement.settled(),
        )

    def advanced(self, *, ply_number: int, clock: ClockState | None = None) -> "MatchRecord":
        """This match, one move further on and still being played.

        `clock` is `None` for an untimed match and for a caller with nothing
        to change; passing one replaces the stored state, which is what a
        timed move does in the same write that advances the ply.
        """
        if self.status is not MatchRecordStatus.ACTIVE:
            raise InvalidMatchTransition(f"A {self.status.value} match cannot be advanced.")
        return replace(self, ply_number=ply_number, clock=clock or self.clock)

    # --- draw agreement and resignation — A64-020.5C-pre §2 ---------------
    #
    # Four transitions and one rule each, all of them refusing anything that
    # is not `active` first. The refusal is here rather than at the call
    # site because §2 forbids the gateway implementing transition rules,
    # and "a completed match cannot be offered a draw" is exactly such a
    # rule — one the transport would otherwise have to remember four times.

    def resigned_by(self, side: PlayerSide, *, at: datetime) -> "MatchRecord":
        """This match, given up by `side`. The opponent wins — §1.

        Settled through `completed`, so a resignation and a checkmate reach
        the permanent record by the identical path: one status write, one
        result, one `ended_at`. The rule that the opponent wins is
        `resignation_result`'s, which `Match.resign` also uses.

        No board, no ply, no clock change. A resigned game must still
        replay to the position it was abandoned in (GE-67), which it does
        because nothing here touches the move log.
        """
        self._require_active("resign")
        return self.completed(resignation_result(side), ply_number=self.ply_number, at=at)

    def offered_draw(self, side: PlayerSide, *, at: datetime) -> "MatchRecord":
        """This match with `side` offering a draw — §1, §3.

        Not idempotent for a repeat, deliberately. §1 permits "idempotent
        **or** one stable bounded result", and the refusal is the more
        useful of the two: a player whose offer already stands has nothing
        to gain from a silent success, and a client that resent because it
        never saw the answer learns the offer is live — which is the state
        it wanted to reach.
        """
        self._require_active("offer a draw")

        if self.draw_agreement.is_pending:
            raise DrawOfferAlreadyPending("A draw offer already stands on this match.")
        if not self.draw_agreement.may_offer(side, at_ply=self.ply_number):
            raise DrawOfferNotAllowedYet("Wait for your opponent to move before offering again.")

        offer = DrawOffer(offered_by=side, offered_at_ply=self.ply_number, offered_at=at)
        return replace(self, draw_agreement=self.draw_agreement.opened(offer))

    def accepted_draw(self, side: PlayerSide, *, at: datetime) -> "MatchRecord":
        """This match, drawn by agreement — §1.

        Only the recipient may accept, checked here rather than in the
        service, because "the offering side cannot accept their own offer"
        is a rule about the state and not about the request.
        """
        self._require_active("accept a draw")
        self._require_recipient(side)
        return self.completed(agreed_draw_result(), ply_number=self.ply_number, at=at)

    def declined_draw(self, side: PlayerSide, *, at: datetime) -> "MatchRecord":
        """This match with the standing offer refused — §1.

        Changes **nothing else**: no board, no clock, no turn, no ply. A
        decline is an answer to a question, and a question being answered
        is not a move.

        The offerer is put under the re-offer restriction here, which is
        the whole of the spam rule for this path — see `DrawAgreement`.
        """
        self._require_active("decline a draw")
        self._require_recipient(side)
        return replace(self, draw_agreement=self.draw_agreement.resolved(at_ply=self.ply_number))

    def after_move_by(self, side: PlayerSide, *, at_ply: int) -> "MatchRecord":
        """This match with any offer the mover was holding cleared — §10.

        Called on the authoritative move path and **only** for a move that
        was applied, which is what makes "a rejected move leaves the offer
        pending" true by construction rather than by a check: a rejected
        move never reaches this method because it never reaches the write.

        A move by the *offerer* leaves their own offer standing. They asked
        and are still waiting; playing on while the opponent thinks is the
        ordinary way a draw offer is made.
        """
        offer = self.draw_agreement.offer
        if offer is None or not offer.is_to(side):
            return self
        return replace(self, draw_agreement=self.draw_agreement.resolved(at_ply=at_ply))

    def _require_active(self, action: str) -> None:
        if self.status is not MatchRecordStatus.ACTIVE:
            raise MatchNotActive(f"A {self.status.value} match cannot be used to {action}.")

    def _require_recipient(self, side: PlayerSide) -> None:
        offer = self.draw_agreement.offer
        if offer is None:
            raise DrawOfferNotPending("There is no draw offer to answer.")
        if not offer.is_to(side):
            raise DrawOfferNotRecipient("You cannot answer your own draw offer.")

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

    def ticket_ids(self) -> tuple[UUID | None, UUID | None]:
        """Both source queue tickets, light first, `None` where there is none.

        The pair is kept positional rather than filtered, because a caller
        asking "which ticket sat light" must not have to guess from a list
        of one. `queue_ticket_ids` below is for the callers that want only
        the tickets that exist.
        """
        return (self.light.queue_ticket_id, self.dark.queue_ticket_id)

    def queue_ticket_ids(self) -> tuple[UUID, ...]:
        """Only the tickets this match actually has.

        Empty for a tournament, a challenge or a rematch. What
        `settlements_for` keys on, so a match with no tickets simply
        matches no reconciliation query rather than matching a fabricated
        one.
        """
        return tuple(ticket for ticket in self.ticket_ids() if ticket is not None)

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
        return accepted._with(
            status=MatchRecordStatus.ACTIVE,
            settled_at=at,
            clock=_started(accepted.time_control, at=at),
        )

    def system_activated(self, at: datetime) -> "MatchRecord":
        """This match, active without either player having been asked.

        For a fixture rather than an offer — a tournament pairing two people
        entered a tournament to play (`game.public.AcceptancePolicy.SYSTEM`).
        There is nobody to ask, so there is no window to miss and the match
        can never expire unanswered.

        Both `accepted_at` instants are set to `at`, which is not a fiction
        about who clicked what: `ck_match__active_iff_both_accepted` and
        this aggregate's own invariant both read "active means two seats
        answered", and the system answered for both. Whether the players
        then *turn up* is a different question, and one the originating
        context answers with its own policy rather than by leaving a match
        pending forever.

        Only from `PENDING_ACCEPTANCE`, and it says so: activating a
        cancelled or expired match would resurrect one, and activating an
        active one is a caller that has lost track of its own request.
        """
        if self.status is not MatchRecordStatus.PENDING_ACCEPTANCE:
            raise MatchNotPending("Only a pending match can be activated.")
        return self._with(
            light=self.light.accepting(at),
            dark=self.dark.accepting(at),
            status=MatchRecordStatus.ACTIVE,
            settled_at=at,
            clock=_started(self.time_control, at=at),
        )

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
        clock: ClockState | None = None,
    ) -> "MatchRecord":
        """This match with some fields changed and the rest carried across.

        `None` means "unchanged" rather than "cleared", which is safe here
        because none of the six fields is ever *un*-set: a seat only gains
        an `accepted_at`, `status`, `declined_by` and `settled_at` are
        written once when the handshake ends, and a clock is replaced but
        never removed — an untimed match passes `None` because it has none
        to replace, which is "unchanged" and is the right answer. A
        transition that had to clear one would be a transition this state
        machine does not have.

        Every other field is carried verbatim, which is what makes the
        creation-time facts immutable without a guard.

        **`replace`, not a fresh `MatchRecord`.** Naming the fields to carry
        across meant carrying the ones that existed when this was written:
        `origin` and `origin_ref` were added by A64-019.0 and never added
        here, so a system-activated tournament match lost both on the way to
        storage and became a queue match with no reference — the round trip
        R-25 exists for, broken at its first step. `ply_number`, the clock
        and the result columns were dropped the same way; those happen to be
        empty at every point this is reached today, which is exactly why
        nothing noticed. Same defect as `MatchSeat.accepting`, same fix.
        """
        # `None` means "unchanged" rather than "cleared" — see the docstring
        # above on why that is safe for every one of the six.
        return replace(
            self,
            light=light if light is not None else self.light,
            dark=dark if dark is not None else self.dark,
            status=status if status is not None else self.status,
            declined_by=declined_by if declined_by is not None else self.declined_by,
            settled_at=settled_at if settled_at is not None else self.settled_at,
            clock=clock if clock is not None else self.clock,
        )


def _started(control: TimeControl | None, *, at: datetime) -> ClockState | None:
    """Both clocks at their full budget from `at`, or `None` if untimed —
    A64-020.5A-pre §14.

    Called by the two transitions that make a match playable, and by nothing
    else: **activation is when a clock starts**, because that is the first
    instant either player could legally move. A clock started at creation
    would charge LIGHT for however long DARK took to accept.

    A free function rather than a method because it answers a question about
    a control, not about this record, and both callers already hold the
    control they mean.
    """
    return None if control is None else ClockState.start(control, at=at)


__all__ = ["MatchRecord", "MatchRecordStatus", "MatchSeat"]
