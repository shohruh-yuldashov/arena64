"""The authoritative live snapshot — `game`'s answer to "where is this game".
A64-016.6 §1.

§1 is explicit about the boundary: *"Gateway must not assemble snapshots from
Game internals."* So this is one published read that returns everything a
reconnecting client needs, and the gateway forwards it without knowing what a
board is.

## Why the position is a fingerprint plus a placement list

`MatchSnapshot` carries the pieces as a flat list of primitives and the
fingerprint beside them. Neither is a `Position`, because publishing one would
let the gateway hold a board and R-7 forbids it — but a client genuinely
cannot render a game from a fingerprint alone, so the placement has to cross.

The list is the *safe serialised representation* §1 asks for: square names and
piece descriptions, which is exactly what `engine.serialization` already
produces and what the corpus already uses. A second encoding here would be a
second thing a client's parser has to get right.

## Why the sequence is the ply

A64-016.5 made the ply the clock's version for the same reason it is the
synchronisation sequence here: it advances exactly when the position changes,
it is already under the match row's lock, and it is already what
`uq_move__ply` serialises on. §2 asks for "the existing monotonic per-Match
sequence" and this is it — there is no second counter to keep in step.

## What is deliberately absent

No move history. A snapshot is *where the game is*, not how it got there —
the durable log answers that and is unbounded, so putting it here would make
the reconnect payload grow with the length of the game, which is precisely
what a snapshot exists to avoid.

No opponent profile, no handles, no ratings. Those are `users`' and are
composed by whoever renders them; a snapshot that carried them would make
`game` depend on a module it has no business knowing about.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.engine import PlayerSide
from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.public.moves import ClockView


@dataclass(frozen=True, slots=True)
class PlacedPiece:
    """One piece on one square, as a client renders it.

    Three strings. Not a `Piece` and not a `BoardCoordinate`, because both
    are engine types and the gateway may hold neither — see this module's
    docstring on why the placement crosses at all when the fingerprint does
    not suffice.
    """

    square: str
    side: str
    rank: str


@dataclass(frozen=True, slots=True)
class DrawOfferState:
    """A standing offer, as a reconnecting client needs to see it.

    Three primitives. Not a `DrawOffer`, because that is a `game` domain
    type and the gateway may not hold one — the same rule that makes the
    position cross as `PlacedPiece` rather than as a `Position`.
    """

    offered_by: PlayerSide
    offered_at_ply: int
    offered_at: datetime


@dataclass(frozen=True, slots=True)
class MatchSnapshot:
    """A live match, complete enough to resume from.

    The **synchronisation baseline** (§6): a client that applies this and
    remembers `sequence` is exactly as up to date as the server was when it
    was built, and everything after that sequence is an incremental event.
    """

    match_id: UUID
    engine_version: int
    variant: ProductVariant
    status: MatchRecordStatus

    sequence: int
    """The match's monotonic sequence — the ply. See this module's
    docstring on why there is no second counter."""

    side_to_move: PlayerSide
    fingerprint: str
    pieces: tuple[PlacedPiece, ...]
    """The position: a comparison key and the placement it describes. A
    client that has both can render the board and check that it agrees."""

    light_player_id: UUID
    dark_player_id: UUID

    clock: ClockView | None
    """The authoritative clock, or `None` for an untimed match — §7.

    Carried so a reconnecting client renders the server's numbers rather
    than extrapolating from its own countdown, which drifted for the whole
    time it was disconnected.
    """

    draw_offer: DrawOfferState | None
    """The standing draw offer and who may act on it — A64-020.5C-pre §9.

    **The facts, not a viewer's permissions.** A snapshot is built by
    `snapshot_of(match_id)` and has no viewer, so making it viewer-relative
    would mean a second read per participant and a reader that could be
    asked for somebody else's view. The projection decides what a given
    client sees — see `gateway.projections`, which has two functions
    precisely so a spectator's cannot accidentally carry this.

    `None` when nothing stands, which is also every match played before
    this phase.
    """

    may_offer_light: bool
    may_offer_dark: bool
    """Whether each side could open a new offer right now — §3, §9.

    Derived here rather than left to the client, because the rule is a ply
    comparison against durable thresholds and a client recomputing it would
    be a second implementation of the spam rule that can disagree with the
    authoritative one.

    The **thresholds themselves are deliberately absent**: they are internal
    bookkeeping, and §9 says not to publish cooldown internals. A client
    needs to know whether the button is enabled, not the arithmetic behind
    it.
    """

    outcome: MatchOutcome | None
    termination_reason: TerminationReason | None
    winner: PlayerSide | None
    """The result, when the match has ended. A client that reconnects to a
    finished game is told so here rather than by sending a move and being
    refused."""

    observed_at: datetime
    """When the server built this. The instant a client corrects its own
    skew against — see `ClockView.server_time`, which carries the same value
    and is the one a countdown is anchored to."""

    def includes(self, player_id: UUID) -> bool:
        """Whether this player is one of the two.

        The membership check a resume needs, on the snapshot rather than at
        the call site, so a match that later grows a third seat changes here
        instead of everywhere.
        """
        return player_id in (self.light_player_id, self.dark_player_id)


class MatchSnapshotReader(Protocol):
    """`game`'s answer to "where is this match" — §1.

    One method. A port that could also list a player's matches would be one
    the gateway could enumerate history with, and it has no reason to.
    """

    async def snapshot_of(self, match_id: UUID) -> MatchSnapshot | None:
        """The match's current state, or `None` if there is no such match.

        `None` rather than a distinguishable "not found" versus "not
        yours": the caller is deciding whether to admit a socket, and a
        reader that reported the difference would make live match
        identifiers enumerable — the same argument `MatchRosterReader` makes.

        Does **not** lock. Two clients resuming at the same instant are not
        competing for anything, so a `FOR UPDATE` here would serialise every
        reconnect on one match for no invariant.
        """
        ...


__all__ = ["DrawOfferState", "MatchSnapshot", "MatchSnapshotReader", "PlacedPiece"]
