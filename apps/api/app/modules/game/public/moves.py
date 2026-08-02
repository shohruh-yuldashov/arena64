"""Submitting a move — `game`'s live-play boundary. A64-016.3 §5.

The gateway must not know the rules. §5 says so twice ("do not duplicate
movement rules in the gateway", "the gateway remains a transport adapter")
and R-7 has said it since the architecture was written: "the gateway
validates, authenticates, rate-limits, routes, and fans out. It never decides
whether a move is legal."

So this is the whole of what a transport may ask: *this player wants to play
this path in this match*. Everything downstream — whose turn it is, whether
the path is legal, what it captures, whether it crowns — is `game`'s, and the
gateway receives a result it can serialise and route.

## Why the request carries a path and nothing else

§2: "Do not accept only from/to coordinates" and "prefer server-derived
capture and promotion data when possible". A path does both.

A from/to pair is ambiguous in draughts — the same origin and destination can
be reached by two capture sequences taking different pieces, and picking one
for the player is picking which of their pieces survives. The full path
disambiguates.

**Captures and promotion are not accepted at all.** The client could send
them, and then the server would have to either trust them (a client
declaring what it took) or verify them (work it has to do anyway). Instead
the engine generates the legal moves for the position and finds the one whose
path matches: the captures and the crown come from the generator, so a
tampered client cannot claim to have taken a piece it did not jump. That is
strictly stronger than validation and is less code.

## Why an explicit result rather than the new position

`SubmitMoveResult` carries a **fingerprint**, a ply and the applied move —
not a `Position`. Three reasons, in order of weight:

1. A `Position` is a `game` domain object, and publishing one would let the
   gateway hold a board. R-7 again.
2. The client already has the position: it applied the move optimistically
   (AD-23). What it needs is confirmation and a way to detect divergence,
   which is exactly a fingerprint.
3. A full board on every move is the largest payload in the protocol,
   multiplied by every move of every game. The fingerprint is a string.

A client whose fingerprint disagrees resynchronises — which is a separate
task's problem and is why the field exists now rather than later.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.engine import PlayerSide
from app.modules.game.domain.exceptions import (
    IllegalMoveSubmitted,
    MatchNotActive,
    NotYourTurn,
    StaleMatchState,
)


@dataclass(frozen=True, slots=True)
class SubmitMoveRequest:
    """One player's attempt to play one move.

    Primitive-only — two identifiers and a tuple of square names. Nothing
    here is an engine type, so the gateway builds one from a decoded frame
    without importing `engine`, and nothing is a `game` type either, so the
    command does not smuggle a domain object across the boundary. The same
    shape `CreateMatchRequest` takes, for the same reason.
    """

    match_id: UUID
    player_id: UUID
    """The **authenticated** player, resolved from the socket's redeemed
    ticket. §4 forbids trusting a payload-supplied identity, and the
    protocol frame has no field for one — so this is the only value that
    can reach here, structurally rather than by convention."""

    path: tuple[str, ...]
    """The squares the piece occupies in order, in algebraic notation
    (`("c3", "e5", "g3")`). At least two.

    Strings rather than a coordinate type, because `BoardCoordinate` is an
    engine type and the gateway may not hold one. Parsed at this boundary,
    where a malformed square is a rejected request rather than an exception
    inside a handler.
    """


@dataclass(frozen=True, slots=True)
class AppliedMove:
    """What the engine determined the move actually was.

    **Server-derived**, which is the point. The client sent a path; these
    are the captures the generator says that path takes and the rank it
    says the piece ends with. A client rendering its optimistic board
    against these can tell immediately whether it guessed the capture
    sequence the server chose.
    """

    path: tuple[str, ...]
    captured: tuple[str, ...]
    """The squares of the pieces taken, in the order they were jumped.
    Empty for a quiet move."""

    promoted_to: str | None
    """The rank the piece ends with when the move crowns it, otherwise
    `None`. A string rather than `PieceRank` for the reason `path` is a
    string: the gateway serialises this and must not hold an engine enum."""


@dataclass(frozen=True, slots=True)
class SubmitMoveResult:
    """A move that was played, and the state that followed it."""

    match_id: UUID
    ply: int
    """How many moves have been played, counting this one.

    **The authoritative sequence number** (§6). It is the version the
    optimistic concurrency check is made against, so two moves submitted
    against the same state cannot both apply — see `SubmitMoveUseCase`.
    Also the ordering key a client uses to detect a gap.
    """

    side_to_move: PlayerSide
    """Whose turn it now is. Published as the engine enum, which is
    already on `game.public` — `PendingMatchView.your_side` carries it."""

    fingerprint: str
    """A deterministic rendering of the resulting position, for divergence
    detection. Not a wire format and not a storage format — see
    `Position.fingerprint`, whose contract this forwards unchanged."""

    applied: AppliedMove


class SubmitMoveUseCase(Protocol):
    """`game`'s live-play command. One method, and deliberately only one.

    The gateway can submit and can do nothing else: it cannot read a
    position, cannot enumerate matches, cannot resign one. A transport tier
    that could do any of those would be a transport tier worth
    compromising, and R-7's "the gateway contains no domain logic" is worth
    nothing if the port it holds is wide.

    The four failures it raises are re-exported from `game`'s own taxonomy
    above rather than redeclared here — a published error must be *the*
    error the service raises, or a consumer catching the published name
    would miss the real one, which is a bug that appears the first time
    something actually fails.
    """

    async def submit(self, request: SubmitMoveRequest) -> SubmitMoveResult:
        """Validates and applies one move, or raises.

        **Applies at most once per call, and never twice concurrently.**
        The live position carries a ply, and the write is conditional on
        the ply that was read — so two moves submitted against the same
        state produce one `SubmitMoveResult` and one `StaleMatchState`
        rather than two applications. §6 forbids a process-local lock for
        this, and a compare-and-set inside Redis is the mechanism
        architecture.md AD-18 already assigns to live match state.

        Raises, in the order checked:

            MatchNotFound          no such match, **or** this player is not
                                   in it — one failure, because
                                   distinguishing them makes live match
                                   identifiers enumerable
            MatchNotActive         the match is not being played
            NotYourTurn            the player does not own the side to move
            IllegalMoveSubmitted   the path is not legal here
            StaleMatchState        another writer won; retry

        The ordering is deliberate: identity before state before rules, so
        a caller who may not see a match learns nothing about it, and a
        caller whose turn it is not is not told whether their move would
        have been legal.
        """
        ...


__all__ = [
    "AppliedMove",
    "IllegalMoveSubmitted",
    "MatchNotActive",
    "NotYourTurn",
    "StaleMatchState",
    "SubmitMoveRequest",
    "SubmitMoveResult",
    "SubmitMoveUseCase",
]
