"""What the pairing scan reports, and what it costs to report it —
A64-015.6 §6 and §7.

§7 asks for scan observability and forbids the obvious way to get it: "do not
emit one structured log record for every candidate comparison". Those two
pull against each other, and this file is the evidence that both hold — the
scan's outcome, its candidate volume and its exclusions are all countable,
and the cost of counting them is O(1) per scan rather than O(n²).

It also holds A64-015.5 §9's cardinality rule, which A64-015.6 §6 makes
load-bearing: `AggregatingMetrics` keeps one entry per live series in memory,
so "every label value comes from a closed enumeration" stopped being hygiene
and became the reason the accumulator cannot grow without bound.

`PairingService`, `PairingEngine` and `QueueRetentionService` all run for
real. What is substituted is storage, the clock, `game` and `friends`.
"""

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.application.metrics import (
    PAIRING_CANDIDATES,
    PAIRING_EXCLUSIONS,
    PAIRING_SCANS,
    RETENTION_DELETIONS,
    AcceptanceFailureAction,
    DeliveryOutcome,
    ExclusionReason,
    RetentionRelation,
    ScanOutcome,
)
from app.modules.matchmaking.application.services import PairingService
from app.modules.matchmaking.domain.events import ReconciliationAction
from app.modules.matchmaking.domain.pairing import PairingEngine, RatingWindowPolicy
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType
from app.modules.matchmaking.domain.queue_ticket import QueueTicket
from app.platform.metrics import AggregatingMetrics
from tests.fakes.metrics import RecordingMetrics
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.pairing import (
    RecordingMatchCreation,
    RefusingMatchCreation,
    StubExclusions,
    StubRatings,
    StubRecentOpponents,
)
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import InMemoryQueueRepository, RecordingPublisher
from tests.fakes.time_controls import BLITZ

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)
RESERVATION_TTL = 30.0

POOL = QueuePool(
    variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED, time_control_id=BLITZ.id
)

#: Wide enough that nothing is excluded by rating, so a test about exclusions
#: is not accidentally a test about the window.
GENEROUS = RatingWindowPolicy(
    initial_points=1000, widen_every_seconds=30, widen_by_points=0, maximum_points=1000
)

#: A canonical UUID string, for the assertion that no label looks like one.
_UUID_SHAPED = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
def tickets() -> InMemoryQueueRepository:
    return InMemoryQueueRepository()


@pytest.fixture
def exclusions() -> StubExclusions:
    return StubExclusions()


@pytest.fixture
def opponents() -> StubRecentOpponents:
    return StubRecentOpponents()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


def _service(
    tickets: InMemoryQueueRepository,
    exclusions: StubExclusions,
    opponents: StubRecentOpponents,
    metrics: RecordingMetrics | AggregatingMetrics,
    *,
    matches: object | None = None,
) -> PairingService:
    return PairingService(
        tickets=tickets,
        engine=PairingEngine(GENEROUS),
        exclusions=exclusions,
        opponents=opponents,
        ratings=StubRatings(),
        matches=matches if matches is not None else RecordingMatchCreation(),  # type: ignore[arg-type]
        events=RecordingPublisher(),
        unit_of_work=NullUnitOfWork(),
        clock=MovableClock(NOW),
        metrics=metrics,
        candidate_batch_size=50,
        reservation_ttl_seconds=RESERVATION_TTL,
    )


def _queued(store: InMemoryQueueRepository, *, player_id: UUID | None = None) -> QueueTicket:
    ticket = QueueTicket(
        player_id=player_id or generate_uuid7(),
        pool=POOL,
        time_control=BLITZ,
        rating_snapshot=1500,
        entered_at=NOW,
        expires_at=NOW + TTL,
    )
    store.tickets[ticket.id] = ticket
    return ticket


class TestEveryScanReportsAnOutcome:
    """§7: "the number of pairing attempts, successful pairings, and
    exclusions"."""

    @pytest.mark.asyncio
    async def test_a_scan_that_pairs_is_counted_as_paired(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        _queued(tickets), _queued(tickets)

        await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)

        assert metrics.counts(PAIRING_SCANS) == {ScanOutcome.PAIRED: 1.0}

    @pytest.mark.asyncio
    async def test_an_empty_pool_is_counted_as_idle(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        """The most common outcome by far on a quiet platform, and the one a
        scan-attempt counter exists to make visible — an idle scan that
        counted nothing would be indistinguishable from a scan that never
        ran."""
        await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)

        assert metrics.counts(PAIRING_SCANS) == {ScanOutcome.IDLE: 1.0}

    @pytest.mark.asyncio
    async def test_a_pool_the_rules_forbid_pairing_is_counted_separately(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        """ "Two people are waiting and neither may play the other" is a
        different operational fact from "nobody is waiting", and the
        difference is what an operator investigating a stuck queue needs."""
        one, other = _queued(tickets), _queued(tickets)
        exclusions.block(one.player_id, other.player_id)

        await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)

        assert metrics.counts(PAIRING_SCANS) == {ScanOutcome.NO_PAIR: 1.0}

    @pytest.mark.asyncio
    async def test_a_refused_creation_is_counted_separately(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        """`game` declining to create the match is a failure of the pairing
        pipeline rather than of the pool, so it must not read as `no_pair`."""
        _queued(tickets), _queued(tickets)
        service = _service(tickets, exclusions, opponents, metrics, matches=RefusingMatchCreation())

        await service.pair_once(pool=POOL)

        assert metrics.counts(PAIRING_SCANS) == {ScanOutcome.CREATION_REFUSED: 1.0}

    @pytest.mark.asyncio
    async def test_exactly_one_outcome_is_recorded_per_scan(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        """A scan that recorded two outcomes would make `rate(scans)` mean
        nothing, and it is the failure an exit point added later would
        introduce silently."""
        _queued(tickets), _queued(tickets)

        await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)

        assert sum(metrics.counts(PAIRING_SCANS).values()) == 1.0

    @pytest.mark.asyncio
    async def test_every_scan_reports_the_candidates_it_saw(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        """`rate(candidates) / rate(scans)` is the mean pool depth a scan
        sees, which is the question — and a counter answers it losslessly
        where an observation would be one record per scan."""
        for _ in range(4):
            _queued(tickets)

        await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)

        assert metrics.observations(PAIRING_CANDIDATES) == [4.0]


class TestExclusionsAreCountedByRule:
    @pytest.mark.asyncio
    async def test_a_blocked_pair_names_the_block(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        one, other = _queued(tickets), _queued(tickets)
        exclusions.block(one.player_id, other.player_id)

        await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)

        assert metrics.counts(PAIRING_EXCLUSIONS) == {
            ExclusionReason.BLOCKED: 1.0,
            ExclusionReason.RECENT_OPPONENT: 0.0,
        }

    @pytest.mark.asyncio
    async def test_a_recent_opponent_names_the_rematch_rule(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        """BL-2 and QT-3 refuse the same pairing for entirely different
        reasons, and an operator asking "why is this pool stuck" needs to
        know which."""
        one, other = _queued(tickets), _queued(tickets)
        opponents.played(one.player_id, other.player_id)

        await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)

        assert metrics.counts(PAIRING_EXCLUSIONS) == {
            ExclusionReason.BLOCKED: 0.0,
            ExclusionReason.RECENT_OPPONENT: 1.0,
        }

    @pytest.mark.asyncio
    async def test_a_scan_with_no_exclusions_reports_zero_rather_than_silence(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        """Both series exist with a value of zero. A series that is absent
        says "the scan did not reach the exclusion step"; one that reads zero
        says "it did, and nobody was excluded" — and only the second lets an
        operator conclude the rules are not what is holding the pool up."""
        _queued(tickets), _queued(tickets)

        await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)

        assert metrics.counts(PAIRING_EXCLUSIONS) == {
            ExclusionReason.BLOCKED: 0.0,
            ExclusionReason.RECENT_OPPONENT: 0.0,
        }


class TestTheHotPathStaysCheap:
    """§7: "do not emit one structured log record for every candidate
    comparison"."""

    @pytest.mark.asyncio
    async def test_a_scan_of_twenty_candidates_takes_a_constant_number_of_measurements(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        """The engine compares up to n² pairs. Twenty candidates is 190
        comparisons, and the measurement count must not track it — three
        (scans, candidates, and nothing excluded) rather than hundreds."""
        for _ in range(20):
            _queued(tickets)

        await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)

        assert len(metrics.recorded) <= 4

    @pytest.mark.asyncio
    async def test_doubling_the_pool_does_not_change_the_measurement_count(
        self,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
    ) -> None:
        """The property, stated as the comparison it is about: whatever the
        constant is, it must not be a function of pool depth."""
        counts = []
        for depth in (10, 20):
            tickets, metrics = InMemoryQueueRepository(), RecordingMetrics()
            for _ in range(depth):
                _queued(tickets)
            await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)
            counts.append(len(metrics.recorded))

        assert counts[0] == counts[1]

    @pytest.mark.asyncio
    async def test_a_days_worth_of_idle_scans_emits_one_record_per_series(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
    ) -> None:
        """The arithmetic §6 exists for, run end to end: the scan counts into
        a real `AggregatingMetrics`, and a flush interval's worth of ticks
        reaches the sink as two records rather than 1,680."""
        sink = RecordingMetrics()
        aggregating = AggregatingMetrics(sink=sink)
        service = _service(tickets, exclusions, opponents, aggregating)

        for _ in range(840):
            await service.pair_once(pool=POOL)

        assert sink.recorded == []
        assert aggregating.flush() == 2
        assert sink.counts(PAIRING_SCANS) == {ScanOutcome.IDLE: 840.0}


class TestLabelsAreBounded:
    """A64-015.5 §9, load-bearing since A64-015.6 §6: the accumulator holds
    one entry per live series, so an unbounded label domain is a leak."""

    def test_every_label_enum_is_closed(self) -> None:
        """A `StrEnum` fixes the series count at import time. This is the
        property that makes an in-memory accumulator safe here and would make
        it a defect in a system that labelled by identifier."""
        for enum in (
            AcceptanceFailureAction,
            DeliveryOutcome,
            ExclusionReason,
            ReconciliationAction,
            RetentionRelation,
            ScanOutcome,
        ):
            assert issubclass(enum, StrEnum)

    def test_the_whole_label_domain_is_small(self) -> None:
        """Every series this module can produce, counted. The number is a
        tripwire rather than a target — a label added from an identifier
        would move it by orders of magnitude."""
        total = sum(
            len(enum)
            for enum in (
                AcceptanceFailureAction,
                DeliveryOutcome,
                ExclusionReason,
                ReconciliationAction,
                RetentionRelation,
                ScanOutcome,
            )
        )

        assert total < 50

    @pytest.mark.asyncio
    async def test_no_scan_ever_labels_by_identifier(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        """Asserted against what was actually emitted rather than against the
        call sites, so a label built by interpolation is caught too."""
        one, other = _queued(tickets), _queued(tickets)
        exclusions.block(one.player_id, other.player_id)

        await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)

        for value in metrics.label_values():
            assert not _UUID_SHAPED.search(value)

    @pytest.mark.asyncio
    async def test_every_value_a_scan_emits_belongs_to_its_enum(
        self,
        tickets: InMemoryQueueRepository,
        exclusions: StubExclusions,
        opponents: StubRecentOpponents,
        metrics: RecordingMetrics,
    ) -> None:
        one, other = _queued(tickets), _queued(tickets)
        opponents.played(one.player_id, other.player_id)

        await _service(tickets, exclusions, opponents, metrics).pair_once(pool=POOL)

        allowed = {str(member) for member in ScanOutcome} | {
            str(member) for member in ExclusionReason
        }
        assert metrics.label_values() <= allowed


class TestEveryPrunedRelationIsCounted:
    """§9's observability half, asserted structurally.

    A relation the retention run deletes from but does not count is one whose
    growth is invisible until it is the incident.
    """

    def test_the_label_enum_covers_every_relation_the_result_reports(self) -> None:
        from app.modules.matchmaking.application.services import QueueRetentionResult

        deleted_fields = {
            name.removesuffix("_deleted")
            for name in QueueRetentionResult.__dataclass_fields__
            if name.endswith("_deleted")
        }

        assert deleted_fields == {
            "tickets",
            "matches",
            "cooldowns",
            "cooldown_audits",
            "timeline_entries",
        }
        assert len(RetentionRelation) == len(deleted_fields)

    def test_the_metric_name_is_namespaced_by_its_owner(self) -> None:
        """Like every `event_type` on this platform, so an operator filtering
        by producer filters on the prefix."""
        assert RETENTION_DELETIONS.startswith("matchmaking.")
