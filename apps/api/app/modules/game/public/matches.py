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
will read it.

## What is deliberately absent: time control

A64-015.3 §9 lists time control among the request's fields, and this
request does not carry one. The reason is recorded rather than skipped:
`reference.time_control` (database.md §6.2) does not exist in code, and
`QueuePool` deliberately does not invent one — putting the definition of
"blitz" in `matchmaking` would hand the module least entitled to own it a
grouping key that every rating category (DM-10) and every leaderboard
would inherit.

A nullable placeholder field would be worse than the gap: it would be a
contract that says a time control is optional, when in fact every real
match has one. When `reference.time_control` ships, `QueuePool` gains a
component and this request gains a field, in one change.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.core.exceptions import DomainError
from app.modules.engine import EngineVersion, PlayerSide
from app.modules.game.public.variants import ProductVariant


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


class MatchCreationUnavailable(MatchCreationRefused):
    """No implementation of `MatchCreationUseCase` is wired.

    Its own type rather than a bare refusal, because the operator response
    is completely different: a refusal is a match that legitimately should
    not exist, and this is a deployment that cannot create any match at
    all. See `UnavailableMatchCreation` on why this ships rather than a
    stub that pretends.
    """


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

    queue_ticket_id: UUID
    """Which queue ticket this player arrived on.

    Provenance, not identity: it lets a stored match be traced back to the
    pairing that produced it, and it is what makes `pairing_id` verifiable
    rather than merely asserted.
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

    light: MatchParticipant
    """Moves first (`PlayerSide.LIGHT`)."""

    dark: MatchParticipant

    def __post_init__(self) -> None:
        # A pairing of somebody with themselves is not a match, and it is
        # the one malformed request this port can detect on its own. It
        # would mean `matchmaking` selected one ticket twice, which the
        # engine's own ordering makes impossible — so reaching this is a
        # defect, and failing here is how it stays one line long.
        if self.light.player_id == self.dark.player_id:
            raise ValueError("a match needs two different players")
        if self.light.queue_ticket_id == self.dark.queue_ticket_id:
            raise ValueError("a match needs two different queue tickets")

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


class UnavailableMatchCreation:
    """The implementation this repository ships until matches are stored.

    A64-015.3 §9 says plainly: "If actual Match persistence is not ready,
    provide an explicit application port and test adapter. **Do not fake
    persistence.**" `game` has a complete rules kernel and a `Match`
    aggregate (A64-014.6) and no repository, no table and no migration for
    one — so there is nothing this class could honestly return.

    What it does instead is fail in the one way that is true, and the
    failure is *useful*: it drives A64-015.3 §10's compensation path in
    production exactly as a real refusal would, so the path that returns
    two players to the queue is exercised by the deployment rather than
    only by a test.

    The alternative — returning a fabricated `match_id` — would mark two
    tickets `matched` and delete two players from the queue in exchange for
    a game that does not exist. That is worse than not pairing at all,
    which is why pairing is wired but **not scheduled** (see
    `MATCHMAKING_PAIRING_ENABLED`).

    A64-015.4 replaces this with the real use case and nothing else in the
    graph changes.
    """

    async def create_match(self, request: CreateMatchRequest) -> CreateMatchResult:
        raise MatchCreationUnavailable(
            "Match creation is not available in this build; no match was created."
        )


__all__ = [
    "CreateMatchRequest",
    "CreateMatchResult",
    "MatchCreationRefused",
    "MatchCreationUnavailable",
    "MatchCreationUseCase",
    "MatchParticipant",
    "PlayerSide",
    "UnavailableMatchCreation",
]
