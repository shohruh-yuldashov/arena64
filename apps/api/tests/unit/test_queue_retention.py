"""Retention — A64-015.5 §8.

A64-014.1 shipped `queue_ticket` with the gap in its own model docstring
("resolved tickets accumulate … storage grows with matches attempted,
forever"), and A64-015.4 recorded the same for the abandoned half of
`game.match`. This file is the evidence that both are bounded, and — much
more importantly — that neither can reach a row that is still in use.

The safety property is asserted from **two directions**, because it is held
in two places and a test of only one would pass while the other rotted:

    the predicate  a live ticket and an active match are excluded by the
                   `WHERE`, so no horizon can reach them. Asserted here
                   against the in-memory store, and against real PostgreSQL
                   in `tests/contract/test_queue_retention.py`
    the horizon    a settings validator keeps it clear of the ticket's own
                   lifetime. Asserted in `test_settings.py`
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.application.metrics import (
    RETENTION_DELETIONS,
    RetentionRelation,
)
from app.modules.matchmaking.application.services import (
    QueueRetentionService,
    queue_retention_policy,
)
from app.modules.matchmaking.domain.cooldown import CooldownReason, QueueCooldown
from app.modules.matchmaking.domain.cooldown_audit import CooldownRecord
from app.modules.matchmaking.domain.events import ReconciliationAction
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType
from app.modules.matchmaking.domain.queue_ticket import QueueStatus, QueueTicket
from app.modules.matchmaking.domain.reconciliation_timeline import ReconciliationEntry
from tests.fakes.audit import (
    InMemoryCooldownAuditRepository,
    InMemoryReconciliationTimelineRepository,
)
from tests.fakes.cooldowns import InMemoryCooldownRepository
from tests.fakes.metrics import RecordingMetrics
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.presence_redis import MovableClock
from tests.fakes.retention import InMemoryAbandonedMatches, InMemoryQueueRetentionStore
from tests.fakes.time_controls import BLITZ

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)

TICKET_HORIZON = timedelta(hours=72)
MATCH_HORIZON = timedelta(hours=168)

#: A64-015.6 §9. Both longer than the rows they explain — ninety days for the
#: cooldown audit against the bar's one hour, fourteen days for the timeline
#: against the ticket's three.
AUDIT_HORIZON = timedelta(hours=2160)
TIMELINE_HORIZON = timedelta(hours=336)

POOL = QueuePool(
    variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED, time_control_id=BLITZ.id
)


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def tickets() -> InMemoryQueueRetentionStore:
    return InMemoryQueueRetentionStore()


@pytest.fixture
def matches() -> InMemoryAbandonedMatches:
    return InMemoryAbandonedMatches()


@pytest.fixture
def cooldowns() -> InMemoryCooldownRepository:
    return InMemoryCooldownRepository()


@pytest.fixture
def cooldown_audit() -> InMemoryCooldownAuditRepository:
    return InMemoryCooldownAuditRepository()


@pytest.fixture
def timeline() -> InMemoryReconciliationTimelineRepository:
    return InMemoryReconciliationTimelineRepository()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


@pytest.fixture
def retention(
    tickets: InMemoryQueueRetentionStore,
    matches: InMemoryAbandonedMatches,
    cooldowns: InMemoryCooldownRepository,
    cooldown_audit: InMemoryCooldownAuditRepository,
    timeline: InMemoryReconciliationTimelineRepository,
    clock: MovableClock,
    metrics: RecordingMetrics,
) -> QueueRetentionService:
    return QueueRetentionService(
        tickets=tickets,
        matches=matches,
        cooldowns=cooldowns,
        cooldown_audit=cooldown_audit,
        timeline=timeline,
        unit_of_work=NullUnitOfWork(),
        clock=clock,
        metrics=metrics,
        policy=queue_retention_policy(
            ticket_retention_hours=72,
            abandoned_match_retention_hours=168,
            cooldown_retention_hours=1,
            cooldown_audit_retention_hours=2160,
            timeline_retention_hours=336,
            batch_size=100,
            max_batches=5,
        ),
    )


def _resolved(store: InMemoryQueueRetentionStore, *, age: timedelta) -> QueueTicket:
    """A terminal ticket, resolved `age` ago."""
    resolved_at = NOW - age
    ticket = QueueTicket(
        player_id=generate_uuid7(),
        pool=POOL,
        time_control=BLITZ,
        rating_snapshot=1500,
        entered_at=resolved_at - TTL,
        expires_at=resolved_at + TTL,
        status=QueueStatus.EXPIRED,
        resolved_at=resolved_at,
    )
    store.tickets[ticket.id] = ticket
    return ticket


def _live(
    store: InMemoryQueueRetentionStore,
    *,
    age: timedelta,
    status: QueueStatus = QueueStatus.WAITING,
) -> QueueTicket:
    """A ticket that is still in the queue, entered `age` ago."""
    entered = NOW - age
    ticket = QueueTicket(
        player_id=generate_uuid7(),
        pool=POOL,
        time_control=BLITZ,
        rating_snapshot=1500,
        entered_at=entered,
        expires_at=entered + TTL,
        status=status,
        reserved_until=(NOW - age + timedelta(seconds=30))
        if status is QueueStatus.RESERVED
        else None,
    )
    store.tickets[ticket.id] = ticket
    return ticket


class TestTerminalTicketsAreCleaned:
    async def test_a_ticket_past_the_horizon_is_deleted(
        self, retention: QueueRetentionService, tickets: InMemoryQueueRetentionStore
    ) -> None:
        stale = _resolved(tickets, age=TICKET_HORIZON + timedelta(hours=1))

        result = await retention.prune_once()

        assert result.tickets_deleted == 1
        assert stale.id not in tickets.tickets

    async def test_a_ticket_inside_the_horizon_is_kept(
        self, retention: QueueRetentionService, tickets: InMemoryQueueRetentionStore
    ) -> None:
        """Three days, and the question it answers is "why was I matched
        with them" — asked by support the same day or the next."""
        recent = _resolved(tickets, age=timedelta(hours=1))

        result = await retention.prune_once()

        assert result.tickets_deleted == 0
        assert recent.id in tickets.tickets

    async def test_the_run_is_bounded(
        self,
        tickets: InMemoryQueueRetentionStore,
        matches: InMemoryAbandonedMatches,
        cooldowns: InMemoryCooldownRepository,
        clock: MovableClock,
        metrics: RecordingMetrics,
    ) -> None:
        """CLAUDE.md §10.5. The case it is for is the **first** run after
        this ships: a year of history would otherwise be one job holding
        locks until it finished."""
        service = QueueRetentionService(
            tickets=tickets,
            matches=matches,
            cooldowns=cooldowns,
            cooldown_audit=InMemoryCooldownAuditRepository(),
            timeline=InMemoryReconciliationTimelineRepository(),
            unit_of_work=NullUnitOfWork(),
            clock=clock,
            metrics=metrics,
            policy=queue_retention_policy(
                ticket_retention_hours=72,
                abandoned_match_retention_hours=168,
                cooldown_retention_hours=1,
                cooldown_audit_retention_hours=2160,
                timeline_retention_hours=336,
                batch_size=2,
                max_batches=2,
            ),
        )
        for _ in range(10):
            _resolved(tickets, age=TICKET_HORIZON + timedelta(hours=1))

        result = await service.prune_once()

        assert result.tickets_deleted == 4

    async def test_repeated_runs_drain_the_backlog(
        self, retention: QueueRetentionService, tickets: InMemoryQueueRetentionStore
    ) -> None:
        for _ in range(3):
            _resolved(tickets, age=TICKET_HORIZON + timedelta(hours=1))

        await retention.prune_once()

        assert tickets.tickets == {}


class TestLiveTicketsAreNeverDeleted:
    """The safety property §8 is really about, and the one that must hold
    however the horizon is configured."""

    @pytest.mark.parametrize("status", [QueueStatus.WAITING, QueueStatus.RESERVED])
    async def test_a_live_ticket_survives_any_age(
        self,
        retention: QueueRetentionService,
        tickets: InMemoryQueueRetentionStore,
        status: QueueStatus,
    ) -> None:
        """Excluded by predicate rather than by the horizon. A `reserved`
        ticket that old is precisely the row reconciliation is about to
        recover — deleting it would turn a recoverable pairing into a player
        who is silently no longer in any queue."""
        ancient = _live(tickets, age=TICKET_HORIZON * 10, status=status)

        result = await retention.prune_once()

        assert result.tickets_deleted == 0
        assert ancient.id in tickets.tickets

    async def test_a_live_ticket_past_the_horizon_is_reported_as_an_alarm(
        self, retention: QueueRetentionService, tickets: InMemoryQueueRetentionStore
    ) -> None:
        """A `waiting` ticket older than the whole horizon means the expiry
        sweep has stopped, and a `reserved` one means reconciliation has.
        Both are silent failures; this is what makes them loud."""
        _live(tickets, age=TICKET_HORIZON + timedelta(hours=1))

        result = await retention.prune_once()

        assert result.live_tickets_past_horizon == 1

    async def test_a_healthy_platform_reports_no_alarm(
        self, retention: QueueRetentionService, tickets: InMemoryQueueRetentionStore
    ) -> None:
        _live(tickets, age=timedelta(minutes=2))

        result = await retention.prune_once()

        assert result.live_tickets_past_horizon == 0


class TestAbandonedMatchesAreCleaned:
    async def test_a_cancelled_match_past_the_horizon_is_deleted(
        self, retention: QueueRetentionService, matches: InMemoryAbandonedMatches
    ) -> None:
        matches.abandon(settled_at=NOW - MATCH_HORIZON - timedelta(hours=1))

        result = await retention.prune_once()

        assert result.matches_deleted == 1

    async def test_a_recently_cancelled_match_is_kept(
        self, retention: QueueRetentionService, matches: InMemoryAbandonedMatches
    ) -> None:
        """Seven days, longer than the ticket horizon: "why did my opponent
        decline" is where a support conversation starts a week later."""
        matches.abandon(settled_at=NOW - timedelta(days=2))

        result = await retention.prune_once()

        assert result.matches_deleted == 0

    async def test_a_played_match_is_never_deleted(
        self, retention: QueueRetentionService, matches: InMemoryAbandonedMatches
    ) -> None:
        """The permanent competitive record A-4 is about. Excluded by
        predicate, so no configuration can reach it."""
        matches.activate(created_at=NOW - MATCH_HORIZON * 10)

        result = await retention.prune_once()

        assert result.matches_deleted == 0
        assert matches.active == 1

    async def test_a_pending_match_past_the_horizon_is_kept_and_reported(
        self, retention: QueueRetentionService, matches: InMemoryAbandonedMatches
    ) -> None:
        """A pairing still awaiting an answer is not abandoned however old
        it looks — and one that old is a reconciliation failure the sweep
        must surface rather than delete the evidence of."""
        matches.leave_pending(created_at=NOW - MATCH_HORIZON - timedelta(hours=1))

        result = await retention.prune_once()

        assert result.matches_deleted == 0
        assert result.unresolved_matches_past_horizon == 1


class TestLapsedCooldownsAreCleaned:
    async def test_an_expired_cooldown_is_deleted(
        self,
        retention: QueueRetentionService,
        cooldowns: InMemoryCooldownRepository,
        clock: MovableClock,
    ) -> None:
        lapsed = QueueCooldown.after_decline(
            generate_uuid7(), at=NOW - timedelta(hours=3), seconds=60
        )
        await cooldowns.apply(lapsed)

        result = await retention.prune_once()

        assert result.cooldowns_deleted == 1
        assert await cooldowns.active_for(lapsed.player_id, now=clock.now()) is None

    async def test_an_active_cooldown_survives(
        self, retention: QueueRetentionService, cooldowns: InMemoryCooldownRepository
    ) -> None:
        """A bar that is still in force is the one row on this relation
        that must not be removed — deleting it would let a decliner queue
        immediately."""
        live = QueueCooldown.after_decline(generate_uuid7(), at=NOW, seconds=60)
        await cooldowns.apply(live)

        result = await retention.prune_once()

        assert result.cooldowns_deleted == 0
        assert live.player_id in cooldowns.cooldowns


class TestTheRunItself:
    async def test_an_empty_platform_is_idle(self, retention: QueueRetentionService) -> None:
        assert (await retention.prune_once()).is_idle

    async def test_it_never_raises(
        self, retention: QueueRetentionService, tickets: InMemoryQueueRetentionStore
    ) -> None:
        """A maintenance job that propagated would stop the schedule that
        called it, and a retention job that has silently stopped is
        invisible until the table it was bounding is the incident."""
        tickets.fails = True

        result = await retention.prune_once()

        assert result.is_idle

    async def test_running_twice_deletes_nothing_the_second_time(
        self, retention: QueueRetentionService, tickets: InMemoryQueueRetentionStore
    ) -> None:
        """Duplicate task delivery is a certainty under AD-17's
        at-least-once contract."""
        _resolved(tickets, age=TICKET_HORIZON + timedelta(hours=1))

        first = await retention.prune_once()
        second = await retention.prune_once()

        assert first.tickets_deleted == 1
        assert second.tickets_deleted == 0

    async def test_deletions_are_counted_by_relation(
        self,
        retention: QueueRetentionService,
        tickets: InMemoryQueueRetentionStore,
        matches: InMemoryAbandonedMatches,
        metrics: RecordingMetrics,
    ) -> None:
        _resolved(tickets, age=TICKET_HORIZON + timedelta(hours=1))
        matches.abandon(settled_at=NOW - MATCH_HORIZON - timedelta(hours=1))

        await retention.prune_once()

        assert metrics.counts(RETENTION_DELETIONS) == {
            RetentionRelation.QUEUE_TICKET.value: 1.0,
            RetentionRelation.ABANDONED_MATCH.value: 1.0,
            RetentionRelation.QUEUE_COOLDOWN.value: 0.0,
            RetentionRelation.COOLDOWN_AUDIT.value: 0.0,
            RetentionRelation.PAIRING_TIMELINE.value: 0.0,
        }

    async def test_the_labels_are_bounded(
        self,
        retention: QueueRetentionService,
        tickets: InMemoryQueueRetentionStore,
        metrics: RecordingMetrics,
    ) -> None:
        _resolved(tickets, age=TICKET_HORIZON + timedelta(hours=1))

        await retention.prune_once()

        assert metrics.label_values() <= {member.value for member in RetentionRelation}


def _recorded(store: InMemoryCooldownAuditRepository, *, age: timedelta) -> CooldownRecord:
    """An audit row applied `age` ago."""
    applied_at = NOW - age
    record = CooldownRecord(
        player_id=generate_uuid7(),
        reason=CooldownReason.DECLINED_MATCH,
        source_match_id=generate_uuid7(),
        applied_at=applied_at,
        expires_at=applied_at + timedelta(seconds=60),
        extended_existing=False,
    )
    store.records.append(record)
    return record


def _projected(
    store: InMemoryReconciliationTimelineRepository, *, age: timedelta
) -> ReconciliationEntry:
    """A timeline entry for something that happened `age` ago."""
    occurred_at = NOW - age
    entry = ReconciliationEntry(
        event_id=generate_uuid7(),
        ticket_id=generate_uuid7(),
        player_id=generate_uuid7(),
        action=ReconciliationAction.REQUEUED,
        match_id=None,
        pairing_id=None,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
    )
    store.entries.append(entry)
    return entry


class TestTheAuditTrailOutlivesWhatItExplains:
    """A64-015.6 §9. The two audit relations are bounded like everything
    else, on **longer** horizons than the operational rows they describe —
    which is the whole reason they are separate relations."""

    async def test_an_audit_row_past_its_horizon_is_deleted(
        self,
        retention: QueueRetentionService,
        cooldown_audit: InMemoryCooldownAuditRepository,
    ) -> None:
        _recorded(cooldown_audit, age=AUDIT_HORIZON + timedelta(hours=1))

        result = await retention.prune_once()

        assert result.cooldown_audits_deleted == 1
        assert cooldown_audit.records == []

    async def test_an_audit_row_inside_its_horizon_is_kept(
        self,
        retention: QueueRetentionService,
        cooldown_audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """Ninety days, against the cooldown's one hour: the dispute arrives
        long after the bar lifted, and an audit trail pruned with the thing
        it explains answers nothing."""
        _recorded(cooldown_audit, age=timedelta(days=30))

        result = await retention.prune_once()

        assert result.cooldown_audits_deleted == 0
        assert len(cooldown_audit.records) == 1

    async def test_the_audit_outlives_the_bar_it_describes(
        self,
        retention: QueueRetentionService,
        cooldowns: InMemoryCooldownRepository,
        cooldown_audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """The asymmetry, asserted as one run rather than two facts: the
        enforcement row goes and the record of it stays."""
        lapsed = QueueCooldown(
            player_id=generate_uuid7(),
            reason=CooldownReason.DECLINED_MATCH,
            created_at=NOW - timedelta(days=2),
            expires_at=NOW - timedelta(days=2) + timedelta(seconds=60),
        )
        cooldowns.cooldowns[lapsed.player_id] = lapsed
        _recorded(cooldown_audit, age=timedelta(days=2))

        result = await retention.prune_once()

        assert result.cooldowns_deleted == 1
        assert len(cooldown_audit.records) == 1

    async def test_a_timeline_entry_past_its_horizon_is_deleted(
        self,
        retention: QueueRetentionService,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        _projected(timeline, age=TIMELINE_HORIZON + timedelta(hours=1))

        result = await retention.prune_once()

        assert result.timeline_entries_deleted == 1
        assert timeline.entries == []

    async def test_a_timeline_entry_inside_its_horizon_is_kept(
        self,
        retention: QueueRetentionService,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        _projected(timeline, age=timedelta(days=7))

        result = await retention.prune_once()

        assert result.timeline_entries_deleted == 0
        assert len(timeline.entries) == 1

    async def test_the_timeline_outlives_the_ticket_it_is_about(
        self,
        retention: QueueRetentionService,
        tickets: InMemoryQueueRetentionStore,
        timeline: InMemoryReconciliationTimelineRepository,
    ) -> None:
        """The question is asked about a ticket that is gone — which is why
        the entry carries the ticket id rather than a foreign key to it."""
        _resolved(tickets, age=TICKET_HORIZON + timedelta(hours=1))
        _projected(timeline, age=TICKET_HORIZON + timedelta(hours=1))

        result = await retention.prune_once()

        assert result.tickets_deleted == 1
        assert len(timeline.entries) == 1

    async def test_both_relations_are_counted_by_the_metric(
        self,
        retention: QueueRetentionService,
        cooldown_audit: InMemoryCooldownAuditRepository,
        timeline: InMemoryReconciliationTimelineRepository,
        metrics: RecordingMetrics,
    ) -> None:
        """§9's observability half. A relation the run deletes from but does
        not count is one whose growth is invisible until it is the
        incident."""
        _recorded(cooldown_audit, age=AUDIT_HORIZON + timedelta(hours=1))
        _projected(timeline, age=TIMELINE_HORIZON + timedelta(hours=1))

        await retention.prune_once()

        counted = metrics.counts(RETENTION_DELETIONS)
        assert counted[RetentionRelation.COOLDOWN_AUDIT.value] == 1.0
        assert counted[RetentionRelation.PAIRING_TIMELINE.value] == 1.0

    async def test_every_relation_reports_even_when_it_deleted_nothing(
        self, retention: QueueRetentionService, metrics: RecordingMetrics
    ) -> None:
        """A series that reads zero says "the job ran and found nothing"; an
        absent series says "the job did not run". Telling those apart is the
        operational value of a retention metric."""
        await retention.prune_once()

        assert set(metrics.counts(RETENTION_DELETIONS)) == {
            member.value for member in RetentionRelation
        }

    async def test_the_audit_horizons_are_bounded(self) -> None:
        """Not unbounded retention wearing an audit label. §9 asks for a
        stated horizon on every relation, and a policy that let one grow
        forever would be the growth these tests exist to prevent."""
        policy = queue_retention_policy(
            ticket_retention_hours=72,
            abandoned_match_retention_hours=168,
            cooldown_retention_hours=1,
            cooldown_audit_retention_hours=2160,
            timeline_retention_hours=336,
            batch_size=100,
            max_batches=5,
        )

        assert policy.cooldown_audit_retention == AUDIT_HORIZON
        assert policy.timeline_retention == TIMELINE_HORIZON

    async def test_an_audit_horizon_shorter_than_the_bar_is_refused(self) -> None:
        """The one ordering that would defeat the relation's purpose, caught
        at construction rather than discovered when the data is gone."""
        with pytest.raises(ValueError, match="cooldown_audit_retention must exceed"):
            queue_retention_policy(
                ticket_retention_hours=72,
                abandoned_match_retention_hours=168,
                cooldown_retention_hours=48,
                cooldown_audit_retention_hours=1,
                timeline_retention_hours=336,
                batch_size=100,
                max_batches=5,
            )
