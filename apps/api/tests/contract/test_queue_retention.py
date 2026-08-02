"""The two retention stores and the cooldown upsert against real
PostgreSQL — A64-015.5 §3 and §8.

`tests/unit/test_queue_retention.py` covers the horizons and the drain loop
over in-memory storage. What it cannot cover is what only a real database
has, and both of the properties this task's correctness rests on are in that
category:

    the partial-index predicate   a live ticket and an active match are
                                  unreachable from the delete, whatever the
                                  horizon says
    ON CONFLICT ... GREATEST      a repeated decline extends rather than
                                  overwrites, in **one statement**

Both are unfalsifiable against a dictionary — a fake can model them, and a
model that agrees with itself proves nothing.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.identifiers import generate_uuid7
from app.modules.engine import CURRENT_ENGINE_VERSION, PlayerSide
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.infrastructure import (
    MatchRecordModel,
    SqlAlchemyMatchRecordRepository,
    SqlAlchemyMatchRetentionStore,
)
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.domain.cooldown import CooldownReason, QueueCooldown
from app.modules.matchmaking.domain.exceptions import AlreadyQueued
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType
from app.modules.matchmaking.domain.queue_ticket import QueueTicket
from app.modules.matchmaking.infrastructure import (
    QueueTicketModel,
    SqlAlchemyCooldownRepository,
    SqlAlchemyQueueRepository,
    SqlAlchemyQueueRetentionStore,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
TTL = 600.0
WINDOW = timedelta(seconds=30)

POOL = QueuePool(variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED)


@pytest_asyncio.fixture
async def tickets(contract_session: AsyncSession) -> SqlAlchemyQueueRepository:
    return SqlAlchemyQueueRepository(contract_session)


@pytest_asyncio.fixture
async def retention(contract_session: AsyncSession) -> SqlAlchemyQueueRetentionStore:
    return SqlAlchemyQueueRetentionStore(contract_session)


@pytest_asyncio.fixture
async def cooldowns(contract_session: AsyncSession) -> SqlAlchemyCooldownRepository:
    return SqlAlchemyCooldownRepository(contract_session)


@pytest_asyncio.fixture
async def matches(contract_session: AsyncSession) -> SqlAlchemyMatchRetentionStore:
    return SqlAlchemyMatchRetentionStore(contract_session)


def _ticket(*, player_id: UUID | None = None) -> QueueTicket:
    return QueueTicket.enter(
        player_id=player_id or generate_uuid7(),
        pool=POOL,
        rating_snapshot=1500,
        at=NOW,
        ttl=TTL,
    )


async def _match(
    session: AsyncSession, *, status: MatchRecordStatus, at: datetime = NOW
) -> MatchRecord:
    accepted = at if status is MatchRecordStatus.ACTIVE else None
    record = MatchRecord(
        pairing_id=generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(
            player_id=generate_uuid7(), queue_ticket_id=generate_uuid7(), accepted_at=accepted
        ),
        dark=MatchSeat(
            player_id=generate_uuid7(), queue_ticket_id=generate_uuid7(), accepted_at=accepted
        ),
        created_at=at,
        acceptance_deadline=at + WINDOW,
        status=status,
        # `ck_match__declined_iff_cancelled`: a cancelled match names who
        # refused it, and nothing else may. Set here rather than left to the
        # aggregate's transitions because this helper builds the *stored*
        # shape directly — a retention test has no business driving an
        # acceptance handshake to reach it.
        declined_by=PlayerSide.LIGHT if status is MatchRecordStatus.CANCELLED else None,
        settled_at=None if status.is_pending else at,
    )
    stored, _ = await SqlAlchemyMatchRecordRepository(session).create(record)
    return stored


class TestTicketRetention:
    async def test_a_terminal_ticket_past_the_horizon_goes(
        self,
        tickets: SqlAlchemyQueueRepository,
        retention: SqlAlchemyQueueRetentionStore,
        contract_session: AsyncSession,
    ) -> None:
        stored = await tickets.enqueue(_ticket())
        await tickets.cancel(stored.cancelled(NOW))

        deleted = await retention.prune_resolved(before=NOW + timedelta(days=1), batch_size=10)

        assert deleted == 1
        assert await contract_session.get(QueueTicketModel, stored.id) is None

    async def test_a_terminal_ticket_inside_the_horizon_stays(
        self, tickets: SqlAlchemyQueueRepository, retention: SqlAlchemyQueueRetentionStore
    ) -> None:
        stored = await tickets.enqueue(_ticket())
        await tickets.cancel(stored.cancelled(NOW))

        deleted = await retention.prune_resolved(before=NOW - timedelta(days=1), batch_size=10)

        assert deleted == 0

    async def test_a_waiting_ticket_is_unreachable_at_any_horizon(
        self,
        tickets: SqlAlchemyQueueRepository,
        retention: SqlAlchemyQueueRetentionStore,
        contract_session: AsyncSession,
    ) -> None:
        """The safety property, driven against the real predicate. A horizon
        far in the future would delete everything the statement can see —
        and it cannot see a live ticket, because `resolved_at IS NOT NULL`
        is `ck_queue_ticket__resolved_iff_terminal` read from the other
        side."""
        stored = await tickets.enqueue(_ticket())

        deleted = await retention.prune_resolved(before=NOW + timedelta(days=3650), batch_size=10)

        assert deleted == 0
        assert await contract_session.get(QueueTicketModel, stored.id) is not None

    async def test_a_reserved_ticket_is_unreachable_too(
        self,
        tickets: SqlAlchemyQueueRepository,
        retention: SqlAlchemyQueueRetentionStore,
        contract_session: AsyncSession,
    ) -> None:
        """The row reconciliation is about to recover. Deleting it would
        turn a recoverable pairing into a player who is silently no longer
        in any queue."""
        stored = await tickets.enqueue(_ticket())
        await tickets.reserve([stored.reserved(until=NOW + WINDOW)])

        deleted = await retention.prune_resolved(before=NOW + timedelta(days=3650), batch_size=10)

        assert deleted == 0
        assert await contract_session.get(QueueTicketModel, stored.id) is not None

    async def test_the_delete_is_bounded(
        self, tickets: SqlAlchemyQueueRepository, retention: SqlAlchemyQueueRetentionStore
    ) -> None:
        for _ in range(4):
            stored = await tickets.enqueue(_ticket())
            await tickets.cancel(stored.cancelled(NOW))

        deleted = await retention.prune_resolved(before=NOW + timedelta(days=1), batch_size=2)

        assert deleted == 2

    async def test_live_tickets_past_the_horizon_are_counted(
        self, tickets: SqlAlchemyQueueRepository, retention: SqlAlchemyQueueRetentionStore
    ) -> None:
        """The alarm: a `waiting` ticket older than the whole horizon means
        the expiry sweep has stopped."""
        await tickets.enqueue(_ticket())

        assert await retention.live_before(NOW + timedelta(days=1)) == 1
        assert await retention.live_before(NOW - timedelta(days=1)) == 0


class TestAbandonedMatchRetention:
    async def test_a_cancelled_match_past_the_horizon_goes(
        self, matches: SqlAlchemyMatchRetentionStore, contract_session: AsyncSession
    ) -> None:
        record = await _match(contract_session, status=MatchRecordStatus.CANCELLED)

        deleted = await matches.prune_abandoned(before=NOW + timedelta(days=1), batch_size=10)

        assert deleted == 1
        assert await contract_session.get(MatchRecordModel, record.id) is None

    async def test_an_expired_match_goes_too(
        self, matches: SqlAlchemyMatchRetentionStore, contract_session: AsyncSession
    ) -> None:
        await _match(contract_session, status=MatchRecordStatus.EXPIRED)

        assert await matches.prune_abandoned(before=NOW + timedelta(days=1), batch_size=10) == 1

    async def test_an_active_match_is_unreachable_at_any_horizon(
        self, matches: SqlAlchemyMatchRetentionStore, contract_session: AsyncSession
    ) -> None:
        """The permanent competitive record A-4 is about, excluded by
        predicate rather than by configuration."""
        record = await _match(contract_session, status=MatchRecordStatus.ACTIVE)

        deleted = await matches.prune_abandoned(before=NOW + timedelta(days=3650), batch_size=10)

        assert deleted == 0
        assert await contract_session.get(MatchRecordModel, record.id) is not None

    async def test_a_pending_match_is_kept_and_counted(
        self, matches: SqlAlchemyMatchRetentionStore, contract_session: AsyncSession
    ) -> None:
        """Excluded twice over — by the status list and by a null cutoff
        column — because a pending match that old is a reconciliation
        failure the sweep must surface rather than delete."""
        record = await _match(contract_session, status=MatchRecordStatus.PENDING_ACCEPTANCE)

        deleted = await matches.prune_abandoned(before=NOW + timedelta(days=3650), batch_size=10)

        assert deleted == 0
        assert await contract_session.get(MatchRecordModel, record.id) is not None
        assert await matches.unsettled_before(NOW + timedelta(days=1)) == 1


class TestTheCooldownUpsert:
    async def test_a_cooldown_round_trips(self, cooldowns: SqlAlchemyCooldownRepository) -> None:
        cooldown = QueueCooldown.after_decline(generate_uuid7(), at=NOW, seconds=60)

        stored = await cooldowns.apply(cooldown)

        assert stored == cooldown
        assert await cooldowns.active_for(cooldown.player_id, now=NOW) == cooldown

    async def test_an_expired_cooldown_reads_as_absent(
        self, cooldowns: SqlAlchemyCooldownRepository
    ) -> None:
        """Expiry is applied in the query, exactly as `active_ticket`
        applies its own deadline: a lapsed row retention has not reached
        must not refuse anybody."""
        cooldown = QueueCooldown.after_decline(generate_uuid7(), at=NOW, seconds=60)
        await cooldowns.apply(cooldown)

        assert (
            await cooldowns.active_for(cooldown.player_id, now=NOW + timedelta(minutes=5)) is None
        )

    async def test_a_second_decline_extends_rather_than_overwrites(
        self, cooldowns: SqlAlchemyCooldownRepository
    ) -> None:
        """§3's rule, held by `GREATEST` inside one statement. A
        read-then-write would be correct until two declines landed
        together, at which point the second would shorten the first."""
        player_id = generate_uuid7()
        first = QueueCooldown.after_decline(player_id, at=NOW, seconds=300)
        await cooldowns.apply(first)

        second = QueueCooldown.after_decline(player_id, at=NOW + timedelta(seconds=10), seconds=60)
        stored = await cooldowns.apply(second)

        assert stored.expires_at == first.expires_at

    async def test_a_later_decline_wins(self, cooldowns: SqlAlchemyCooldownRepository) -> None:
        player_id = generate_uuid7()
        await cooldowns.apply(QueueCooldown.after_decline(player_id, at=NOW, seconds=60))

        longer = QueueCooldown.after_decline(player_id, at=NOW, seconds=600)
        stored = await cooldowns.apply(longer)

        assert stored.expires_at == longer.expires_at

    async def test_the_original_application_instant_survives(
        self, cooldowns: SqlAlchemyCooldownRepository
    ) -> None:
        """ "When did this player start being barred" is the question, and a
        second decline does not restart it."""
        player_id = generate_uuid7()
        first = QueueCooldown.after_decline(player_id, at=NOW, seconds=60)
        await cooldowns.apply(first)

        await cooldowns.apply(
            QueueCooldown.after_decline(player_id, at=NOW + timedelta(seconds=30), seconds=600)
        )

        stored = await cooldowns.active_for(player_id, now=NOW + timedelta(seconds=30))
        assert stored is not None
        assert stored.created_at == first.created_at
        assert stored.reason is CooldownReason.DECLINED_MATCH

    async def test_lapsed_cooldowns_are_pruned(
        self, cooldowns: SqlAlchemyCooldownRepository
    ) -> None:
        await cooldowns.apply(QueueCooldown.after_decline(generate_uuid7(), at=NOW, seconds=60))

        deleted = await cooldowns.prune_expired(before=NOW + timedelta(hours=1), batch_size=10)

        assert deleted == 1

    async def test_an_active_cooldown_is_never_pruned(
        self, cooldowns: SqlAlchemyCooldownRepository
    ) -> None:
        await cooldowns.apply(QueueCooldown.after_decline(generate_uuid7(), at=NOW, seconds=600))

        assert await cooldowns.prune_expired(before=NOW, batch_size=10) == 0


class TestTheRequeueIndex:
    async def test_one_source_ticket_produces_one_replacement(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """`uq_queue_ticket__requeued_from` — A64-015.5 §2's idempotency,
        and the guard the event ledger cannot provide: two workers
        processing one entry concurrently both pass every read."""
        source = await tickets.enqueue(_ticket())
        reserved = source.reserved(until=NOW + WINDOW)
        await tickets.reserve([reserved])
        await tickets.complete([reserved.matched(NOW)], at=NOW)
        matched = await tickets.by_id(source.id)
        assert matched is not None

        await tickets.enqueue(matched.requeued(at=NOW, ttl=TTL))
        # A second replacement for the same source — the player's live
        # ticket is gone from the picture, so QT-1 would not catch this.
        await tickets.cancel(
            (await tickets.active_ticket(source.player_id, now=NOW)).cancelled(NOW)  # type: ignore[union-attr]
        )

        with pytest.raises(AlreadyQueued):
            await tickets.enqueue(matched.requeued(at=NOW, ttl=TTL))
        await contract_session.rollback()

    async def test_ordinary_tickets_are_unconstrained(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        """The index is partial on `source_ticket_id IS NOT NULL`, so the
        overwhelming majority of tickets — the ones a player entered
        themselves — are not in it at all."""
        for _ in range(3):
            stored = await tickets.enqueue(_ticket())
            await tickets.cancel(stored.cancelled(NOW))

        rows = await tickets.queue_snapshot(pool=POOL, now=NOW, limit=10)
        assert rows.waiting == 0

    async def test_the_provenance_round_trips(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        source = await tickets.enqueue(_ticket())
        reserved = source.reserved(until=NOW + WINDOW)
        await tickets.reserve([reserved])
        await tickets.complete([reserved.matched(NOW)], at=NOW)
        matched = await tickets.by_id(source.id)
        assert matched is not None

        replacement = await tickets.enqueue(matched.requeued(at=NOW, ttl=TTL))

        row = await contract_session.get(QueueTicketModel, replacement.id)
        assert row is not None
        assert row.source_ticket_id == source.id
        assert row.entered_at == source.entered_at


class TestTwoWorkersPruningTogether:
    """`SKIP LOCKED` on the retention claim — the property a dictionary
    cannot have.

    Runs off `contract_engine` rather than `contract_session`, for the
    reason `test_queue_repository.py`'s claim test does: that fixture binds
    its session to one connection inside an outer transaction it always
    rolls back, so a `commit()` there is invisible to any other connection —
    which is precisely the visibility this is about.
    """

    async def test_they_take_disjoint_sets(self, contract_engine: AsyncEngine) -> None:
        ids: list[UUID] = []
        engine = contract_engine
        try:
            async with AsyncSession(engine, expire_on_commit=False) as seeding:
                repository = SqlAlchemyQueueRepository(seeding)
                for _ in range(4):
                    stored = await repository.enqueue(_ticket())
                    await repository.cancel(stored.cancelled(NOW))
                    ids.append(stored.id)
                await seeding.commit()

            async with (
                AsyncSession(engine, expire_on_commit=False) as first_session,
                AsyncSession(engine, expire_on_commit=False) as second_session,
            ):
                first = await SqlAlchemyQueueRetentionStore(first_session).prune_resolved(
                    before=NOW + timedelta(days=1), batch_size=2
                )
                # The first session has **not** committed, so its two rows
                # are still locked when the second pruner polls.
                second = await SqlAlchemyQueueRetentionStore(second_session).prune_resolved(
                    before=NOW + timedelta(days=1), batch_size=2
                )
                await first_session.rollback()
                await second_session.rollback()

                assert first == 2
                assert second == 2
        finally:
            async with AsyncSession(engine) as cleanup:
                await cleanup.execute(delete(QueueTicketModel).where(QueueTicketModel.id.in_(ids)))
                await cleanup.commit()
