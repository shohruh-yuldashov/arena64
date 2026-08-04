"""The durable move log against real PostgreSQL — A64-016.4 §11.

A64-016.3's known-gaps list named this as the thing to build before a real
game is played: AD-18 pairs the Redis live position with a durable move log,
and without the second half a Redis failure loses an in-flight game with
nothing to replay from.

The real `LiveMoveService`, the real engine collaborators, the real
repositories and real PostgreSQL run here. What is substituted is the Redis
live-position **cache** — which is now a cache, so substituting it proves the
log is authoritative rather than hiding that it is not.

Five tests for §11's seven persistence requirements, because §11 caps this
task at eight and the bus takes one. Each carries the assertions belonging to
one *mechanism* — the transaction, the unique index, the round trip, the
settlement, and the replay — rather than one per line.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.identifiers import generate_uuid7
from app.modules.engine import CURRENT_ENGINE_VERSION, PlayerSide
from app.modules.game.application.ports import LiveMatchState, LoggedMove
from app.modules.game.application.services import LiveMoveService, PersistedMatchReplay
from app.modules.game.domain.match import MatchStatus
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.domain.result import MatchOutcome
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.infrastructure import MoveLogModel
from app.modules.game.infrastructure.repositories import (
    SqlAlchemyMatchRecordRepository,
    SqlAlchemyMoveLogRepository,
)
from app.modules.game.presentation.dependencies import SessionScopedLiveMoves
from app.modules.game.public import (
    IllegalMoveSubmitted,
    MatchNotActive,
    SubmitMoveRequest,
    SubmitMoveResult,
    engine_services,
)
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
WINDOW = timedelta(seconds=30)

#: The opening moves of a Russian 8x8 game, in the order LIGHT then DARK.
#: Real legal moves rather than invented ones — the engine generates them and
#: §11 forbids duplicating its rule tests, so these are here to *drive* the
#: persistence rather than to assert anything about the rules.
OPENING = (("c3", "d4"), ("b6", "a5"), ("d4", "c5"))


class _NullLiveCache:
    """A `LiveMatchStore` that remembers nothing.

    Deliberately empty, and that is the point of using it: the live position
    is a **cache** since A64-016.4, so a store that never returns anything
    forces every submission to rebuild the aggregate from the durable log.
    Every test below therefore exercises the path a Redis failure takes.
    """

    async def load(self, match_id: UUID) -> LiveMatchState | None:
        return None

    async def advance(
        self, match_id: UUID, *, state: LiveMatchState, expected_ply: int, ttl_seconds: int
    ) -> bool:
        return True


class _RecordingEvents:
    """An `EventPublisher` that keeps what was staged.

    Real outbox rows would work and would make every assertion below also an
    assertion about `platform.outbox`. What matters here is *which* events
    the transaction stages and that it stages them at all — the outbox's own
    durability is `tests/contract/test_outbox_repository.py`'s.
    """

    def __init__(self) -> None:
        self.published: list[object] = []

    async def publish(self, event: object) -> object:
        self.published.append(event)
        return event

    def of_type(self, event_type: str) -> list[object]:
        return [
            event
            for event in self.published
            if getattr(type(event), "event_type", None) == event_type
        ]


@pytest_asyncio.fixture
async def matches(contract_session: AsyncSession) -> SqlAlchemyMatchRecordRepository:
    return SqlAlchemyMatchRecordRepository(contract_session)


@pytest_asyncio.fixture
async def moves(contract_session: AsyncSession) -> SqlAlchemyMoveLogRepository:
    return SqlAlchemyMoveLogRepository(contract_session)


@pytest_asyncio.fixture
def events() -> _RecordingEvents:
    return _RecordingEvents()


def _service(contract_session: AsyncSession, events: _RecordingEvents) -> LiveMoveService:
    """The real service over real repositories and the real engine."""
    engine = engine_services()
    return LiveMoveService(
        matches=SqlAlchemyMatchRecordRepository(contract_session),
        moves=SqlAlchemyMoveLogRepository(contract_session),
        live=_NullLiveCache(),
        events=events,  # type: ignore[arg-type]
        generator=engine.generator,
        applier=engine.applier,
        evaluator=engine.terminal,
        draw_rules=engine.draw_rules,
        clock=MovableClock(NOW),
        live_state_ttl_seconds=3600,
    )


async def _active_match(
    matches: SqlAlchemyMatchRecordRepository, *, light: UUID, dark: UUID
) -> MatchRecord:
    """A match both players accepted, ready to be played."""
    record = MatchRecord(
        pairing_id=generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(player_id=light, queue_ticket_id=generate_uuid7(), accepted_at=NOW),
        dark=MatchSeat(player_id=dark, queue_ticket_id=generate_uuid7(), accepted_at=NOW),
        created_at=NOW,
        acceptance_deadline=NOW + WINDOW,
        status=MatchRecordStatus.ACTIVE,
        settled_at=NOW,
    )
    stored, _ = await matches.create(record)
    return stored


class TestTheDurableMoveLog:
    async def test_an_accepted_move_writes_one_row_carrying_its_whole_path(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        events: _RecordingEvents,
    ) -> None:
        """§11.1 and §11.3 together — one row, and the path survives it.

        The path is the whole point of the column (R-15). A multi-jump
        reaches its destination by a specific route capturing specific
        pieces, and two routes can share endpoints — so an origin and a
        destination produce an archive that cannot replay its own games.

        Asserted against the **row** rather than the service's return value,
        because a service that reported a path it did not store would pass
        every assertion made on what it returned.
        """
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(matches, light=light, dark=dark)
        service = _service(contract_session, events)

        await service.submit(
            SubmitMoveRequest(match_id=record.id, player_id=light, path=("c3", "d4"))
        )

        rows = (
            await contract_session.scalars(
                select(MoveLogModel).where(MoveLogModel.match_id == record.id)
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].path == ["c3", "d4"]
        assert rows[0].ply_number == 1
        assert rows[0].seat is PlayerSide.LIGHT
        assert rows[0].engine_version == CURRENT_ENGINE_VERSION.number
        # §2: "position hash required for every accepted move" — non-empty,
        # not merely non-null.
        assert rows[0].position_hash
        # Nullable until A64-016.5, and null rather than zero: a clock
        # reading of zero is a flagged player, and it must not be
        # indistinguishable from a clock that does not exist yet.
        assert rows[0].think_time_ms is None
        assert rows[0].remaining_clock_ms is None

    async def test_the_match_and_the_move_log_advance_together_or_not_at_all(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        events: _RecordingEvents,
    ) -> None:
        """§11.4 — the transaction boundary.

        §3 requires that "a partial state such as *match advanced but move
        record missing* must be impossible". Two directions are asserted,
        and the second is the one a passing happy path would hide:

        **Together on success.** After three moves the match says ply 3 and
        the log holds three rows, read back from the database rather than
        from the service.

        **Neither on failure.** An illegal move raises, and the match's ply
        and the log's length are both unchanged — the service touched the
        aggregate in memory before it refused, and nothing reached the
        database.
        """
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(matches, light=light, dark=dark)
        service = _service(contract_session, events)

        for index, path in enumerate(OPENING):
            mover = light if index % 2 == 0 else dark
            await service.submit(SubmitMoveRequest(match_id=record.id, player_id=mover, path=path))

        advanced = await matches.by_id(record.id)
        assert advanced is not None
        assert advanced.ply_number == 3
        assert await _logged(contract_session, record.id) == 3

        with pytest.raises(IllegalMoveSubmitted):
            await service.submit(
                SubmitMoveRequest(match_id=record.id, player_id=dark, path=("a1", "h8"))
            )

        unchanged = await matches.by_id(record.id)
        assert unchanged is not None
        assert unchanged.ply_number == 3
        assert await _logged(contract_session, record.id) == 3

    async def test_two_moves_for_one_ply_produce_one_row(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        moves: SqlAlchemyMoveLogRepository,
        events: _RecordingEvents,
    ) -> None:
        """§11.2 — `uq_move__ply` refuses a duplicate, the database decides.

        §2 forbids relying on in-memory deduplication, so the second write
        goes straight to the repository: no check-then-insert could refuse
        it, because it never asks. The index is the mechanism.

        Inside a **savepoint**, because a failed statement poisons its
        transaction and the assertion that matters comes afterwards — "the
        log still holds exactly one row". Without the nesting the rollback
        would take the first move with it and the count would be zero for
        the wrong reason.
        """
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(matches, light=light, dark=dark)
        service = _service(contract_session, events)

        await service.submit(
            SubmitMoveRequest(match_id=record.id, player_id=light, path=("c3", "d4"))
        )
        logged = await moves.for_replay(record.id)

        savepoint = await contract_session.begin_nested()
        with pytest.raises(IntegrityError):
            await moves.append(
                record.id,
                LoggedMove(
                    record=logged[0],
                    seat=PlayerSide.LIGHT,
                    engine_version=CURRENT_ENGINE_VERSION,
                    created_at=NOW,
                ),
            )
        await savepoint.rollback()

        assert await _logged(contract_session, record.id) == 1

    async def test_a_terminal_move_completes_the_match_and_closes_it_to_further_moves(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        events: _RecordingEvents,
    ) -> None:
        """§11.5 and §11.6 — settlement, and what it forbids afterwards.

        A **real game played to its end** through the real service: each
        side plays the deterministic first choice from the engine's own
        legal moves until the engine says the game is over. That is 53
        plies of Russian 8x8 ending in `all_pieces_captured`.

        Playing rather than seeding is what makes this worth having. There
        is nowhere to write a near-terminal position — the log is the
        source, so a match with no moves starts from the opening — and a
        test that could seed one would be testing a path production does
        not have.

        The rules are the engine's and are not re-asserted (§11 forbids
        duplicating them). What is asserted is that terminal evaluation ran
        after **every** move rather than only the last, that the settlement
        is durable, and that a move arriving afterwards is refused under
        the row lock — §6's "reject all later move submissions".
        """
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(matches, light=light, dark=dark)
        service = _service(contract_session, events)

        result = await _play_to_the_end(service, record, light=light, dark=dark)

        assert result.outcome is MatchOutcome.WIN
        assert result.winner is not None

        settled = await matches.by_id(record.id)
        assert settled is not None
        assert settled.status is MatchRecordStatus.COMPLETED
        assert settled.ended_at is not None
        assert settled.result is not None
        assert settled.result.outcome is MatchOutcome.WIN
        assert settled.ply_number == await _logged(contract_session, record.id)

        # §10: one completion event on the ply that ended the game, and one
        # `move_applied` per ply — so a consumer counting completions counts
        # games and one counting applications counts moves.
        assert len(events.of_type("game.match_completed")) == 1
        assert len(events.of_type("game.move_applied")) == settled.ply_number

        loser = dark if settled.result.winner is PlayerSide.LIGHT else light
        with pytest.raises(MatchNotActive):
            await service.submit(
                SubmitMoveRequest(match_id=record.id, player_id=loser, path=("a1", "b2"))
            )

    async def test_a_replay_of_the_persisted_log_reaches_the_recorded_position(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        moves: SqlAlchemyMoveLogRepository,
        events: _RecordingEvents,
    ) -> None:
        """§11.7 — the log is replay-compatible without translation.

        The whole reason the log exists. AD-19's mitigation is "the durable
        move log allows a match to be reconstructed by replay through the
        engine", and this is that sentence executed: `PersistedMatchReplay`
        loads what was stored, `ReplayEngine` plays it, and the position it
        reaches is the one the last move produced.

        Checked against the **stored fingerprint**, not against the
        service's return value — the fingerprint is what a replay compares
        ply by ply, so a log that round-tripped a path incorrectly would
        diverge here rather than silently reconstruct a different game.

        `ReplayEngine` is not substituted: §4 forbids a second replay
        format, and using the real one is what proves there is only one.
        """
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(matches, light=light, dark=dark)
        service = _service(contract_session, events)

        for index, path in enumerate(OPENING):
            mover = light if index % 2 == 0 else dark
            await service.submit(SubmitMoveRequest(match_id=record.id, player_id=mover, path=path))

        data = await PersistedMatchReplay(matches=matches, moves=moves).replay_data(record.id)
        assert data is not None

        replayed = engine_services().replay.replay(data)

        logged = await moves.for_replay(record.id)
        assert replayed.position.fingerprint == logged[-1].resulting_position_hash
        assert replayed.ply_number == 3
        assert replayed.status is MatchStatus.ACTIVE
        # §4: occurrence counts are *rebuilt*, never persisted. The replay
        # has them because it applied the log, and no column carries them.
        assert replayed.current_position_occurrences >= 1


async def _play_to_the_end(
    service: LiveMoveService, record: MatchRecord, *, light: UUID, dark: UUID
) -> SubmitMoveResult:
    """Plays a match to completion, one deterministic move at a time.

    The choice is "most captures first, then lexicographic by path", which
    is arbitrary and — the property that matters — **deterministic**: the
    same game every run, so a failure is reproducible rather than a flake.

    Bounded at `_PLY_CEILING` and asserted rather than broken out of. A
    game that did not end would otherwise pass as a test that quietly
    stopped playing, which is the failure mode a bare `break` produces.
    """
    engine = engine_services()
    replay = PersistedMatchReplay(
        matches=service._matches,  # noqa: SLF001 — reading the same log the service writes
        moves=service._moves,  # noqa: SLF001
    )

    for _ in range(_PLY_CEILING):
        data = await replay.replay_data(record.id)
        assert data is not None
        position = engine.replay.replay(data).position

        choice = min(
            engine.generator.legal_moves(position),
            key=lambda move: (-len(move.captured), tuple(str(sq) for sq in move.path)),
        )
        mover = light if position.side_to_move is PlayerSide.LIGHT else dark

        result = await service.submit(
            SubmitMoveRequest(
                match_id=record.id,
                player_id=mover,
                path=tuple(str(square) for square in choice.path),
            )
        )
        if result.outcome is not None:
            return result

    raise AssertionError(f"the game did not end within {_PLY_CEILING} plies")


async def _logged(session: AsyncSession, match_id: UUID) -> int:
    """How many moves the database holds for one match."""
    return int(
        await session.scalar(
            select(func.count()).select_from(MoveLogModel).where(MoveLogModel.match_id == match_id)
        )
        or 0
    )


#: The bound on `_play_to_the_end`. Generous against the 53 plies the
#: deterministic game actually takes, and present so a rules change that
#: made the game unending fails loudly instead of hanging the suite.
_PLY_CEILING = 400


class TestAcknowledgementAfterCommit:
    """§7 — "return an accepted acknowledgement only after the durable
    transaction commits", and "if persistence fails, do not acknowledge the
    move as accepted".

    The only test here that goes through `SessionScopedLiveMoves` rather
    than the service directly, because the commit is *its* — the service
    deliberately never commits, so that it can be composed into a larger
    transaction, and the boundary is where the rule actually lives.
    """

    async def test_a_result_is_only_returned_once_the_row_is_visible_elsewhere(
        self, contract_engine: AsyncEngine
    ) -> None:
        """Read back through a **second connection**, which is the whole
        point: an uncommitted row is visible to the session that wrote it
        and to nothing else, so a service that acknowledged before
        committing would pass every assertion made on its own session.

        The failure direction is asserted too, and it is the one §7 is
        actually about: an illegal move leaves nothing behind. Without it a
        service that committed *before* deciding would pass the first half.
        """
        factory = async_sessionmaker(contract_engine, expire_on_commit=False)
        light, dark = generate_uuid7(), generate_uuid7()

        async with factory() as setup:
            record = await _active_match(
                SqlAlchemyMatchRecordRepository(setup), light=light, dark=dark
            )
            await setup.commit()

        moves = SessionScopedLiveMoves(
            session_factory=factory,
            live=_NullLiveCache(),
            engine=engine_services(),
            clock=MovableClock(NOW),
            live_state_ttl_seconds=3600,
        )

        result = await moves.submit(
            SubmitMoveRequest(match_id=record.id, player_id=light, path=("c3", "d4"))
        )
        assert result.ply == 1

        async with factory() as elsewhere:
            assert await _logged(elsewhere, record.id) == 1
            advanced = await SqlAlchemyMatchRecordRepository(elsewhere).by_id(record.id)
            assert advanced is not None
            assert advanced.ply_number == 1

        with pytest.raises(IllegalMoveSubmitted):
            await moves.submit(
                SubmitMoveRequest(match_id=record.id, player_id=dark, path=("a1", "h8"))
            )

        async with factory() as after:
            assert await _logged(after, record.id) == 1
            unchanged = await SqlAlchemyMatchRecordRepository(after).by_id(record.id)
            assert unchanged is not None
            assert unchanged.ply_number == 1
