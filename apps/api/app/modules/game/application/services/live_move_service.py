"""`LiveMoveService` — validating and applying one live move. A64-016.3 §5, §6.

The only place on the platform where a move submitted over a socket meets the
rules. The gateway holds `SubmitMoveUseCase` and can do nothing but call this;
everything about whose turn it is, whether a path is playable, what it takes
and whether it crowns is decided here, by the engine collaborators `game`
already owns.

## The order of the checks, and why it is that order

    1. the match exists and this player is in it   -> MatchNotFound
    2. the match is being played                   -> MatchNotActive
    3. the player owns the side to move            -> NotYourTurn
    4. the path is a legal move here               -> IllegalMoveSubmitted
    5. nobody else wrote first                     -> StaleMatchState

Identity, then state, then rules. A caller who may not see a match learns
nothing about it — step 1 collapses "no such match" and "not yours" for the
reason `MatchAcceptanceUseCase.accept` does — and a caller whose turn it is
not is refused *before* the engine tells them whether their move would have
been legal, which would otherwise be a free rules oracle against a position
they are not entitled to reason about.

## Server-derived captures, by construction

The client sends a path. This asks the generator for the legal moves in the
position and looks for the one whose path matches. So `captured` and
`promotes_to` are the **generator's**, never the client's: a tampered client
cannot claim to have taken a piece it did not jump, because it is not asked.

That also means an illegal path and a legal path with lying captures are the
same failure, which is correct — both are "no legal move has this path".

## Concurrency: compare-and-set on the ply

`LiveMatchStore.advance` writes only if the stored ply is still the one that
was read. Two moves submitted against the same state produce one write and
one `StaleMatchState`, decided inside Redis.

A process-local lock is forbidden by §6 and would be wrong anyway: the two
players of a match may be on different gateway nodes, so a lock in one
process guards nothing.

**The read is not locked and does not need to be.** A stale read produces a
failed CAS, which is exactly the outcome a lock would have prevented by
waiting — and waiting is worse here, because the loser's move has almost
certainly become illegal in the meantime and the honest answer is to say so.

## What is deliberately not here

No clock, no timeout adjudication, no terminal-state detection, no durable
move log, no match completion. Each is out of A64-016.3's scope, and the
absence of the move log is the significant one — see `LiveMatchStore` and
`docs/01-architecture/websocket.md` §16 for what it costs.
"""

import logging

from app.modules.engine import (
    BoardCoordinate,
    InvalidCoordinate,
    Move,
    MoveApplier,
    MoveGenerator,
    PlayerSide,
    Position,
    initial_board,
)
from app.modules.game.application.ports import (
    LiveMatchState,
    LiveMatchStore,
    MatchRecordRepository,
)
from app.modules.game.domain.exceptions import (
    IllegalMoveSubmitted,
    MatchNotActive,
    MatchNotFound,
    NotYourTurn,
    StaleMatchState,
)
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus
from app.modules.game.domain.variants import board_variant_of
from app.modules.game.public.moves import (
    AppliedMove,
    SubmitMoveRequest,
    SubmitMoveResult,
)

logger = logging.getLogger(__name__)


class LiveMoveService:
    """`SubmitMoveUseCase` over the live position store and the engine."""

    def __init__(
        self,
        *,
        matches: MatchRecordRepository,
        live: LiveMatchStore,
        generator: MoveGenerator,
        applier: MoveApplier,
        live_state_ttl_seconds: int,
    ) -> None:
        self._matches = matches
        self._live = live
        self._generator = generator
        self._applier = applier
        self._live_state_ttl_seconds = live_state_ttl_seconds

    async def submit(self, request: SubmitMoveRequest) -> SubmitMoveResult:
        """Validates and applies one move. See `SubmitMoveUseCase.submit`."""
        record = await self._matches.by_id(request.match_id)
        if record is None or not _includes(record, request.player_id):
            # One failure for both, so live match identifiers are not
            # enumerable by the difference between two responses.
            raise MatchNotFound("No such match.")

        if record.status is not MatchRecordStatus.ACTIVE:
            raise MatchNotActive(f"The match is {record.status.value}.")

        state = await self._live.load(request.match_id) or self._seeded(record)
        side = _side_of(record, request.player_id)
        if state.position.side_to_move is not side:
            raise NotYourTurn("It is not your turn.")

        move = self._legal_move_for(state.position, request.path)
        applied = LiveMatchState(
            position=self._applier.apply(state.position, move), ply=state.ply + 1
        )

        written = await self._live.advance(
            request.match_id,
            state=applied,
            expected_ply=state.ply,
            ttl_seconds=self._live_state_ttl_seconds,
        )
        if not written:
            # Another writer got there first. Never retried here — see this
            # module's docstring on why the honest answer is to say so.
            #
            # Not counted here. `MATCH_OUTCOMES` is the acceptance
            # handshake's closed three-member enum and widening it would
            # put two unrelated questions in one series; the gateway counts
            # this as a rejection category, which is where §15 asks for it
            # and where every other realtime measurement already lives.
            raise StaleMatchState("The match moved on; re-read and retry.")

        # `INFO`, once per move, carrying no board and no path: the payload
        # is the game and a log of every move would be a searchable record
        # of one (§15). The ply is what an operator correlates on.
        logger.info(
            "live_move_applied",
            extra={"match_id": str(request.match_id), "ply": applied.ply},
        )
        return _result_for(request, state=applied, move=move)

    def _seeded(self, record: MatchRecord) -> LiveMatchState:
        """The opening position, for a match nobody has moved in.

        **Lazy rather than seeded on activation**, and that is a deliberate
        trade. Writing the position when a match becomes `active` would
        need an outbox consumer, and a consumer that had not run yet would
        make the first move of a game fail for a reason the player cannot
        act on. Deriving it is deterministic — the variant fixes the
        opening — so the first mover simply computes what the consumer
        would have written.

        Two nodes seeding concurrently is safe: both compute the same
        position, both call `advance` with `expected_ply=0`, and exactly
        one write lands.
        """
        return LiveMatchState(
            position=Position(
                board=initial_board(board_variant_of(record.variant)),
                side_to_move=PlayerSide.LIGHT,
            ),
            ply=0,
        )

    def _legal_move_for(self, position: Position, path: tuple[str, ...]) -> Move:
        """The legal move whose path is `path`, or `IllegalMoveSubmitted`.

        Asks the generator and matches on the path, which is what makes the
        captures and the promotion server-derived — see this module's
        docstring.

        A malformed square is the **same** failure as an illegal move, not
        a validation error. From the client's side both mean "that is not a
        move you can play here", and a distinct code would tell a prober
        the difference between a square that does not exist and one that
        does but is empty.
        """
        try:
            squares = tuple(BoardCoordinate.parse(square) for square in path)
        except (InvalidCoordinate, ValueError) as exc:
            raise IllegalMoveSubmitted("That is not a legal move.") from exc

        for candidate in self._generator.legal_moves(position):
            if candidate.path == squares:
                return candidate

        # The detail is the operator's, the code is the client's
        # (CLAUDE.md §9.7). No board and no path in the log — see §15.
        logger.debug("live_move_rejected", extra={"squares": len(squares)})
        raise IllegalMoveSubmitted("That is not a legal move.")


def _includes(record: MatchRecord, player_id: object) -> bool:
    return player_id in (record.light.player_id, record.dark.player_id)


def _side_of(record: MatchRecord, player_id: object) -> PlayerSide:
    """Which side this player holds. Called only after `_includes`."""
    return PlayerSide.LIGHT if record.light.player_id == player_id else PlayerSide.DARK


def _result_for(
    request: SubmitMoveRequest, *, state: LiveMatchState, move: Move
) -> SubmitMoveResult:
    """The published result for a move that was applied.

    Renders every engine value as a primitive here rather than at the
    gateway, so the transport never holds a `BoardCoordinate` or a
    `PieceRank` — which is what keeps `.importlinter`'s gateway contract
    true of the *data* as well as of the imports.
    """
    return SubmitMoveResult(
        match_id=request.match_id,
        ply=state.ply,
        side_to_move=state.position.side_to_move,
        fingerprint=state.position.fingerprint,
        applied=AppliedMove(
            path=tuple(str(square) for square in move.path),
            captured=tuple(str(square) for square in move.captured),
            promoted_to=move.promotes_to.value if move.promotes_to is not None else None,
        ),
    )


__all__ = ["LiveMoveService"]
