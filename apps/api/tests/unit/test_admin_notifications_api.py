"""The admin Notification Operations surface — A64-024.7.

Tests over the **real** route handlers and the **real**
`NotificationOperationsService`, with storage in memory. What is asserted is
what an operator, a recipient and an attacker each meet: that no route can
send anything, that no push credential has a field to arrive in, that the
one mutation is bounded and audited, and that the console never claims a
delivery the platform cannot observe.

The guarded `UPDATE` itself is not asserted here — a race between a worker
and an administrator is PostgreSQL's, and
`tests/contract/test_admin_notification_operations.py` is where it can fail.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.core.identifiers import generate_uuid7
from app.modules.admin.application.services import (
    AuditRecorder,
    NotificationOperationsService,
)
from app.modules.admin.domain.audit import AuditAction, AuditOutcome, AuditSubjectType
from app.modules.admin.domain.exceptions import RetryUnavailable
from app.modules.admin.presentation.routers.notifications import (
    MAX_PAGE_SIZE,
    admin_notifications_router,
    list_notifications,
    read_notification,
    retry_delivery,
)
from app.modules.admin.presentation.schemas.notifications import (
    AdminNotificationDetailResponse,
    AdminNotificationPageResponse,
    AdminNotificationSummary,
    AdminPushDeliveryView,
)
from app.modules.notifications.domain.push_delivery import (
    PushDeliveryOutcome,
    PushDeliveryStatus,
)
from app.modules.notifications.domain.record import (
    NavigationTargetType,
    NotificationCategory,
    NotificationType,
)
from app.modules.notifications.public import (
    AdminNotificationRecord,
    AdminPushDelivery,
)
from app.modules.users.public import AdminUserRecord
from tests.fakes.admin_audit import InMemoryAuditEntries
from tests.fakes.notification_operations import InMemoryNotificationDirectory
from tests.fakes.presence_redis import MovableClock
from tests.unit.test_admin_authorization import NullUnitOfWork

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _record(**overrides: object) -> AdminNotificationRecord:
    base = {
        "id": generate_uuid7(),
        "recipient_id": generate_uuid7(),
        "type": NotificationType.TOURNAMENT_ROUND_PUBLISHED,
        "category": NotificationCategory.TOURNAMENT,
        "target_type": NavigationTargetType.TOURNAMENT,
        "target_ref": str(generate_uuid7()),
        "source_event_id": generate_uuid7(),
        "created_at": NOW,
        "read_at": None,
        "push_capable": True,
    }
    base.update(overrides)
    return AdminNotificationRecord(**base)  # type: ignore[arg-type]


def _delivery(**overrides: object) -> AdminPushDelivery:
    base = {
        "subscription_id": generate_uuid7(),
        "status": PushDeliveryStatus.FAILED,
        "outcome": PushDeliveryOutcome.ATTEMPTS_EXHAUSTED,
        "attempt_count": 5,
        "next_attempt_at": None,
        "last_attempt_at": NOW,
        "delivered_at": None,
        "created_at": NOW,
        "subscription_created_at": NOW - timedelta(days=30),
        "subscription_last_seen_at": NOW,
        "subscription_revoked_at": None,
    }
    base.update(overrides)
    return AdminPushDelivery(**base)  # type: ignore[arg-type]


class _Fixture:
    def __init__(self) -> None:
        self.directory = InMemoryNotificationDirectory()
        self.entries = InMemoryAuditEntries()
        self.clock = MovableClock(NOW)
        self.operations = NotificationOperationsService(
            deliveries=self.directory,
            audit=AuditRecorder(entries=self.entries, clock=self.clock),
            unit_of_work=NullUnitOfWork(),
            clock=self.clock,
        )


class TestWhatTheSurfaceCannotDo:
    def test_no_route_creates_a_notification_and_every_route_is_guarded(self) -> None:
        """The two structural claims, asserted against the route table.

        There is no compose, no broadcast and no send: the only non-`GET`
        route is the retry, and it takes both of its identifiers from the
        path. A route added later without `CurrentAdmin` is exactly what the
        second half catches.
        """
        from app.modules.admin.presentation.dependencies import require_admin

        assert admin_notifications_router.routes
        writes = []
        for route in admin_notifications_router.routes:
            methods: set[str] = getattr(route, "methods", set())
            path = getattr(route, "path", "")
            if methods - {"GET", "HEAD"}:
                writes.append(path)

            dependant = getattr(route, "dependant", None)
            assert dependant is not None
            assert require_admin in {sub.call for sub in dependant.dependencies}, path

        # Exactly one mutation, and it is a retry of something that already
        # exists rather than the creation of something new.
        assert len(writes) == 1
        assert writes[0].endswith("/deliveries/{subscription_id}/retry")

    def test_the_retry_route_accepts_no_body(self) -> None:
        """A body would be a place for a recipient, a payload or a target to
        arrive — and this endpoint must change none of them."""
        route = next(
            route
            for route in admin_notifications_router.routes
            if "retry" in getattr(route, "path", "")
        )
        assert route.body_field is None  # type: ignore[attr-defined]

    def test_no_response_model_can_carry_push_credentials(self) -> None:
        """§8, asserted as absence.

        A push endpoint or a `p256dh`/`auth` key in a console is a
        credential that could be replayed against a push service. There is
        no field for one, so no serialisation path could produce one
        whatever the delivery row gains next.
        """
        forbidden = {
            "endpoint",
            "p256dh",
            "auth",
            "auth_secret",
            "keys",
            "vapid",
            "vapid_private_key",
            "payload",
            "body",
            "title",
            "provider_response",
            "email",
            "password_hash",
            "token",
        }
        for model in (
            AdminPushDeliveryView,
            AdminNotificationSummary,
            AdminNotificationDetailResponse,
        ):
            assert not forbidden & set(model.model_fields), model.__name__

    def test_the_response_never_claims_the_device_displayed_anything(self) -> None:
        """§9 — there is no `delivered_at` on the way out.

        The column is named `delivered_at` and the fact is "a push service
        accepted the request"; the response says `accepted_at`, because
        nothing downstream of this platform reports an acknowledgement and a
        field named for one would be a claim the system cannot support.
        """
        assert "accepted_at" in AdminPushDeliveryView.model_fields
        assert "delivered_at" not in AdminPushDeliveryView.model_fields


class TestTheReadSurface:
    @pytest.mark.asyncio
    async def test_a_page_costs_one_listing_and_two_batches(self) -> None:
        """The N+1 this endpoint would naturally grow.

        Every notification names a recipient and owes deliveries on every
        device — two per-row reads on the naive shape, which on a fifty-row
        page is a hundred queries. One batch each is the whole cost.
        """
        fixture = _Fixture()
        shared = generate_uuid7()
        for _ in range(MAX_PAGE_SIZE):
            record = _record(recipient_id=shared)
            fixture.directory.add(record, [_delivery(), _delivery()])

        accounts = _Accounts({shared: "player"})
        page = await _list(fixture, accounts, limit=MAX_PAGE_SIZE)

        assert len(page.items) == MAX_PAGE_SIZE
        assert fixture.directory.list_calls == 1
        assert fixture.directory.delivery_batches == [MAX_PAGE_SIZE]
        assert len(accounts.batches) == 1
        # The recipient appears on every row and is asked for once.
        assert accounts.batches[0] == 1

    @pytest.mark.asyncio
    async def test_the_push_summary_reports_the_worst_device(self) -> None:
        """A notification that reached two devices and failed on a third is
        `failed`, because the third is the one an operator must act on. A
        "mostly fine" summary would hide the row this console exists for.
        """
        fixture = _Fixture()
        mixed = _record()
        fixture.directory.add(
            mixed,
            [
                _delivery(status=PushDeliveryStatus.SENT, outcome=PushDeliveryOutcome.DELIVERED),
                _delivery(status=PushDeliveryStatus.SENT, outcome=PushDeliveryOutcome.DELIVERED),
                _delivery(),
            ],
        )
        unpushed = _record(push_capable=False)
        fixture.directory.add(unpushed, [])

        page = await _list(fixture, _Accounts({}))
        summaries = {item.id: item.push_summary for item in page.items}

        assert summaries[mixed.id] == "failed"
        # No device owed anything — not a failure, and not reported as one.
        assert summaries[unpushed.id] == "none"

    @pytest.mark.asyncio
    async def test_only_an_exhausted_delivery_offers_a_retry(self) -> None:
        """The eligibility rule, as the console sees it.

        `SKIPPED_PREFERENCE` is the one that matters most: offering a retry
        there would be an administrator overriding somebody's stated choice.
        `SUBSCRIPTION_GONE` has nowhere to send, and `PERMANENT_FAILURE` is
        the same question answered the same way.
        """
        fixture = _Fixture()
        record = _record()
        fixture.directory.add(
            record,
            [
                _delivery(),
                _delivery(
                    status=PushDeliveryStatus.SKIPPED,
                    outcome=PushDeliveryOutcome.SKIPPED_PREFERENCE,
                ),
                _delivery(
                    status=PushDeliveryStatus.FAILED,
                    outcome=PushDeliveryOutcome.SUBSCRIPTION_GONE,
                ),
                _delivery(
                    status=PushDeliveryStatus.FAILED,
                    outcome=PushDeliveryOutcome.PERMANENT_FAILURE,
                ),
                _delivery(status=PushDeliveryStatus.PENDING, outcome=None),
            ],
        )

        detail = await _detail(fixture, record.id, _Accounts({}))
        assert [delivery.can_retry for delivery in detail.deliveries] == [
            True,
            False,
            False,
            False,
            False,
        ]

    @pytest.mark.asyncio
    async def test_a_notification_that_does_not_exist_is_a_404(self) -> None:
        fixture = _Fixture()
        with pytest.raises(HTTPException) as missing:
            await _detail(fixture, generate_uuid7(), _Accounts({}))
        assert missing.value.status_code == 404


class TestTheOneMutation:
    @pytest.mark.asyncio
    async def test_a_retry_re_arms_the_row_and_audits_it_in_one_go(self) -> None:
        """The invariant A64-024.8 exists to make possible.

        The delivery returns to `pending` and the entry names the
        administrator, the notification and the device. **`attempt_count` is
        untouched** — the worker's cap is applied after the attempt it
        grants, so this buys exactly one more and the row goes terminal
        again by the existing mechanism.
        """
        fixture = _Fixture()
        record = _record()
        target = _delivery()
        fixture.directory.add(record, [target])
        admin = generate_uuid7()

        view = await _retry(fixture, record.id, target.subscription_id, admin)

        assert view.status == PushDeliveryStatus.PENDING.value
        assert view.attempt_count == 5
        assert view.can_retry is False

        assert len(fixture.entries.rows) == 1
        entry = fixture.entries.rows[0]
        assert entry.action is AuditAction.NOTIFICATION_DELIVERY_RETRIED
        assert entry.outcome is AuditOutcome.SUCCEEDED
        assert entry.subject_type is AuditSubjectType.NOTIFICATION
        assert entry.subject_ref == str(record.id)
        assert entry.actor_id == admin
        assert entry.after["subscription_id"] == str(target.subscription_id)

    @pytest.mark.asyncio
    async def test_the_audit_metadata_carries_no_payload_or_credential(self) -> None:
        """§13 — small and structured. The device and the previous state,
        and nothing that would be permanent and useless."""
        fixture = _Fixture()
        record = _record()
        target = _delivery()
        fixture.directory.add(record, [target])

        await _retry(fixture, record.id, target.subscription_id, generate_uuid7())

        after = fixture.entries.rows[0].after
        forbidden = {"payload", "endpoint", "p256dh", "auth", "title", "body", "email"}
        assert not forbidden & set(after)

    @pytest.mark.asyncio
    async def test_a_second_retry_is_refused_and_the_refusal_is_audited(self) -> None:
        """§11 and §15 — repeated clicking is a conflict, not a storm.

        The re-armed row is `pending`, which the eligibility rule excludes,
        so nothing more can be queued until a worker has settled it. The
        refusal writes a `FAILED` entry per A64-024.6's policy, and writes
        no second `SUCCEEDED` one for a transition that did not happen.
        """
        fixture = _Fixture()
        record = _record()
        target = _delivery()
        fixture.directory.add(record, [target])
        admin = generate_uuid7()

        await _retry(fixture, record.id, target.subscription_id, admin)
        with pytest.raises(RetryUnavailable):
            await _retry(fixture, record.id, target.subscription_id, admin)

        outcomes = [entry.outcome for entry in fixture.entries.rows]
        assert outcomes == [AuditOutcome.SUCCEEDED, AuditOutcome.FAILED]
        assert fixture.entries.rows[-1].after["refused"] == "delivery_not_retryable"

    @pytest.mark.asyncio
    async def test_a_skipped_preference_delivery_can_never_be_retried(self) -> None:
        """§22 — a retry must not become a way around somebody's choice.

        Refused at the storage guard, not merely hidden in the console: a
        caller that constructed the request by hand gets the same `409`.
        """
        fixture = _Fixture()
        record = _record()
        muted = _delivery(
            status=PushDeliveryStatus.SKIPPED, outcome=PushDeliveryOutcome.SKIPPED_PREFERENCE
        )
        fixture.directory.add(record, [muted])

        with pytest.raises(RetryUnavailable):
            await _retry(fixture, record.id, muted.subscription_id, generate_uuid7())

        assert fixture.directory.deliveries[record.id][0].status is PushDeliveryStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_retrying_creates_no_second_notification(self) -> None:
        """§10.7 — the durable record is untouched.

        A retry that wrote a second notification row would put a duplicate
        in somebody's inbox for an operational action they never saw. There
        is no path from here to the notification table at all.
        """
        fixture = _Fixture()
        record = _record()
        target = _delivery()
        fixture.directory.add(record, [target])

        await _retry(fixture, record.id, target.subscription_id, generate_uuid7())

        assert len(fixture.directory.records) == 1
        assert len(fixture.directory.deliveries[record.id]) == 1


class _Accounts:
    """`AdministrativeUserDirectory`, counting batch reads."""

    def __init__(self, known: dict[UUID, str]) -> None:
        self.known = known
        self.batches: list[int] = []

    async def accounts_by_ids(self, user_ids: list[UUID]) -> dict[UUID, AdminUserRecord]:
        self.batches.append(len(set(user_ids)))
        wanted = set(user_ids)
        return {
            user_id: AdminUserRecord(
                id=user_id,
                username=name,
                email=f"{name}@example.com",
                display_name=None,
                is_active=True,
                is_verified=True,
                created_at=NOW,
            )
            for user_id, name in self.known.items()
            if user_id in wanted
        }

    async def find_account(self, user_id: UUID) -> None:  # pragma: no cover
        raise AssertionError("the notifications router must not read accounts one at a time")

    async def list_accounts(self, **_: object) -> None:  # pragma: no cover
        raise AssertionError("the notifications router must not list accounts")


class _Headers:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


class _Identity:
    def __init__(self, account_id: UUID) -> None:
        self.id = account_id


async def _list(
    fixture: _Fixture, accounts: _Accounts, **kwargs: object
) -> AdminNotificationPageResponse:
    return await list_notifications(
        _Identity(generate_uuid7()),  # type: ignore[arg-type]
        fixture.directory,  # type: ignore[arg-type]
        accounts,  # type: ignore[arg-type]
        _Headers(),  # type: ignore[arg-type]
        recipient_id=kwargs.get("recipient_id"),  # type: ignore[arg-type]
        failed_push_only=bool(kwargs.get("failed_push_only", False)),
        limit=kwargs.get("limit", 25),  # type: ignore[arg-type]
        cursor=None,
    )


async def _detail(
    fixture: _Fixture, notification_id: UUID, accounts: _Accounts
) -> AdminNotificationDetailResponse:
    return await read_notification(
        notification_id,
        _Identity(generate_uuid7()),  # type: ignore[arg-type]
        fixture.directory,  # type: ignore[arg-type]
        accounts,  # type: ignore[arg-type]
        _Headers(),  # type: ignore[arg-type]
    )


async def _retry(
    fixture: _Fixture, notification_id: UUID, subscription_id: UUID, admin: UUID
) -> AdminPushDeliveryView:
    return await retry_delivery(
        notification_id,
        subscription_id,
        _Identity(admin),  # type: ignore[arg-type]
        fixture.operations,
        fixture.directory,  # type: ignore[arg-type]
        _Headers(),  # type: ignore[arg-type]
    )
