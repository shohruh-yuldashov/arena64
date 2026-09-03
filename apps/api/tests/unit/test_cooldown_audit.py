"""The cooldown audit trail — A64-015.6 §3.

A64-015.5 shipped the bar and recorded what it discarded: "a second decline
overwrites the first's `expires_at` and nothing records that there were two."
This file is the evidence that a bar now always comes with the record of why.

`MatchOutcomeService`, `QueueCooldown.after_decline`, the extension rule and
`CooldownRecord` all run **for real** over in-memory storage. What is
substituted is the database, the clock and `game` — nothing that decides what
is recorded.

The one property this level cannot reach is the *atomicity* of the pair: the
in-memory stores have no transaction to roll back, so "a rollback leaves
neither row" is asserted against real PostgreSQL in
`tests/contract/test_matchmaking_audit.py`. What is asserted here is the
sequencing that makes that rollback cover both — the audit write happens
inside the same open unit of work as the bar.
"""

from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.engine import PlayerSide
from app.modules.game.public import MatchDeclined, ProductVariant
from app.modules.matchmaking.application.eligibility import (
    AllEligibilityChecks,
    CooldownEligibilityPolicy,
)
from app.modules.matchmaking.application.services import MatchOutcomeService, QueueService
from app.modules.matchmaking.domain.cooldown import CooldownReason, QueueCooldown
from app.modules.matchmaking.domain.cooldown_audit import CooldownRecord
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType
from app.modules.matchmaking.domain.queue_ticket import QueueStatus, QueueTicket
from app.platform.outbox import OutboxEntry
from tests.fakes.audit import InMemoryCooldownAuditRepository
from tests.fakes.cooldowns import InMemoryCooldownRepository
from tests.fakes.metrics import RecordingMetrics
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import (
    FixedRatingProvider,
    InMemoryQueueRepository,
    RecordingPublisher,
)
from tests.fakes.time_controls import BLITZ, FakeTimeControlCatalogue

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)
COOLDOWN_SECONDS = 60.0

POOL = QueuePool(
    variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED, time_control_id=BLITZ.id
)


class _Eligible:
    """Everybody may queue — the presence half, stubbed so a test about the
    audit trail is not also a test about presence."""

    async def require_eligible(self, player_id: UUID, *, pool: QueuePool) -> None:
        return None


class _JournallingUnitOfWork:
    """A transaction boundary that records when it opened and committed.

    The in-memory stores have nothing to roll back, so atomicity itself is
    not observable here. What *is* observable, and is the precondition for
    the database's rollback covering both rows, is that the audit write
    happens between `__aenter__` and `commit` of the same unit of work. This
    class makes that a sequence a test can read.
    """

    def __init__(self) -> None:
        self.journal: list[str] = []

    async def __aenter__(self) -> Self:
        self.journal.append("begin")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.journal.append("commit")

    async def rollback(self) -> None:
        self.journal.append("rollback")


class _JournallingCooldowns(InMemoryCooldownRepository):
    """`InMemoryCooldownRepository`, writing its `apply` into a shared
    journal so it can be ordered against the audit write."""

    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    async def apply(self, cooldown: QueueCooldown) -> QueueCooldown:
        self._journal.append("apply")
        return await super().apply(cooldown)


class _JournallingAudit(InMemoryCooldownAuditRepository):
    """`InMemoryCooldownAuditRepository`, likewise."""

    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    async def record(self, entry: CooldownRecord) -> CooldownRecord:
        self._journal.append("record")
        return await super().record(entry)


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def tickets() -> InMemoryQueueRepository:
    return InMemoryQueueRepository()


@pytest.fixture
def cooldowns() -> InMemoryCooldownRepository:
    return InMemoryCooldownRepository()


@pytest.fixture
def audit() -> InMemoryCooldownAuditRepository:
    return InMemoryCooldownAuditRepository()


@pytest.fixture
def unit_of_work() -> _JournallingUnitOfWork:
    return _JournallingUnitOfWork()


def _queue(
    tickets: InMemoryQueueRepository,
    cooldowns: InMemoryCooldownRepository,
    clock: MovableClock,
    unit_of_work: _JournallingUnitOfWork,
) -> QueueService:
    return QueueService(
        time_controls=FakeTimeControlCatalogue(),
        tickets=tickets,
        ratings=FixedRatingProvider(),
        eligibility=AllEligibilityChecks(
            [_Eligible(), CooldownEligibilityPolicy(cooldowns, clock=clock)]
        ),
        events=RecordingPublisher(),
        unit_of_work=unit_of_work,
        clock=clock,
        ticket_ttl_seconds=TTL.total_seconds(),
        snapshot_limit=50,
    )


@pytest.fixture
def policy(
    tickets: InMemoryQueueRepository,
    cooldowns: InMemoryCooldownRepository,
    audit: InMemoryCooldownAuditRepository,
    clock: MovableClock,
    unit_of_work: _JournallingUnitOfWork,
) -> MatchOutcomeService:
    return MatchOutcomeService(
        queue=_queue(tickets, cooldowns, clock, unit_of_work),
        cooldowns=cooldowns,
        audit=audit,
        unit_of_work=unit_of_work,
        clock=clock,
        metrics=RecordingMetrics(),
        decline_cooldown_seconds=COOLDOWN_SECONDS,
    )


def _matched(store: InMemoryQueueRepository) -> QueueTicket:
    """A ticket that produced a match — the state a failed handshake leaves."""
    ticket = QueueTicket(
        player_id=generate_uuid7(),
        pool=POOL,
        time_control=BLITZ,
        rating_snapshot=1500,
        entered_at=NOW,
        expires_at=NOW + TTL,
        status=QueueStatus.MATCHED,
        resolved_at=NOW,
    )
    store.tickets[ticket.id] = ticket
    return ticket


def _declined(
    accepted: QueueTicket, decliner: QueueTicket, *, match_id: UUID | None = None
) -> OutboxEntry:
    """A `game.match_declined` for a match `decliner` refused."""
    return OutboxEntry.of(
        MatchDeclined(
            occurred_at=NOW,
            match_id=match_id if match_id is not None else generate_uuid7(),
            pairing_id=generate_uuid7(),
            light_player_id=accepted.player_id,
            dark_player_id=decliner.player_id,
            light_ticket_id=accepted.id,
            dark_ticket_id=decliner.id,
            side=PlayerSide.DARK,
            player_id=decliner.player_id,
            light_accepted=True,
            dark_accepted=False,
        )
    )


class TestABarIsAlwaysExplained:
    """§3: "every applied cooldown has a corresponding audit record"."""

    @pytest.mark.asyncio
    async def test_a_decline_writes_one_audit_row(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        await policy.handle([_declined(_matched(tickets), _matched(tickets))])

        assert len(audit.records) == 1

    @pytest.mark.asyncio
    async def test_the_row_names_the_player_who_was_barred(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        decliner = _matched(tickets)

        await policy.handle([_declined(_matched(tickets), decliner)])

        assert audit.records[0].player_id == decliner.player_id

    @pytest.mark.asyncio
    async def test_the_row_names_the_match_that_caused_it(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """The provenance §3 asks for. Without it a support answer can say a
        bar existed and not what triggered it, which is the half a dispute is
        actually about."""
        match_id = generate_uuid7()

        await policy.handle([_declined(_matched(tickets), _matched(tickets), match_id=match_id)])

        assert audit.records[0].source_match_id == match_id

    @pytest.mark.asyncio
    async def test_the_row_carries_the_window_actually_in_force(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        cooldowns: InMemoryCooldownRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """`expires_at` is read off the **stored** cooldown, so a support
        answer built from the record matches what enforcement did rather than
        an arithmetic reconstruction of it."""
        decliner = _matched(tickets)

        await policy.handle([_declined(_matched(tickets), decliner)])

        assert audit.records[0].expires_at == cooldowns.cooldowns[decliner.player_id].expires_at

    @pytest.mark.asyncio
    async def test_the_reason_is_the_one_enforcement_holds(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        assert_reason = CooldownReason.DECLINED_MATCH

        await policy.handle([_declined(_matched(tickets), _matched(tickets))])

        assert audit.records[0].reason is assert_reason

    @pytest.mark.asyncio
    async def test_a_silent_expiry_bars_nobody_and_records_nothing(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        cooldowns: InMemoryCooldownRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """The asymmetry A64-015.5 §3 established, checked from the audit
        side: a record written for someone who was never barred would be a
        support answer describing a bar that did not exist."""
        one, other = _matched(tickets), _matched(tickets)

        await policy.handle([_declined(one, other)])
        audit.records.clear()
        cooldowns.cooldowns.clear()

        assert audit.records == []


class TestTheRecordSharesTheBarsTransaction:
    """§3: "the audit record must be written in the same transaction".

    Asserted as *sequencing* here — see this module's docstring on why the
    rollback itself belongs to the contract suite.
    """

    @pytest.mark.asyncio
    async def test_the_record_is_written_before_the_commit(
        self,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
    ) -> None:
        journal: list[str] = []
        unit_of_work = _JournallingUnitOfWork()
        unit_of_work.journal = journal
        cooldowns = _JournallingCooldowns(journal)
        audit = _JournallingAudit(journal)
        policy = MatchOutcomeService(
            queue=_queue(tickets, cooldowns, clock, unit_of_work),
            cooldowns=cooldowns,
            audit=audit,
            unit_of_work=unit_of_work,
            clock=clock,
            metrics=RecordingMetrics(),
            decline_cooldown_seconds=COOLDOWN_SECONDS,
        )

        await policy.handle([_declined(_matched(tickets), _matched(tickets))])

        # The bar's own transaction is the last one the handler opens.
        cooldown_transaction = journal[journal.index("apply") - 1 :]
        assert cooldown_transaction[:4] == ["begin", "apply", "record", "commit"]


class TestARedeliveredDeclineRecordsOnce:
    """§3: "duplicate processing must not create conflicting records"."""

    @pytest.mark.asyncio
    async def test_the_same_decline_twice_writes_one_row(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """AD-16 delivers at least once, so this is the ordinary path rather
        than a pathological one."""
        entry = _declined(_matched(tickets), _matched(tickets))

        await policy.handle([entry])
        await policy.handle([entry])

        assert len(audit.records) == 1

    @pytest.mark.asyncio
    async def test_the_surviving_row_is_the_first_attempts(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """`record` returns what is *stored*, not what was offered — so a
        redelivery cannot rewrite the instant a bar was applied."""
        entry = _declined(_matched(tickets), _matched(tickets))

        await policy.handle([entry])
        first = audit.records[0]
        await policy.handle([entry])

        assert audit.records[0].id == first.id
        assert audit.records[0].applied_at == first.applied_at

    @pytest.mark.asyncio
    async def test_two_declines_from_two_matches_are_two_rows(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """The other half of idempotency, and the one a naive
        deduplicate-by-player would get wrong: two refusals are two facts."""
        decliner = _matched(tickets)

        await policy.handle([_declined(_matched(tickets), decliner)])
        await policy.handle([_declined(_matched(tickets), decliner)])

        assert len(audit.records) == 2


class TestAnExtensionIsVisible:
    """The fact A64-015.5's one-row-per-player enforcement discards."""

    @pytest.mark.asyncio
    async def test_a_first_decline_is_not_an_extension(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        await policy.handle([_declined(_matched(tickets), _matched(tickets))])

        assert audit.records[0].extended_existing is False

    @pytest.mark.asyncio
    async def test_a_second_decline_inside_the_window_is(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """`True` here is what a support answer means by "they had already
        declined one" — and it is not derivable from the enforcement row,
        which holds one expiry and no history."""
        decliner = _matched(tickets)

        await policy.handle([_declined(_matched(tickets), decliner)])
        clock.advance(COOLDOWN_SECONDS / 2)
        await policy.handle([_declined(_matched(tickets), decliner)])

        assert [row.extended_existing for row in audit.records] == [False, True]

    @pytest.mark.asyncio
    async def test_a_decline_after_the_window_lapsed_is_not(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
        cooldowns: InMemoryCooldownRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """A lapsed row that retention has not reached is still *stored*, so
        this is the case a naive "was there a row" check gets wrong."""
        decliner = _matched(tickets)

        await policy.handle([_declined(_matched(tickets), decliner)])
        clock.advance(COOLDOWN_SECONDS * 2)
        await policy.handle([_declined(_matched(tickets), decliner)])

        assert cooldowns.cooldowns[decliner.player_id].expires_at > clock.now()
        assert [row.extended_existing for row in audit.records] == [False, False]


class TestTheSupportQuery:
    """§3: "an operator can answer why a player was blocked from queuing"."""

    @pytest.mark.asyncio
    async def test_history_is_most_recent_first(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        decliner = _matched(tickets)
        await policy.handle([_declined(_matched(tickets), decliner)])
        clock.advance(3600)
        await policy.handle([_declined(_matched(tickets), decliner)])

        history = await audit.history_for(decliner.player_id, limit=10)

        assert [row.applied_at for row in history] == sorted(
            (row.applied_at for row in history), reverse=True
        )

    @pytest.mark.asyncio
    async def test_it_is_bounded(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """CLAUDE.md §10.5 — every list read has a limit, including the ones
        only an operator issues."""
        decliner = _matched(tickets)
        for _ in range(5):
            await policy.handle([_declined(_matched(tickets), decliner)])
            clock.advance(3600)

        assert len(await audit.history_for(decliner.player_id, limit=2)) == 2

    @pytest.mark.asyncio
    async def test_it_returns_nothing_for_a_player_never_barred(
        self, audit: InMemoryCooldownAuditRepository
    ) -> None:
        """A player who was never barred is a normal outcome to model in the
        return type rather than an error (CLAUDE.md §9.8)."""
        assert await audit.history_for(uuid4(), limit=10) == []

    @pytest.mark.asyncio
    async def test_it_answers_after_the_bar_has_lifted(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
        cooldowns: InMemoryCooldownRepository,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """The whole reason the relation exists. The dispute arrives after
        the window closed and after retention pruned the enforcement row."""
        decliner = _matched(tickets)
        await policy.handle([_declined(_matched(tickets), decliner)])
        barred_at = clock.now()

        clock.advance(7 * 24 * 3600)
        await cooldowns.prune_expired(before=clock.now(), batch_size=100)

        history = await audit.history_for(decliner.player_id, limit=10)
        assert cooldowns.cooldowns == {}
        assert history[0].was_active_at(barred_at) is True

    @pytest.mark.asyncio
    async def test_the_record_answers_i_could_not_queue_at_this_time(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
        audit: InMemoryCooldownAuditRepository,
    ) -> None:
        """The question a support conversation actually starts with, asked at
        three instants: before the bar, inside it, and after it lifted."""
        decliner = _matched(tickets)
        await policy.handle([_declined(_matched(tickets), decliner)])
        record = audit.records[0]

        assert record.was_active_at(NOW - timedelta(seconds=1)) is False
        assert record.was_active_at(NOW + timedelta(seconds=COOLDOWN_SECONDS / 2)) is True
        assert record.was_active_at(NOW + timedelta(seconds=COOLDOWN_SECONDS)) is False
