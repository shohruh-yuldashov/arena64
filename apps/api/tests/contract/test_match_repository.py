"""`SqlAlchemyMatchRecordRepository` against real PostgreSQL — A64-015.4.

`tests/unit/test_match_acceptance.py` covers the lifecycle over in-memory
storage. What it cannot cover is the property the whole task is built
around and that only a real database has:

    a unique index on pairing_id     idempotency under two workers

A64-015.4 §3 forbids in-memory deduplication and check-then-insert by name,
and both forbidden shapes *pass* against a dictionary — a fake can model
uniqueness, and a model that agrees with itself proves nothing. So it is
asserted here, with two real sessions, two real transactions and the real
constraint.

The rest of the file is the mapping and the predicates: three native enums
and an engine version round trip, the partial indexes constrain only pending
rows, the compare-and-set refuses a match somebody else answered, and the
two reads `matchmaking` consumes return what the reconciler and the pairing
scan need.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.identifiers import generate_uuid7
from app.modules.engine import CURRENT_ENGINE_VERSION, EngineVersion, PlayerSide
from app.modules.game.application.services.pairing_settlement_service import (
    GamePairingSettlements,
)
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.infrastructure import MatchRecordModel, SqlAlchemyMatchRecordRepository
from app.modules.game.public import MatchOrigin, ProductVariant

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)

#: The default `MATCHMAKING_RESERVATION_TTL_SECONDS`, and the same instant
#: both source queue tickets carry as `reserved_until`.
WINDOW = timedelta(seconds=30)


def _record(
    *,
    pairing_id: UUID | None = None,
    light: UUID | None = None,
    dark: UUID | None = None,
    at: datetime = NOW,
    status: MatchRecordStatus = MatchRecordStatus.PENDING_ACCEPTANCE,
) -> MatchRecord:
    accepted = at if status is MatchRecordStatus.ACTIVE else None
    return MatchRecord(
        pairing_id=pairing_id or generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(
            player_id=light or generate_uuid7(),
            queue_ticket_id=generate_uuid7(),
            accepted_at=accepted,
        ),
        dark=MatchSeat(
            player_id=dark or generate_uuid7(),
            queue_ticket_id=generate_uuid7(),
            accepted_at=accepted,
        ),
        created_at=at,
        acceptance_deadline=at + WINDOW,
        status=status,
        settled_at=None if status.is_pending else at,
    )


def _tournament_record(*, origin_ref: UUID, at: datetime = NOW) -> MatchRecord:
    """A match created by a bracket — A64-019.6.

    No queue tickets, because the entrant did not arrive through a queue.
    `origin` and `origin_ref` carry where it came from instead, which is
    R-25's mechanism and the reason the ticket columns never had to.
    """
    return MatchRecord(
        pairing_id=generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(player_id=generate_uuid7()),
        dark=MatchSeat(player_id=generate_uuid7()),
        created_at=at,
        acceptance_deadline=at + WINDOW,
        origin=MatchOrigin.TOURNAMENT,
        origin_ref=origin_ref,
    )


@pytest_asyncio.fixture
async def matches(contract_session: AsyncSession) -> SqlAlchemyMatchRecordRepository:
    return SqlAlchemyMatchRecordRepository(contract_session)


class TestCreate:
    async def test_a_match_round_trips_through_the_table(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """Three native enums, an engine version stored as its primitive,
        and four `timestamptz` columns. A mapping that stored a Python
        member name rather than its value would round trip *differently*
        rather than fail, which is the failure a `text` column would have.
        """
        record = _record()

        stored, created = await matches.create(record)

        assert created
        assert stored == record
        assert (await matches.by_pairing(record.pairing_id)) == record

    async def test_the_engine_version_survives_as_a_comparable_number(
        self, matches: SqlAlchemyMatchRecordRepository, contract_session: AsyncSession
    ) -> None:
        """AD-15's question is "which matches were played under a version
        older than the fix", and that has to be an indexable comparison
        rather than a parse."""
        record = _record()
        await matches.create(record)

        row = await contract_session.get(MatchRecordModel, record.id)

        assert row is not None
        assert row.engine_version == CURRENT_ENGINE_VERSION.as_primitive()
        assert (await matches.by_pairing(record.pairing_id)) is not None

    async def test_a_pending_match_carries_no_settlement_instant(
        self, matches: SqlAlchemyMatchRecordRepository, contract_session: AsyncSession
    ) -> None:
        """`ck_match__settled_iff_answered`, enforced by the database as
        well as by the aggregate — so a row written by a repair script
        cannot claim an outcome without its instant."""
        record = _record()
        await matches.create(record)

        row = await contract_session.get(MatchRecordModel, record.id)

        assert row is not None
        assert row.settled_at is None
        assert row.declined_by is None

    async def test_an_unknown_pairing_reads_as_none(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        assert await matches.by_pairing(generate_uuid7()) is None


class TestPairingIdempotency:
    """§3. The unique index, and what the repository makes of it."""

    async def test_a_second_match_for_one_pairing_returns_the_first(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """The retry contract in one assertion: same `match_id`,
        `created=False`. A worker that died after `game` committed and
        before it settled the tickets reaches exactly this path."""
        first = _record()
        await matches.create(first)

        retry = _record(pairing_id=first.pairing_id)
        stored, created = await matches.create(retry)

        assert not created
        assert stored.id == first.id

    async def test_the_retry_does_not_leave_a_broken_transaction(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """The `SAVEPOINT` is load-bearing rather than tidy: PostgreSQL
        aborts the whole transaction on a constraint violation, so without
        one to roll back to, the re-read *and every later statement* would
        fail with `InFailedSQLTransaction` — turning an invisible retry into
        a 500."""
        first = _record()
        await matches.create(first)
        await matches.create(_record(pairing_id=first.pairing_id))

        # The session must still be usable. Before the savepoint this raised.
        assert await matches.by_pairing(first.pairing_id) is not None
        assert (await matches.create(_record()))[1]

    async def test_two_different_pairings_get_two_matches(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """The other half: idempotency must not collapse distinct pairs."""
        one, _ = await matches.create(_record())
        other, created = await matches.create(_record())

        assert created
        assert one.id != other.id

    async def test_one_queue_ticket_produces_at_most_one_match(
        self, matches: SqlAlchemyMatchRecordRepository, contract_session: AsyncSession
    ) -> None:
        """`uq_match__light_ticket`. Already true by construction — a ticket
        is reserved once and matched once — and this is what makes it true
        under a bug as well.

        The failure propagates rather than deduplicating: there is no match
        for *this* pairing to return, so there is nothing honest to hand
        back and `PairingService` compensates.
        """
        first = _record()
        await matches.create(first)

        colliding = MatchRecord(
            pairing_id=generate_uuid7(),
            variant=first.variant,
            rated=first.rated,
            engine_version=first.engine_version,
            light=first.light,
            dark=MatchSeat(player_id=generate_uuid7(), queue_ticket_id=generate_uuid7()),
            created_at=NOW,
            acceptance_deadline=NOW + WINDOW,
        )

        with pytest.raises(IntegrityError):
            await matches.create(colliding)
        await contract_session.rollback()


class TestConcurrentCreation:
    """Two workers, two transactions, one pairing — §14's first race.

    Runs off `contract_engine` rather than `contract_session`: that fixture
    binds its session to one connection inside an outer transaction it
    always rolls back, so a `commit()` there releases a savepoint and is
    invisible to any other connection — which is precisely the visibility
    this test is about. Rows are therefore committed for real and deleted in
    `finally`.

    The same shape as `test_queue_repository.py`'s claim test, and
    deliberately so: A64-015.4 requires the platform's proven mechanisms
    rather than new ones, and running the identical assertion against both
    is what makes "reuse" checkable rather than asserted.
    """

    async def test_two_workers_cannot_create_two_matches_for_one_pairing(
        self, contract_engine: AsyncEngine
    ) -> None:
        """The loser's insert is refused by `uq_match__pairing_id`, and the
        repository turns that refusal into the winner's row.

        This is the assertion A64-015.4 §3 exists for. A check-then-insert
        would pass every other test in this file and fail this one: both
        workers read no row, both insert, and two players who agreed to one
        game have two.
        """
        pairing_id = generate_uuid7()
        created_ids: set[UUID] = set()
        try:
            async with (
                AsyncSession(contract_engine, expire_on_commit=False) as first_session,
                AsyncSession(contract_engine, expire_on_commit=False) as second_session,
            ):
                first = SqlAlchemyMatchRecordRepository(first_session)
                second = SqlAlchemyMatchRecordRepository(second_session)

                one, one_created = await first.create(_record(pairing_id=pairing_id))
                await first_session.commit()

                # The second worker arrives with the winner's row already
                # committed and visible — the ordinary shape of a retry
                # after a crash, and of two schedulers scanning one pool.
                other, other_created = await second.create(_record(pairing_id=pairing_id))
                await second_session.commit()

                created_ids = {one.id, other.id}

                assert one_created
                assert not other_created
                assert one.id == other.id
        finally:
            async with AsyncSession(contract_engine) as cleanup:
                await cleanup.execute(
                    delete(MatchRecordModel).where(MatchRecordModel.id.in_(created_ids))
                )
                await cleanup.commit()

    async def test_only_one_row_exists_afterwards(self, contract_engine: AsyncEngine) -> None:
        """The property that matters is about the *table*, not about what
        the two callers were told."""
        pairing_id = generate_uuid7()
        match_id: UUID | None = None
        try:
            async with AsyncSession(contract_engine, expire_on_commit=False) as session:
                repository = SqlAlchemyMatchRecordRepository(session)
                stored, _ = await repository.create(_record(pairing_id=pairing_id))
                await repository.create(_record(pairing_id=pairing_id))
                await session.commit()
                match_id = stored.id

            async with AsyncSession(contract_engine) as reader:
                rows = await reader.scalars(
                    MatchRecordModel.__table__.select().where(
                        MatchRecordModel.__table__.c.pairing_id == pairing_id
                    )
                )
                assert len(list(rows.all())) == 1
        finally:
            async with AsyncSession(contract_engine) as cleanup:
                await cleanup.execute(
                    delete(MatchRecordModel).where(MatchRecordModel.id == match_id)
                )
                await cleanup.commit()


class TestSettle:
    async def test_an_acceptance_is_written(self, matches: SqlAlchemyMatchRecordRepository) -> None:
        record, _ = await matches.create(_record())

        assert await matches.settle(record.accepted_by(PlayerSide.LIGHT, at=NOW))

        stored = await matches.by_pairing(record.pairing_id)
        assert stored is not None
        assert stored.light.accepted_at == NOW

    async def test_an_activation_is_written(
        self, matches: SqlAlchemyMatchRecordRepository, contract_session: AsyncSession
    ) -> None:
        """`ck_match__active_iff_both_accepted` is what stops a repair
        script writing an active match nobody accepted, so an activation
        that carried one blank seat would be refused by the database rather
        than only by the aggregate."""
        record, _ = await matches.create(_record())
        both = record.accepted_by(PlayerSide.LIGHT, at=NOW).accepted_by(PlayerSide.DARK, at=NOW)

        assert await matches.settle(both)

        row = await contract_session.get(MatchRecordModel, record.id)
        assert row is not None
        assert row.status is MatchRecordStatus.ACTIVE
        assert row.settled_at == NOW

    async def test_a_decline_records_the_side(
        self, matches: SqlAlchemyMatchRecordRepository, contract_session: AsyncSession
    ) -> None:
        """`ck_match__declined_iff_cancelled`: a row cannot say "expired"
        and name somebody who declined, which is the difference between an
        absence and a decision."""
        record, _ = await matches.create(_record())

        assert await matches.settle(record.declined(PlayerSide.DARK, at=NOW))

        row = await contract_session.get(MatchRecordModel, record.id)
        assert row is not None
        assert row.status is MatchRecordStatus.CANCELLED
        assert row.declined_by is PlayerSide.DARK

    async def test_a_settled_match_cannot_be_settled_again(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """The compare-and-set. Two players answering at once race here, and
        without the predicate the later write silently overwrites the
        earlier — a decline overwriting an activation, or the reverse."""
        record, _ = await matches.create(_record())
        await matches.settle(record.declined(PlayerSide.LIGHT, at=NOW))

        assert not await matches.settle(record.declined(PlayerSide.DARK, at=NOW))

    async def test_an_unknown_match_settles_nothing(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        assert not await matches.settle(_record().declined(PlayerSide.LIGHT, at=NOW))


class TestPendingReads:
    async def test_a_participant_finds_their_pending_match(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        record, _ = await matches.create(_record())

        assert (await matches.pending_for(record.light.player_id)) == record
        assert (await matches.pending_for(record.dark.player_id)) == record

    async def test_a_stranger_finds_nothing(self, matches: SqlAlchemyMatchRecordRepository) -> None:
        await matches.create(_record())

        assert await matches.pending_for(generate_uuid7()) is None

    async def test_a_settled_match_is_no_longer_pending(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """The two partial indexes are predicated on `pending_acceptance`,
        which is what keeps this read bounded by concurrency rather than by
        history."""
        record, _ = await matches.create(_record())
        await matches.settle(record.declined(PlayerSide.LIGHT, at=NOW))

        assert await matches.pending_for(record.light.player_id) is None

    async def test_a_lock_returns_the_row(self, matches: SqlAlchemyMatchRecordRepository) -> None:
        record, _ = await matches.create(_record())

        assert (await matches.lock(record.id)) == record

    async def test_locking_an_unknown_match_returns_none(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        assert await matches.lock(generate_uuid7()) is None


class TestOverdueClaims:
    async def test_an_overdue_pending_match_is_claimed(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        record, _ = await matches.create(_record())

        claimed = await matches.claim_overdue(now=NOW + WINDOW + timedelta(seconds=1), limit=10)

        assert [each.id for each in claimed] == [record.id]

    async def test_claiming_does_not_transition_the_match(
        self, matches: SqlAlchemyMatchRecordRepository, contract_session: AsyncSession
    ) -> None:
        """A worker that dies here leaves matches the next tick claims
        again — `settle` is what resolves them."""
        record, _ = await matches.create(_record())

        await matches.claim_overdue(now=NOW + WINDOW + timedelta(seconds=1), limit=10)

        row = await contract_session.get(MatchRecordModel, record.id)
        assert row is not None
        assert row.status is MatchRecordStatus.PENDING_ACCEPTANCE

    async def test_a_match_inside_its_window_is_not_claimed(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        await matches.create(_record())

        assert list(await matches.claim_overdue(now=NOW, limit=10)) == []

    async def test_a_settled_match_is_never_claimed(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        record, _ = await matches.create(_record())
        await matches.settle(record.declined(PlayerSide.LIGHT, at=NOW))

        overdue = await matches.claim_overdue(now=NOW + WINDOW + timedelta(seconds=1), limit=10)

        assert list(overdue) == []

    async def test_the_claim_is_bounded(self, matches: SqlAlchemyMatchRecordRepository) -> None:
        for offset in range(4):
            await matches.create(_record(at=NOW + timedelta(seconds=offset)))

        claimed = await matches.claim_overdue(now=NOW + WINDOW + timedelta(minutes=1), limit=2)

        assert len(claimed) == 2


class TestTheTwoPublishedReads:
    async def test_settlements_are_found_by_either_ticket(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """The reconciler holds one orphaned reserved ticket and does not
        know who the partner was, so the lookup has to work from either
        side."""
        record, _ = await matches.create(_record())

        found = await matches.settlements_for([record.dark.queue_ticket_id])

        assert [each.id for each in found] == [record.id]

    async def test_a_ticket_with_no_match_finds_nothing(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """The ordinary answer for a reservation whose worker died before it
        called `game`, and the case whose action is "put this player back in
        the queue"."""
        await matches.create(_record())

        assert list(await matches.settlements_for([generate_uuid7()])) == []

    async def test_the_latest_opponent_is_the_most_recent_settled_match(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        player, old, recent = generate_uuid7(), generate_uuid7(), generate_uuid7()
        await matches.create(
            _record(
                light=player,
                dark=old,
                at=NOW - timedelta(hours=1),
                status=MatchRecordStatus.ACTIVE,
            )
        )
        await matches.create(
            _record(light=recent, dark=player, at=NOW, status=MatchRecordStatus.ACTIVE)
        )

        latest = await matches.latest_opponent_among([player, old, recent])

        assert latest[player] == recent

    async def test_a_pending_match_is_not_a_game_they_have_played(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        record, _ = await matches.create(_record())

        latest = await matches.latest_opponent_among(list(record.player_ids()))

        assert latest == {}

    async def test_the_read_is_empty_for_players_with_no_history(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        assert await matches.latest_opponent_among([generate_uuid7()]) == {}


class TestNonQueueParticipants:
    """A64-019.6 — a match need not have come from a queue.

    The four cases are one property seen from both sides: a queue pairing
    still records real tickets and is still reconcilable by them, and every
    other origin records none and is invisible to that lookup rather than
    answering it with a fabricated id.
    """

    async def test_a_queue_match_persists_its_real_tickets(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """The behaviour that must not change. `matchmaking` supplies two
        real ticket ids and both survive the round trip — the durable link
        A64-015.3 recorded as missing and A64-015.4 added."""
        record, _ = await matches.create(_record())

        stored = await matches.by_id(record.id)

        assert stored is not None
        assert stored.origin is MatchOrigin.QUEUE
        assert stored.light.queue_ticket_id == record.light.queue_ticket_id
        assert stored.dark.queue_ticket_id == record.dark.queue_ticket_id
        assert stored.queue_ticket_ids() == record.ticket_ids()

    async def test_a_tournament_match_persists_null_tickets_and_round_trips(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """The correction. A tournament entrant arrived through a bracket,
        so the column says so by being empty rather than by holding a
        derived uuid5 that asserts a ticket nobody ever held.

        `origin` and `origin_ref` are what survive instead, which is R-25's
        whole mechanism — asserted here because a round trip that lost them
        would leave a match the tournament could never recognise again.
        """
        reference = generate_uuid7()
        record, created = await matches.create(_tournament_record(origin_ref=reference))

        assert created
        stored = await matches.by_id(record.id)

        assert stored is not None
        assert stored.origin is MatchOrigin.TOURNAMENT
        assert stored.origin_ref == reference
        assert stored.light.queue_ticket_id is None
        assert stored.dark.queue_ticket_id is None
        assert stored.queue_ticket_ids() == ()

    async def test_two_tournament_matches_coexist_without_colliding(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """`uq_match__light_ticket` is unique over a nullable column, and
        PostgreSQL treats each `NULL` as distinct — so a second ticketless
        match is not a duplicate. This is the assertion that says the
        indexes did not have to be rewritten as partial ones."""
        await matches.create(_tournament_record(origin_ref=generate_uuid7()))
        second, created = await matches.create(_tournament_record(origin_ref=generate_uuid7()))

        assert created
        assert second.light.queue_ticket_id is None

    async def test_reconciliation_still_resolves_queue_matches_and_ignores_the_rest(
        self, matches: SqlAlchemyMatchRecordRepository
    ) -> None:
        """`matchmaking`'s recovery is unchanged, and a ticketless match is
        simply not an answer to its question.

        Both halves matter: the first is the regression this change could
        have caused, and the second is what stops a tournament match being
        returned for a ticket that was never issued.
        """
        queued, _ = await matches.create(_record())
        await matches.create(_tournament_record(origin_ref=generate_uuid7()))

        settlements = await GamePairingSettlements(matches).settlements_for(
            [queued.light.queue_ticket_id, generate_uuid7()]
        )

        assert list(settlements) == [queued.light.queue_ticket_id]
        assert settlements[queued.light.queue_ticket_id].match_id == queued.id


class TestTheDatabaseHoldsTheInvariants:
    """The CHECK constraints, driven by SQL rather than through the
    aggregate — the aggregate already refuses these shapes, and the point is
    that a repair script cannot get past the database either (BE-06)."""

    async def test_an_active_match_needs_both_acceptances(
        self, matches: SqlAlchemyMatchRecordRepository, contract_session: AsyncSession
    ) -> None:
        record, _ = await matches.create(_record())

        with pytest.raises(IntegrityError, match="ck_match__active_iff_both_accepted"):
            await contract_session.execute(
                MatchRecordModel.__table__.update()
                .where(MatchRecordModel.__table__.c.id == record.id)
                .values(status=MatchRecordStatus.ACTIVE.value, settled_at=NOW)
            )
        await contract_session.rollback()

    async def test_a_window_cannot_close_before_it_opens(
        self, matches: SqlAlchemyMatchRecordRepository, contract_session: AsyncSession
    ) -> None:
        record, _ = await matches.create(_record())

        with pytest.raises(IntegrityError, match="ck_match__acceptance_window_positive"):
            await contract_session.execute(
                MatchRecordModel.__table__.update()
                .where(MatchRecordModel.__table__.c.id == record.id)
                .values(acceptance_deadline=NOW - timedelta(seconds=1))
            )
        await contract_session.rollback()

    async def test_an_engine_version_is_positive(self, contract_session: AsyncSession) -> None:
        with pytest.raises(ValueError, match="positive integer"):
            EngineVersion(number=0)
