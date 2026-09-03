"""Resignation and draw agreement against real PostgreSQL —
A64-020.5C-pre §16.

The real `GameCommandService`, the real `LiveMoveService`, the real
repositories and real PostgreSQL. What is substituted is the outbox
publisher — which keeps every assertion here about *which events a
transaction stages*, rather than also about `platform.outbox`'s durability,
which is `test_outbox_repository.py`'s.

Five tests for §16's items 1, 3, 5, 6, 7, 8 and 10. Each carries the
assertions belonging to one *mechanism* — settlement, durability, the
agreed draw, the decline, and the two races — because §16 caps this phase at
ten across every layer and the domain and the transport need the rest.

The database is what makes these worth running at all. The `CHECK`
constraints, the compare-and-set and the row lock are the three things that
cannot be exercised against a fake, and each of them is the guard that holds
when the application code is wrong.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import generate_uuid7
from app.modules.engine import CURRENT_ENGINE_VERSION, PlayerSide
from app.modules.game.application.ports import ClaimedDeadline, LiveMatchState
from app.modules.game.application.services import (
    GameCommandService,
    GameMatchSnapshot,
    LiveMoveService,
    PersistedMatchReplay,
)
from app.modules.game.domain.clock import ClockState, TimeControl
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.infrastructure.models import MatchRecordModel
from app.modules.game.infrastructure.repositories import (
    SqlAlchemyMatchRecordRepository,
    SqlAlchemyMoveLogRepository,
)
from app.modules.game.public import (
    DrawOfferNotAllowedYet,
    GameCommand,
    GameCommandRequest,
    IllegalMoveSubmitted,
    MatchNotActive,
    MatchNotFound,
    SubmitMoveRequest,
    engine_services,
)
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
WINDOW = timedelta(seconds=30)


class _NullLiveCache:
    """A `LiveMatchStore` that remembers nothing, so every move rebuilds
    the aggregate from the durable log — the path a Redis failure takes."""

    async def load(self, match_id: UUID) -> LiveMatchState | None:
        return None

    async def advance(
        self, match_id: UUID, *, state: LiveMatchState, expected_ply: int, ttl_seconds: int
    ) -> bool:
        return True


class _RecordingDeadlines:
    """A `ClockDeadlineStore` that records what it was asked to do.

    Every match here is untimed, so nothing is ever scheduled — recorded
    rather than ignored, because §11's "a terminal command cancels the
    deadline" is asserted against `cancelled` and a silent fake would make
    that assertion vacuous.
    """

    def __init__(self) -> None:
        self.cancelled: list[UUID] = []

    async def schedule(
        self, match_id: UUID, *, ply_number: int, side: PlayerSide, deadline: datetime
    ) -> None: ...

    async def cancel(self, match_id: UUID) -> None:
        self.cancelled.append(match_id)

    async def claim_expired(self, *, now: datetime, limit: int) -> Sequence[ClaimedDeadline]:
        return ()


class _RecordingEvents:
    """An `EventPublisher` that keeps what was staged."""

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


@pytest.fixture
def events() -> _RecordingEvents:
    return _RecordingEvents()


@pytest.fixture
def deadlines() -> _RecordingDeadlines:
    return _RecordingDeadlines()


def _commands(
    session: AsyncSession, events: _RecordingEvents, deadlines: _RecordingDeadlines
) -> GameCommandService:
    return GameCommandService(
        matches=SqlAlchemyMatchRecordRepository(session),
        deadlines=deadlines,
        events=events,  # type: ignore[arg-type]
        clock=MovableClock(NOW),
    )


def _moves(session: AsyncSession, events: _RecordingEvents) -> LiveMoveService:
    engine = engine_services()
    return LiveMoveService(
        matches=SqlAlchemyMatchRecordRepository(session),
        moves=SqlAlchemyMoveLogRepository(session),
        live=_NullLiveCache(),
        deadlines=_RecordingDeadlines(),
        events=events,  # type: ignore[arg-type]
        generator=engine.generator,
        applier=engine.applier,
        evaluator=engine.terminal,
        draw_rules=engine.draw_rules,
        clock=MovableClock(NOW),
        live_state_ttl_seconds=3600,
    )


def _snapshots(session: AsyncSession) -> GameMatchSnapshot:
    """The real reconnect reader, assembled exactly as the WebSocket route
    assembles it — see `game.presentation.dependencies`."""
    repository = SqlAlchemyMatchRecordRepository(session)
    return GameMatchSnapshot(
        matches=repository,
        replays=PersistedMatchReplay(
            matches=repository, moves=SqlAlchemyMoveLogRepository(session)
        ),
        engine=engine_services().replay,
        clock=MovableClock(NOW),
    )


async def _active_match(
    matches: SqlAlchemyMatchRecordRepository,
    *,
    light: UUID,
    dark: UUID,
    timed: bool = False,
) -> MatchRecord:
    """A match both players accepted, ready to be played.

    Untimed by default, because nothing except §11's deadline cancellation
    depends on a clock and an untimed match keeps every other assertion
    about the thing it is testing. `timed=True` is what the resignation
    test uses, and it is the only way `_cancel_deadline` is reached at all
    — the service correctly skips a match that never had a deadline.
    """
    control = TimeControl(initial_ms=60_000, increment_ms=0) if timed else None
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
        time_control=control,
        clock=(
            ClockState(
                light_ms=60_000,
                dark_ms=60_000,
                active_side=PlayerSide.LIGHT,
                turn_started_at=NOW,
            )
            if timed
            else None
        ),
    )
    stored, _ = await matches.create(record)
    return stored


class TestResignation:
    async def test_resigning_gives_the_opponent_the_win_and_publishes_one_completion(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        events: _RecordingEvents,
        deadlines: _RecordingDeadlines,
    ) -> None:
        """§16.1, plus §11 and §12.

        Four things in one settlement, because they are one transaction and
        asserting them apart would be four tests of the same write:

            the row      completed, `resignation`, the **opponent** as
                         winner, and an `ended_at`
            the event    exactly one `MatchCompleted`, carrying the same
                         outcome — this is what `rating`, `statistics` and
                         `tournaments` consume, and §12 forbids a second
                         completion event or a new rating path
            the clock    the deadline is cancelled (§11)
            the board    the ply is untouched — GE-67's "a resigned game
                         must still replay to the position it was
                         abandoned in"

        Asserted against the **row** rather than the returned result,
        because a service that reported a settlement it did not store would
        pass every assertion made on what it returned.
        """
        light, dark = generate_uuid7(), generate_uuid7()
        # Timed, so §11's deadline cancellation is actually reached.
        record = await _active_match(matches, light=light, dark=dark, timed=True)

        await _commands(contract_session, events, deadlines).execute(
            GameCommandRequest(match_id=record.id, player_id=light, command=GameCommand.RESIGN)
        )
        await contract_session.commit()

        row = await contract_session.get(MatchRecordModel, record.id)
        assert row is not None
        assert row.status is MatchRecordStatus.COMPLETED
        assert row.termination_reason is TerminationReason.RESIGNATION
        assert row.outcome is MatchOutcome.WIN
        # LIGHT resigned, so DARK won. The single most valuable assertion
        # in this file: a platform that awarded it the other way would have
        # unusable statistics and nobody would notice for months.
        assert row.winner is PlayerSide.DARK
        assert row.ended_at is not None
        assert row.ply_number == 0

        completions = events.of_type("game.match_completed")
        assert len(completions) == 1
        assert deadlines.cancelled == [record.id]

    async def test_a_stranger_cannot_resign_and_a_finished_match_cannot_be_resigned_twice(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        events: _RecordingEvents,
        deadlines: _RecordingDeadlines,
    ) -> None:
        """§16.2 and §6's "settled Match cannot be overwritten".

        The stranger is told the match does not exist rather than that they
        are not a participant, which is the disclosure rule the whole
        subsystem keeps: one answer for both, so live match identifiers
        cannot be enumerated by sending resignations at them.

        The second half is the one that protects the permanent record. A
        resignation replayed after the game ended must not rewrite it — MT-10
        makes a completed match permanent, and the refusal comes from the
        status check under the row lock rather than from a caller
        remembering to look.
        """
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(matches, light=light, dark=dark)
        commands = _commands(contract_session, events, deadlines)

        with pytest.raises(MatchNotFound):
            await commands.execute(
                GameCommandRequest(
                    match_id=record.id,
                    player_id=generate_uuid7(),
                    command=GameCommand.RESIGN,
                )
            )

        await commands.execute(
            GameCommandRequest(match_id=record.id, player_id=light, command=GameCommand.RESIGN)
        )
        await contract_session.commit()

        with pytest.raises(MatchNotActive):
            await commands.execute(
                GameCommandRequest(match_id=record.id, player_id=dark, command=GameCommand.RESIGN)
            )


class TestDrawAgreement:
    async def test_an_offer_is_durable_and_reappears_in_the_snapshot(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        events: _RecordingEvents,
        deadlines: _RecordingDeadlines,
    ) -> None:
        """§16.3, and the whole of §1's durability requirement.

        The offer is written by one service and read back by **another** —
        `GameMatchSnapshot`, which is what a reconnecting client actually
        reaches. That is the assertion that matters: a value the command
        service could read back from its own memory proves nothing about a
        page refresh, and the snapshot is the path a refresh takes.

        `may_offer` is asserted from both sides because the client renders a
        button from it, and a snapshot that said LIGHT could offer while an
        offer of theirs already stood would be a button the server refuses.
        """
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(matches, light=light, dark=dark)

        await _commands(contract_session, events, deadlines).execute(
            GameCommandRequest(match_id=record.id, player_id=light, command=GameCommand.OFFER_DRAW)
        )
        await contract_session.commit()

        # A fresh read through the reconnect path, not the command's own.
        snapshot = await _snapshots(contract_session).snapshot_of(record.id)
        assert snapshot is not None
        assert snapshot.draw_offer is not None
        assert snapshot.draw_offer.offered_by is PlayerSide.LIGHT
        assert snapshot.draw_offer.offered_at_ply == 0
        # Neither side may open a new one while this stands.
        assert not snapshot.may_offer_light
        assert not snapshot.may_offer_dark
        # Nothing was settled: an offer is a question, not a result.
        assert snapshot.outcome is None
        assert events.of_type("game.match_completed") == []

    async def test_accepting_settles_an_agreed_draw_and_declining_settles_nothing(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        events: _RecordingEvents,
        deadlines: _RecordingDeadlines,
    ) -> None:
        """§16.5 and §16.6 in one flow, because the second is the control
        for the first.

        A decline and an accept differ in exactly one way — whether the
        match ends — and asserting them apart would leave "does a decline
        change the board" tested against a match no accept had touched.
        Here the same match is offered twice, declined once and accepted
        once, so the two paths are compared against one another.

        The decline's assertions are the interesting half: no outcome, no
        event, and the ply, the status and the clock all unchanged. §1 says
        a decline "does not alter board, clock, turn or ply", and this is
        the check that it does not.
        """
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(matches, light=light, dark=dark)
        commands = _commands(contract_session, events, deadlines)

        await commands.execute(
            GameCommandRequest(match_id=record.id, player_id=light, command=GameCommand.OFFER_DRAW)
        )
        await commands.execute(
            GameCommandRequest(match_id=record.id, player_id=dark, command=GameCommand.DECLINE_DRAW)
        )
        await contract_session.commit()

        declined = await contract_session.get(MatchRecordModel, record.id)
        assert declined is not None
        assert declined.status is MatchRecordStatus.ACTIVE
        assert declined.outcome is None
        assert declined.ply_number == 0
        assert declined.draw_offer_by is None
        assert declined.clock_turn_started_at is None
        assert events.of_type("game.match_completed") == []

        # LIGHT is now under the re-offer restriction and DARK is not —
        # §3, asserted here against the durable row rather than against an
        # in-memory aggregate.
        with pytest.raises(DrawOfferNotAllowedYet):
            await commands.execute(
                GameCommandRequest(
                    match_id=record.id, player_id=light, command=GameCommand.OFFER_DRAW
                )
            )

        await commands.execute(
            GameCommandRequest(match_id=record.id, player_id=dark, command=GameCommand.OFFER_DRAW)
        )
        await commands.execute(
            GameCommandRequest(match_id=record.id, player_id=light, command=GameCommand.ACCEPT_DRAW)
        )
        await contract_session.commit()

        drawn = await contract_session.get(MatchRecordModel, record.id)
        assert drawn is not None
        assert drawn.status is MatchRecordStatus.COMPLETED
        assert drawn.outcome is MatchOutcome.DRAW
        assert drawn.termination_reason is TerminationReason.AGREED_DRAW
        assert drawn.winner is None
        # The row carries no agreement state once the match is terminal —
        # `ck_match__draw_offer_iff_active` would refuse it otherwise, so
        # this is also the constraint being exercised.
        assert drawn.draw_offer_by is None
        assert len(events.of_type("game.match_completed")) == 1

    async def test_the_recipients_applied_move_clears_the_offer_and_a_rejected_one_does_not(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        events: _RecordingEvents,
        deadlines: _RecordingDeadlines,
    ) -> None:
        """§16.7 and §16.8, and §10's whole point.

        The rejected move is the assertion worth having. It is easy to
        write an expiration that fires when the *frame arrives* rather than
        when the move is *applied*, and the difference only shows when a
        client sends an illegal move — at which point a player has lost
        their standing draw offer to somebody else's bug.

        Here the illegal move is refused by the real engine, and the offer
        is asserted **from the database** afterwards, so nothing about the
        in-memory record can hide a write that did not happen.
        """
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(matches, light=light, dark=dark)

        await _commands(contract_session, events, deadlines).execute(
            GameCommandRequest(match_id=record.id, player_id=light, command=GameCommand.OFFER_DRAW)
        )
        await contract_session.commit()

        # DARK is the recipient. LIGHT moves first, and it is LIGHT's turn:
        # their own move must leave the offer standing.
        await _moves(contract_session, events).submit(
            SubmitMoveRequest(match_id=record.id, player_id=light, path=("c3", "d4"))
        )
        await contract_session.commit()

        still_open = await contract_session.get(MatchRecordModel, record.id)
        assert still_open is not None
        assert still_open.ply_number == 1
        assert still_open.draw_offer_by is PlayerSide.LIGHT

        # DARK sends an illegal move. Refused, and the offer survives it.
        with pytest.raises(IllegalMoveSubmitted):
            await _moves(contract_session, events).submit(
                SubmitMoveRequest(match_id=record.id, player_id=dark, path=("a1", "h8"))
            )
        await contract_session.rollback()

        survived = await contract_session.get(MatchRecordModel, record.id)
        assert survived is not None
        assert survived.draw_offer_by is PlayerSide.LIGHT

        # DARK plays a legal move. Now the offer is gone.
        await _moves(contract_session, events).submit(
            SubmitMoveRequest(match_id=record.id, player_id=dark, path=("b6", "a5"))
        )
        await contract_session.commit()

        cleared = await contract_session.get(MatchRecordModel, record.id)
        assert cleared is not None
        assert cleared.ply_number == 2
        assert cleared.draw_offer_by is None
        # And LIGHT is under the re-offer restriction: DARK moved at ply 2,
        # so LIGHT waits for DARK's move at ply 4 — §3's arithmetic, from
        # the database this time.
        assert cleared.light_draw_offer_from_ply == 4


class TestExactlyOneTerminalResult:
    async def test_a_resignation_and_an_accepted_draw_cannot_both_settle_one_match(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        events: _RecordingEvents,
        deadlines: _RecordingDeadlines,
    ) -> None:
        """§6 and §16.10 — "no Match can end both by resignation and agreed
        draw".

        The two commands are issued back to back against the same match.
        Serialised by the row lock in production; here they run in one
        session, which is the *stricter* arrangement — the second sees the
        first's write with no lock to hide behind, so a status check that
        was missing would let it through.

        One `MatchCompleted` is the assertion that protects `rating`: two
        completion events for one match would move both players' ratings
        twice, and §12 forbids duplicating them.
        """
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(matches, light=light, dark=dark)
        commands = _commands(contract_session, events, deadlines)

        await commands.execute(
            GameCommandRequest(match_id=record.id, player_id=light, command=GameCommand.OFFER_DRAW)
        )
        await commands.execute(
            GameCommandRequest(match_id=record.id, player_id=dark, command=GameCommand.RESIGN)
        )
        await contract_session.commit()

        # DARK's resignation landed first. The accept must now lose.
        with pytest.raises(MatchNotActive):
            await commands.execute(
                GameCommandRequest(
                    match_id=record.id, player_id=dark, command=GameCommand.ACCEPT_DRAW
                )
            )

        row = await contract_session.get(MatchRecordModel, record.id)
        assert row is not None
        assert row.termination_reason is TerminationReason.RESIGNATION
        assert row.winner is PlayerSide.LIGHT
        assert len(events.of_type("game.match_completed")) == 1

        # And a move racing the settled match is refused too, so the
        # permanent record cannot be advanced past its own result.
        with pytest.raises(MatchNotActive):
            await _moves(contract_session, events).submit(
                SubmitMoveRequest(match_id=record.id, player_id=light, path=("c3", "d4"))
            )
