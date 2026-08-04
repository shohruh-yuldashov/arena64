"""`LiveMoveService` — one move, validated, applied, logged and settled.
A64-016.4 §3, §5, §6.

A64-016.3 built this over a bare `MoveApplier` and a Redis compare-and-set:
it applied moves and detected nothing terminal, and its own documentation
recorded both gaps. This is the rewrite that closes them, and the shape
changed in one important way — **it now plays through the `Match` aggregate**
rather than around it.

## Why `Match.play` rather than `applier.apply`

Because `Match.play` already is the flow §5 describes, and has been since
A64-014.7:

    apply the move  ->  ask the terminal evaluator  ->  ask the draw rules

including §5's "a decisive terminal result must remain higher priority than a
draw" — the evaluator is asked first and a win short-circuits, because a
position can be both a third repetition and a win for the side that just
moved, and a game that was won is not a game that was drawn.

Reimplementing that sequence here would have been the duplication §5 forbids
("do not re-derive legal-move or remaining-piece rules"), and it would have
been a second copy of a priority rule that only shows its absence in a game
somebody loses.

It also makes replay and live play **the same code path**: `ReplayEngine`
calls `match.play` too, so a game replayed from the log reaches the result it
reached when it was played, by construction rather than by agreement.

## The transaction — §3

One PostgreSQL transaction, in this order:

    1. lock the match row              FOR UPDATE, no SKIP LOCKED
    2. rebuild the Match aggregate     from the durable log
    3. match.play(...)                 validate, apply, evaluate, settle
    4. append the move row             uq_move__ply refuses a duplicate ply
    5. advance or complete the match    compare-and-set on ply_number
    6. enqueue the outbox events        same transaction (AD-16)

"Match advanced but move record missing" is impossible because 4 and 5 are
one transaction. The acknowledgement is the caller's and is sent **after**
commit — §7 — which is why this returns a result rather than sending one.

## Concurrency — §8

Three mechanisms, and each covers what the others cannot:

    the row lock       serialises two players of one match. `FOR UPDATE`
                       without `SKIP LOCKED`, because the second submitter
                       has nowhere else to go and must wait and then see
                       what the first wrote
    uq_move__ply       refuses a second row for one ply, whatever took the
                       lock. §2 forbids in-memory deduplication, and this is
                       the mechanism rather than a check
    advance's CAS      refuses a match write whose ply moved, which holds if
                       a future path forgets the lock

A process-local lock is forbidden (§8) and would be wrong anyway: the two
players may be served by different gateway nodes.

## Why the aggregate is rebuilt from the log rather than cached

A64-016.3 kept the position in Redis and treated it as authoritative. It is
now a **cache of a replay**: the durable log is the source, and
`_rebuild` replays it. That is what closes AD-19's gap — a Redis failure
costs a rebuild rather than a game.

The cost is a replay per submission, O(plies). It is paid deliberately and
is the reason `LiveMatchStore` still exists: `LiveMoveService` reads the
cache when it is warm and rebuilds when it is not, and either way the
durable log decides. The cache can never be *wrong* in a way that matters,
because the ply it carries is checked against the match row under the lock.
"""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.core.clock import Clock
from app.modules.engine import (
    BoardCoordinate,
    InvalidCoordinate,
    Move,
    MoveApplier,
    MoveGenerator,
    PlayerSide,
    Position,
    TerminalStateEvaluator,
    initial_board,
)
from app.modules.game.application.ports import (
    ClockDeadlineStore,
    LiveMatchState,
    LiveMatchStore,
    LoggedMove,
    MatchRecordRepository,
    MoveLogRepository,
)
from app.modules.game.domain.clock import ClockState
from app.modules.game.domain.draws import DrawRuleSet
from app.modules.game.domain.events import MatchCompleted, MoveApplied, SeatSummary
from app.modules.game.domain.exceptions import (
    ClockExpired,
    IllegalMoveSubmitted,
    MatchNotActive,
    MatchNotFound,
    NotYourTurn,
    StaleMatchState,
)
from app.modules.game.domain.match import Match, MatchStatus
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.domain.move_log import MoveRecord
from app.modules.game.domain.result import MatchResult
from app.modules.game.domain.variants import board_variant_of
from app.modules.game.public.moves import (
    AppliedMove,
    ClockView,
    SubmitMoveRequest,
    SubmitMoveResult,
)
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


class LiveMoveService:
    """`SubmitMoveUseCase` over the durable log, the engine and the outbox."""

    def __init__(
        self,
        *,
        matches: MatchRecordRepository,
        moves: MoveLogRepository,
        live: LiveMatchStore,
        deadlines: ClockDeadlineStore,
        events: EventPublisher,
        generator: MoveGenerator,
        applier: MoveApplier,
        evaluator: TerminalStateEvaluator,
        draw_rules: DrawRuleSet,
        clock: Clock,
        live_state_ttl_seconds: int,
    ) -> None:
        self._matches = matches
        self._moves = moves
        self._live = live
        self._deadlines = deadlines
        self._events = events
        self._generator = generator
        self._applier = applier
        self._evaluator = evaluator
        self._draw_rules = draw_rules
        self._clock = clock
        self._live_state_ttl_seconds = live_state_ttl_seconds

    async def submit(self, request: SubmitMoveRequest) -> SubmitMoveResult:
        """Validates, applies, logs and settles one move.

        Does **not** commit: the caller's unit of work spans everything
        written here, which is what makes §3's atomicity a property of the
        transaction rather than of the ordering below.
        """
        # **First**, before anything that could take time. MT-9 makes the
        # gateway receive instant authoritative, and a fallback taken here
        # rather than after the lock is what keeps an untimed caller from
        # being charged for the lock it waited on.
        received_at = request.received_at or self._clock.now()

        record = await self._locked(request)
        aggregate = await self._rebuild(record)

        side = _side_of(record, request.player_id)
        if aggregate.side_to_move is not side:
            raise NotYourTurn("It is not your turn.")

        # §4 step 3, and it is before the engine deliberately: a move from a
        # player whose flag had already fallen when the frame arrived is not
        # a legal move that loses, it is a move that never happened.
        if record.clock is not None and record.clock.has_flagged(received_at):
            raise ClockExpired("Your time ran out.")

        move = self._legal_move_for(aggregate.position, request.path)
        aggregate.play(move, self._applier, self._evaluator, self._draw_rules)

        clock = _charged(record, at=received_at)
        at = self._clock.now()
        await self._append(
            record, aggregate, move=move, side=side, at=at, received_at=received_at, clock=clock
        )
        settled = await self._advance(
            record, aggregate, expected_ply=record.ply_number, at=at, clock=clock
        )
        await self._publish(settled, aggregate, move=move, side=side, at=at)
        await self._reschedule(settled, clock=clock)
        await self._cache(aggregate, match_id=record.id)

        return _result_for(
            request, aggregate=aggregate, move=move, settled=settled, clock=clock, at=at
        )

    async def _locked(self, request: SubmitMoveRequest) -> MatchRecord:
        """The match row, locked for this transaction — §3, §8.

        `FOR UPDATE` and **not** `SKIP LOCKED`, which is the same choice
        A64-015.4 made for acceptance and for the same reason: a sweeper
        that skips a locked row moves on to another one, and the second
        player of a match has nowhere else to go. They must wait and then
        see what the first wrote — which is how a move submitted against a
        stale view becomes `NotYourTurn` rather than a silent overwrite.

        The two refusals here are §5's first two steps and are ordered
        identity-before-state: a caller who may not see a match learns
        nothing about it.
        """
        record = await self._matches.lock(request.match_id)
        if record is None or not _includes(record, request.player_id):
            raise MatchNotFound("No such match.")

        # §6: "reject all later move submissions". A completed match is
        # caught here, under the lock, so a move racing the settlement of
        # its own game is refused rather than applied to a finished board.
        if record.status is not MatchRecordStatus.ACTIVE:
            raise MatchNotActive(f"The match is {record.status.value}.")

        return record

    async def _rebuild(self, record: MatchRecord) -> Match:
        """The match as an aggregate, ready to play a move into.

        From the **durable log**, which is what makes the log authoritative
        rather than decorative — A64-016.3's Redis state was the source and
        a Redis failure lost the game.

        Replayed rather than reconstructed field by field, because the draw
        rules read `plies_since_progress` and a repetition count, and both
        are derived from the sequence of moves. §4 forbids persisting them
        ("replay must rebuild them by applying the log"), so replaying is
        not a fallback here — it is the only way to get them right.

        The engine collaborators are this service's own, so a replay and a
        live move apply the identical rules.
        """
        aggregate = Match(
            variant=board_variant_of(record.variant),
            engine_version=record.engine_version,
            position=Position(
                board=initial_board(board_variant_of(record.variant)),
                side_to_move=PlayerSide.LIGHT,
            ),
        )
        aggregate.start()

        for logged in await self._moves.for_replay(record.id):
            aggregate.play(logged.move, self._applier, self._evaluator, self._draw_rules)

        return aggregate

    async def _append(
        self,
        record: MatchRecord,
        aggregate: Match,
        *,
        move: Move,
        side: PlayerSide,
        at: datetime,
        received_at: datetime,
        clock: ClockState | None,
    ) -> None:
        """Writes the move row — §1, and its clock readings — A64-016.5 §3.

        `uq_move__ply` is what refuses a duplicate, and its refusal is
        caught here and reported as `StaleMatchState`: a second submission
        for one ply is not an internal error, it is the ordinary outcome of
        two clients racing, and the client's recourse is to re-read and
        retry.

        The clock readings go in **in the same transaction**, which AD-05
        requires ("capturable only at move time") and §3 restates ("do not
        backfill these values later"). They are `None` for an untimed match
        and never for a timed one, which is what makes "null means no clock"
        readable rather than ambiguous.
        """
        entry = LoggedMove(
            record=MoveRecord(
                ply_number=aggregate.ply_number,
                move=move,
                resulting_position_hash=aggregate.position.fingerprint,
                think_time_ms=(record.clock.elapsed_ms(received_at) if record.clock else None),
                remaining_clock_ms=clock.remaining(side) if clock else None,
            ),
            seat=side,
            engine_version=record.engine_version,
            created_at=at,
            received_at=received_at,
        )

        try:
            await self._moves.append(record.id, entry)
        except IntegrityError as conflict:
            # The unique index fired: another writer took this ply. Chained
            # so the constraint name survives into the log, where it is the
            # difference between "two players raced" and "the schema is
            # wrong".
            raise StaleMatchState("The match moved on; re-read and retry.") from conflict

    async def _advance(
        self,
        record: MatchRecord,
        aggregate: Match,
        *,
        expected_ply: int,
        at: datetime,
        clock: ClockState | None = None,
    ) -> MatchRecord:
        """Writes the match's new ply, and its result if the game ended.

        One statement for both, because a move that ends a game advances
        the ply *and* completes the match — two writes would be a window in
        which a match is one move on and has no result.

        A losing compare-and-set is `StaleMatchState`. It should be
        unreachable under the row lock, and it is kept because §8 asks for
        the guarantee rather than for the lock: a future path that reads
        without locking gets a refusal instead of a silent overwrite.
        """
        advanced = (
            record.completed(
                _result_of(aggregate), ply_number=aggregate.ply_number, at=at, clock=clock
            )
            if aggregate.status is MatchStatus.COMPLETED
            else record.advanced(ply_number=aggregate.ply_number, clock=clock)
        )

        if not await self._matches.advance(advanced, expected_ply=expected_ply):
            raise StaleMatchState("The match moved on; re-read and retry.")

        return advanced

    async def _publish(
        self,
        record: MatchRecord,
        aggregate: Match,
        *,
        move: Move,
        side: PlayerSide,
        at: datetime,
    ) -> None:
        """Stages the durable events — §10, AD-16.

        In the **same transaction** as the move row and the match write, so
        an event for a move that rolled back cannot exist. That is the whole
        of the transactional outbox and the reason nothing here publishes
        directly.

        `MatchCompleted` is emitted only on the ply that ends the game, so a
        consumer counting completions counts games rather than moves.
        """
        await self._events.publish(
            MoveApplied(
                occurred_at=at,
                match_id=record.id,
                ply_number=aggregate.ply_number,
                side=side,
                path=tuple(str(square) for square in move.path),
                resulting_position_hash=aggregate.position.fingerprint,
            )
        )

        result = record.result
        if result is None:
            return

        await self._events.publish(
            MatchCompleted(
                occurred_at=at,
                match_id=record.id,
                variant=record.variant,
                rated=record.rated,
                outcome=result.outcome,
                termination_reason=result.reason,
                winner=result.winner,
                ply_number=aggregate.ply_number,
                engine_version=record.engine_version.as_primitive(),
                speed_class=record.light.rating.speed_class if record.light.rating else None,
                light=_seat_summary(record.light),
                dark=_seat_summary(record.dark),
            )
        )
        logger.info(
            "match_completed",
            extra={
                "match_id": str(record.id),
                "outcome": result.outcome.value,
                "reason": result.reason.value,
                "plies": aggregate.ply_number,
            },
        )

    async def _reschedule(self, record: MatchRecord, *, clock: ClockState | None) -> None:
        """Moves the match's deadline to the new active side — §5.

        Cancelled rather than rescheduled once the match completes, which is
        §5's "remove deadline after Match completion": a finished game with a
        live deadline is one a worker will claim and then correctly refuse,
        which is work nobody needs done.

        **Never raises.** The move is already applied and about to be
        committed, so a deadline that could not be written is a match that
        will not flag — bad, and strictly better than a move that fails
        after it was made. A rising `clock_deadline_write_failed` is what
        makes it visible.
        """
        if clock is None:
            return

        try:
            if record.status is not MatchRecordStatus.ACTIVE:
                await self._deadlines.cancel(record.id)
                return

            await self._deadlines.schedule(
                record.id,
                ply_number=record.ply_number,
                side=clock.active_side,
                deadline=clock.deadline(),
            )
        except Exception as exc:  # noqa: BLE001 — a deadline must not fail a move
            logger.error(
                "clock_deadline_write_failed",
                extra={"match_id": str(record.id), "error": type(exc).__name__},
                exc_info=exc,
            )

    async def _cache(self, aggregate: Match, *, match_id: UUID) -> None:
        """Warms the live-position cache.

        **Never raises, and never affects the answer.** The durable log is
        the source since A64-016.4; this is a read-through cache whose only
        job is to save a replay, so a Redis failure costs one rebuild.

        Written unconditionally rather than only when it changed, because
        the write is what refreshes the TTL — a game where both players
        think for a long time must not lapse mid-move.
        """
        try:
            await self._live.advance(
                match_id,
                state=LiveMatchState(position=aggregate.position, ply=aggregate.ply_number),
                expected_ply=aggregate.ply_number - 1,
                ttl_seconds=self._live_state_ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — a cache must not fail a move
            logger.warning(
                "live_match_cache_write_failed",
                extra={"match_id": str(match_id), "error": type(exc).__name__},
            )

    def _legal_move_for(self, position: Position, path: tuple[str, ...]) -> Move:
        """The legal move whose path is `path`, or `IllegalMoveSubmitted`.

        Asks the generator and matches on the path, which is what makes the
        captures and the promotion **server-derived**: the client sends
        squares, and what it took and whether it crowned come from the
        engine. A tampered client cannot claim a capture it did not make,
        because it is not asked.

        A malformed square is the same failure as an illegal move. From the
        client's side both mean "that is not a move you can play here", and
        a distinct code would tell a prober the difference between a square
        that does not exist and one that does but is empty.
        """
        try:
            squares = tuple(BoardCoordinate.parse(square) for square in path)
        except (InvalidCoordinate, ValueError) as exc:
            raise IllegalMoveSubmitted("That is not a legal move.") from exc

        for candidate in self._generator.legal_moves(position):
            if candidate.path == squares:
                return candidate

        # The detail is the operator's, the code is the client's
        # (CLAUDE.md §9.7). No board and no path in the log.
        logger.debug("live_move_rejected", extra={"squares": len(squares)})
        raise IllegalMoveSubmitted("That is not a legal move.")


def _includes(record: MatchRecord, player_id: UUID) -> bool:
    return player_id in (record.light.player_id, record.dark.player_id)


def _side_of(record: MatchRecord, player_id: UUID) -> PlayerSide:
    """Which side this player holds. Called only after `_includes`."""
    return PlayerSide.LIGHT if record.light.player_id == player_id else PlayerSide.DARK


def _result_of(aggregate: Match) -> MatchResult:
    """The result of a completed aggregate.

    Narrowed here rather than at the call site so the `None` case — which
    cannot happen for a `COMPLETED` match, because `Match._complete` and
    `Match._draw` both set one — is stated once.
    """
    result = aggregate.result
    if result is None:  # pragma: no cover — a completed match has a result
        raise StaleMatchState("The match completed without a result.")
    return result


def _charged(record: MatchRecord, *, at: datetime) -> ClockState | None:
    """The clock after this move, or `None` for an untimed match.

    `at` is the mover's `received_at`, never the instant this ran — MT-9,
    and the whole of §7's guarantee: the elapsed time charged is what the
    player actually spent, and the platform's own delay between receiving
    the frame and committing it is charged to nobody.
    """
    if record.clock is None or record.time_control is None:
        return None
    return record.clock.charged(record.time_control, at=at)


def _clock_view(clock: ClockState | None, *, at: datetime) -> ClockView | None:
    """The clock as a client renders it. `None` for an untimed match."""
    if clock is None:
        return None
    return ClockView(
        light_ms=clock.light_ms,
        dark_ms=clock.dark_ms,
        active_side=clock.active_side,
        deadline=clock.deadline(),
        server_time=at,
    )


def _result_for(
    request: SubmitMoveRequest,
    *,
    aggregate: Match,
    move: Move,
    settled: MatchRecord,
    clock: ClockState | None = None,
    at: datetime,
) -> SubmitMoveResult:
    """The published result for a move that was applied.

    Renders every engine value as a primitive here, so the gateway never
    holds a `BoardCoordinate` or a `PieceRank` — which keeps
    `.importlinter`'s gateway contract true of the *data* as well as the
    imports.
    """
    result = settled.result
    return SubmitMoveResult(
        match_id=request.match_id,
        ply=aggregate.ply_number,
        side_to_move=aggregate.position.side_to_move,
        fingerprint=aggregate.position.fingerprint,
        applied=AppliedMove(
            path=tuple(str(square) for square in move.path),
            captured=tuple(str(square) for square in move.captured),
            promoted_to=move.promotes_to.value if move.promotes_to is not None else None,
        ),
        clock=_clock_view(clock, at=at),
        outcome=result.outcome if result is not None else None,
        termination_reason=result.reason if result is not None else None,
        winner=result.winner if result is not None else None,
    )


__all__ = ["LiveMoveService"]


def _seat_summary(seat: MatchSeat) -> SeatSummary | None:
    """A seat's persisted rating snapshot, for the completion event.

    `None` when the seat has none — a match created before A64-017.2.
    `rating` treats that as "not rateable", which is correct: nothing
    captured what these players rated, so nothing can compute what they
    should rate now without inventing it.
    """
    if seat.rating is None:
        return None
    return SeatSummary(
        player_id=seat.player_id,
        rating_value=seat.rating.value,
        rating_deviation=seat.rating.deviation,
        rating_volatility=seat.rating.volatility,
        games_played=seat.rating.games_played,
        is_provisional=seat.rating.is_provisional,
    )
