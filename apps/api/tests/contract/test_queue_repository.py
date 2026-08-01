"""`SqlAlchemyQueueRepository` against real PostgreSQL — A64-014.1.

`tests/unit/test_queue_service.py` covers the use cases over in-memory
storage. What it cannot cover is the two properties the queue is designed
around and that only a real database has:

    SELECT ... FOR UPDATE SKIP LOCKED     two workers, disjoint sets
    a partial unique index                QT-1 under concurrency

Both are unfalsifiable against a dictionary — a fake can *model* them, and a
model that agrees with itself proves nothing — so they are asserted here,
with real transactions and a real constraint.

The rest of the file is the mapping and the predicates: enums round trip,
the partial index constrains only live rows, and the two compare-and-set
writes refuse a row somebody else already resolved.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.identifiers import generate_uuid7
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.domain.exceptions import AlreadyQueued
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType, Region
from app.modules.matchmaking.domain.queue_ticket import (
    QueueStatus,
    QueueTicket,
)
from app.modules.matchmaking.infrastructure import QueueTicketModel, SqlAlchemyQueueRepository

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
TTL = 600.0


def _pool(queue_type: QueueType = QueueType.RANKED, region: Region = Region.EUROPE) -> QueuePool:
    """A pool for the one variant Arena64 offers."""
    return QueuePool(variant=ProductVariant.RUSSIAN_8X8, queue_type=queue_type, region=region)


def _ticket(
    *,
    player_id: UUID | None = None,
    at: datetime = NOW,
    ttl: float = TTL,
    queue_type: QueueType = QueueType.RANKED,
    region: Region = Region.EUROPE,
) -> QueueTicket:
    return QueueTicket.enter(
        player_id=player_id or generate_uuid7(),
        pool=_pool(queue_type, region),
        rating_snapshot=1500,
        at=at,
        ttl=ttl,
    )


@pytest_asyncio.fixture
async def tickets(contract_session: AsyncSession) -> SqlAlchemyQueueRepository:
    return SqlAlchemyQueueRepository(contract_session)


class TestEnqueue:
    async def test_a_ticket_round_trips_through_the_table(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """Three native enums and two `timestamptz` columns. A mapping that
        stored a Python member name rather than its value would round trip
        *differently* rather than fail, which is the failure a `text` column
        would have."""
        ticket = await tickets.enqueue(_ticket(region=Region.NORTH_AMERICA))
        await contract_session.flush()
        contract_session.expunge_all()

        stored = await tickets.active_ticket(ticket.player_id, now=NOW)
        assert stored is not None
        assert stored.id == ticket.id
        assert stored.queue_type is QueueType.RANKED
        assert stored.region is Region.NORTH_AMERICA
        assert stored.status is QueueStatus.WAITING
        assert stored.rating_snapshot == 1500
        assert stored.expires_at == NOW + timedelta(seconds=TTL)

    async def test_a_second_live_ticket_is_refused(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        """QT-1, enforced by `uq_queue_ticket__one_live_per_player` and
        translated to the type the service would have raised. This is the
        guard that holds when two joins race — the service's read-first
        check does not."""
        player = generate_uuid7()
        await tickets.enqueue(_ticket(player_id=player))

        with pytest.raises(AlreadyQueued):
            await tickets.enqueue(_ticket(player_id=player))

    async def test_the_constraint_ignores_the_pool(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        """**Across all pools.** An index keyed on `(player_id, queue_type)`
        would pass every other test in this class and permit exactly the
        multi-queueing QT-1 exists to prevent."""
        player = generate_uuid7()
        await tickets.enqueue(_ticket(player_id=player, queue_type=QueueType.RANKED))

        with pytest.raises(AlreadyQueued):
            await tickets.enqueue(
                _ticket(player_id=player, queue_type=QueueType.CASUAL, region=Region.ASIA)
            )

    async def test_a_player_may_queue_again_after_resolving(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """The index is **partial** on `waiting`. A plain unique would mean
        a player could queue once, ever."""
        player = generate_uuid7()
        first = await tickets.enqueue(_ticket(player_id=player))
        await contract_session.flush()
        await tickets.cancel(first.cancelled(NOW + timedelta(seconds=10)))

        await tickets.enqueue(_ticket(player_id=player, at=NOW + timedelta(seconds=20)))

    async def test_two_players_may_hold_tickets_at_once(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        await tickets.enqueue(_ticket())
        await tickets.enqueue(_ticket())


class TestActiveTicket:
    async def test_a_player_with_no_ticket_reads_as_none(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        assert await tickets.active_ticket(generate_uuid7(), now=NOW) is None

    async def test_a_due_ticket_reads_as_absent(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """The deadline is applied in the query, so every reader agrees what
        "queued" means and none of them can forget the second half."""
        ticket = await tickets.enqueue(_ticket())
        await contract_session.flush()

        assert await tickets.active_ticket(ticket.player_id, now=ticket.expires_at) is None

    async def test_a_resolved_ticket_reads_as_absent(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        ticket = await tickets.enqueue(_ticket())
        await contract_session.flush()
        await tickets.cancel(ticket.cancelled(NOW))

        assert await tickets.active_ticket(ticket.player_id, now=NOW) is None


class TestCancel:
    async def test_cancelling_a_waiting_ticket_applies(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        ticket = await tickets.enqueue(_ticket())
        await contract_session.flush()

        assert await tickets.cancel(ticket.cancelled(NOW + timedelta(seconds=5))) is True

    async def test_cancelling_a_resolved_ticket_does_not_apply(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """The compare-and-set. Two devices cancelling at once, or a cancel
        arriving as the expiry sweep commits — a blind `UPDATE` would let
        the later write overwrite the earlier transition."""
        ticket = await tickets.enqueue(_ticket())
        await contract_session.flush()
        await tickets.cancel(ticket.cancelled(NOW))

        assert await tickets.cancel(ticket.cancelled(NOW + timedelta(seconds=1))) is False

    async def test_cancelling_an_unknown_ticket_does_not_apply(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        assert await tickets.cancel(_ticket().cancelled(NOW)) is False


class TestQueueSnapshot:
    async def test_a_snapshot_reports_its_own_pool_only(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        await tickets.enqueue(_ticket(queue_type=QueueType.RANKED, region=Region.EUROPE))
        await tickets.enqueue(_ticket(queue_type=QueueType.RANKED, region=Region.EUROPE))
        await tickets.enqueue(_ticket(queue_type=QueueType.CASUAL, region=Region.EUROPE))
        await tickets.enqueue(_ticket(queue_type=QueueType.RANKED, region=Region.ASIA))
        await contract_session.flush()

        snapshot = await tickets.queue_snapshot(
            pool=_pool(QueueType.RANKED, Region.EUROPE), now=NOW, limit=10
        )

        assert snapshot.waiting == 2
        assert len(snapshot.tickets) == 2

    async def test_tickets_arrive_oldest_first(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """The pairing order. Inserted newest-first so insertion order
        cannot be what makes this pass."""
        later = await tickets.enqueue(_ticket(at=NOW + timedelta(seconds=5)))
        earlier = await tickets.enqueue(_ticket(at=NOW))
        await contract_session.flush()

        snapshot = await tickets.queue_snapshot(
            pool=_pool(QueueType.RANKED, Region.EUROPE), now=NOW, limit=10
        )

        assert [ticket.id for ticket in snapshot.tickets] == [earlier.id, later.id]

    async def test_the_page_is_bounded_and_the_depth_is_not(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """`QueueSnapshot` keeps the two apart so a bounded read never
        becomes a wrong number."""
        for offset in range(4):
            await tickets.enqueue(_ticket(at=NOW + timedelta(seconds=offset)))
        await contract_session.flush()

        snapshot = await tickets.queue_snapshot(
            pool=_pool(QueueType.RANKED, Region.EUROPE), now=NOW, limit=2
        )

        assert snapshot.waiting == 4
        assert len(snapshot.tickets) == 2

    async def test_a_due_ticket_is_not_in_the_snapshot(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        ticket = await tickets.enqueue(_ticket())
        await contract_session.flush()

        snapshot = await tickets.queue_snapshot(
            pool=QueuePool(
                variant=ProductVariant.RUSSIAN_8X8,
                queue_type=QueueType.RANKED,
                region=Region.EUROPE,
            ),
            now=ticket.expires_at,
            limit=10,
        )

        assert snapshot.waiting == 0


class TestClaimAndExpire:
    async def test_a_due_ticket_is_claimed(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        ticket = await tickets.enqueue(_ticket())
        await contract_session.flush()

        claimed = await tickets.claim_due(now=ticket.expires_at, limit=10, claimed_by="w1")

        assert [item.id for item in claimed] == [ticket.id]

    async def test_claiming_does_not_transition_the_ticket(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """The lock is what excludes another worker; the status change is
        `expire`'s. A worker that dies between the two leaves rows the next
        sweep simply claims again."""
        ticket = await tickets.enqueue(_ticket())
        await contract_session.flush()

        claimed = await tickets.claim_due(now=ticket.expires_at, limit=10, claimed_by="w1")

        assert claimed[0].status is QueueStatus.WAITING

    async def test_a_ticket_that_is_not_due_is_not_claimed(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        await tickets.enqueue(_ticket())
        await contract_session.flush()

        assert not await tickets.claim_due(now=NOW, limit=10, claimed_by="w1")

    async def test_a_resolved_ticket_is_not_claimed(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        ticket = await tickets.enqueue(_ticket())
        await contract_session.flush()
        await tickets.cancel(ticket.cancelled(NOW))

        assert not await tickets.claim_due(now=ticket.expires_at, limit=10, claimed_by="w1")

    async def test_claims_arrive_in_deadline_order(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """Oldest deadline first, so a backlog drains in the order the
        tickets actually lapsed — which is what makes each expiry event's
        `occurred_at` agree with the relay's publication order."""
        later = await tickets.enqueue(_ticket(at=NOW, ttl=TTL + 60))
        earlier = await tickets.enqueue(_ticket(at=NOW, ttl=TTL))
        await contract_session.flush()

        claimed = await tickets.claim_due(
            now=NOW + timedelta(seconds=TTL + 120), limit=10, claimed_by="w1"
        )

        assert [item.id for item in claimed] == [earlier.id, later.id]

    async def test_the_claim_is_bounded_by_the_limit(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        for offset in range(4):
            await tickets.enqueue(_ticket(at=NOW + timedelta(seconds=offset)))
        await contract_session.flush()

        claimed = await tickets.claim_due(
            now=NOW + timedelta(seconds=TTL + 60), limit=2, claimed_by="w1"
        )

        assert len(claimed) == 2

    async def test_expiring_stamps_the_rows(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        ticket = await tickets.enqueue(_ticket())
        await contract_session.flush()

        expired = await tickets.expire([ticket.id], at=ticket.expires_at)

        assert expired == 1
        assert await tickets.active_ticket(ticket.player_id, now=NOW) is None

    async def test_expiring_a_resolved_ticket_counts_nothing(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """`status = 'waiting'` in the predicate as well as the id list, so
        a ticket the player cancelled between the claim and the commit is
        not re-stamped as expired."""
        ticket = await tickets.enqueue(_ticket())
        await contract_session.flush()
        await tickets.cancel(ticket.cancelled(NOW))

        assert await tickets.expire([ticket.id], at=ticket.expires_at) == 0

    async def test_expiring_nothing_issues_no_statement(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        """A sweep routinely ticks with nothing due, and
        `UPDATE ... WHERE id IN ()` is a statement issued to change
        nothing."""
        assert await tickets.expire([], at=NOW) == 0


class TestPairingTransitions:
    """`reserve`, `release` and `complete` against the real table — the
    three writes A64-015.3 added, and the CHECK and index that constrain
    them.

    All-or-nothing is the property worth a database test: it comes from one
    `UPDATE` with a compare-and-set, and a fake that wrote per ticket would
    pass a test the schema would fail.
    """

    async def test_a_pair_reserves(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        one = await tickets.enqueue(_ticket())
        other = await tickets.enqueue(_ticket(player_id=generate_uuid7()))

        applied = await tickets.reserve([one.reserved(), other.reserved()])

        assert applied
        assert (await _status(contract_session, one.id)) is QueueStatus.RESERVED

    async def test_a_reserved_ticket_carries_no_resolution_instant(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """`ck_queue_ticket__resolved_iff_terminal` counts `reserved` as
        live, so a reservation that stamped one would be refused by the
        database and not merely by the aggregate."""
        one = await tickets.enqueue(_ticket())
        await tickets.reserve([one.reserved()])

        row = await contract_session.get(QueueTicketModel, one.id)
        assert row is not None
        assert row.resolved_at is None

    async def test_a_reserved_player_still_holds_their_live_ticket(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        """QT-1's index widened to cover `reserved` (A64-015.3). A player
        being paired is still queued, and a second join must still be
        refused."""
        one = await tickets.enqueue(_ticket())
        await tickets.reserve([one.reserved()])

        with pytest.raises(AlreadyQueued):
            await tickets.enqueue(_ticket(player_id=one.player_id))

    async def test_a_reserved_ticket_is_not_in_a_pool_scan(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        """`ix_queue_ticket__pool` still says `waiting`, which is what makes
        a reserved pair invisible to every other worker's next scan."""
        one = await tickets.enqueue(_ticket())
        await tickets.reserve([one.reserved()])

        snapshot = await tickets.queue_snapshot(pool=_pool(), now=NOW, limit=10)

        assert snapshot.tickets == ()
        assert snapshot.waiting == 0

    async def test_a_waiting_ticket_cannot_be_reserved_twice(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        """The compare-and-set: a second worker's reservation finds the row
        no longer `waiting` and applies nothing."""
        one = await tickets.enqueue(_ticket())
        reserved = one.reserved()
        await tickets.reserve([reserved])

        assert not await tickets.reserve([reserved])

    async def test_reserving_is_all_or_nothing(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """One of the two has already been cancelled. Neither may move — a
        half-reserved pair strands one player with no match coming."""
        one = await tickets.enqueue(_ticket())
        other = await tickets.enqueue(_ticket(player_id=generate_uuid7()))
        await tickets.cancel(other.cancelled(NOW))

        applied = await tickets.reserve([one.reserved(), other.reserved()])

        assert not applied
        assert (await _status(contract_session, one.id)) is QueueStatus.WAITING

    async def test_releasing_returns_a_ticket_to_waiting(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        one = await tickets.enqueue(_ticket())
        reserved = one.reserved()
        await tickets.reserve([reserved])

        assert await tickets.release([reserved.released()])
        assert (await _status(contract_session, one.id)) is QueueStatus.WAITING

    async def test_a_released_ticket_keeps_its_entry_time(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """§10: a platform failure must not cost a player their place in
        line. `release` writes `status` and `resolved_at`, and `entered_at`
        is not in the statement at all."""
        one = await tickets.enqueue(_ticket())
        reserved = one.reserved()
        await tickets.reserve([reserved])
        await tickets.release([reserved.released()])

        row = await contract_session.get(QueueTicketModel, one.id)
        assert row is not None
        assert row.entered_at == one.entered_at
        assert row.expires_at == one.expires_at

    async def test_a_waiting_ticket_cannot_be_released(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        """Compensation for an action that did not happen. The predicate is
        what stops a stray call resurrecting a ticket the sweep resolved."""
        one = await tickets.enqueue(_ticket())

        assert not await tickets.release([one])

    async def test_completing_marks_a_reserved_pair_matched(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        one = await tickets.enqueue(_ticket())
        other = await tickets.enqueue(_ticket(player_id=generate_uuid7()))
        reserved = [one.reserved(), other.reserved()]
        await tickets.reserve(reserved)

        at = NOW + timedelta(seconds=3)
        applied = await tickets.complete([ticket.matched(at) for ticket in reserved], at=at)

        assert applied
        assert (await _status(contract_session, one.id)) is QueueStatus.MATCHED
        assert (await _status(contract_session, other.id)) is QueueStatus.MATCHED

    async def test_a_matched_ticket_stamps_the_creation_instant(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        one = await tickets.enqueue(_ticket())
        reserved = one.reserved()
        await tickets.reserve([reserved])

        at = NOW + timedelta(seconds=3)
        await tickets.complete([reserved.matched(at)], at=at)

        row = await contract_session.get(QueueTicketModel, one.id)
        assert row is not None
        assert row.resolved_at == at

    async def test_a_matched_player_may_queue_again(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        """`matched` is terminal, so it leaves QT-1's index — the player is
        free to queue for their next game once this one ends."""
        one = await tickets.enqueue(_ticket())
        reserved = one.reserved()
        await tickets.reserve([reserved])
        at = NOW + timedelta(seconds=3)
        await tickets.complete([reserved.matched(at)], at=at)

        await tickets.enqueue(_ticket(player_id=one.player_id))

    async def test_an_unreserved_ticket_cannot_be_completed(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        """§8: no ticket is marked matched before `game` accepted, and the
        reservation is the evidence that it did."""
        one = await tickets.enqueue(_ticket())

        at = NOW + timedelta(seconds=3)
        applied = await tickets.complete([_matched_without_reserving(one, at)], at=at)

        assert not applied
        assert (await _status(contract_session, one.id)) is QueueStatus.WAITING


class TestAbandonedReservations:
    """A worker that dies mid-pairing leaves two reserved tickets. Without
    the widened `ix_queue_ticket__due` they would occupy QT-1's index
    forever and lock both players out of the queue permanently."""

    async def test_a_reserved_ticket_past_its_deadline_is_claimed(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        one = await tickets.enqueue(_ticket())
        await tickets.reserve([one.reserved()])

        claimed = await tickets.claim_due(
            now=NOW + timedelta(seconds=TTL + 1), limit=10, claimed_by="w1"
        )

        assert [ticket.id for ticket in claimed] == [one.id]

    async def test_a_reserved_ticket_within_its_window_is_left_alone(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        """A live reservation is a pairing in flight. Only one past its own
        queue deadline is abandoned by any measure."""
        one = await tickets.enqueue(_ticket())
        await tickets.reserve([one.reserved()])

        claimed = await tickets.claim_due(now=NOW, limit=10, claimed_by="w1")

        assert list(claimed) == []

    async def test_an_abandoned_reservation_expires(
        self, tickets: SqlAlchemyQueueRepository, contract_session: AsyncSession
    ) -> None:
        one = await tickets.enqueue(_ticket())
        await tickets.reserve([one.reserved()])

        at = NOW + timedelta(seconds=TTL + 1)
        assert await tickets.expire([one.id], at=at) == 1
        assert (await _status(contract_session, one.id)) is QueueStatus.EXPIRED

    async def test_the_player_can_queue_again_afterwards(
        self, tickets: SqlAlchemyQueueRepository
    ) -> None:
        """The whole point: an abandoned reservation must not be a
        permanent lockout."""
        one = await tickets.enqueue(_ticket())
        await tickets.reserve([one.reserved()])
        await tickets.expire([one.id], at=NOW + timedelta(seconds=TTL + 1))

        await tickets.enqueue(_ticket(player_id=one.player_id))


async def _status(session: AsyncSession, ticket_id: UUID) -> QueueStatus:
    """The row's status as PostgreSQL currently holds it."""
    session.expire_all()
    row = await session.get(QueueTicketModel, ticket_id)
    assert row is not None
    return row.status


def _matched_without_reserving(ticket: QueueTicket, at: datetime) -> QueueTicket:
    """A `matched` ticket the aggregate would refuse to build.

    Constructed directly, because the point of the test it serves is that
    the *repository's* predicate is the guard as well — `QueueTicket.matched`
    already refuses this, and a database that did not would let a repair
    script mark a waiting ticket matched.
    """
    return QueueTicket(
        id=ticket.id,
        player_id=ticket.player_id,
        pool=ticket.pool,
        rating_snapshot=ticket.rating_snapshot,
        entered_at=ticket.entered_at,
        expires_at=ticket.expires_at,
        status=QueueStatus.MATCHED,
        resolved_at=at,
    )


class TestConcurrentWorkers:
    """`SKIP LOCKED`, with two real transactions. The property that makes the
    expiry sweep horizontally scalable, and the only one a dictionary cannot
    model.

    Runs off `contract_engine` rather than `contract_session`: that fixture
    binds its session to one connection inside an outer transaction it always
    rolls back, so a `commit()` there releases a savepoint and is invisible to
    any other connection — which is precisely the visibility this test is
    about. Rows are therefore committed for real and deleted in `finally`.

    The same shape as `tests/contract/test_outbox_repository.py`'s, and
    deliberately so: A64-014.1 required the queue to reuse the outbox's
    proven claim rather than invent one, and running the identical assertion
    against both is what makes "reuse" checkable rather than asserted.
    """

    async def test_two_workers_claim_disjoint_sets(self, contract_engine: AsyncEngine) -> None:
        """The first worker's uncommitted claim locks its rows; the second
        skips them rather than waiting on them or duplicating them.

        Waiting would make N sweepers one sweeper with extra latency;
        duplicating would expire every ticket twice on every tick and emit
        two events for one lapse.
        """
        due_at = NOW + timedelta(seconds=TTL + 60)
        ids: list[UUID] = []
        try:
            async with AsyncSession(contract_engine, expire_on_commit=False) as seeding:
                repository = SqlAlchemyQueueRepository(seeding)
                for offset in range(4):
                    stored = await repository.enqueue(_ticket(at=NOW + timedelta(seconds=offset)))
                    ids.append(stored.id)
                await seeding.commit()

            async with (
                AsyncSession(contract_engine, expire_on_commit=False) as first_session,
                AsyncSession(contract_engine, expire_on_commit=False) as second_session,
            ):
                first = await SqlAlchemyQueueRepository(first_session).claim_due(
                    now=due_at, limit=2, claimed_by="w1"
                )
                # The first session has **not** committed, so its two rows
                # are still locked when the second worker polls.
                second = await SqlAlchemyQueueRepository(second_session).claim_due(
                    now=due_at, limit=2, claimed_by="w2"
                )
                await first_session.rollback()
                await second_session.rollback()

            taken_first = {ticket.id for ticket in first}
            taken_second = {ticket.id for ticket in second}

            assert len(taken_first) == 2
            assert len(taken_second) == 2
            assert taken_first.isdisjoint(taken_second)
            assert taken_first | taken_second == set(ids)
        finally:
            await _cleanup(contract_engine, ids)

    async def test_two_workers_cannot_claim_one_pairing_pair(
        self, contract_engine: AsyncEngine
    ) -> None:
        """§15.9, and the property the in-memory fake cannot model: two
        pairing workers that selected the **same** pair, racing.

        The first holds both rows; the second's `SKIP LOCKED` returns fewer
        than two and `claim_pair` therefore returns nothing rather than the
        one row it managed to lock. Returning that row would hand the loser
        half a pairing — and, worse, a lock on a ticket the winner is about
        to reserve.
        """
        ids: list[UUID] = []
        try:
            async with AsyncSession(contract_engine, expire_on_commit=False) as seeding:
                repository = SqlAlchemyQueueRepository(seeding)
                for offset in range(2):
                    stored = await repository.enqueue(_ticket(at=NOW + timedelta(seconds=offset)))
                    ids.append(stored.id)
                await seeding.commit()

            async with (
                AsyncSession(contract_engine, expire_on_commit=False) as first_session,
                AsyncSession(contract_engine, expire_on_commit=False) as second_session,
            ):
                first = await SqlAlchemyQueueRepository(first_session).claim_pair(ids, now=NOW)
                # Uncommitted, so both rows are still locked when the second
                # worker polls for exactly the same two.
                second = await SqlAlchemyQueueRepository(second_session).claim_pair(ids, now=NOW)
                await first_session.rollback()
                await second_session.rollback()

            assert len(first) == 2
            assert list(second) == []
        finally:
            await _cleanup(contract_engine, ids)

    async def test_a_partly_locked_pair_is_claimed_by_nobody(
        self, contract_engine: AsyncEngine
    ) -> None:
        """The overlapping case, which is the one a naive implementation
        gets wrong: two scans chose pairs that share **one** ticket.

        The second worker can lock its free ticket and not the shared one,
        so a `claim_pair` that returned what it managed to get would reserve
        a single ticket for a pairing that can never complete — a player
        invisible to every future scan with no match coming.
        """
        ids: list[UUID] = []
        try:
            async with AsyncSession(contract_engine, expire_on_commit=False) as seeding:
                repository = SqlAlchemyQueueRepository(seeding)
                for offset in range(3):
                    stored = await repository.enqueue(_ticket(at=NOW + timedelta(seconds=offset)))
                    ids.append(stored.id)
                await seeding.commit()

            async with (
                AsyncSession(contract_engine, expire_on_commit=False) as first_session,
                AsyncSession(contract_engine, expire_on_commit=False) as second_session,
            ):
                first = await SqlAlchemyQueueRepository(first_session).claim_pair(ids[:2], now=NOW)
                overlapping = await SqlAlchemyQueueRepository(second_session).claim_pair(
                    [ids[1], ids[2]], now=NOW
                )
                await first_session.rollback()
                await second_session.rollback()

            assert len(first) == 2
            assert list(overlapping) == []
        finally:
            await _cleanup(contract_engine, ids)

    async def test_two_players_racing_to_queue_produce_one_ticket(
        self, contract_engine: AsyncEngine
    ) -> None:
        """QT-1 under concurrency — the reason this aggregate is in
        PostgreSQL at all.

        Both sessions pass the service's read-first check (neither sees the
        other's uncommitted row), so the partial unique index is the only
        thing left. Without it both commit and the player holds two live
        tickets, which is A-4-grade corruption rather than a duplicate row.
        """
        player = generate_uuid7()
        ids: list[UUID] = []
        try:
            async with (
                AsyncSession(contract_engine, expire_on_commit=False) as first_session,
                AsyncSession(contract_engine, expire_on_commit=False) as second_session,
            ):
                first = await SqlAlchemyQueueRepository(first_session).enqueue(
                    _ticket(player_id=player)
                )
                ids.append(first.id)
                await first_session.commit()

                with pytest.raises(AlreadyQueued):
                    second = await SqlAlchemyQueueRepository(second_session).enqueue(
                        _ticket(player_id=player)
                    )
                    ids.append(second.id)
                await second_session.rollback()
        finally:
            await _cleanup(contract_engine, ids)


async def _cleanup(engine: AsyncEngine, ticket_ids: list[UUID]) -> None:
    """Removes rows `TestConcurrentWorkers` committed.

    Only that class needs it: it has to commit for a lock and a constraint
    to be visible across connections, and therefore escapes the session
    fixture's rollback. Everything else in this file stays inside it.
    """
    if not ticket_ids:
        return

    async with AsyncSession(engine) as session:
        await session.execute(delete(QueueTicketModel).where(QueueTicketModel.id.in_(ticket_ids)))
        await session.commit()
