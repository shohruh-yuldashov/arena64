"""`OutboxPruner` and `RetentionPolicy` — A64-014.1.

The pruner runs **for real** over an in-memory store, so the horizon
arithmetic, the batch draining, the ordering of the two prunes and the
never-raises promise are all genuinely exercised. What is faked is the
`DELETE`, and the predicates that statement carries are asserted against
real PostgreSQL in `tests/contract/test_outbox_retention.py` — an in-memory
store cannot prove that a `WHERE` clause excludes what it claims to.

The most valuable assertion in this file is the one that says what is
*not* deleted. A retention job that removes an unpublished entry destroys an
event nobody has delivered, silently and permanently, and it is the one
failure here with no recovery path.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from app.platform.outbox.retention import (
    OutboxPruner,
    RetentionPolicy,
    prune_request,
    retention_policy,
)
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class InMemoryRetentionStore:
    """Rows as `(instant, is_published)` pairs, and one counter each.

    Deliberately not a copy of `InMemoryOutbox`: what the pruner needs from
    a store is "how old is this and has it been delivered", and modelling
    the whole entry would invite an assertion about a field the pruner never
    reads.
    """

    def __init__(
        self,
        *,
        entries: Sequence[tuple[datetime, bool]] = (),
        ledger: Sequence[datetime] = (),
    ) -> None:
        self.entries = list(entries)
        self.ledger = list(ledger)
        #: Every batch size the pruner asked for, in order. Asserted by the
        #: tests that care whether draining stopped when it should have.
        self.entry_batches: list[int] = []
        self.ledger_batches: list[int] = []

    async def prune_published(self, *, before: datetime, batch_size: int) -> int:
        self.entry_batches.append(batch_size)
        doomed = [entry for entry in self.entries if entry[0] < before and entry[1]][:batch_size]
        for entry in doomed:
            self.entries.remove(entry)
        return len(doomed)

    async def prune_processed_events(self, *, before: datetime, batch_size: int) -> int:
        self.ledger_batches.append(batch_size)
        doomed = [instant for instant in self.ledger if instant < before][:batch_size]
        for instant in doomed:
            self.ledger.remove(instant)
        return len(doomed)

    async def unpublished_before(self, instant: datetime) -> int:
        return sum(1 for entry in self.entries if entry[0] < instant and not entry[1])


def _pruner(
    store: InMemoryRetentionStore,
    *,
    policy: RetentionPolicy | None = None,
    clock: MovableClock | None = None,
) -> OutboxPruner:
    return OutboxPruner(
        store=store,
        unit_of_work=NullUnitOfWork(),
        clock=clock or MovableClock(NOW),
        policy=policy
        or retention_policy(
            published_retention_days=14,
            ledger_retention_days=30,
            batch_size=100,
            max_batches=10,
        ),
    )


class TestPolicy:
    def test_a_ledger_horizon_shorter_than_the_outbox_is_refused(self) -> None:
        """The ordering invariant, enforced where it can be. Dropping a
        ledger row while its entry can still be claimed lets that entry be
        redelivered *and* re-handled — the double effect the ledger exists
        to prevent."""
        with pytest.raises(ValueError, match="ledger_retention"):
            retention_policy(
                published_retention_days=30,
                ledger_retention_days=14,
                batch_size=100,
                max_batches=10,
            )

    def test_equal_horizons_are_accepted(self) -> None:
        """Equal is safe: within a run the outbox is pruned first, and a
        ledger row's `processed_at` is never earlier than its entry's
        `occurred_at`."""
        retention_policy(
            published_retention_days=14,
            ledger_retention_days=14,
            batch_size=100,
            max_batches=10,
        )

    def test_a_non_positive_horizon_is_refused(self) -> None:
        with pytest.raises(ValueError, match="published_retention"):
            RetentionPolicy(
                published_retention=timedelta(0),
                ledger_retention=timedelta(days=1),
                batch_size=100,
                max_batches=10,
            )

    def test_a_non_positive_batch_is_refused(self) -> None:
        """A zero batch is not a slower prune, it is a job that deletes
        nothing forever while reporting success."""
        with pytest.raises(ValueError, match="batch_size"):
            retention_policy(
                published_retention_days=14,
                ledger_retention_days=30,
                batch_size=0,
                max_batches=10,
            )


class TestPruning:
    async def test_a_published_entry_past_the_horizon_is_deleted(self) -> None:
        store = InMemoryRetentionStore(entries=[(NOW - timedelta(days=20), True)])

        result = await _pruner(store).prune_once()

        assert result.entries_deleted == 1
        assert store.entries == []

    async def test_a_published_entry_inside_the_horizon_is_kept(self) -> None:
        store = InMemoryRetentionStore(entries=[(NOW - timedelta(days=13), True)])

        result = await _pruner(store).prune_once()

        assert result.entries_deleted == 0
        assert len(store.entries) == 1

    async def test_an_unpublished_entry_is_never_deleted(self) -> None:
        """**The assertion that matters most in this file.** An exhausted
        entry is still owed to somebody, and `OutboxEntry` is explicit that
        it must stay visible in the backlog rather than be tidied away."""
        store = InMemoryRetentionStore(entries=[(NOW - timedelta(days=365), False)])

        result = await _pruner(store).prune_once()

        assert result.entries_deleted == 0
        assert len(store.entries) == 1

    async def test_a_retained_unpublished_entry_is_counted(self) -> None:
        """The number that says why the floor did not move — and, once
        partitions exist, why the oldest cannot be detached."""
        store = InMemoryRetentionStore(
            entries=[(NOW - timedelta(days=365), False), (NOW - timedelta(days=20), True)]
        )

        result = await _pruner(store).prune_once()

        assert result.retained_unpublished == 1

    async def test_a_ledger_row_past_its_own_horizon_is_deleted(self) -> None:
        store = InMemoryRetentionStore(ledger=[NOW - timedelta(days=40)])

        result = await _pruner(store).prune_once()

        assert result.ledger_deleted == 1

    async def test_a_ledger_row_uses_the_longer_horizon(self) -> None:
        """Thirty days against the outbox's fourteen. A row aged twenty is
        past the entry horizon and inside the ledger's, so it stays."""
        store = InMemoryRetentionStore(ledger=[NOW - timedelta(days=20)])

        result = await _pruner(store).prune_once()

        assert result.ledger_deleted == 0

    async def test_an_idle_run_reports_idle(self) -> None:
        result = await _pruner(InMemoryRetentionStore()).prune_once()

        assert result.is_idle


class TestBatching:
    async def test_draining_stops_when_a_batch_comes_back_short(self) -> None:
        """A short batch means the horizon is caught up — the steady state,
        and it costs exactly one empty statement per relation per run."""
        store = InMemoryRetentionStore(entries=[(NOW - timedelta(days=20), True)])

        await _pruner(
            store,
            policy=retention_policy(
                published_retention_days=14,
                ledger_retention_days=30,
                batch_size=10,
                max_batches=5,
            ),
        ).prune_once()

        assert store.entry_batches == [10]

    async def test_draining_continues_while_batches_come_back_full(self) -> None:
        store = InMemoryRetentionStore(entries=[(NOW - timedelta(days=20), True) for _ in range(5)])

        result = await _pruner(
            store,
            policy=retention_policy(
                published_retention_days=14,
                ledger_retention_days=30,
                batch_size=2,
                max_batches=10,
            ),
        ).prune_once()

        assert result.entries_deleted == 5
        assert store.entries == []

    async def test_a_run_is_bounded_by_max_batches(self) -> None:
        """CLAUDE.md §10.5, and the case it is for is the *first* run after
        this ships: a year of retained rows must drain over several runs
        rather than in one job holding locks until it finishes."""
        store = InMemoryRetentionStore(
            entries=[(NOW - timedelta(days=20), True) for _ in range(100)]
        )

        result = await _pruner(
            store,
            policy=retention_policy(
                published_retention_days=14,
                ledger_retention_days=30,
                batch_size=2,
                max_batches=3,
            ),
        ).prune_once()

        assert result.entries_deleted == 6
        assert len(store.entries) == 94


class TestFailure:
    async def test_a_failing_store_does_not_raise(self) -> None:
        """A retention job that propagated an exception would stop the
        schedule that called it — the same argument `OutboxRelay.run_once`
        makes, and a retention job that has silently stopped is invisible
        until the table it was bounding is the incident."""

        class BrokenStore(InMemoryRetentionStore):
            async def prune_published(self, *, before: datetime, batch_size: int) -> int:
                raise RuntimeError("connection reset")

        result = await _pruner(BrokenStore()).prune_once()

        assert result.is_idle
        assert result.entries_deleted == 0


class TestRequest:
    def test_the_prune_request_carries_no_payload(self) -> None:
        """A request carrying a cutoff would let a stale schedule dispatch
        yesterday's horizon, which on the one job that deletes anything is a
        way to delete more than the policy allows."""
        request = prune_request()

        assert request.payload == {}
        assert request.name == "platform.outbox.prune"

    def test_the_prune_is_routed_to_maintenance(self) -> None:
        """AD-20: minutes to hours, never on a path anybody waits on. A
        retention job sharing a pool with the clock worker is that
        decision's own worked example of what must not happen."""
        assert prune_request().queue == "maintenance"
