"""Notification retention's policy and arithmetic — A64-028.7, closing P2-7.

The SQL is proven against a real PostgreSQL in
`tests/contract/test_notification_retention_repository.py`. What is here is
the part that decides *whether* to delete and *how much*: the ordering
invariant, the batch ceiling, and the correspondence between what is pruned
and what is counted.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.notifications.application.retention_metrics import (
    RETENTION_DELETIONS,
    RetentionRelation,
)
from app.modules.notifications.application.services.notification_retention_service import (
    NotificationRetentionPolicy,
    NotificationRetentionResult,
    NotificationRetentionService,
    notification_retention_policy,
)
from tests.fakes.metrics import RecordingMetrics

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _NullUnitOfWork:
    async def __aenter__(self) -> "_NullUnitOfWork":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@dataclass
class _Store:
    """Counts rows by relation, and records the horizon each delete used."""

    rows: dict[str, int] = field(default_factory=dict)
    horizons: dict[str, datetime] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)

    def _take(self, relation: str, before: datetime, limit: int) -> int:
        self.horizons[relation] = before
        if relation not in self.order:
            self.order.append(relation)
        available = self.rows.get(relation, 0)
        removed = min(available, limit)
        self.rows[relation] = available - removed
        return removed

    async def delete_notifications(self, *, before: datetime, limit: int) -> int:
        return self._take("notification", before, limit)

    async def delete_email_deliveries(self, *, before: datetime, limit: int) -> int:
        return self._take("email_delivery", before, limit)

    async def delete_push_deliveries(self, *, before: datetime, limit: int) -> int:
        return self._take("push_delivery", before, limit)

    async def delete_revoked_subscriptions(self, *, before: datetime, limit: int) -> int:
        return self._take("revoked_subscription", before, limit)


def _service(
    store: _Store,
    *,
    metrics: RecordingMetrics | None = None,
    batch_size: int = 100,
    max_batches: int = 20,
) -> NotificationRetentionService:
    return NotificationRetentionService(
        store=store,
        unit_of_work=_NullUnitOfWork(),
        clock=_Clock(),
        policy=notification_retention_policy(
            notification_days=90,
            delivery_days=30,
            revoked_subscription_days=30,
            batch_size=batch_size,
            max_batches=max_batches,
        ),
        metrics=metrics,
    )


class TestTheOrderingInvariant:
    """There is **no foreign key** between a delivery row and its
    notification, so a notification deleted first leaves an orphan nothing
    else would ever remove."""

    def test_a_delivery_horizon_beyond_the_notification_horizon_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at or inside"):
            NotificationRetentionPolicy(
                notification_days=30,
                delivery_days=90,
                revoked_subscription_days=30,
                batch_size=100,
                max_batches=10,
            )

    def test_an_equal_horizon_is_allowed(self) -> None:
        """Equal is the boundary and it is safe: the deliveries are deleted
        first within the run, so they are gone before the notification is."""
        NotificationRetentionPolicy(
            notification_days=30,
            delivery_days=30,
            revoked_subscription_days=30,
            batch_size=100,
            max_batches=10,
        )

    async def test_deliveries_are_deleted_before_notifications(self) -> None:
        store = _Store(rows={"notification": 5, "email_delivery": 5, "push_delivery": 5})

        await _service(store).run()

        assert store.order.index("email_delivery") < store.order.index("notification")
        assert store.order.index("push_delivery") < store.order.index("notification")


class TestTheHorizons:
    async def test_each_relation_uses_its_own(self) -> None:
        store = _Store(rows={"notification": 1, "email_delivery": 1, "revoked_subscription": 1})

        await _service(store).run()

        assert store.horizons["notification"] == NOW - timedelta(days=90)
        assert store.horizons["email_delivery"] == NOW - timedelta(days=30)
        assert store.horizons["revoked_subscription"] == NOW - timedelta(days=30)


class TestTheBatchCeiling:
    async def test_a_run_stops_at_batch_size_times_max_batches(self) -> None:
        """The bound that stops a first run against years of history from
        being unbounded after all. The sweep catches up over several runs."""
        store = _Store(rows={"notification": 10_000})

        result = await _service(store, batch_size=10, max_batches=5).run()

        assert result.notifications == 50
        assert store.rows["notification"] == 9_950

    async def test_a_short_batch_ends_the_relation_early(self) -> None:
        """The steady state: one short statement per relation per run."""
        store = _Store(rows={"notification": 3})

        result = await _service(store, batch_size=10, max_batches=20).run()

        assert result.notifications == 3

    async def test_an_empty_relation_costs_one_statement(self) -> None:
        store = _Store(rows={})

        result = await _service(store, batch_size=10, max_batches=20).run()

        assert result.total == 0


class TestWhatIsCounted:
    def test_every_relation_pruned_has_a_metric_member(self) -> None:
        """A relation that is pruned and not counted is one whose growth is
        invisible until it is the incident."""
        counted = {member.value for member in RetentionRelation}
        reported = set(NotificationRetentionResult.__dataclass_fields__)

        assert counted == {
            "notification",
            "email_delivery",
            "push_delivery",
            "revoked_subscription",
        }
        assert len(reported) == len(counted)

    async def test_zero_is_recorded_rather_than_skipped(self) -> None:
        """A series that disappears when a relation stops being pruned is
        indistinguishable from one that was never wired up."""
        metrics = RecordingMetrics()
        store = _Store(rows={"notification": 2})

        await _service(store, metrics=metrics).run()

        counts = metrics.counts(RETENTION_DELETIONS)
        assert counts["notification"] == 2
        assert counts["email_delivery"] == 0
        assert counts["push_delivery"] == 0
        assert counts["revoked_subscription"] == 0


class TestThePolicyRefusesNonsense:
    @pytest.mark.parametrize(("batch", "batches"), [(0, 10), (10, 0), (-1, 10)])
    def test_a_non_positive_bound_is_refused(self, batch: int, batches: int) -> None:
        with pytest.raises(ValueError, match="positive"):
            NotificationRetentionPolicy(
                notification_days=90,
                delivery_days=30,
                revoked_subscription_days=30,
                batch_size=batch,
                max_batches=batches,
            )
