"""The command `matchmaking` sends `game` — A64-015.3.

architecture.md §7 draws one inbound edge into `game` from `matchmaking`
and labels it "creates match". This is that edge, stated as a port rather
than as an aggregate: `matchmaking` hands over a request and receives an
identifier, and at no point holds a `Match`.

## Why a command and not the aggregate

R-3 is the reason `game.public` publishes no `Match`, and the reason is
not tidiness. A consumer holding the aggregate could advance a game, and
the module that decides who plays whom has no business being able to. What
it needs is narrower and completely expressible: *these two players, this
rule set, these sides — make it exist.*

The asymmetry with every other consumer is worth stating, because it looks
like an exception to R-3 and is not. `rating`, `statistics` and the rest
learn about matches by subscribing to `game`'s events — they react to
something that happened. `matchmaking` is upstream of the match: nothing
has happened yet, and there is no event that could carry a request. An
edge that points *into* a module is a command, and a command is a port.

## Idempotency is the request's own field

`pairing_id` is derived by `matchmaking` from the two claimed ticket ids
and is stable across retries — see `matchmaking.domain.pairing.pairing_id`.
It is on the request rather than passed beside it because that is what
makes the contract enforceable: an implementation that ignored it would be
visibly ignoring a field, not merely failing to be told.

A retry of the same pairing must return the **same** `match_id` with
`created=False`. That sentence is the whole idempotency contract, and
`MatchCreationUseCase.create_match` states it again where an implementer
will read it. Since A64-015.4 it is held by a **unique index** on
`game.match.pairing_id` rather than by an implementation's good intentions
— see `game.infrastructure.models`.

## The acceptance deadline arrives on the request

A64-015.4 §5 asks for the reservation deadline and the acceptance timeout
to be one coherent model rather than two timers. They are made one *here*:
`matchmaking` computes a single instant from its clock and
`MATCHMAKING_RESERVATION_TTL_SECONDS`, writes it onto both reserved tickets
as `reserved_until`, and sends the same value as `acceptance_deadline`.

`game` therefore does not own the duration and does not read a clock to
derive it. That is deliberate: whoever owns the reservation owns the
window, and a `game` setting for "how long may a player take to accept"
would be a second number that has to be kept below the queue's own — a
constraint nothing could enforce across two settings classes.

## The time control arrives on the request — A64-020.5A-pre §12

A64-015.3 §9 listed it and A64-015.4 could not supply it, because
`reference.time_control` did not exist. It does now, and the change this
file predicted is the one that happened: `QueuePool` gained a component and
this request gained a field.

`MatchTimeControl` is **primitive-only and `game`-agnostic**, like
`SeatRating` beside it and for the identical reason: `game` must not import
`reference` any more than it imports `rating`, so the two integers are
spelled twice and `PersistentMatchCreation` is the one place they meet.

It is **optional**, and that is a gap rather than a policy — the second one
this file has recorded, stated in the same spirit as the first. Every queue
pairing supplies a control since A64-020.5A-pre. A tournament does not:
`specs/tournament.md` has no time control on a `TournamentFormat`, and
inventing one here would put the choice in the module that creates the match
rather than the one that runs the competition. So a tournament match is
untimed today, exactly as every match was before this task, and `None`
continues to mean what `game.domain.clock` says it means: the clock
machinery does not run at all.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.core.exceptions import DomainError
from app.modules.engine import EngineVersion, PlayerSide
from app.modules.game.domain.variants import MatchOrigin, ProductVariant


class AcceptancePolicy(StrEnum):
    """Whether a match waits for its two players to answer — A64-019.5H.

    A **named policy on the request**, rather than a boolean or something
    inferred from `origin`. A boolean parameter selecting behaviour is what
    CLAUDE.md §2.3 forbids, and inferring it from the origin would silently
    decide the question for `challenge` and `rematch` the day either ships —
    those are offers somebody may refuse, and a tournament pairing is not.

    The distinction is *who agreed, and when*. A queue pairing is an offer
    made to two people who have not seen each other; a tournament pairing is
    a fixture two people entered a tournament to play, and the agreement
    happened at registration.
    """

    BILATERAL = "bilateral"
    """Both players must accept before the match becomes playable. The queue
    handshake A64-015.4 built."""

    SYSTEM = "system"
    """The match is created **already active**.

    There is nobody to ask: the participants committed when they entered.
    A match created this way has no acceptance window to miss, so it can
    never expire unanswered — whether the players actually turn up is a
    question the originating context answers with its own policy, and for a
    tournament that is `specs/tournament.md` §6e's no-show deadline.
    """


class MatchCreationRefused(DomainError):
    """`game` will not create the requested match.

    The **expected** failure of the port, and the one A64-015.3 §10's
    compensation exists for: `matchmaking` reserved two tickets, this was
    raised, and both tickets go back to `waiting` with their original
    `entered_at`.

    A `DomainError` rather than an infrastructure one, because a refusal is
    a decision `game` made — a variant withdrawn mid-flight, a player
    already in a live match. An unreachable database raises what a database
    raises, and `matchmaking` compensates for both the same way.
    """


@dataclass(frozen=True, slots=True)
class SeatRating:
    """A rating as it stood when a match was created — the seat snapshot.

    Primitive-only and `game`-agnostic, like every other type on this port.
    It is `rating.public.RatingSnapshot`'s content restated rather than that
    type imported, because `game` must not depend on `rating` at all — R-4
    makes the chain `game -> rating`, and an import here would be the
    back-edge that lets a leaderboard rebuild alter a historical rating.

    **Immutable, and never refreshed.** Once written it is a fact about the
    past: what these two players rated when they sat down. A later read of
    their current rating is a different question, and answering this one
    with it is precisely what PR-3 forbids.
    """

    value: float
    deviation: float
    volatility: float
    """The Glicko-2 triple. All three, because the calculation needs all
    three — a snapshot carrying only the value would make PR-3
    unimplementable."""

    games_played: int
    is_provisional: bool
    """PR-6's mark, captured with the rest so the record explains itself.
    Recomputing it later from `games_played` would need the threshold, which
    is `rating`'s and may change."""

    speed_class: str
    """The rating key's second component — SPEC-RATING §7.1.

    A `str` rather than `rating`'s enum, for the same reason this whole
    class restates rather than imports. The variant is on the match itself
    and is not repeated per seat.
    """


@dataclass(frozen=True, slots=True)
class MatchTimeControl:
    """How much time each side gets in the match being created.

    `game.domain.clock.TimeControl`'s two fields restated rather than that
    type imported, for the reason `SeatRating` restates
    `rating.public.RatingSnapshot`: a port is a contract the *caller* shapes
    its request against, and a caller building one should not have to import
    a domain to do it.

    **Immutable and never refreshed**, like `SeatRating`. It is a fact about
    the past — what these two players agreed to play — and `matchmaking`
    supplies it from the snapshot on their tickets, never from a catalogue
    read at creation time. See `reference.domain.time_control` on why those
    are different numbers.

    No delay, no multi-stage control: this carries exactly what
    `game.domain.clock` can populate, which is the same list
    `reference.time_control` seeds.
    """

    initial_ms: int
    """Each side's budget at the start. Both sides get the same."""

    increment_ms: int = 0
    """What a side gets back after each of its own moves — Fischer."""

    def __post_init__(self) -> None:
        # The same two checks `TimeControl` makes, applied at the boundary
        # rather than only after conversion. A zero budget is not a fast
        # game — it is a match every player loses on their first move — and
        # catching it here names the *request* as malformed rather than
        # failing somewhere inside `game`.
        if self.initial_ms <= 0:
            raise ValueError("a time control gives each side a positive budget")
        if self.increment_ms < 0:
            raise ValueError("an increment cannot be negative")


@dataclass(frozen=True, slots=True)
class MatchParticipant:
    """One side's player, and the ticket that put them here.

    `queue_ticket_id` travels with the player rather than in a separate
    list, so an implementation recording provenance cannot pair the wrong
    ticket with the wrong player by index.
    """

    player_id: UUID
    """DM-06's opaque cross-context identifier. `game` cannot resolve it to
    a person and does not need to."""

    queue_ticket_id: UUID | None
    """Which queue ticket this player arrived on, or `None`.

    Provenance, not identity: it lets a stored match be traced back to the
    pairing that produced it, and it is what makes `pairing_id` verifiable
    rather than merely asserted.

    **`None` for any origin but the queue.** A tournament pairing, a
    challenge and a rematch each produce a match and none produces a
    ticket; requiring one made a caller invent an id, which put a
    fabricated fact in a permanent record. `CreateMatchRequest` still
    *requires* both for `MatchOrigin.QUEUE` — see its `__post_init__`, and
    note that the requirement is origin-specific rather than dropped.
    """

    rating: "SeatRating"
    """This player's rating **at match creation** — SPEC-RATING §7.6, MT-4.

    Carried on the seat rather than looked up later, because PR-3 requires
    the rating calculation to run on the values captured before the game
    was played. Two matches completing concurrently would otherwise each
    compute against the other's partial result, and neither would be
    reproducible from the record.

    Supplied by `matchmaking`, which reads it through `rating.public`.
    **`game` never reads a rating** (`services.md` §10.2 — gameplay core may
    not depend on projections): it stores this and hands it back on
    `match_completed`, and could not compute one if it wanted to.
    """


@dataclass(frozen=True, slots=True)
class CreateMatchRequest:
    """Everything `game` needs to bring a match into existence.

    Frozen and primitive-only — two ids, two ids, an enum, a bool and a
    version. Nothing here is a `matchmaking` type, so the command does not
    smuggle the queue's vocabulary across the boundary; and nothing is a
    `game` type either, so `matchmaking` builds it without importing a
    domain.

    ## Sides are fields, not a list

    `light` and `dark` rather than `participants: tuple[..., ...]` with a
    `side` on each, because the second shape can represent two lights. The
    illegal state is unrepresentable here (CLAUDE.md §2.4) rather than
    checked, and the check that would otherwise be needed cannot be
    forgotten.

    Which player gets which side is `matchmaking`'s decision and is
    deterministic — see `matchmaking.domain.pairing` on why it is derived
    from `pairing_id` rather than from wait time.
    """

    pairing_id: UUID
    """The idempotency key. See this module's docstring."""

    variant: ProductVariant
    rated: bool
    """Whether finishing this match moves a rating.

    `matchmaking`'s `QueueType` collapsed to the one bit `game` acts on.
    Passing the enum would export a queue concept into a module that has no
    queue, and would leave `game` matching on a string to answer a yes/no.
    """

    engine_version: EngineVersion
    """The rules build this match is created under — AD-15.

    Stamped by `matchmaking` from `game_engine_version()` at the moment of
    pairing, so a match records the rules it will actually be played by
    even if the process is upgraded mid-game.
    """

    acceptance_deadline: datetime
    """When an unanswered match stops being offered — A64-015.4 §5.

    Supplied by the caller rather than computed here, and it is the *same
    instant* `matchmaking` wrote onto both reserved queue tickets as
    `reserved_until`. See this module's docstring on why the window's owner
    is the module that owns the reservation.
    """

    light: MatchParticipant
    """Moves first (`PlayerSide.LIGHT`)."""

    dark: MatchParticipant

    origin: MatchOrigin = MatchOrigin.QUEUE
    """Where this match came from — R-25, A64-019.0.

    Defaulted, so `matchmaking` — the only caller today — is unchanged: a
    queue pairing is a queue pairing whether or not it says so.
    """

    origin_ref: UUID | None = None
    """The originating context's own identifier, opaque to `game`.

    A tournament passes its pairing id here and recognises the match again
    when `match_completed` carries it back. That round trip is the entire
    mechanism `services.md` §11.3 assumed already existed.
    """

    time_control: MatchTimeControl | None = None
    """The clock this match is played under, or `None` for an untimed one —
    A64-020.5A-pre §12.

    Supplied by `matchmaking` from the two tickets' snapshot, which is the
    same value both players were shown when they queued. `game` stores it,
    starts the clock from it when the match activates, and adjudicates
    against it; it never resolves it, never reads a catalogue and could not
    — `reference` is not a dependency of this module.

    Defaulted, like `origin` and `acceptance` above, so a caller that has no
    clock to offer is unchanged. See this module's docstring on why that
    caller is a tournament and why its matches are untimed.
    """

    acceptance: AcceptancePolicy = AcceptancePolicy.BILATERAL
    """Whether this match waits to be accepted — A64-019.5H.

    Defaulted, so `matchmaking` is unchanged: a queue pairing is an offer
    and stays one. A tournament asks for `SYSTEM`, and
    `acceptance_deadline` then describes a window nothing will ever use —
    see `AcceptancePolicy.SYSTEM`.
    """

    def __post_init__(self) -> None:
        # A pairing of somebody with themselves is not a match, and it is
        # the one malformed request this port can detect on its own. It
        # would mean `matchmaking` selected one ticket twice, which the
        # engine's own ordering makes impossible — so reaching this is a
        # defect, and failing here is how it stays one line long.
        if self.light.player_id == self.dark.player_id:
            raise ValueError("a match needs two different players")

        # Two *present* tickets must differ. Two absent ones are the
        # ordinary shape of a non-queue match, and comparing `None` to
        # `None` would refuse every one of them.
        if (
            self.light.queue_ticket_id is not None
            and self.light.queue_ticket_id == self.dark.queue_ticket_id
        ):
            raise ValueError("a match needs two different queue tickets")

        # **Origin-specific, not relaxed.** A64-019.5H made the field
        # nullable so a tournament need not invent one; it did not make a
        # queue pairing's provenance optional. A queue match without its
        # tickets is one no reconciler can recover, which is exactly the
        # gap A64-015.4 closed.
        if self.origin is MatchOrigin.QUEUE and None in (
            self.light.queue_ticket_id,
            self.dark.queue_ticket_id,
        ):
            raise ValueError("a queue match records the ticket each player arrived on")

        # A64-020.5A-pre §14: the first flag deadline is written when a
        # match **activates**, and the one place that happens is
        # `MatchAcceptanceService`. A `SYSTEM` match activates at creation
        # instead, so a timed one would start a clock nothing had scheduled
        # a deadline for — a game that can never flag.
        #
        # Unreachable today: the only `SYSTEM` caller is `tournament`, and a
        # tournament format carries no time control (`specs/tournament.md`).
        # Refusing here rather than trusting that is what turns "we happen
        # not to do this" into "this cannot be done", so the task that gives
        # tournaments a clock is made to schedule the deadline rather than
        # discovering months later that nobody flags.
        if self.acceptance is AcceptancePolicy.SYSTEM and self.time_control is not None:
            raise ValueError(
                "a system-activated match cannot carry a time control until its "
                "activation schedules a clock deadline"
            )

    def player_ids(self) -> tuple[UUID, UUID]:
        """Both players, light first. For logging and for the caller that
        needs the pair without unpacking two records."""
        return (self.light.player_id, self.dark.player_id)

    def side_of(self, player_id: UUID) -> PlayerSide:
        """Which side `player_id` was assigned. Raises `KeyError` for
        anybody else, because "not in this match" is not a side."""
        if player_id == self.light.player_id:
            return PlayerSide.LIGHT
        if player_id == self.dark.player_id:
            return PlayerSide.DARK
        raise KeyError(player_id)


@dataclass(frozen=True, slots=True)
class CreateMatchResult:
    """What `game` did with a `CreateMatchRequest`."""

    match_id: UUID
    pairing_id: UUID
    """Echoed back, so a caller correlating an asynchronous implementation's
    result does not have to hold the request."""

    created: bool
    """`True` when this call brought the match into existence, `False` when
    it found one an earlier attempt had already created.

    The observable half of the idempotency contract. A caller does not
    branch on it — both outcomes mean "the match exists, mark the tickets
    matched" — but a **metric** on it is how a retry storm becomes visible,
    and a test asserts on it to prove the second call did not create a
    second match.
    """


class MatchCreationUseCase(Protocol):
    """`game`'s side of the pairing handshake.

    One method, and the narrowest thing that can be called a port into
    `game`: it accepts a command and returns an identifier.
    """

    async def create_match(self, request: CreateMatchRequest) -> CreateMatchResult:
        """Creates the match this pairing produced, or returns the one that
        already exists for it.

        **Idempotent on `pairing_id`.** Calling twice with the same request
        must produce the same `match_id`, with `created=False` on every call
        after the first. A pairing worker that dies after `game` committed
        but before it recorded the outcome will retry, and a second match
        for one pair is two games for two players who agreed to one.

        Raises `MatchCreationRefused` when the match legitimately must not
        exist. The caller's response is to release both tickets back to
        `waiting`, so a refusal must be a decision rather than a transient
        failure — anything transient should raise something transient and be
        retried, not compensated.
        """
        ...


__all__ = [
    "AcceptancePolicy",
    "MatchOrigin",
    "SeatRating",
    "CreateMatchRequest",
    "CreateMatchResult",
    "MatchCreationRefused",
    "MatchCreationUseCase",
    "MatchParticipant",
    "MatchTimeControl",
    "PlayerSide",
]
