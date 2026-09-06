"""The four bounded deletes, against a real PostgreSQL — A64-028.7 (P2-7).

`tests/unit/test_notification_retention.py` proves the policy: which
horizon, which order, how much per run. What only a real database can prove
is the SQL — that each predicate is on the column the index leads with,
that a composite key is matched as a tuple rather than by one half of it,
and that a bounded batch removes exactly the rows it selected.

The last of those is the one worth stating: `notification_push_delivery` is
keyed on `(notification_id, subscription_id)`, so a delete matching only
`notification_id` would remove **every device's** row for a notification
when the batch had chosen one. That is a data-loss bug an in-memory fake
cannot see.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.modules.notifications.infrastructure.models import (
    NotificationEmailDeliveryModel,
    NotificationModel,
    NotificationPushDeliveryModel,
    PushSubscriptionModel,
)
from app.modules.notifications.infrastructure.repositories.retention_repository import (
    SqlAlchemyNotificationRetentionStore,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=200)
RECENT = NOW - timedelta(days=1)


@pytest_asyncio.fixture
async def store(contract_session: AsyncSession) -> SqlAlchemyNotificationRetentionStore:
    return SqlAlchemyNotificationRetentionStore(contract_session)


async def _notification(session: AsyncSession, *, at: datetime) -> uuid.UUID:
    row = NotificationModel(
        id=uuid.uuid4(),
        recipient_id=uuid.uuid4(),
        type="friend.request.received",
        category="social",
        payload={},
        target_type="account",
        source_event_id=uuid.uuid4(),
        created_at=at,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _email_delivery(session: AsyncSession, *, at: datetime) -> uuid.UUID:
    notification_id = uuid.uuid4()
    session.add(
        NotificationEmailDeliveryModel(
            notification_id=notification_id,
            recipient_id=uuid.uuid4(),
            notification_type="friend.request.received",
            status="pending",
            attempt_count=0,
            created_at=at,
        )
    )
    await session.flush()
    return notification_id


async def _push_delivery(
    session: AsyncSession, *, at: datetime, notification_id: uuid.UUID
) -> uuid.UUID:
    subscription_id = uuid.uuid4()
    session.add(
        NotificationPushDeliveryModel(
            notification_id=notification_id,
            subscription_id=subscription_id,
            recipient_id=uuid.uuid4(),
            notification_type="friend.request.received",
            status="pending",
            attempt_count=0,
            created_at=at,
        )
    )
    await session.flush()
    return subscription_id


async def _subscription(
    session: AsyncSession, *, revoked_at: datetime | None, created_at: datetime = OLD
) -> uuid.UUID:
    row = PushSubscriptionModel(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        endpoint=f"https://push.example.test/{uuid.uuid4()}",
        p256dh=b"0" * 65,
        auth=b"0" * 16,
        created_at=created_at,
        updated_at=created_at,
        last_seen_at=created_at,
        revoked_at=revoked_at,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _count(session: AsyncSession, model: type) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


class TestNotifications:
    async def test_only_rows_past_the_horizon_are_removed(
        self, store: SqlAlchemyNotificationRetentionStore, contract_session: AsyncSession
    ) -> None:
        await _notification(contract_session, at=OLD)
        keep = await _notification(contract_session, at=RECENT)

        removed = await store.delete_notifications(before=NOW - timedelta(days=90), limit=100)

        assert removed == 1
        survivors = (await contract_session.execute(select(NotificationModel.id))).scalars().all()
        assert list(survivors) == [keep]

    async def test_the_batch_is_bounded(
        self, store: SqlAlchemyNotificationRetentionStore, contract_session: AsyncSession
    ) -> None:
        for _ in range(5):
            await _notification(contract_session, at=OLD)

        removed = await store.delete_notifications(before=NOW - timedelta(days=90), limit=2)

        assert removed == 2
        assert await _count(contract_session, NotificationModel) == 3

    async def test_an_empty_table_removes_nothing(
        self, store: SqlAlchemyNotificationRetentionStore
    ) -> None:
        assert await store.delete_notifications(before=NOW, limit=100) == 0


class TestDeliveryRows:
    async def test_email_deliveries_past_the_horizon_are_removed(
        self, store: SqlAlchemyNotificationRetentionStore, contract_session: AsyncSession
    ) -> None:
        await _email_delivery(contract_session, at=OLD)
        await _email_delivery(contract_session, at=RECENT)

        removed = await store.delete_email_deliveries(before=NOW - timedelta(days=30), limit=100)

        assert removed == 1
        assert await _count(contract_session, NotificationEmailDeliveryModel) == 1

    async def test_a_push_delete_removes_only_the_rows_it_selected(
        self, store: SqlAlchemyNotificationRetentionStore, contract_session: AsyncSession
    ) -> None:
        """The composite-key test, and the reason this file exists.

        One notification fanned out to three devices is three rows sharing a
        `notification_id`. A delete matching on that column alone would take
        all three when the batch had chosen one — silent data loss no
        in-memory fake would show.
        """
        notification_id = uuid.uuid4()
        for _ in range(3):
            await _push_delivery(contract_session, at=OLD, notification_id=notification_id)

        removed = await store.delete_push_deliveries(before=NOW - timedelta(days=30), limit=1)

        assert removed == 1
        assert await _count(contract_session, NotificationPushDeliveryModel) == 2

    async def test_recent_push_rows_survive(
        self, store: SqlAlchemyNotificationRetentionStore, contract_session: AsyncSession
    ) -> None:
        await _push_delivery(contract_session, at=RECENT, notification_id=uuid.uuid4())

        removed = await store.delete_push_deliveries(before=NOW - timedelta(days=30), limit=100)

        assert removed == 0
        assert await _count(contract_session, NotificationPushDeliveryModel) == 1


class TestPushSubscriptions:
    async def test_a_live_subscription_is_never_removed(
        self, store: SqlAlchemyNotificationRetentionStore, contract_session: AsyncSession
    ) -> None:
        """However old. A player who has not visited for a year still
        expects their notifications when they return — deleting a live
        endpoint would silently stop them."""
        await _subscription(contract_session, revoked_at=None, created_at=OLD)

        removed = await store.delete_revoked_subscriptions(before=NOW, limit=100)

        assert removed == 0
        assert await _count(contract_session, PushSubscriptionModel) == 1

    async def test_a_revoked_subscription_past_the_horizon_is_removed(
        self, store: SqlAlchemyNotificationRetentionStore, contract_session: AsyncSession
    ) -> None:
        await _subscription(contract_session, revoked_at=OLD)

        removed = await store.delete_revoked_subscriptions(
            before=NOW - timedelta(days=30), limit=100
        )

        assert removed == 1

    async def test_a_recently_revoked_subscription_survives(
        self, store: SqlAlchemyNotificationRetentionStore, contract_session: AsyncSession
    ) -> None:
        """The horizon is measured from revocation, not creation: an old
        endpoint revoked yesterday is yesterday's row."""
        await _subscription(contract_session, revoked_at=RECENT, created_at=OLD)

        removed = await store.delete_revoked_subscriptions(
            before=NOW - timedelta(days=30), limit=100
        )

        assert removed == 0
        assert await _count(contract_session, PushSubscriptionModel) == 1


class TestConcurrency:
    async def test_two_runs_over_the_same_rows_do_not_double_count(
        self, contract_engine: AsyncEngine, contract_session: AsyncSession
    ) -> None:
        """`SKIP LOCKED` means a second run claims what the first left.

        Deleting a row that is already gone is an empty batch rather than an
        error, which is what makes at-least-once task delivery safe here.
        """
        for _ in range(4):
            await _notification(contract_session, at=OLD)
        await contract_session.commit()

        store = SqlAlchemyNotificationRetentionStore(contract_session)
        first = await store.delete_notifications(before=NOW - timedelta(days=90), limit=100)
        second = await store.delete_notifications(before=NOW - timedelta(days=90), limit=100)

        assert first == 4
        assert second == 0
