"""Notification operations against real PostgreSQL — A64-024.7.

`tests/unit/test_admin_notifications_api.py` covers what the routes and the
service decide, over in-memory storage. What it cannot cover is what only a
real database has, and it is exactly what makes the one mutation safe:

    the guarded UPDATE   an administrator and the delivery worker cannot both
                         act on one row; whoever loses changes nothing
    the keyset           notifications written in the same instant paginate
                         without repeating or skipping
    the EXISTS filter    a notification with three failed devices appears once

None is falsifiable against a dictionary — a fake can model them, and a model
that agrees with itself proves nothing.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import generate_uuid7
from app.modules.notifications.domain.push_delivery import (
    PushDeliveryOutcome,
    PushDeliveryStatus,
)
from app.modules.notifications.domain.record import (
    NavigationTargetType,
    NotificationCategory,
    NotificationType,
)
from app.modules.notifications.infrastructure.models import (
    NotificationModel,
    NotificationPushDeliveryModel,
    PushSubscriptionModel,
)
from app.modules.notifications.infrastructure.repositories import (
    SqlAlchemyAdministrativeNotificationDirectory,
    SqlAlchemyPushDeliveryRepository,
)
from app.modules.notifications.public import AdminNotificationFilters

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def directory(
    contract_session: AsyncSession,
) -> SqlAlchemyAdministrativeNotificationDirectory:
    return SqlAlchemyAdministrativeNotificationDirectory(contract_session)


async def _notification(
    session: AsyncSession, *, recipient_id: UUID | None = None, created_at: datetime = NOW
) -> NotificationModel:
    row = NotificationModel(
        id=generate_uuid7(),
        recipient_id=recipient_id or generate_uuid7(),
        type=NotificationType.TOURNAMENT_ROUND_PUBLISHED.value,
        category=NotificationCategory.TOURNAMENT.value,
        payload={"tournament": {"id": str(generate_uuid7()), "name": "Friday Blitz"}},
        target_type=NavigationTargetType.TOURNAMENT.value,
        target_ref=str(generate_uuid7()),
        source_event_id=generate_uuid7(),
        created_at=created_at,
    )
    session.add(row)
    await session.flush()
    return row


async def _delivery(
    session: AsyncSession,
    notification: NotificationModel,
    *,
    status: PushDeliveryStatus = PushDeliveryStatus.FAILED,
    outcome: PushDeliveryOutcome | None = PushDeliveryOutcome.ATTEMPTS_EXHAUSTED,
    attempt_count: int = 5,
) -> NotificationPushDeliveryModel:
    subscription = PushSubscriptionModel(
        id=generate_uuid7(),
        user_id=notification.recipient_id,
        endpoint=f"https://push.example/{uuid.uuid4()}",
        p256dh=b"\x04" + b"\x01" * 64,
        auth=b"\x02" * 16,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW,
        last_seen_at=NOW,
    )
    session.add(subscription)

    row = NotificationPushDeliveryModel(
        notification_id=notification.id,
        subscription_id=subscription.id,
        recipient_id=notification.recipient_id,
        notification_type=notification.type,
        status=status.value,
        outcome=outcome.value if outcome else None,
        attempt_count=attempt_count,
        next_attempt_at=None,
        last_attempt_at=NOW,
        delivered_at=None,
        created_at=NOW,
    )
    session.add(row)
    await session.flush()
    return row


class TestTheGuardedRetry:
    async def test_an_exhausted_delivery_becomes_owed_again_without_spending_an_attempt(
        self,
        directory: SqlAlchemyAdministrativeNotificationDirectory,
        contract_session: AsyncSession,
    ) -> None:
        """The whole mutation, and the bound that makes it safe.

        `attempt_count` is untouched, so the worker's cap — applied *after*
        the attempt it grants — turns this into exactly one more try and
        then a terminal row again. A retry that reset the counter would be
        an unbounded loop with a button on it.
        """
        notification = await _notification(contract_session)
        delivery = await _delivery(contract_session, notification)

        rearmed = await directory.retry_delivery(notification.id, delivery.subscription_id, at=NOW)

        assert rearmed is not None
        assert rearmed.status is PushDeliveryStatus.PENDING
        assert rearmed.outcome is None
        assert rearmed.attempt_count == 5
        assert rearmed.next_attempt_at == NOW

    async def test_the_worker_can_claim_a_re_armed_delivery(
        self,
        directory: SqlAlchemyAdministrativeNotificationDirectory,
        contract_session: AsyncSession,
    ) -> None:
        """The point of the whole operation, asserted end to end.

        A terminal row is invisible to `claim_due` — that is what
        `ix_notification_push_delivery__due`'s partial predicate means — so
        the only proof that a retry *does* anything is that the real claim
        query now returns it.
        """
        notification = await _notification(contract_session)
        delivery = await _delivery(contract_session, notification)
        deliveries = SqlAlchemyPushDeliveryRepository(contract_session)

        assert await deliveries.claim_due(now=NOW, limit=10) == []

        await directory.retry_delivery(notification.id, delivery.subscription_id, at=NOW)

        claimed = await deliveries.claim_due(now=NOW, limit=10)
        assert [(row.notification_id, row.subscription_id) for row in claimed] == [
            (notification.id, delivery.subscription_id)
        ]
        # The claim spent the attempt this retry bought — six against a cap
        # the worker will now apply.
        assert claimed[0].attempt_count == 6

    async def test_a_second_retry_matches_nothing(
        self,
        directory: SqlAlchemyAdministrativeNotificationDirectory,
        contract_session: AsyncSession,
    ) -> None:
        """The race, resolved by the statement rather than by a read.

        Once re-armed the row is `pending`, which the `WHERE` excludes — so
        a second administrator, or the same one clicking twice, changes
        nothing and is told so. This is also what stops an admin retry from
        colliding with a delivery the worker already has in flight.
        """
        notification = await _notification(contract_session)
        delivery = await _delivery(contract_session, notification)

        assert await directory.retry_delivery(notification.id, delivery.subscription_id, at=NOW)
        assert (
            await directory.retry_delivery(notification.id, delivery.subscription_id, at=NOW)
            is None
        )

    async def test_every_ineligible_state_is_refused_by_the_statement(
        self,
        directory: SqlAlchemyAdministrativeNotificationDirectory,
        contract_session: AsyncSession,
    ) -> None:
        """Not merely hidden in the console.

        `SKIPPED_PREFERENCE` matters most: a caller constructing the request
        by hand must not be able to push to somebody who muted the category.
        `SUBSCRIPTION_GONE` has nowhere to send and `PERMANENT_FAILURE` is
        the same question answered the same way.
        """
        for status, outcome in (
            (PushDeliveryStatus.SKIPPED, PushDeliveryOutcome.SKIPPED_PREFERENCE),
            (PushDeliveryStatus.FAILED, PushDeliveryOutcome.SUBSCRIPTION_GONE),
            (PushDeliveryStatus.FAILED, PushDeliveryOutcome.PERMANENT_FAILURE),
            (PushDeliveryStatus.SENT, PushDeliveryOutcome.DELIVERED),
            (PushDeliveryStatus.PENDING, None),
        ):
            notification = await _notification(contract_session)
            delivery = await _delivery(
                contract_session, notification, status=status, outcome=outcome
            )

            assert (
                await directory.retry_delivery(notification.id, delivery.subscription_id, at=NOW)
                is None
            ), outcome

            stored = await contract_session.get(
                NotificationPushDeliveryModel, (notification.id, delivery.subscription_id)
            )
            assert stored is not None
            assert stored.status == status.value

    async def test_a_retry_writes_nothing_to_the_notification_table(
        self,
        directory: SqlAlchemyAdministrativeNotificationDirectory,
        contract_session: AsyncSession,
    ) -> None:
        """§10.7 — no second durable fact.

        A duplicate inbox row for an operational action the recipient never
        saw would be the one user-visible harm this operation could do.
        """
        notification = await _notification(contract_session)
        delivery = await _delivery(contract_session, notification)
        before = len((await contract_session.execute(select(NotificationModel))).scalars().all())

        await directory.retry_delivery(notification.id, delivery.subscription_id, at=NOW)

        after = len((await contract_session.execute(select(NotificationModel))).scalars().all())
        assert after == before


class TestTheReadModel:
    async def test_the_keyset_neither_repeats_nor_skips_rows_sharing_an_instant(
        self,
        directory: SqlAlchemyAdministrativeNotificationDirectory,
        contract_session: AsyncSession,
    ) -> None:
        """A fan-out writes many rows at one instant — a published round
        notifies every entrant — so `created_at` alone cannot order them and
        a cursor on it would silently drop rows at every page boundary."""
        recipient = generate_uuid7()
        for _ in range(5):
            await _notification(contract_session, recipient_id=recipient, created_at=NOW)

        seen: list[UUID] = []
        cursor: str | None = None
        for _ in range(3):
            page = await directory.list_notifications(
                filters=AdminNotificationFilters(recipient_id=recipient), limit=2, cursor=cursor
            )
            seen.extend(record.id for record in page.records)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert len(seen) == 5
        assert len(set(seen)) == 5
        assert cursor is None

    async def test_a_notification_with_several_failed_devices_appears_once(
        self,
        directory: SqlAlchemyAdministrativeNotificationDirectory,
        contract_session: AsyncSession,
    ) -> None:
        """`EXISTS`, not a join.

        A join would return the row once per failed device and break both
        the page size and the keyset — and the bug would only appear for the
        multi-device recipients this console exists to investigate.
        """
        recipient = generate_uuid7()
        failing = await _notification(contract_session, recipient_id=recipient)
        for _ in range(3):
            await _delivery(contract_session, failing)

        healthy = await _notification(contract_session, recipient_id=recipient)
        await _delivery(
            contract_session,
            healthy,
            status=PushDeliveryStatus.SENT,
            outcome=PushDeliveryOutcome.DELIVERED,
        )

        page = await directory.list_notifications(
            filters=AdminNotificationFilters(recipient_id=recipient, failed_push_only=True),
            limit=10,
            cursor=None,
        )
        assert [record.id for record in page.records] == [failing.id]

    async def test_the_delivery_batch_is_one_query_and_carries_device_facts_only(
        self,
        directory: SqlAlchemyAdministrativeNotificationDirectory,
        contract_session: AsyncSession,
    ) -> None:
        """§8 and §16 together.

        The batch answers a page of notifications at once, and what it
        carries about each device is three timestamps — never the endpoint
        or the keys, which are what a push service would accept from anyone
        holding them.
        """
        first = await _notification(contract_session)
        second = await _notification(contract_session)
        await _delivery(contract_session, first)
        await _delivery(contract_session, second)

        grouped = await directory.deliveries_for([first.id, second.id])

        assert set(grouped) == {first.id, second.id}
        delivery = grouped[first.id][0]
        assert delivery.subscription_last_seen_at == NOW
        assert delivery.subscription_revoked_at is None
        assert not {"endpoint", "p256dh", "auth"} & set(type(delivery).__slots__)
