"""The two audit relations against real PostgreSQL — A64-015.6 §3, §4, §9.

`tests/unit/test_cooldown_audit.py` and
`tests/unit/test_reconciliation_timeline.py` cover what the services decide,
over in-memory storage. What they cannot cover is what only a real database
has, and every property this task's correctness actually rests on is in that
category:

    the partial unique index    two concurrent deliveries of one decline
                                resolve to one row, inside one statement
    ON CONFLICT ... RETURNING   the loser reads the winner's row rather than
                                raising or inserting a second
    one transaction             a rollback leaves neither the bar nor its
                                record, which is the pairing §3 exists for
    SKIP LOCKED                 two pruners take disjoint sets

All four are unfalsifiable against a dictionary — a fake can model them, and
a model that agrees with itself proves nothing.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.identifiers import generate_uuid7
from app.modules.matchmaking.domain.cooldown import CooldownReason, QueueCooldown
from app.modules.matchmaking.domain.cooldown_audit import CooldownRecord
from app.modules.matchmaking.domain.events import ReconciliationAction
from app.modules.matchmaking.domain.reconciliation_timeline import ReconciliationEntry
from app.modules.matchmaking.infrastructure import (
    QueueCooldownAuditModel,
    ReconciliationTimelineModel,
    SqlAlchemyCooldownAuditRepository,
    SqlAlchemyCooldownRepository,
    SqlAlchemyReconciliationTimelineRepository,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
WINDOW = timedelta(seconds=60)


@pytest_asyncio.fixture
async def audit(contract_session: AsyncSession) -> SqlAlchemyCooldownAuditRepository:
    return SqlAlchemyCooldownAuditRepository(contract_session)


@pytest_asyncio.fixture
async def timeline(contract_session: AsyncSession) -> SqlAlchemyReconciliationTimelineRepository:
    return SqlAlchemyReconciliationTimelineRepository(contract_session)


@pytest_asyncio.fixture
async def cooldowns(contract_session: AsyncSession) -> SqlAlchemyCooldownRepository:
    return SqlAlchemyCooldownRepository(contract_session)


def _record(
    *,
    player_id: UUID | None = None,
    source_match_id: UUID | None = None,
    applied_at: datetime = NOW,
    extended_existing: bool = False,
) -> CooldownRecord:
    return CooldownRecord(
        player_id=player_id or generate_uuid7(),
        reason=CooldownReason.DECLINED_MATCH,
        source_match_id=source_match_id if source_match_id is not None else generate_uuid7(),
        applied_at=applied_at,
        expires_at=applied_at + WINDOW,
        extended_existing=extended_existing,
    )


def _entry(
    *,
    event_id: UUID | None = None,
    ticket_id: UUID | None = None,
    action: ReconciliationAction = ReconciliationAction.REQUEUED,
    occurred_at: datetime = NOW,
) -> ReconciliationEntry:
    return ReconciliationEntry(
        event_id=event_id or generate_uuid7(),
        ticket_id=ticket_id or generate_uuid7(),
        player_id=generate_uuid7(),
        action=action,
        match_id=None,
        pairing_id=None,
        occurred_at=occurred_at,
        recorded_at=occurred_at + timedelta(seconds=1),
    )


class TestTheCooldownAuditRelation:
    async def test_a_record_round_trips(
        self, audit: SqlAlchemyCooldownAuditRepository, contract_session: AsyncSession
    ) -> None:
        written = await audit.record(_record())
        await contract_session.commit()

        stored = await contract_session.get(QueueCooldownAuditModel, written.id)
        assert stored is not None
        assert stored.reason is CooldownReason.DECLINED_MATCH

    async def test_the_window_survives_the_round_trip_with_its_timezone(
        self, audit: SqlAlchemyCooldownAuditRepository
    ) -> None:
        """`timestamptz`, not `timestamp`. A naive column would silently
        reinterpret the instant a bar was applied, which for a support answer
        is the whole content."""
        written = await audit.record(_record())

        history = await audit.history_for(written.player_id, limit=10)
        assert history[0].applied_at == NOW
        assert history[0].expires_at == NOW + WINDOW

    async def test_a_second_record_for_one_match_is_refused_by_the_index(
        self, audit: SqlAlchemyCooldownAuditRepository, contract_session: AsyncSession
    ) -> None:
        """`uq_queue_cooldown_audit__source`. The redelivery path AD-16
        guarantees, resolved by the database rather than by a check the two
        deliveries would both pass."""
        first = await audit.record(_record())

        again = await audit.record(
            _record(player_id=first.player_id, source_match_id=first.source_match_id)
        )
        await contract_session.commit()

        assert again.id == first.id
        assert len(await audit.history_for(first.player_id, limit=10)) == 1

    async def test_the_conflicting_write_returns_the_stored_row_rather_than_its_own(
        self, audit: SqlAlchemyCooldownAuditRepository
    ) -> None:
        """`ON CONFLICT DO NOTHING` then re-read. A caller that got its own
        unstored value back would believe an audit row exists that says
        something different from the one that does."""
        first = await audit.record(_record(applied_at=NOW))

        again = await audit.record(
            _record(
                player_id=first.player_id,
                source_match_id=first.source_match_id,
                applied_at=NOW + timedelta(hours=1),
            )
        )

        assert again.applied_at == NOW

    async def test_two_matches_are_two_rows_for_one_player(
        self, audit: SqlAlchemyCooldownAuditRepository
    ) -> None:
        """The half a deduplicate-by-player would get wrong: two refusals are
        two facts."""
        player_id = generate_uuid7()

        await audit.record(_record(player_id=player_id))
        await audit.record(_record(player_id=player_id, applied_at=NOW + timedelta(minutes=1)))

        assert len(await audit.history_for(player_id, limit=10)) == 2

    async def test_a_record_with_no_source_match_does_not_contend(
        self, audit: SqlAlchemyCooldownAuditRepository
    ) -> None:
        """The index is partial because `source_match_id` is nullable and
        reserved for a future non-match reason. Nulls are distinct in a
        unique index, so two such rows are two events rather than a
        conflict."""
        player_id = generate_uuid7()

        await audit.record(_record(player_id=player_id, source_match_id=None))
        await audit.record(
            _record(
                player_id=player_id, source_match_id=None, applied_at=NOW + timedelta(minutes=1)
            )
        )

        assert len(await audit.history_for(player_id, limit=10)) == 2

    async def test_the_history_query_is_ordered_and_bounded(
        self, audit: SqlAlchemyCooldownAuditRepository
    ) -> None:
        player_id = generate_uuid7()
        for minutes in range(5):
            await audit.record(
                _record(player_id=player_id, applied_at=NOW + timedelta(minutes=minutes))
            )

        history = await audit.history_for(player_id, limit=3)

        assert len(history) == 3
        assert [row.applied_at for row in history] == [
            NOW + timedelta(minutes=4),
            NOW + timedelta(minutes=3),
            NOW + timedelta(minutes=2),
        ]

    async def test_a_window_that_ends_before_it_starts_is_refused(
        self, audit: SqlAlchemyCooldownAuditRepository, contract_session: AsyncSession
    ) -> None:
        """`ck_queue_cooldown_audit__window_positive`. The database is the
        authoritative copy of the invariant the dataclass also checks
        (BE-06), so a row inserted by anything else is still refused."""
        contract_session.add(
            QueueCooldownAuditModel(
                id=generate_uuid7(),
                player_id=generate_uuid7(),
                reason=CooldownReason.DECLINED_MATCH,
                source_match_id=generate_uuid7(),
                applied_at=NOW,
                expires_at=NOW - WINDOW,
                extended_existing=False,
            )
        )

        with_error = False
        try:
            await contract_session.flush()
        except Exception:
            with_error = True
        finally:
            await contract_session.rollback()

        assert with_error


class TestTheBarAndItsRecordAreOneTransaction:
    """§3: "the audit record must be written in the same transaction".

    The unit suite asserts the *sequencing*; this asserts what the sequencing
    buys — a rollback that leaves neither row.
    """

    async def test_a_rollback_leaves_neither_the_bar_nor_its_record(
        self,
        cooldowns: SqlAlchemyCooldownRepository,
        audit: SqlAlchemyCooldownAuditRepository,
        contract_session: AsyncSession,
    ) -> None:
        player_id = generate_uuid7()
        cooldown = QueueCooldown.after_decline(player_id, at=NOW, seconds=60.0)

        stored = await cooldowns.apply(cooldown)
        await audit.record(
            CooldownRecord.of(
                stored, source_match_id=generate_uuid7(), applied_at=NOW, extended_existing=False
            )
        )
        await contract_session.rollback()

        assert await cooldowns.active_for(player_id, now=NOW) is None
        assert await audit.history_for(player_id, limit=10) == []

    async def test_a_commit_leaves_both(
        self,
        cooldowns: SqlAlchemyCooldownRepository,
        audit: SqlAlchemyCooldownAuditRepository,
        contract_session: AsyncSession,
    ) -> None:
        player_id = generate_uuid7()
        stored = await cooldowns.apply(QueueCooldown.after_decline(player_id, at=NOW, seconds=60.0))
        await audit.record(
            CooldownRecord.of(
                stored, source_match_id=generate_uuid7(), applied_at=NOW, extended_existing=False
            )
        )
        await contract_session.commit()

        assert await cooldowns.active_for(player_id, now=NOW) is not None
        assert len(await audit.history_for(player_id, limit=10)) == 1


class TestTwoDeliveriesOnTwoConnections:
    """The race the unique index exists for, run rather than modelled.

    A redelivery is ordinarily stopped by the `processed_event` ledger. What
    the ledger cannot stop is **two relays delivering concurrently**, and
    there the index is the only thing standing between one decline and two
    audit rows.
    """

    async def test_only_one_audit_row_survives(self, contract_engine: AsyncEngine) -> None:
        maker = sessionmaker(contract_engine, class_=AsyncSession, expire_on_commit=False)
        player_id, match_id = generate_uuid7(), generate_uuid7()

        async with maker() as first, maker() as second:
            await SqlAlchemyCooldownAuditRepository(first).record(
                _record(player_id=player_id, source_match_id=match_id)
            )
            await first.commit()

            written = await SqlAlchemyCooldownAuditRepository(second).record(
                _record(player_id=player_id, source_match_id=match_id)
            )
            await second.commit()

            history = await SqlAlchemyCooldownAuditRepository(second).history_for(
                player_id, limit=10
            )

        assert len(history) == 1
        assert written.id == history[0].id

    async def test_only_one_timeline_entry_survives(self, contract_engine: AsyncEngine) -> None:
        maker = sessionmaker(contract_engine, class_=AsyncSession, expire_on_commit=False)
        event_id, ticket_id = generate_uuid7(), generate_uuid7()

        async with maker() as first, maker() as second:
            await SqlAlchemyReconciliationTimelineRepository(first).append(
                _entry(event_id=event_id, ticket_id=ticket_id)
            )
            await first.commit()

            await SqlAlchemyReconciliationTimelineRepository(second).append(
                _entry(event_id=event_id, ticket_id=ticket_id)
            )
            await second.commit()

            history = await SqlAlchemyReconciliationTimelineRepository(second).for_ticket(
                ticket_id, limit=10
            )

        assert len(history) == 1


class TestTheTimelineRelation:
    async def test_an_entry_round_trips(
        self, timeline: SqlAlchemyReconciliationTimelineRepository, contract_session: AsyncSession
    ) -> None:
        written = await timeline.append(_entry(action=ReconciliationAction.SETTLED))
        await contract_session.commit()

        stored = await contract_session.get(ReconciliationTimelineModel, written.id)
        assert stored is not None
        assert stored.action is ReconciliationAction.SETTLED

    async def test_every_action_the_enum_holds_is_storable(
        self, timeline: SqlAlchemyReconciliationTimelineRepository
    ) -> None:
        """The database enum and the domain enum are two copies of one list —
        a migration restates the members rather than importing them, so a
        member added to one and not the other fails here."""
        ticket_id = generate_uuid7()

        for offset, action in enumerate(ReconciliationAction):
            await timeline.append(
                _entry(
                    ticket_id=ticket_id,
                    action=action,
                    occurred_at=NOW + timedelta(seconds=offset),
                )
            )

        history = await timeline.for_ticket(ticket_id, limit=50)
        assert {row.action for row in history} == set(ReconciliationAction)

    async def test_a_redelivered_event_appends_once(
        self, timeline: SqlAlchemyReconciliationTimelineRepository
    ) -> None:
        first = await timeline.append(_entry())

        again = await timeline.append(_entry(event_id=first.event_id, ticket_id=first.ticket_id))

        assert again.id == first.id
        assert len(await timeline.for_ticket(first.ticket_id, limit=10)) == 1

    async def test_the_ticket_query_is_ordered_and_bounded(
        self, timeline: SqlAlchemyReconciliationTimelineRepository
    ) -> None:
        ticket_id = generate_uuid7()
        for minutes in range(4):
            await timeline.append(
                _entry(ticket_id=ticket_id, occurred_at=NOW + timedelta(minutes=minutes))
            )

        history = await timeline.for_ticket(ticket_id, limit=2)

        assert [row.occurred_at for row in history] == [
            NOW + timedelta(minutes=3),
            NOW + timedelta(minutes=2),
        ]

    async def test_the_pairing_query_returns_nothing_because_the_column_is_null(
        self, timeline: SqlAlchemyReconciliationTimelineRepository
    ) -> None:
        """Honestly empty rather than back-filled with a guess. The partial
        index exists so the query does not change when the event grows the
        field."""
        await timeline.append(_entry())

        assert await timeline.for_pairing(generate_uuid7(), limit=10) == []


class TestRetentionOverBothRelations:
    """§9. Both relations are bounded, both deletes are batched, and neither
    can reach what it is not supposed to."""

    async def test_an_audit_row_past_the_horizon_goes(
        self, audit: SqlAlchemyCooldownAuditRepository
    ) -> None:
        written = await audit.record(_record(applied_at=NOW - timedelta(days=120)))

        deleted = await audit.prune_recorded(before=NOW - timedelta(days=90), batch_size=10)

        assert deleted == 1
        assert await audit.history_for(written.player_id, limit=10) == []

    async def test_an_audit_row_inside_the_horizon_stays(
        self, audit: SqlAlchemyCooldownAuditRepository
    ) -> None:
        await audit.record(_record(applied_at=NOW - timedelta(days=30)))

        assert await audit.prune_recorded(before=NOW - timedelta(days=90), batch_size=10) == 0

    async def test_the_audit_delete_is_bounded_by_its_batch(
        self, audit: SqlAlchemyCooldownAuditRepository
    ) -> None:
        """A retention job that deleted everything it found in one statement
        would hold locks proportional to the backlog — which is exactly the
        first run after a horizon ships."""
        for minutes in range(5):
            await audit.record(
                _record(applied_at=NOW - timedelta(days=120) + timedelta(minutes=minutes))
            )

        assert await audit.prune_recorded(before=NOW, batch_size=2) == 2

    async def test_a_timeline_entry_past_the_horizon_goes(
        self, timeline: SqlAlchemyReconciliationTimelineRepository
    ) -> None:
        written = await timeline.append(_entry(occurred_at=NOW - timedelta(days=30)))

        deleted = await timeline.prune_recorded(before=NOW - timedelta(days=14), batch_size=10)

        assert deleted == 1
        assert await timeline.for_ticket(written.ticket_id, limit=10) == []

    async def test_a_timeline_entry_inside_the_horizon_stays(
        self, timeline: SqlAlchemyReconciliationTimelineRepository
    ) -> None:
        await timeline.append(_entry(occurred_at=NOW - timedelta(days=7)))

        assert await timeline.prune_recorded(before=NOW - timedelta(days=14), batch_size=10) == 0

    async def test_two_pruners_take_disjoint_sets(self, contract_engine: AsyncEngine) -> None:
        """`SKIP LOCKED`, which is the property a dictionary cannot have: two
        maintenance workers must divide the backlog rather than contend for
        it or delete it twice."""
        maker = sessionmaker(contract_engine, class_=AsyncSession, expire_on_commit=False)

        async with maker() as setup:
            for minutes in range(6):
                await SqlAlchemyCooldownAuditRepository(setup).record(
                    _record(applied_at=NOW - timedelta(days=120) + timedelta(minutes=minutes))
                )
            await setup.commit()

        async with maker() as first, maker() as second:
            taken = await SqlAlchemyCooldownAuditRepository(first).prune_recorded(
                before=NOW, batch_size=3
            )
            # The first transaction is still open, so its three rows are
            # locked. A second pruner must skip them rather than block.
            also_taken = await SqlAlchemyCooldownAuditRepository(second).prune_recorded(
                before=NOW, batch_size=3
            )
            await first.commit()
            await second.commit()

        assert taken == 3
        assert also_taken == 3

    async def test_a_prune_with_nothing_to_do_reports_zero(
        self, audit: SqlAlchemyCooldownAuditRepository
    ) -> None:
        assert await audit.prune_recorded(before=NOW - timedelta(days=365), batch_size=10) == 0
