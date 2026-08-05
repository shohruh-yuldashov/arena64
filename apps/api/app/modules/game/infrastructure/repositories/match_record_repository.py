"""The SQLAlchemy adapter for `application.ports.MatchRecordRepository`.

Database-only (repositories.md §2): it decides *how* to store and fetch,
never *whether*. Every "is this allowed" question is answered by
`MatchRecord` and `MatchAcceptanceService`.

## Three statements in this file are interesting, and each for a different
## reason

`create` is where A64-015.4 §3 is either true or a comment. It **inserts
and catches**, rather than reading first: two workers retrying one pairing
both pass any read, and only `uq_match__pairing_id` is correct under
concurrency. The loser's `IntegrityError` is translated into a re-read of
the winner's row, so both callers come away with the same `match_id` —
which is the whole of the idempotency contract.

`lock` is `SELECT ... FOR UPDATE` **without** `SKIP LOCKED`, which is the
opposite of every other claim on this platform. A sweeper skipping a locked
row moves on; two players accepting one match have nowhere else to go, so
the second must wait and then see what the first wrote.

`latest_opponent_among` is a `DISTINCT ON` over a `UNION ALL` of the two
player columns, which is the one shape that answers "each of these players'
most recent opponent" in a single statement. The alternatives are a query
per player — the N+1 inside a job that runs several times a second — or a
window function over every match those players have ever had.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.engine import EngineVersion, PlayerSide
from app.modules.game.domain.clock import ClockState, TimeControl
from app.modules.game.domain.match_record import (
    MatchRecord,
    MatchRecordStatus,
    MatchSeat,
    SeatRating,
)
from app.modules.game.domain.variants import MatchOrigin
from app.modules.game.infrastructure.models import MatchRecordModel

logger = logging.getLogger(__name__)

#: The statuses that mean two players **actually met** — QT-3's rematch
#: guard, A64-020.5A.
#:
#: Derived from the enum rather than typed out, so a sixth status has to be
#: classified rather than silently falling on one side. `active` is here as
#: well as `completed`: a game in progress is the strongest possible reason
#: not to pair the same two again.
_PLAYED_STATUSES: Final = (MatchRecordStatus.ACTIVE, MatchRecordStatus.COMPLETED)


class SqlAlchemyMatchRecordRepository:
    """Constructed per use case with the active unit of work's session
    (repositories.md §5.1) — never holds a session longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: MatchRecordModel) -> MatchRecord:
        return MatchRecord(
            id=row.id,
            pairing_id=row.pairing_id,
            variant=row.variant,
            rated=row.rated,
            origin=row.origin,
            origin_ref=row.origin_ref,
            engine_version=EngineVersion(number=row.engine_version),
            light=MatchSeat(
                player_id=row.light_player_id,
                queue_ticket_id=row.light_ticket_id,
                accepted_at=row.light_accepted_at,
                rating=_seat_rating(
                    row.light_rating_value,
                    row.light_rating_deviation,
                    row.light_rating_volatility,
                    row.light_rating_games,
                    row.light_rating_provisional,
                    row.rating_speed_class,
                ),
            ),
            dark=MatchSeat(
                player_id=row.dark_player_id,
                queue_ticket_id=row.dark_ticket_id,
                accepted_at=row.dark_accepted_at,
                rating=_seat_rating(
                    row.dark_rating_value,
                    row.dark_rating_deviation,
                    row.dark_rating_volatility,
                    row.dark_rating_games,
                    row.dark_rating_provisional,
                    row.rating_speed_class,
                ),
            ),
            created_at=row.created_at,
            acceptance_deadline=row.acceptance_deadline,
            status=row.status,
            declined_by=row.declined_by,
            ply_number=row.ply_number,
            time_control=_time_control_of(row),
            clock=_clock_of(row),
            outcome=row.outcome,
            termination_reason=row.termination_reason,
            winner=row.winner,
            ended_at=row.ended_at,
            settled_at=row.settled_at,
        )

    async def create(self, record: MatchRecord) -> tuple[MatchRecord, bool]:
        """Inserts, or returns the match this pairing already produced.

        **Flushes, never commits.** The caller's unit of work spans the
        match and the `game.match_created` outbox row: one transaction,
        because an event for a match that rolled back is a notification
        about a game nobody has (AD-16).

        The flush is what makes the uniqueness violation surface *here*,
        where it can be translated, rather than at the commit inside the
        service's `async with` — which would escape as a raw
        `IntegrityError` and become a 500 on a retry that should have been
        invisible.

        `SAVEPOINT` around the insert, and it is load-bearing rather than
        tidy: PostgreSQL aborts the whole transaction on a constraint
        violation, so without a nested transaction to roll back to, the
        re-read below would fail with `InFailedSQLTransaction` and the
        retry would be a 500 after all.
        """
        row = MatchRecordModel(
            id=record.id,
            pairing_id=record.pairing_id,
            variant=record.variant,
            rated=record.rated,
            origin=record.origin,
            origin_ref=record.origin_ref,
            engine_version=record.engine_version.as_primitive(),
            light_player_id=record.light.player_id,
            light_ticket_id=record.light.queue_ticket_id,
            light_accepted_at=record.light.accepted_at,
            light_rating_value=record.light.rating.value if record.light.rating else None,
            light_rating_deviation=(record.light.rating.deviation if record.light.rating else None),
            light_rating_volatility=(
                record.light.rating.volatility if record.light.rating else None
            ),
            light_rating_games=record.light.rating.games_played if record.light.rating else None,
            light_rating_provisional=(
                record.light.rating.is_provisional if record.light.rating else None
            ),
            dark_player_id=record.dark.player_id,
            dark_ticket_id=record.dark.queue_ticket_id,
            dark_accepted_at=record.dark.accepted_at,
            dark_rating_value=record.dark.rating.value if record.dark.rating else None,
            dark_rating_deviation=record.dark.rating.deviation if record.dark.rating else None,
            dark_rating_volatility=(record.dark.rating.volatility if record.dark.rating else None),
            dark_rating_games=record.dark.rating.games_played if record.dark.rating else None,
            dark_rating_provisional=(
                record.dark.rating.is_provisional if record.dark.rating else None
            ),
            rating_speed_class=record.light.rating.speed_class if record.light.rating else None,
            created_at=record.created_at,
            acceptance_deadline=record.acceptance_deadline,
            status=record.status,
            declined_by=record.declined_by,
            settled_at=record.settled_at,
            ply_number=record.ply_number,
            time_control_initial_ms=(
                record.time_control.initial_ms if record.time_control else None
            ),
            time_control_increment_ms=(
                record.time_control.increment_ms if record.time_control else None
            ),
            clock_light_ms=record.clock.light_ms if record.clock else None,
            clock_dark_ms=record.clock.dark_ms if record.clock else None,
            clock_turn_started_at=record.clock.turn_started_at if record.clock else None,
        )

        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as error:
            existing = await self.by_pairing(record.pairing_id)
            if existing is None:
                # A different constraint refused this — a queue ticket
                # already carrying a match under another pairing id, most
                # likely. There is nothing honest to return, so the failure
                # propagates and `PairingService` compensates.
                raise
            logger.info(
                "match_creation_deduplicated",
                extra={
                    "pairing_id": str(record.pairing_id),
                    "match_id": str(existing.id),
                    "constraint": _constraint_of(error),
                },
            )
            return existing, False

        return self._to_domain(row), True

    async def by_pairing(self, pairing_id: UUID) -> MatchRecord | None:
        """The match created for a pairing, or `None`.

        Served by `uq_match__pairing_id` — the constraint that enforces
        idempotency is the index that answers the question idempotency is
        about.
        """
        row = await self._session.scalar(
            select(MatchRecordModel).where(MatchRecordModel.pairing_id == pairing_id)
        )
        return self._to_domain(row) if row is not None else None

    async def by_id(self, match_id: UUID) -> MatchRecord | None:
        """The match by its own key, without a lock — A64-016.2.

        A primary-key lookup, which is the cheapest read this relation
        has. See the port on why the room-join path must not take the
        `FOR UPDATE` that `lock` does.
        """
        row = await self._session.scalar(
            select(MatchRecordModel).where(MatchRecordModel.id == match_id)
        )
        return self._to_domain(row) if row is not None else None

    async def lock(self, match_id: UUID) -> MatchRecord | None:
        """The match, with its row locked for the caller's transaction.

        `FOR UPDATE` and **not** `SKIP LOCKED` — see this module's
        docstring on why a player has nowhere else to go.
        """
        row = await self._session.scalar(
            select(MatchRecordModel).where(MatchRecordModel.id == match_id).with_for_update()
        )
        return self._to_domain(row) if row is not None else None

    async def pending_for(self, player_id: UUID) -> MatchRecord | None:
        """The match this player must answer, or `None`.

        Served by the two partial indexes on `(player_id) WHERE status =
        'pending_acceptance'`, so the read is bounded by how many people
        are currently being matched rather than by how many games have been
        played.
        """
        row = await self._session.scalar(
            select(MatchRecordModel)
            .where(
                or_(
                    MatchRecordModel.light_player_id == player_id,
                    MatchRecordModel.dark_player_id == player_id,
                ),
                MatchRecordModel.status == MatchRecordStatus.PENDING_ACCEPTANCE,
            )
            .limit(1)
        )
        return self._to_domain(row) if row is not None else None

    async def settle(self, record: MatchRecord) -> bool:
        """Writes an answered match, only if the row still says what it said
        when it was read.

        The compare-and-set is on the three fields an answer moves —
        `status` and the two `accepted_at` instants — rather than on
        `status` alone. Status alone would be too weak: two players
        accepting concurrently both see `pending_acceptance`, and the
        second's write would then silently discard the first's
        `accepted_at` and leave a match that says active with one seat
        blank. The row lock in `lock` is what actually serialises them; this
        is what makes the write safe if the two are ever separated.

        **The clock is written too** — A64-020.5A-pre §14. The second
        acceptance is when a timed game starts, so it is when
        `MatchRecord.accepted_by` replaces the record's clock with one
        running from that instant. Omitting the three columns here left the
        stored clock at its creation-time value, which is a `turn_started_at`
        earlier than `settled_at`: LIGHT charged for however long DARK took
        to answer. Found by `tests/contract/test_time_control.py`.

        The time control itself is deliberately **not** in the `SET`. It is
        creation-time configuration and an answer does not change it; a
        `settle` that could rewrite it would be a path by which accepting a
        match changed the game being played.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(MatchRecordModel)
                .where(
                    MatchRecordModel.id == record.id,
                    MatchRecordModel.status == MatchRecordStatus.PENDING_ACCEPTANCE,
                )
                .values(
                    status=record.status,
                    declined_by=record.declined_by,
                    settled_at=record.settled_at,
                    light_accepted_at=record.light.accepted_at,
                    light_rating_value=record.light.rating.value if record.light.rating else None,
                    light_rating_deviation=(
                        record.light.rating.deviation if record.light.rating else None
                    ),
                    light_rating_volatility=(
                        record.light.rating.volatility if record.light.rating else None
                    ),
                    light_rating_games=record.light.rating.games_played
                    if record.light.rating
                    else None,
                    light_rating_provisional=(
                        record.light.rating.is_provisional if record.light.rating else None
                    ),
                    dark_accepted_at=record.dark.accepted_at,
                    dark_rating_value=record.dark.rating.value if record.dark.rating else None,
                    dark_rating_deviation=record.dark.rating.deviation
                    if record.dark.rating
                    else None,
                    dark_rating_volatility=(
                        record.dark.rating.volatility if record.dark.rating else None
                    ),
                    dark_rating_games=record.dark.rating.games_played
                    if record.dark.rating
                    else None,
                    dark_rating_provisional=(
                        record.dark.rating.is_provisional if record.dark.rating else None
                    ),
                    rating_speed_class=record.light.rating.speed_class
                    if record.light.rating
                    else None,
                    clock_light_ms=record.clock.light_ms if record.clock else None,
                    clock_dark_ms=record.clock.dark_ms if record.clock else None,
                    clock_turn_started_at=record.clock.turn_started_at if record.clock else None,
                )
            ),
        )
        return int(result.rowcount) == 1

    async def advance(self, record: MatchRecord, *, expected_ply: int) -> bool:
        """Writes a match one move further on, only if its ply is still
        `expected_ply` — A64-016.4 §3, §8.

        The compare-and-set that makes "only one move may win for one match
        version" true even if a caller forgets the row lock. `lock` is what
        actually serialises two players, and this is the guard that holds
        when the two are separated — the same belt-and-braces
        `settle` above keeps for the acceptance handshake.

        Writes the settlement columns unconditionally, because a move that
        ends a game advances the ply *and* completes the match, and two
        statements would be a window in which a match is one move on and
        has no result.

        Returns `False` for a losing write. The caller turns that into
        `StaleMatchState` and never retries internally — see
        `LiveMoveService`.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(MatchRecordModel)
                .where(
                    MatchRecordModel.id == record.id,
                    MatchRecordModel.ply_number == expected_ply,
                    MatchRecordModel.status == MatchRecordStatus.ACTIVE,
                )
                .values(
                    status=record.status,
                    ply_number=record.ply_number,
                    clock_light_ms=record.clock.light_ms if record.clock else None,
                    clock_dark_ms=record.clock.dark_ms if record.clock else None,
                    clock_turn_started_at=(record.clock.turn_started_at if record.clock else None),
                    outcome=record.outcome,
                    termination_reason=record.termination_reason,
                    winner=record.winner,
                    ended_at=record.ended_at,
                )
            ),
        )
        return result.rowcount == 1

    async def claim_overdue(self, *, now: datetime, limit: int) -> Sequence[MatchRecord]:
        """Takes up to `limit` overdue pending matches for this worker.

        `SELECT ... FOR UPDATE SKIP LOCKED`, the mechanism the outbox and
        the queue sweep already use and the one A64-015.4 §14 requires.
        Two reconcilers calling this simultaneously receive disjoint sets.

        Ordered by `acceptance_deadline` so a backlog drains in deadline
        order, which is what makes each `MatchAcceptanceExpired` event's
        `occurred_at` agree with the order the relay publishes them in
        (database.md §12.5). Served by `ix_match__pending_deadline`, whose
        predicate matches the status clause exactly.

        **Claiming is not a transition.** The rows come back pending and
        stay that way until `settle` runs; the lock is what excludes another
        worker, and it lasts as long as the caller's transaction.
        """
        overdue = (
            select(MatchRecordModel.id)
            .where(
                MatchRecordModel.status == MatchRecordStatus.PENDING_ACCEPTANCE,
                MatchRecordModel.acceptance_deadline <= now,
            )
            .order_by(MatchRecordModel.acceptance_deadline, MatchRecordModel.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

        claimed_ids = list((await self._session.scalars(overdue)).all())
        if not claimed_ids:
            return ()

        rows = await self._session.scalars(
            select(MatchRecordModel)
            .where(MatchRecordModel.id.in_(claimed_ids))
            .order_by(MatchRecordModel.acceptance_deadline, MatchRecordModel.id)
        )
        return [self._to_domain(row) for row in rows]

    async def settlements_for(self, ticket_ids: Sequence[UUID]) -> Sequence[MatchRecord]:
        """Every match created from one of these queue tickets.

        One statement for the batch, served by the two unique ticket
        indexes. `OR` across the two columns rather than two queries,
        because a ticket may be on either side and the caller does not know
        which.
        """
        if not ticket_ids:
            return ()

        rows = await self._session.scalars(
            select(MatchRecordModel).where(
                or_(
                    MatchRecordModel.light_ticket_id.in_(ticket_ids),
                    MatchRecordModel.dark_ticket_id.in_(ticket_ids),
                )
            )
        )
        return [self._to_domain(row) for row in rows]

    async def by_origin_refs(
        self, origin_refs: Sequence[UUID], *, origin: MatchOrigin
    ) -> Sequence[MatchRecord]:
        """Every match this context created for these references — R-25.

        `origin` is part of the predicate rather than assumed, which is what
        makes `ix_match__origin_ref` — a composite partial index on
        `(origin, origin_ref)` — the one that serves it, and what stops one
        context reading another's matches by guessing a reference.
        """
        if not origin_refs:
            return ()

        rows = await self._session.scalars(
            select(MatchRecordModel).where(
                MatchRecordModel.origin == origin,
                MatchRecordModel.origin_ref.in_(origin_refs),
            )
        )
        return [self._to_domain(row) for row in rows]

    async def latest_opponent_among(self, player_ids: Sequence[UUID]) -> Mapping[UUID, UUID]:
        """Each player's most recent opponent **they actually played**, in
        one statement.

        `DISTINCT ON (player_id) ... ORDER BY player_id, created_at DESC`
        over a `UNION ALL` of the two sides — see this module's docstring
        on why that shape rather than a query per player.

        **A match that began.** `active` or `completed`, and nothing else.
        QT-3's rematch guard exists so a player is not handed the same
        opponent twice in a row, and the question it asks is whether they
        *played* them — so the three statuses excluded are excluded for
        three different reasons, each of which is the same answer:

            pending_acceptance  an offer nobody has answered yet. Treating
                                it as a game played would veto a re-pairing
                                on the strength of a match that may be
                                about to expire
            expired             the window closed and **neither player ever
                                sat down**. Nothing happened
            cancelled           somebody declined. Also nothing happened,
                                and the decliner already paid for it with a
                                cooldown — barring the pair as well is a
                                second penalty for one refusal

        This was `!= pending_acceptance` until A64-020.5A, which is the
        narrowing `specs/matchmaking.md` recorded as owed. The failure it
        produced is permanent rather than transient: two players whose one
        offer lapsed became each other's most recent opponent forever, so
        the guard vetoed every future pairing between them and neither
        could ever meet the other again.
        """
        if not player_ids:
            return {}

        played = MatchRecordModel.status.in_(_PLAYED_STATUSES)
        as_light = select(
            MatchRecordModel.light_player_id.label("player_id"),
            MatchRecordModel.dark_player_id.label("opponent_id"),
            MatchRecordModel.created_at.label("played_at"),
        ).where(MatchRecordModel.light_player_id.in_(player_ids), played)
        as_dark = select(
            MatchRecordModel.dark_player_id.label("player_id"),
            MatchRecordModel.light_player_id.label("opponent_id"),
            MatchRecordModel.created_at.label("played_at"),
        ).where(MatchRecordModel.dark_player_id.in_(player_ids), played)

        sides = as_light.union_all(as_dark).subquery("sides")
        latest = (
            select(sides.c.player_id, sides.c.opponent_id)
            .distinct(sides.c.player_id)
            .order_by(sides.c.player_id, sides.c.played_at.desc())
        )

        rows = await self._session.execute(latest)
        return {player_id: opponent_id for player_id, opponent_id in rows.all()}


def _time_control_of(row: MatchRecordModel) -> TimeControl | None:
    """The row's time control, or `None` for an untimed match.

    Both columns are null together — `ck_match__clock_iff_time_control` —
    so reading one is enough to decide, and the other is narrowed rather
    than re-checked.
    """
    if row.time_control_initial_ms is None:
        return None
    return TimeControl(
        initial_ms=row.time_control_initial_ms,
        increment_ms=row.time_control_increment_ms or 0,
    )


def _clock_of(row: MatchRecordModel) -> ClockState | None:
    """The row's clock, or `None` for an untimed match.

    `active_side` is **derived from ply parity** rather than stored: LIGHT
    moves first, so an even ply count means LIGHT is to move. Storing it
    would be a third copy of one fact — see `MatchRecordModel`.
    """
    if row.clock_light_ms is None or row.clock_turn_started_at is None:
        return None
    return ClockState(
        light_ms=row.clock_light_ms,
        dark_ms=row.clock_dark_ms or 0,
        active_side=PlayerSide.LIGHT if row.ply_number % 2 == 0 else PlayerSide.DARK,
        turn_started_at=row.clock_turn_started_at,
    )


def _constraint_of(error: IntegrityError) -> str:
    """The name of the constraint a driver error names, for the log line.

    Diagnostic only — the decision above is made by whether a row for this
    `pairing_id` exists, not by a string — so an unrecognised shape
    degrades to a placeholder rather than raising inside error handling.
    """
    name = getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)
    return str(name) if name else "unknown"


__all__ = ["SqlAlchemyMatchRecordRepository"]


def _seat_rating(
    value: float | None,
    deviation: float | None,
    volatility: float | None,
    games_played: int | None,
    is_provisional: bool | None,
    speed_class: str | None,
) -> SeatRating | None:
    """A row's seat columns as a snapshot, or `None` if it has none.

    **All or nothing.** A match created before A64-017.2 has every column
    null; a partially-written seat would be a snapshot the rating
    calculation could not use, so it is treated as absent rather than
    reconstructed with defaults — a default here would be a made-up rating
    on a permanent record.
    """
    if (
        value is None
        or deviation is None
        or volatility is None
        or games_played is None
        or is_provisional is None
        or speed_class is None
    ):
        return None
    return SeatRating(
        value=value,
        deviation=deviation,
        volatility=volatility,
        games_played=games_played,
        is_provisional=is_provisional,
        speed_class=speed_class,
    )
