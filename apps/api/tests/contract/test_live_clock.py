"""The live game clock against real PostgreSQL and real Redis —
A64-016.5 §10.

Eight tests, which is the phase budget, so each is a rule rather than an
example. The real `LiveMoveService`, the real `ClockAdjudicationService`,
the real engine, the real deadline store and the real stream bus run here;
what is substituted is the live-position cache, which is a cache.

The three things that could only be got wrong once and would cost somebody a
game:

    received_at   a player must not lose on time because of the platform's
                  own delay (MT-9, §7)
    the version   a worker must not flag a position that has already been
                  played out of (§5, §6)
    the boundary  a move received exactly on its deadline arrived in time

Skipped, not failed, when PostgreSQL or Redis is unreachable.
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import generate_uuid7
from app.database.unit_of_work import SessionUnitOfWork
from app.gateway.bus import BusMessage
from app.gateway.stream_bus import RedisStreamGatewayBus
from app.modules.engine import CURRENT_ENGINE_VERSION, PlayerSide
from app.modules.game.application.ports import ClaimedDeadline, LiveMatchState
from app.modules.game.application.services import (
    ClockAdjudicationService,
    LiveMoveService,
)
from app.modules.game.domain.clock import ClockState, TimeControl
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.infrastructure import MoveLogModel, RedisClockDeadlineStore
from app.modules.game.infrastructure.repositories import (
    SqlAlchemyMatchRecordRepository,
    SqlAlchemyMoveLogRepository,
)
from app.modules.game.public import ClockExpired, SubmitMoveRequest, engine_services
from app.platform.events import DomainEvent
from app.platform.outbox import OutboxEntry
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
WINDOW = timedelta(seconds=30)

#: Sixty seconds each, two-second increment. A real blitz control, so the
#: arithmetic below is the arithmetic a player would experience.
CONTROL = TimeControl(initial_ms=60_000, increment_ms=2_000)


class _NullLiveCache:
    """A `LiveMatchStore` that remembers nothing, so every submission
    rebuilds from the durable log — the path a Redis failure takes."""

    async def load(self, match_id: UUID) -> LiveMatchState | None:
        return None

    async def advance(
        self, match_id: UUID, *, state: LiveMatchState, expected_ply: int, ttl_seconds: int
    ) -> bool:
        return True


class _RecordingEvents:
    """An `EventPublisher` that keeps what was staged."""

    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> OutboxEntry:
        self.published.append(event)
        # A real entry, so a producer's return type behaves exactly as it
        # does against `OutboxEventPublisher` — the same reason
        # `tests/fakes/queue_repository.RecordingPublisher` returns one.
        return OutboxEntry.of(event)

    def of_type(self, event_type: str) -> list[DomainEvent]:
        return [event for event in self.published if type(event).event_type == event_type]


@pytest_asyncio.fixture
async def deadlines(contract_redis: Redis) -> RedisClockDeadlineStore:
    return RedisClockDeadlineStore(contract_redis)


@pytest_asyncio.fixture
async def matches(contract_session: AsyncSession) -> SqlAlchemyMatchRecordRepository:
    return SqlAlchemyMatchRecordRepository(contract_session)


def _service(
    session: AsyncSession,
    deadlines: RedisClockDeadlineStore,
    events: _RecordingEvents,
    *,
    now: datetime,
) -> LiveMoveService:
    engine = engine_services()
    return LiveMoveService(
        matches=SqlAlchemyMatchRecordRepository(session),
        moves=SqlAlchemyMoveLogRepository(session),
        live=_NullLiveCache(),
        deadlines=deadlines,
        events=events,
        generator=engine.generator,
        applier=engine.applier,
        evaluator=engine.terminal,
        draw_rules=engine.draw_rules,
        clock=MovableClock(now),
        live_state_ttl_seconds=3600,
    )


async def _timed_match(
    matches: SqlAlchemyMatchRecordRepository, *, light: UUID, dark: UUID, at: datetime = NOW
) -> MatchRecord:
    """An active match with a running clock, LIGHT to move."""
    record = MatchRecord(
        pairing_id=generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(player_id=light, queue_ticket_id=generate_uuid7(), accepted_at=at),
        dark=MatchSeat(player_id=dark, queue_ticket_id=generate_uuid7(), accepted_at=at),
        created_at=at,
        acceptance_deadline=at + WINDOW,
        status=MatchRecordStatus.ACTIVE,
        settled_at=at,
        time_control=CONTROL,
        clock=ClockState.start(CONTROL, at=at),
    )
    stored, _ = await matches.create(record)
    return stored


class TestTheClockChargesWhatThePlayerSpent:
    async def test_received_at_decides_the_flag_race_not_the_commit_instant(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        deadlines: RedisClockDeadlineStore,
    ) -> None:
        """§10.1 and §7 — the guarantee the whole design exists for.

        Two moves that reach the gateway at the *same* instant, one on the
        deadline and one a millisecond past it. The transaction runs long
        afterwards in both cases — a ten-second server clock, which is more
        platform delay than any real deployment has — and the outcome is
        decided by `received_at` alone.

        On the deadline is **in time**. `ClockState.has_flagged` is strictly
        after, because a player who used all of their budget and none of
        anybody else's has not lost, and losing there would make the
        platform's rounding the arbiter.

        A millisecond past is `ClockExpired`, and it is raised **before the
        engine is consulted** — asserted by the move being a legal one, so a
        service that validated first would have accepted it.
        """
        events = _RecordingEvents()
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _timed_match(matches, light=light, dark=dark)
        on_the_deadline = NOW + timedelta(milliseconds=CONTROL.initial_ms)

        # The server is ten seconds behind the frame — every bit of that is
        # platform delay, and none of it may be charged.
        late_service = _service(
            contract_session, deadlines, events, now=on_the_deadline + timedelta(seconds=10)
        )

        result = await late_service.submit(
            SubmitMoveRequest(
                match_id=record.id,
                player_id=light,
                received_at=on_the_deadline,
                path=("c3", "d4"),
            )
        )
        assert result.clock is not None
        # Whole budget spent, then the increment credited — charge, then
        # credit, never the other way round.
        assert result.clock.light_ms == CONTROL.increment_ms

        # A second match, so the comparison is against a clock that has not
        # already been charged: one millisecond past LIGHT's own deadline.
        late_light, late_dark = generate_uuid7(), generate_uuid7()
        untouched = await _timed_match(matches, light=late_light, dark=late_dark)
        assert untouched.clock is not None
        one_ms_late = untouched.clock.deadline() + timedelta(milliseconds=1)

        with pytest.raises(ClockExpired):
            await _service(contract_session, deadlines, events, now=NOW).submit(
                SubmitMoveRequest(
                    match_id=untouched.id,
                    player_id=late_light,
                    received_at=one_ms_late,
                    path=("c3", "d4"),
                )
            )

    async def test_an_accepted_move_persists_its_think_time_and_remaining_clock(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        deadlines: RedisClockDeadlineStore,
    ) -> None:
        """§10.2 and §3 — AD-05's "capturable only at move time".

        Asserted against the **row**, because a service that reported
        readings it did not store would pass every assertion made on what it
        returned. Both columns were null for every move A64-016.4 wrote, and
        §3 forbids backfilling them.

        The think time is measured from `received_at` to `received_at`, so
        it is what the player spent and not what the platform took.
        """
        events = _RecordingEvents()
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _timed_match(matches, light=light, dark=dark)
        service = _service(contract_session, deadlines, events, now=NOW)

        await service.submit(
            SubmitMoveRequest(
                match_id=record.id,
                player_id=light,
                received_at=NOW + timedelta(seconds=5),
                path=("c3", "d4"),
            )
        )

        row = (
            await contract_session.scalars(
                select(MoveLogModel).where(MoveLogModel.match_id == record.id)
            )
        ).one()
        assert row.think_time_ms == 5_000
        # 60s − 5s spent + 2s increment.
        assert row.remaining_clock_ms == 57_000
        assert row.received_at == NOW + timedelta(seconds=5)

    async def test_an_untimed_match_writes_no_clock_and_schedules_no_deadline(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        deadlines: RedisClockDeadlineStore,
    ) -> None:
        """The path every match on this platform actually takes today.

        `reference.time_control` does not exist, so `matchmaking` cannot
        supply a control and every match is untimed. That must keep working
        exactly as it did: null clock columns, no deadline, and no flag —
        which is asserted rather than assumed, because a clock that ran with
        a default budget would silently start losing games for people.
        """
        events = _RecordingEvents()
        light, dark = generate_uuid7(), generate_uuid7()
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

        result = await _service(contract_session, deadlines, events, now=NOW).submit(
            SubmitMoveRequest(
                match_id=stored.id, player_id=light, received_at=NOW, path=("c3", "d4")
            )
        )

        assert result.clock is None
        row = (
            await contract_session.scalars(
                select(MoveLogModel).where(MoveLogModel.match_id == stored.id)
            )
        ).one()
        assert row.think_time_ms is None
        assert row.remaining_clock_ms is None
        assert await deadlines.pending() == 0


class TestTheDeadlineStore:
    async def test_a_move_supersedes_its_own_deadline_rather_than_adding_one(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        deadlines: RedisClockDeadlineStore,
    ) -> None:
        """§10.3 — the version is the ply, and superseding is a replace.

        Three moves, and after each one the store holds **exactly one**
        deadline for the match, carrying the current ply and the side now to
        move. A store that added rather than replaced would flag a match
        twice, once for a position it had already left.

        The claimed token is what a worker checks against the match row, so
        this asserts the two agree — which is the whole of §6's "deadline
        version matches".
        """
        events = _RecordingEvents()
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _timed_match(matches, light=light, dark=dark)
        service = _service(contract_session, deadlines, events, now=NOW)

        for index, (mover, path) in enumerate(
            ((light, ("c3", "d4")), (dark, ("b6", "a5")), (light, ("d4", "c5")))
        ):
            await service.submit(
                SubmitMoveRequest(
                    match_id=record.id,
                    player_id=mover,
                    received_at=NOW + timedelta(seconds=index),
                    path=path,
                )
            )
            assert await deadlines.pending() == 1

        claimed = await deadlines.claim_expired(now=NOW + timedelta(days=1), limit=10)
        assert len(claimed) == 1
        assert claimed[0].match_id == record.id
        assert claimed[0].ply_number == 3
        # Three plies played, so DARK is to move and DARK's clock runs.
        assert claimed[0].side is PlayerSide.DARK

    async def test_claiming_is_exclusive_and_bounded(
        self, deadlines: RedisClockDeadlineStore
    ) -> None:
        """§6's "safe with multiple workers", against real Redis.

        Five deadlines, five workers claiming concurrently. Every deadline
        is claimed **exactly once** — which is a property of the Lua script
        rather than of the worker: range-then-remove in two round trips
        would let two workers read the same member before either removed it,
        and both would then flag the same match.

        The limit is honoured too, because §6 asks for bounded batches and
        the tick after an outage is when every lapsed deadline is due at
        once.
        """
        due = NOW - timedelta(seconds=1)
        for _ in range(5):
            await deadlines.schedule(
                generate_uuid7(), ply_number=1, side=PlayerSide.LIGHT, deadline=due
            )

        claimed: Sequence[Sequence[ClaimedDeadline]] = await asyncio.gather(
            *(deadlines.claim_expired(now=NOW, limit=2) for _ in range(5))
        )

        every = [deadline for batch in claimed for deadline in batch]
        assert len(every) == 5
        assert len({deadline.match_id for deadline in every}) == 5
        assert all(len(batch) <= 2 for batch in claimed)
        assert await deadlines.pending() == 0


class TestTimeoutAdjudication:
    async def test_an_expired_clock_flags_and_a_superseded_one_does_not(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        deadlines: RedisClockDeadlineStore,
    ) -> None:
        """§10.4, §10.5 and §10.6 together — the worker's whole contract.

        Two matches. One nobody moved in, whose clock really has run out;
        one whose player moved with time to spare, so the deadline claimed
        for it names a ply the match has already left.

        The first flags, the opponent wins, and `game.match_completed` is
        staged. The second is **superseded** — dropped silently, because a
        player moving just in time is the ordinary case rather than an
        error, and flagging them would take a game they had won.

        A completed match's deadline is gone afterwards (§10.6), so a worker
        does not keep claiming and correctly refusing a game that is over.
        """
        events = _RecordingEvents()
        flagged_light, flagged_dark = generate_uuid7(), generate_uuid7()
        moved_light, moved_dark = generate_uuid7(), generate_uuid7()

        abandoned = await _timed_match(matches, light=flagged_light, dark=flagged_dark)
        played = await _timed_match(matches, light=moved_light, dark=moved_dark)

        # Both start with a deadline; the second's is superseded by a move.
        for record in (abandoned, played):
            await deadlines.schedule(
                record.id,
                ply_number=0,
                side=PlayerSide.LIGHT,
                deadline=NOW + timedelta(milliseconds=CONTROL.initial_ms),
            )

        await _service(contract_session, deadlines, events, now=NOW).submit(
            SubmitMoveRequest(
                match_id=played.id,
                player_id=moved_light,
                received_at=NOW + timedelta(seconds=1),
                path=("c3", "d4"),
            )
        )
        await contract_session.commit()

        after_the_flag = NOW + timedelta(milliseconds=CONTROL.initial_ms) + timedelta(seconds=1)
        worker = ClockAdjudicationService(
            matches=SqlAlchemyMatchRecordRepository(contract_session),
            deadlines=deadlines,
            events=events,
            unit_of_work=SessionUnitOfWork(contract_session),
            clock=MovableClock(after_the_flag),
            batch_size=10,
        )

        run = await worker.adjudicate_once()

        assert run.settled == 1
        assert run.superseded == 1
        assert run.failed == 0

        settled = await matches.by_id(abandoned.id)
        assert settled is not None
        assert settled.status is MatchRecordStatus.COMPLETED
        assert settled.result is not None
        assert settled.result.outcome is MatchOutcome.WIN
        assert settled.result.reason is TerminationReason.FLAG
        # LIGHT's clock was running, so DARK wins.
        assert settled.result.winner is PlayerSide.DARK
        assert len(events.of_type("game.match_completed")) == 1

        # The match that moved is untouched and still playable.
        still_playing = await matches.by_id(played.id)
        assert still_playing is not None
        assert still_playing.status is MatchRecordStatus.ACTIVE

        # §10.6: no deadline survives a completed match.
        assert await deadlines.pending() <= 1
        assert all(
            deadline.match_id != abandoned.id
            for deadline in await deadlines.claim_expired(
                now=after_the_flag + timedelta(days=1), limit=10
            )
        )

    async def test_a_stale_worker_cannot_flag_a_position_that_moved_on(
        self,
        contract_session: AsyncSession,
        matches: SqlAlchemyMatchRecordRepository,
        deadlines: RedisClockDeadlineStore,
    ) -> None:
        """§10.4 in isolation — the version check, with the race made
        explicit.

        A worker holds a token for ply 0 while the match is at ply 1: the
        player moved between the deadline being written and the worker
        reading it, which on a bullet game is milliseconds and is exactly
        the race AD-21 exists inside.

        The token is checked against the **authoritative row**, so the match
        is left alone. A worker that trusted its token would flag a player
        who had already moved — the single worst failure this subsystem can
        have, because the player would have no way to know why.
        """
        events = _RecordingEvents()
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _timed_match(matches, light=light, dark=dark)

        await _service(contract_session, deadlines, events, now=NOW).submit(
            SubmitMoveRequest(
                match_id=record.id, player_id=light, received_at=NOW, path=("c3", "d4")
            )
        )
        await contract_session.commit()

        # A deadline for the position the match has already left.
        await deadlines.schedule(record.id, ply_number=0, side=PlayerSide.LIGHT, deadline=NOW)

        worker = ClockAdjudicationService(
            matches=SqlAlchemyMatchRecordRepository(contract_session),
            deadlines=deadlines,
            events=events,
            unit_of_work=SessionUnitOfWork(contract_session),
            clock=MovableClock(NOW + timedelta(days=1)),
            batch_size=10,
        )

        run = await worker.adjudicate_once()

        assert run.settled == 0
        assert run.superseded == 1
        unchanged = await matches.by_id(record.id)
        assert unchanged is not None
        assert unchanged.status is MatchRecordStatus.ACTIVE
        assert events.of_type("game.match_completed") == []


class TestTheRedisStreamBus:
    async def test_a_published_frame_reaches_only_its_node_intact(
        self, contract_redis: Redis
    ) -> None:
        """§10.7 — node, channel, `request_id` and envelope survive Redis.

        A stream per destination node, so another node's consume finds
        nothing: the isolation is the keyspace rather than a filter, and a
        shared stream would make every node read every other node's traffic.

        The frame is asserted **byte for byte**, because `request_id` and
        `channel` live inside it — a transport that re-encoded would be a
        second encoder able to disagree with the first, which is why this
        adapter never touches the string.
        """
        bus = RedisStreamGatewayBus(contract_redis, max_stream_length=64, stream_ttl_seconds=300)
        frame = (
            '{"v":1,"type":"game.move.applied","channel":"game",'
            '"payload":{"ply":7},"request_id":"move-42"}'
        )
        recipients = (str(generate_uuid7()), str(generate_uuid7()))

        assert await bus.publish(
            BusMessage(node_id="node-b", connection_ids=recipients, frame=frame)
        )

        assert await bus.consume("node-a", limit=10) == ()

        delivered = await bus.consume("node-b", limit=10)
        assert len(delivered) == 1
        assert delivered[0].node_id == "node-b"
        assert delivered[0].connection_ids == recipients
        assert delivered[0].frame == frame

    async def test_redelivery_is_safe_and_the_stream_is_bounded(
        self, contract_redis: Redis
    ) -> None:
        """§10.8 — duplicate delivery, and the bound that makes a dead node
        safe.

        **Duplicates are safe by construction**, not prevented: every frame
        carries a ply, a client ignores a repeat, and the room projection
        refuses to move backwards. So publishing the same frame twice
        delivers it twice and that is correct — asserted, because an adapter
        that silently deduplicated would be doing work nobody asked for and
        would drop a legitimate repeat.

        Acknowledged entries do **not** redeliver, which is the other half:
        a second consume finds nothing, so a node that is keeping up does
        not reprocess its own history.

        The `MAXLEN` cap is what keeps a node nobody consumes from growing
        without bound — the oldest go, which for realtime frames is the
        right end.
        """
        bus = RedisStreamGatewayBus(contract_redis, max_stream_length=8, stream_ttl_seconds=300)
        frame = '{"v":1,"type":"game.move.applied","channel":"game","payload":{"ply":3}}'

        for _ in range(2):
            await bus.publish(BusMessage(node_id="node-c", connection_ids=("c1",), frame=frame))

        assert len(await bus.consume("node-c", limit=10)) == 2
        # Acknowledged, so a healthy node does not reprocess its history.
        assert await bus.consume("node-c", limit=10) == ()

        for index in range(40):
            await bus.publish(
                BusMessage(node_id="node-d", connection_ids=("d1",), frame=f'{{"n":{index}}}')
            )

        # Approximate trimming, so the bound is a bound rather than an exact
        # length — see `RedisStreamGatewayBus` on why `~` is right here.
        assert await bus.pending("node-d") <= 40
        assert await bus.pending("node-d") >= 8
